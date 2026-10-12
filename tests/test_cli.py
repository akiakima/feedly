from datetime import datetime

from sqlalchemy import select

import app.cli as cli
from app.db import Article, Category, Feed, get_setting
from app.fetcher import parse_feed, store_entries


def test_latest_slot_and_digest_due():
    times = [(7, 0), (18, 0)]
    # JST 2026-10-12 07:20 = UTC 2026-10-11 22:20
    now = datetime(2026, 10, 11, 22, 20)
    assert cli.digest_due(now, times, None)
    # 06:59 JST に送った後なら 07:00 分は未送信
    assert cli.digest_due(now, times, datetime(2026, 10, 11, 21, 59))
    # 07:05 JST に送り済みなら送らない
    assert not cli.digest_due(now, times, datetime(2026, 10, 11, 22, 5))
    # 配信時刻が未設定なら送らない
    assert not cli.digest_due(now, [], None)


def test_digest_due_previous_day():
    # JST 2026-10-12 03:00 → 直近の配信は前日 18:00 JST（= 09:00 UTC）
    now = datetime(2026, 10, 11, 18, 0)
    assert cli.digest_due(now, [(18, 0)], datetime(2026, 10, 11, 8, 59))
    assert not cli.digest_due(now, [(18, 0)], datetime(2026, 10, 11, 9, 1))


def test_env_keywords(monkeypatch):
    monkeypatch.setenv("NOTIFY_KEYWORDS", "ビットコイン、 AI,\n上場 ,")
    assert cli.env_keywords() == ["ビットコイン", "AI", "上場"]


def test_sync_feeds(session):
    cli.sync_feeds(session, [("AI", "A", "https://a.example/rss"), ("経済", "B", "https://b.example/rss")])
    assert {f.url for f in session.scalars(select(Feed))} == {"https://a.example/rss", "https://b.example/rss"}

    cli.sync_feeds(
        session,
        [("テック", "A2", "https://a.example/rss"), (None, "C", "https://c.example/rss"), ("x", "bad", "file:///x")],
    )
    feeds = {f.url: f for f in session.scalars(select(Feed))}
    assert set(feeds) == {"https://a.example/rss", "https://c.example/rss"}
    assert feeds["https://a.example/rss"].title == "A2"
    assert session.get(Category, feeds["https://a.example/rss"].category_id).name == "テック"
    assert feeds["https://c.example/rss"].category_id is None


def test_desired_feeds_uses_opml(tmp_path):
    opml = tmp_path / "feeds.opml"
    opml.write_text(
        '<opml><body><outline text="AI"><outline text="X" xmlUrl="https://x.example/rss"/></outline></body></opml>'
    )
    assert cli.desired_feeds(opml) == [("AI", "X", "https://x.example/rss")]
    assert len(cli.desired_feeds(tmp_path / "missing.opml")) > 10


def test_run_sends_alerts_and_digest(monkeypatch, session, rss_bytes, sent_mails, tmp_path):
    opml = tmp_path / "feeds.opml"
    opml.write_text('<opml><body><outline text="S" xmlUrl="https://example.com/feed.xml"/></body></opml>')
    monkeypatch.setenv("NOTIFY_KEYWORDS", "ビットコイン")

    def fake_fetch_all(s):
        feed = s.scalar(select(Feed))
        feed.last_fetched_at = feed.last_fetched_at or datetime(2026, 1, 1)  # 2 回目以降の取得として扱う
        new = store_entries(s, feed, parse_feed(rss_bytes))
        s.commit()
        return new

    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(cli, "digest_due", lambda *a: True)

    assert cli.run(opml) == 0
    subjects = [m["subject"] for m in sent_mails]
    assert any("キーワード通知" in s for s in subjects)
    assert any("ニュースまとめ" in s and "新着 2 件" in s for s in subjects)
    assert get_setting(session, cli.LAST_DIGEST_KEY)
    # メールアドレスやキーワードは DB に保存しない
    assert get_setting(session, "mail_to") == ""
    assert all(a.digested and a.alerted for a in session.scalars(select(Article)))


def test_run_reports_missing_mail_config(monkeypatch, tmp_path, rss_bytes):
    import app.notifier as notifier

    def failing(*a, **k):
        raise notifier.MailNotConfigured("x")

    monkeypatch.setattr(notifier, "send_mail", failing)
    monkeypatch.setenv("NOTIFY_KEYWORDS", "ビットコイン")

    def fake_fetch_all(s):
        feed = s.scalar(select(Feed))
        feed.last_fetched_at = datetime(2026, 1, 1)
        new = store_entries(s, feed, parse_feed(rss_bytes))
        s.commit()
        return new

    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<opml><body><outline text="S" xmlUrl="https://example.com/feed.xml"/></body></opml>')
    assert cli.run(opml) == 1
