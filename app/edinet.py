"""EDINET（金融庁の電子開示システム）から、ウォッチリスト銘柄の開示書類を取り込む。

EDINET API v2 は無料・公式の API で、API キー（多要素認証つきのアカウントで発行）が必要。
有価証券報告書・四半期報告書・臨時報告書・自己株券買付状況報告書・
大量保有報告書（提出者が上場企業の場合）などが対象になる。

取り込んだ書類は通常の記事と同じように一覧・ダイジェストに並び、
キーワード通知が有効ならすぐにメールでも知らせる。
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from .db import Category, Feed, get_setting
from .fetcher import FetchResult, ParsedEntry, _client, store_entries
from .timeutil import LOCAL_TZ

log = logging.getLogger(__name__)
# httpx は INFO でリクエスト URL（API キーを含む）を出すので抑える
logging.getLogger("httpx").setLevel(logging.WARNING)

API_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
# 書類の PDF は API キーなしで公開されている URL を使う（メールに API キーを載せないため）
PDF_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"
FEED_URL = "edinet://watchlist"
FEED_TITLE = "EDINET 開示書類（ウォッチリスト）"
CATEGORY_NAME = "IR・開示"

_CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")


def parse_watchlist(raw: str) -> list[str]:
    """「7203, 6758 9984」のような入力を 4 桁の証券コードのリストにする。"""
    codes = []
    for part in re.split(r"[,\s、]+", (raw or "").upper()):
        part = part.strip()
        if _CODE_RE.match(part) and part not in codes:
            codes.append(part)
    return codes


def is_edinet_feed(feed: Feed) -> bool:
    return feed.url == FEED_URL


def ensure_feed(session) -> Feed:
    feed = session.scalar(select(Feed).where(Feed.url == FEED_URL))
    if feed:
        return feed
    cat = session.scalar(select(Category).where(Category.name == CATEGORY_NAME))
    if cat is None:
        cat = Category(name=CATEGORY_NAME, position=-1)
        session.add(cat)
        session.flush()
    feed = Feed(title=FEED_TITLE, url=FEED_URL, site_url="https://disclosure2.edinet-fsa.go.jp/", category_id=cat.id)
    session.add(feed)
    session.flush()
    return feed


def _parse_submit_time(value: str) -> datetime:
    """"2026-10-19 09:15"（日本時間）を UTC の naive datetime にする。"""
    local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def entries_from_results(results: list[dict], codes: list[str]) -> list[ParsedEntry]:
    wanted = set(codes)
    entries = []
    for doc in results:
        sec = (doc.get("secCode") or "")[:4]
        if sec not in wanted or doc.get("withdrawalStatus") == "1" or not doc.get("docID"):
            continue
        filer = doc.get("filerName") or ""
        desc = doc.get("docDescription") or "開示書類"
        submitted = doc.get("submitDateTime") or ""
        try:
            published = _parse_submit_time(submitted)
        except ValueError:
            published = datetime.now(timezone.utc).replace(tzinfo=None)
        entries.append(
            ParsedEntry(
                guid=f"edinet:{doc['docID']}",
                title=f"【{sec} {filer}】{desc}"[:1000],
                link=PDF_URL.format(doc_id=doc["docID"]),
                summary=f"EDINET · 証券コード {sec} · 提出 {submitted}",
                published_at=published,
            )
        )
    return entries


def download_documents(api_key: str, day: date) -> list[dict]:
    params = {"date": day.isoformat(), "type": "2", "Subscription-Key": api_key}
    with _client() as client:
        res = client.get(API_URL, params=params)
    res.raise_for_status()
    data = res.json()
    status = str((data.get("metadata") or {}).get("status", "200"))
    if status != "200":
        # API キーの誤りなどは本文の metadata で返ってくる。キーそのものはログに出さない
        message = (data.get("metadata") or {}).get("message", "")
        raise ValueError(f"EDINET API エラー（status {status}）: {message}")
    return data.get("results") or []


def fetch_edinet(session, api_key: str, codes: list[str], days: int = 2):
    """当日と前日の開示書類を取得し、ウォッチリスト銘柄の分を保存する。新着を返す。"""
    if not api_key or not codes:
        return []
    feed = ensure_feed(session)
    today = datetime.now(LOCAL_TZ).date()
    entries: list[ParsedEntry] = []
    try:
        for offset in range(days):
            entries.extend(entries_from_results(download_documents(api_key, today - timedelta(days=offset)), codes))
    except (httpx.HTTPError, ValueError) as exc:
        # httpx の例外メッセージには URL（＝API キー）が含まれうるので、種類だけを記録する
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        log.warning("EDINET の取得に失敗しました: %s", detail)
        feed.last_error = f"EDINET の取得に失敗しました: {detail}"
        feed.last_fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
        return []
    new = store_entries(session, feed, FetchResult(title=FEED_TITLE, entries=entries))
    session.commit()
    return new


def watchlist_from_settings(session, default: str = "") -> list[str]:
    return parse_watchlist(get_setting(session, "ir_watchlist", default))
