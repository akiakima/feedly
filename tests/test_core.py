from datetime import datetime

import pytest
from sqlalchemy import select

from app.db import Article, Category, Feed, Keyword, set_setting
from app.fetcher import find_feed_links, parse_feed, store_entries
from app.notifier import build_digest, send_digest, send_keyword_alerts
from app.opml import export_opml, parse_opml
from app.scheduler import parse_times


def test_parse_rss(rss_bytes):
    result = parse_feed(rss_bytes)
    assert result.title == "サンプル経済ニュース"
    # リンクのない記事は除外される
    assert len(result.entries) == 2
    first = result.entries[0]
    assert first.title == "日経平均が反発、 半導体株 に買い"
    assert first.summary == "東京株式市場で 日経平均 が反発した。"
    assert first.published_at == datetime(2026, 10, 10, 0, 0)  # UTC で保存
    # guid がなければリンクを使う
    assert result.entries[1].guid == "https://example.com/articles/2"


def test_parse_atom(atom_bytes):
    result = parse_feed(atom_bytes)
    assert result.title == "Sample AI Blog"
    assert result.site_url == "https://ai.example.com/"
    entry = result.entries[0]
    assert entry.guid == "urn:example:ai:1"
    assert entry.summary == "A long body about the model."


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_feed(b"<html><body>not a feed</body></html>")


def test_find_feed_links():
    page = """<html><head>
      <link rel="stylesheet" href="/s.css">
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
      <link rel="alternate" type="application/atom+xml" href="https://other.example.com/atom">
    </head></html>"""
    assert find_feed_links(page, "https://example.com/news/") == [
        "https://example.com/feed.xml",
        "https://other.example.com/atom",
    ]


def make_feed(session, category_name="経済"):
    cat = Category(name=category_name)
    session.add(cat)
    session.flush()
    feed = Feed(title="サンプル", url=f"https://example.com/{category_name}.xml", category_id=cat.id)
    session.add(feed)
    session.flush()
    return feed


def test_store_entries_dedup_and_first_fetch(session, rss_bytes):
    feed = make_feed(session)
    first = store_entries(session, feed, parse_feed(rss_bytes))
    session.commit()
    assert len(first) == 2
    # 初回取得分は通知済み扱い（登録直後に大量のメールが届かないように）
    assert all(a.digested and a.alerted for a in first)

    again = store_entries(session, feed, parse_feed(rss_bytes))
    session.commit()
    assert again == []
    assert session.scalar(select(Article).where(Article.feed_id == feed.id).limit(1)) is not None
    assert len(list(session.scalars(select(Article)))) == 2


def _add_article(session, feed, title, summary="", guid=None):
    a = Article(
        feed_id=feed.id,
        guid=guid or title,
        title=title,
        link=f"https://example.com/{guid or title}",
        summary=summary,
        published_at=datetime(2026, 10, 10, 1, 0),
    )
    session.add(a)
    return a


def test_keyword_alerts(session, sent_mails):
    feed = make_feed(session)
    feed.last_fetched_at = datetime(2026, 10, 1)
    session.add(Keyword(word="bitcoin"))
    _add_article(session, feed, "Bitcoin が急騰")
    _add_article(session, feed, "日銀が金融政策を維持", summary="BITCOIN には触れず")
    _add_article(session, feed, "関係のない記事")
    session.commit()

    assert send_keyword_alerts(session) == 2
    assert len(sent_mails) == 1
    assert "bitcoin" in sent_mails[0]["subject"]
    assert sent_mails[0]["to"] == "me@example.com"
    # 2 回目は送らない
    assert send_keyword_alerts(session) == 0
    assert len(sent_mails) == 1


def test_keyword_alerts_disabled(session, sent_mails):
    feed = make_feed(session)
    session.add(Keyword(word="AI"))
    _add_article(session, feed, "AI の記事")
    set_setting(session, "keyword_alert_enabled", "0")
    session.commit()
    assert send_keyword_alerts(session) == 0
    assert sent_mails == []


def test_digest_groups_by_category(session, sent_mails):
    econ = make_feed(session, "経済")
    crypto = make_feed(session, "仮想通貨")
    _add_article(session, econ, "円安が進む")
    _add_article(session, crypto, "イーサリアムが上昇")
    _add_article(session, crypto, "取引所の新サービス")
    session.commit()

    assert send_digest(session) == 3
    mail = sent_mails[0]
    assert "新着 3 件" in mail["subject"]
    assert "■ 経済（1 件）" in mail["text"]
    assert "■ 仮想通貨（2 件）" in mail["text"]
    assert "イーサリアムが上昇" in mail["html"]
    # 送信済みは次回のダイジェストに入らない
    assert send_digest(session) == 0


def test_digest_truncates_long_categories(session):
    feed = make_feed(session)
    articles = [_add_article(session, feed, f"記事{i}") for i in range(15)]
    session.commit()
    for a in articles:
        session.refresh(a)
    _, text, _ = build_digest(articles)
    assert "ほか 5 件" in text


def test_parse_times():
    assert parse_times("18:00, 7:05、25:00 abc 07:05") == [(7, 5), (18, 0)]
    assert parse_times("") == []


def test_opml_roundtrip(session):
    feed = make_feed(session, "AI")
    session.commit()
    cat = session.get(Category, feed.category_id)
    loose = Feed(title='A & "B"', url="https://loose.example.com/rss")
    xml = export_opml([cat], [loose])
    assert parse_opml(xml) == [
        ("AI", "サンプル", "https://example.com/AI.xml", ""),
        (None, 'A & "B"', "https://loose.example.com/rss", ""),
    ]
