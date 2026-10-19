import dataclasses
import logging
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.cli as cli
import app.edinet as edinet
import app.main as main
from app.db import Article, Category, Feed
from app.notifier import send_keyword_alerts
from app.opml import export_opml

SAMPLE = [
    {
        "docID": "S100AAAA",
        "secCode": "72030",
        "filerName": "トヨタ自動車株式会社",
        "docDescription": "臨時報告書",
        "submitDateTime": "2026-10-19 15:30",
        "withdrawalStatus": "0",
    },
    {
        "docID": "S100BBBB",
        "secCode": "99990",
        "filerName": "対象外株式会社",
        "docDescription": "有価証券報告書",
        "submitDateTime": "2026-10-19 09:00",
        "withdrawalStatus": "0",
    },
    {
        "docID": "S100CCCC",
        "secCode": "72030",
        "filerName": "トヨタ自動車株式会社",
        "docDescription": "取り下げられた書類",
        "submitDateTime": "2026-10-19 10:00",
        "withdrawalStatus": "1",
    },
    {"docID": "S100DDDD", "secCode": None, "filerName": "ファンド", "docDescription": "x", "submitDateTime": ""},
]


def test_parse_watchlist():
    assert edinet.parse_watchlist("7203, 6758、9984 7203 abc 12345 285a") == ["7203", "6758", "9984", "285A"]
    assert edinet.parse_watchlist("") == []


def test_entries_from_results():
    entries = edinet.entries_from_results(SAMPLE, ["7203"])
    assert len(entries) == 1
    e = entries[0]
    assert e.guid == "edinet:S100AAAA"
    assert e.title == "【7203 トヨタ自動車株式会社】臨時報告書"
    # API キー不要の公開 PDF にリンクする
    assert e.link == "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100AAAA.pdf"
    assert e.published_at == datetime(2026, 10, 19, 6, 30)  # JST 15:30 → UTC


def test_fetch_edinet_stores_and_alerts(monkeypatch, session, sent_mails):
    calls = []

    def fake_download(api_key, day):
        calls.append(day)
        return SAMPLE

    monkeypatch.setattr(edinet, "download_documents", fake_download)
    # 1 回目は初回扱い（通知済み）
    first = edinet.fetch_edinet(session, "key", ["7203"])
    assert len(first) == 1 and first[0].alerted
    assert len(calls) == 2  # 当日と前日

    new_doc = dict(SAMPLE[0], docID="S100EEEE", docDescription="自己株券買付状況報告書")
    monkeypatch.setattr(edinet, "download_documents", lambda k, d: [new_doc])
    second = edinet.fetch_edinet(session, "key", ["7203"])
    assert len(second) == 1

    feed = session.scalar(select(Feed).where(Feed.url == edinet.FEED_URL))
    assert session.get(Category, feed.category_id).name == "IR・開示"

    # キーワードがなくても IR 開示はすぐ通知する
    assert send_keyword_alerts(session, []) == 1
    assert "IR開示" in sent_mails[0]["subject"]
    assert "自己株券買付状況報告書" in sent_mails[0]["text"]


def test_fetch_edinet_skips_without_key_or_codes(session):
    assert edinet.fetch_edinet(session, "", ["7203"]) == []
    assert edinet.fetch_edinet(session, "key", []) == []
    assert session.scalar(select(Feed)) is None


def test_fetch_edinet_error_does_not_leak_key(monkeypatch, session, caplog):
    secret = "SECRET-API-KEY-123"

    def failing(api_key, day):
        request = httpx.Request("GET", f"{edinet.API_URL}?Subscription-Key={api_key}")
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    monkeypatch.setattr(edinet, "download_documents", failing)
    with caplog.at_level(logging.DEBUG):
        assert edinet.fetch_edinet(session, secret, ["7203"]) == []
    feed = session.scalar(select(Feed).where(Feed.url == edinet.FEED_URL))
    assert feed.last_error and secret not in feed.last_error
    assert secret not in caplog.text


def test_download_documents_reports_api_error(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params):
            return httpx.Response(
                200,
                json={"metadata": {"status": "401", "message": "Access denied due to invalid subscription key."}},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(edinet, "_client", lambda: FakeClient())
    with pytest.raises(ValueError, match="401"):
        edinet.download_documents("bad", datetime(2026, 10, 19).date())


def test_edinet_feed_not_exported_or_fetched_as_rss(session):
    feed = edinet.ensure_feed(session)
    session.commit()
    cat = session.get(Category, feed.category_id)
    assert "edinet://" not in export_opml([cat], [])


def test_cli_keeps_edinet_feed_and_uses_secret_opml(monkeypatch, session):
    edinet.ensure_feed(session)
    session.commit()
    monkeypatch.setenv(
        "FEEDS_OPML_XML",
        '<opml><body><outline text="IR"><outline text="社 IR" xmlUrl="https://ir.example.com/rss"/></outline></body></opml>',
    )
    wanted = cli.desired_feeds(None)
    assert wanted == [("IR", "社 IR", "https://ir.example.com/rss")]
    cli.sync_feeds(session, wanted)
    urls = {f.url for f in session.scalars(select(Feed))}
    assert urls == {edinet.FEED_URL, "https://ir.example.com/rss"}


def test_settings_watchlist(monkeypatch):
    with TestClient(main.app) as client:
        res = client.post("/ir-watchlist", data={"codes": "7203, 6758 x"})
        assert "2 銘柄" in res.text
        assert 'value="7203 6758"' in res.text
        assert "IR・開示" in client.get("/").text


def test_refresh_edinet_feed(monkeypatch, session):
    monkeypatch.setattr(edinet, "download_documents", lambda k, d: SAMPLE)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, edinet_api_key="key", ir_watchlist="7203"))
    feed = edinet.ensure_feed(session)
    session.commit()
    with TestClient(main.app) as client:
        res = client.post(f"/feeds/{feed.id}/refresh")
    assert "新着 1 件" in res.text
    assert session.scalar(select(Article).where(Article.guid == "edinet:S100AAAA")) is not None
