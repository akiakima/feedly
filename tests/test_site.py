import json
from datetime import datetime, timedelta

import pytest

import app.cli as cli
from app.db import Article, Category, Feed, utcnow
from app.site import SitePasswordError, build_payload, build_site, decrypt_payload, encrypt_payload, site_salt

PASSWORD = "correct horse battery staple"


def add_articles(session):
    cat = Category(name="AI")
    session.add(cat)
    session.flush()
    feed = Feed(title="サンプル", url="https://example.com/rss", category_id=cat.id)
    ir = Feed(title="EDINET", url="edinet://watchlist")
    session.add_all([feed, ir])
    session.flush()
    now = utcnow()
    session.add_all(
        [
            Article(feed_id=feed.id, guid="1", title="新しい記事", link="https://example.com/1",
                    summary="概要", published_at=now - timedelta(hours=1)),
            Article(feed_id=feed.id, guid="2", title="古い記事", link="https://example.com/2",
                    published_at=now - timedelta(days=30)),
            Article(feed_id=feed.id, guid="3", title="危険なリンク", link="javascript:alert(1)",
                    published_at=now),
            Article(feed_id=ir.id, guid="edinet:S100", title="【7203 トヨタ】臨時報告書",
                    link="https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100.pdf", published_at=now),
        ]
    )
    session.commit()


def test_payload_filters_old_and_unsafe(session):
    add_articles(session)
    payload = build_payload(session, days=7)
    titles = [a["t"] for a in payload["articles"]]
    assert "新しい記事" in titles and "【7203 トヨタ】臨時報告書" in titles
    assert "古い記事" not in titles and "危険なリンク" not in titles
    assert payload["categories"] == ["AI"]
    assert {a["c"] for a in payload["articles"]} == {"AI", "未分類"}


def test_encrypt_roundtrip():
    payload = {"articles": [{"t": "日本語のタイトル"}]}
    blob = encrypt_payload(payload, PASSWORD, b"0" * 16, iterations=1000)
    assert "日本語" not in json.dumps(blob)
    assert decrypt_payload(blob, PASSWORD) == payload
    with pytest.raises(Exception):
        decrypt_payload(blob, PASSWORD + "x")


def test_short_password_rejected():
    with pytest.raises(SitePasswordError):
        encrypt_payload({}, "short", b"0" * 16)


def test_salt_is_stable(session):
    assert site_salt(session) == site_salt(session)


def test_build_site(session, tmp_path):
    add_articles(session)
    out = tmp_path / "site"
    assert build_site(session, out, PASSWORD) == 2
    assert {p.name for p in out.iterdir()} >= {"index.html", "app.js", "style.css", "data.json"}
    raw = (out / "data.json").read_text()
    # 公開されるファイルに平文の記事・ウォッチリストが含まれない
    assert "トヨタ" not in raw and "example.com" not in raw
    assert len(decrypt_payload(json.loads(raw), PASSWORD)["articles"]) == 2


def test_cli_build_site(monkeypatch, tmp_path, session):
    add_articles(session)
    monkeypatch.setenv("SITE_PASSWORD", "short")
    assert cli.main(["build-site", "--out", str(tmp_path / "a")]) == 1
    monkeypatch.setenv("SITE_PASSWORD", PASSWORD)
    assert cli.main(["build-site", "--out", str(tmp_path / "b")]) == 0
    assert (tmp_path / "b" / "data.json").exists()


def test_run_without_mail_marks_notified(monkeypatch, session, rss_bytes, tmp_path):
    from app.fetcher import parse_feed, store_entries
    from sqlalchemy import select

    monkeypatch.setattr(cli, "smtp_configured", lambda: False)
    monkeypatch.setattr(cli, "settings", cli.settings.__class__(**{**cli.settings.__dict__, "mail_to": ""}))

    def fake_fetch_all(s):
        feed = s.scalar(select(Feed))
        feed.last_fetched_at = datetime(2026, 1, 1)
        new = store_entries(s, feed, parse_feed(rss_bytes))
        s.commit()
        return new

    monkeypatch.setattr(cli, "fetch_all", fake_fetch_all)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<opml><body><outline text="S" xmlUrl="https://example.com/feed.xml"/></body></opml>')
    assert cli.run(opml) == 0
    session.expire_all()
    assert all(a.alerted and a.digested for a in session.scalars(select(Article)))
