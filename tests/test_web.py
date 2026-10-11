import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app.db import Article, Category, Feed, Keyword, SessionLocal
from app.fetcher import parse_feed


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def fake_discover(monkeypatch, rss_bytes):
    def _discover(url):
        return "https://example.com/feed.xml", parse_feed(rss_bytes)

    monkeypatch.setattr(main, "discover_feed", _discover)


def test_pages_render_empty(client):
    for path in ("/", "/feeds", "/settings", "/?view=starred", "/?view=all&q=test"):
        res = client.get(path)
        assert res.status_code == 200, path


def test_add_feed_and_read_flow(client, fake_discover):
    res = client.post("/feeds", data={"url": "example.com", "new_category": "マクロ経済"})
    assert res.status_code == 200
    assert "サンプル経済ニュース" in res.text

    with SessionLocal() as s:
        feed = s.scalar(select(Feed))
        assert feed.category.name == "マクロ経済"
        article_ids = list(s.scalars(select(Article.id)))
    assert len(article_ids) == 2

    page = client.get("/")
    assert "ビットコインが最高値を更新" in page.text

    res = client.post(
        f"/articles/{article_ids[0]}/toggle-star", headers={"Accept": "application/json"}
    )
    assert res.json()["is_starred"] is True
    assert "日経平均" in client.get("/?view=starred").text or "ビットコイン" in client.get("/?view=starred").text

    opened = client.get(f"/articles/{article_ids[1]}/open", follow_redirects=False)
    assert opened.status_code == 302
    assert opened.headers["location"].startswith("https://example.com/articles/")

    client.post("/mark-read", data={"category": "", "feed": "", "q": "", "next": "/"})
    with SessionLocal() as s:
        assert all(s.scalars(select(Article.is_read)))
    assert "未読の記事はありません" in client.get("/").text


def test_duplicate_feed_rejected(client, fake_discover):
    client.post("/feeds", data={"url": "example.com"})
    res = client.post("/feeds", data={"url": "example.com"})
    assert "登録済み" in res.text


def test_category_and_feed_management(client, fake_discover):
    client.post("/categories", data={"name": "不動産"})
    client.post("/feeds", data={"url": "example.com"})
    with SessionLocal() as s:
        cat = s.scalar(select(Category).where(Category.name == "不動産"))
        feed = s.scalar(select(Feed))
    client.post(
        f"/feeds/{feed.id}/edit",
        data={"title": "改名", "url": feed.url, "category_id": str(cat.id)},
    )
    client.post(f"/feeds/{feed.id}/toggle")
    with SessionLocal() as s:
        feed = s.get(Feed, feed.id)
        assert (feed.title, feed.category_id, feed.enabled) == ("改名", cat.id, False)

    client.post(f"/categories/{cat.id}/delete")
    with SessionLocal() as s:
        assert s.get(Feed, feed.id).category_id is None

    client.post(f"/feeds/{feed.id}/delete")
    with SessionLocal() as s:
        assert s.scalar(select(Feed)) is None
        assert s.scalar(select(Article)) is None


def test_presets_and_opml(client):
    client.post("/feeds/presets")
    with SessionLocal() as s:
        count = len(list(s.scalars(select(Feed))))
    assert count > 10
    opml = client.get("/opml")
    assert opml.status_code == 200
    assert "仮想通貨" in opml.text

    with SessionLocal() as s:
        for f in s.scalars(select(Feed)):
            s.delete(f)
        s.commit()
    client.post("/opml", files={"file": ("feeds.opml", opml.content, "text/xml")})
    with SessionLocal() as s:
        assert len(list(s.scalars(select(Feed)))) == count


def test_settings_and_keywords(client, sent_mails):
    client.post(
        "/settings",
        data={"mail_to": "news@example.com", "digest_times": "8:00, 21:30", "digest_enabled": "1"},
    )
    page = client.get("/settings")
    assert 'value="news@example.com"' in page.text
    assert 'value="08:00,21:30"' in page.text

    client.post("/keywords", data={"words": "AI、半導体, ai"})
    with SessionLocal() as s:
        assert sorted(s.scalars(select(Keyword.word))) == ["AI", "半導体"]

    client.post("/settings/test-mail")
    assert sent_mails[-1]["to"] == "news@example.com"
