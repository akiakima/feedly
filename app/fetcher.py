"""RSS / Atom フィードの取得・解析・保存。"""

import calendar
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import feedparser
import httpx
from sqlalchemy import delete, select

from .db import Article, Feed, utcnow

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; FeedlyClone/1.0; +https://github.com/akiakima/feedly)"
TIMEOUT = 20.0
SUMMARY_LENGTH = 280

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def clean_text(value: str, limit: int | None = None) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = _SPACE_RE.sub(" ", html.unescape(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


@dataclass
class ParsedEntry:
    guid: str
    title: str
    link: str
    summary: str
    published_at: datetime


@dataclass
class FetchResult:
    title: str = ""
    site_url: str = ""
    entries: list[ParsedEntry] = field(default_factory=list)
    etag: str = ""
    modified: str = ""
    not_modified: bool = False


def _entry_datetime(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            dt = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).replace(tzinfo=None)
            # 未来日時を返すフィードがあるので現在時刻で頭打ちにする
            return min(dt, utcnow())
    return utcnow()


def parse_feed(content: bytes | str) -> FetchResult:
    parsed = feedparser.parse(content)
    if not parsed.get("version") and not parsed.entries:
        reason = parsed.get("bozo_exception") or "フィード形式ではありません"
        raise ValueError(f"RSS / Atom として解析できませんでした: {reason}")

    result = FetchResult(
        title=clean_text(parsed.feed.get("title", "")),
        site_url=parsed.feed.get("link", ""),
    )
    for entry in parsed.entries:
        link = entry.get("link", "")
        title = clean_text(entry.get("title", "")) or "(タイトルなし)"
        guid = entry.get("id") or link or title
        if not link.startswith(("http://", "https://")):
            continue
        summary = entry.get("summary", "")
        if not summary and entry.get("content"):
            summary = entry.content[0].get("value", "")
        result.entries.append(
            ParsedEntry(
                guid=guid[:1000],
                title=title[:1000],
                link=link[:2000],
                summary=clean_text(summary, SUMMARY_LENGTH),
                published_at=_entry_datetime(entry),
            )
        )
    return result


def download_feed(url: str, etag: str = "", modified: str = "") -> FetchResult:
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=headers) as client:
        res = client.get(url)
    if res.status_code == 304:
        return FetchResult(not_modified=True, etag=etag, modified=modified)
    res.raise_for_status()
    result = parse_feed(res.content)
    result.etag = res.headers.get("etag", "")
    result.modified = res.headers.get("last-modified", "")
    return result


class _AlternateLinkParser(HTMLParser):
    FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in self.FEED_TYPES:
            if a.get("href"):
                self.links.append(a["href"])


def find_feed_links(page_html: str, base_url: str) -> list[str]:
    parser = _AlternateLinkParser()
    parser.feed(page_html)
    return [urljoin(base_url, href) for href in parser.links]


def discover_feed(url: str) -> tuple[str, FetchResult]:
    """URL がフィードならそのまま、Web ページなら <link rel="alternate"> からフィードを探す。"""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        res = client.get(url)
        res.raise_for_status()
        try:
            return str(res.url), parse_feed(res.content)
        except ValueError:
            pass
        for candidate in find_feed_links(res.text, str(res.url)):
            try:
                sub = client.get(candidate)
                sub.raise_for_status()
                return str(sub.url), parse_feed(sub.content)
            except (httpx.HTTPError, ValueError):
                continue
    raise ValueError("このページから RSS / Atom フィードが見つかりませんでした。")


def store_entries(session, feed: Feed, result: FetchResult) -> list[Article]:
    """新着記事だけを保存して返す。初回取得分は通知済み扱いにして大量通知を防ぐ。"""
    first_fetch = feed.last_fetched_at is None
    feed.last_fetched_at = utcnow()
    feed.last_error = ""
    if result.not_modified:
        return []
    feed.etag = result.etag[:300]
    feed.modified = result.modified[:300]
    if result.title and (not feed.title or feed.title == feed.url):
        feed.title = result.title[:300]
    if result.site_url and not feed.site_url:
        feed.site_url = result.site_url[:1000]

    guids = [e.guid for e in result.entries]
    existing = set(
        session.scalars(
            select(Article.guid).where(Article.feed_id == feed.id, Article.guid.in_(guids))
        )
    ) if guids else set()

    new_articles = []
    seen = set()
    for entry in result.entries:
        if entry.guid in existing or entry.guid in seen:
            continue
        seen.add(entry.guid)
        article = Article(
            feed_id=feed.id,
            guid=entry.guid,
            title=entry.title,
            link=entry.link,
            summary=entry.summary,
            published_at=entry.published_at,
            digested=first_fetch,
            alerted=first_fetch,
        )
        session.add(article)
        new_articles.append(article)
    return new_articles


def fetch_feeds(session, feeds: list[Feed], workers: int = 6) -> list[Article]:
    """複数フィードを並列にダウンロードし、DB への書き込みは呼び出しスレッドで行う。"""

    def _download(feed_info):
        feed_id, url, etag, modified = feed_info
        try:
            return feed_id, download_feed(url, etag, modified), None
        except Exception as exc:  # noqa: BLE001 - フィードごとのエラーは記録して続行
            return feed_id, None, exc

    infos = [(f.id, f.url, f.etag, f.modified) for f in feeds]
    by_id = {f.id: f for f in feeds}
    new_articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for feed_id, result, error in pool.map(_download, infos):
            feed = by_id[feed_id]
            if error is not None:
                log.warning("フィード取得失敗 %s: %s", feed.url, error)
                feed.last_fetched_at = utcnow()
                feed.last_error = str(error)[:1000] or error.__class__.__name__
                continue
            new_articles.extend(store_entries(session, feed, result))
    session.commit()
    return new_articles


def fetch_all(session) -> list[Article]:
    feeds = list(session.scalars(select(Feed).where(Feed.enabled.is_(True))))
    return fetch_feeds(session, feeds)


def purge_old_articles(session, retention_days: int) -> int:
    """既読かつ未保存の古い記事を削除する。"""
    cutoff = utcnow() - timedelta(days=retention_days)
    res = session.execute(
        delete(Article).where(
            Article.fetched_at < cutoff,
            Article.is_starred.is_(False),
            Article.is_read.is_(True),
        )
    )
    session.commit()
    return res.rowcount or 0
