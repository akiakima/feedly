"""サーバーなしでニュース通知だけを動かすコマンド（GitHub Actions 用）。

    python -m app.cli run

1 回の実行で「フィード取得 → キーワード通知 → 時刻が来ていればダイジェスト送信」を行う。
定期実行は GitHub Actions のスケジュールに任せる（.github/workflows/notify.yml）。

設定はすべて環境変数（GitHub Secrets / Variables）から読む。
メールアドレスとキーワードは DB に保存しないので、Actions のキャッシュにも残らない。
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from . import presets
from .config import BASE_DIR, settings
from .db import Category, Feed, SessionLocal, get_setting, init_db, set_setting, utcnow
from .edinet import FEED_URL as EDINET_FEED_URL
from .edinet import fetch_edinet, parse_watchlist
from .fetcher import fetch_all, purge_old_articles
from .notifier import MailNotConfigured, send_digest, send_keyword_alerts
from .opml import parse_opml
from .scheduler import parse_times
from .timeutil import LOCAL_TZ

log = logging.getLogger("app.cli")

LAST_DIGEST_KEY = "last_digest_at"


def desired_feeds(opml_path: Path | None) -> list[tuple[str | None, str, str]]:
    """購読するフィード一覧。

    優先順: 環境変数 FEEDS_OPML_XML（Secret に入れれば購読先を非公開にできる）
    → feeds.opml ファイル → おすすめフィード。
    """
    inline = os.environ.get("FEEDS_OPML_XML", "").strip()
    if inline:
        return [(cat, title, url) for cat, title, url, _ in parse_opml(inline.encode())]
    if opml_path and opml_path.exists():
        return [(cat, title, url) for cat, title, url, _ in parse_opml(opml_path.read_bytes())]
    return list(presets.FEEDS)


def sync_feeds(session, wanted: list[tuple[str | None, str, str]]) -> None:
    """DB のフィードを一覧に合わせる（追加・ジャンル変更・削除）。"""
    cats = {c.name: c for c in session.scalars(select(Category))}

    def category_id(name: str | None) -> int | None:
        if not name:
            return None
        if name not in cats:
            cats[name] = Category(name=name, position=len(cats))
            session.add(cats[name])
            session.flush()
        return cats[name].id

    by_url = {f.url: f for f in session.scalars(select(Feed))}
    wanted_urls = set()
    for cat_name, title, url in wanted:
        if not url.startswith(("http://", "https://")) or url in wanted_urls:
            continue
        wanted_urls.add(url)
        feed = by_url.get(url)
        if feed is None:
            session.add(Feed(title=title[:300], url=url, category_id=category_id(cat_name)))
        else:
            feed.title = title[:300] or feed.title
            feed.category_id = category_id(cat_name)
            feed.enabled = True
    for url, feed in by_url.items():
        if url not in wanted_urls and url != EDINET_FEED_URL:
            session.delete(feed)
    session.commit()


def latest_slot(now_local: datetime, times: list[tuple[int, int]]) -> datetime | None:
    """now_local 以前で直近のダイジェスト配信時刻（ローカル時刻）を返す。"""
    candidates = []
    for days_ago in (0, 1):
        day = now_local.date() - timedelta(days=days_ago)
        for hour, minute in times:
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now_local.tzinfo)
            if slot <= now_local:
                candidates.append(slot)
    return max(candidates) if candidates else None


def digest_due(now_utc: datetime, times: list[tuple[int, int]], last_sent_utc: datetime | None) -> bool:
    """配信時刻を過ぎていて、その時刻以降まだ送っていなければ True。

    GitHub Actions の定期実行は数分〜数十分遅れることがあるので、
    「時刻ちょうど」ではなく「時刻を過ぎた最初の実行」で送る。
    """
    slot = latest_slot(now_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ), times)
    if slot is None:
        return False
    if last_sent_utc is None:
        return True
    return last_sent_utc.replace(tzinfo=timezone.utc) < slot


def env_keywords() -> list[str]:
    raw = os.environ.get("NOTIFY_KEYWORDS", "")
    return [k.strip() for k in raw.replace("、", ",").replace("\n", ",").split(",") if k.strip()]


def run(opml_path: Path | None) -> int:
    init_db()
    with SessionLocal() as session:
        sync_feeds(session, desired_feeds(opml_path))
        new_articles = fetch_all(session)
        codes = parse_watchlist(settings.ir_watchlist)
        if codes and not settings.edinet_api_key:
            log.warning("IR_WATCHLIST が設定されていますが EDINET_API_KEY がないため、IR 開示は取得しません")
        new_articles += fetch_edinet(session, settings.edinet_api_key, codes)
        failed = list(session.scalars(select(Feed).where(Feed.last_error != "")))
        log.info("新着 %d 件（取得エラー %d フィード）", len(new_articles), len(failed))
        # 公開リポジトリでは Actions のログを誰でも見られるので、購読先の名前や URL は
        # LOG_FEED_DETAILS=true のときだけ出す
        if os.environ.get("LOG_FEED_DETAILS", "").lower() == "true":
            for feed in failed:
                log.warning("取得エラー: %s (%s)", feed.title, feed.last_error[:150])

        status = 0
        try:
            hits = send_keyword_alerts(session, env_keywords())
            log.info("キーワード通知: %d 件", hits)

            now = utcnow()
            last = get_setting(session, LAST_DIGEST_KEY)
            last_dt = datetime.fromisoformat(last) if last else None
            if digest_due(now, parse_times(settings.digest_times), last_dt):
                sent = send_digest(session, force=True)
                log.info("ダイジェスト送信: %d 件", sent)
                set_setting(session, LAST_DIGEST_KEY, now.isoformat())
                session.commit()
        except MailNotConfigured as exc:
            log.error("メール設定が不足しています: %s", exc)
            status = 1
        except Exception as exc:  # noqa: BLE001
            # 公開リポジトリではログが誰でも見られるので、宛先などを含みうる詳細は出さない
            log.error("メール送信に失敗しました（%s）。SMTP の設定を確認してください。", type(exc).__name__)
            status = 1

        purge_old_articles(session, settings.retention_days, only_read=False)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="フィードを取得してメール通知する")
    run_p.add_argument(
        "--opml",
        default=os.environ.get("FEEDS_OPML", str(BASE_DIR / "feeds.opml")),
        help="購読フィードの OPML（なければおすすめフィードを使う）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx のリクエストログには URL（API キーを含みうる）が出るので抑える
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if os.environ.get("LOG_FEED_DETAILS", "").lower() != "true":
        logging.getLogger("app.fetcher").setLevel(logging.ERROR)
    if args.command == "run":
        return run(Path(args.opml) if args.opml else None)
    return 2


if __name__ == "__main__":
    sys.exit(main())
