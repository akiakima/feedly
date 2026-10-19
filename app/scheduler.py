"""定期実行ジョブ（フィード取得・キーワード通知・ダイジェスト送信・古い記事の削除）。"""

import logging
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import SessionLocal, get_setting
from .edinet import fetch_edinet, watchlist_from_settings
from .fetcher import fetch_all, purge_old_articles
from .notifier import MailNotConfigured, send_digest, send_keyword_alerts

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=settings.timezone)
_fetch_lock = threading.Lock()
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_times(value: str) -> list[tuple[int, int]]:
    times = []
    for part in re.split(r"[,\s、]+", value or ""):
        m = _TIME_RE.match(part.strip())
        if m:
            times.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(times))


def fetch_job() -> int:
    """全フィードを取得し、キーワード一致があれば通知する。新着件数を返す。"""
    if not _fetch_lock.acquire(blocking=False):
        log.info("前回の取得がまだ実行中のためスキップします")
        return 0
    try:
        with SessionLocal() as session:
            new_articles = fetch_all(session)
            new_articles += fetch_edinet(
                session, settings.edinet_api_key, watchlist_from_settings(session, settings.ir_watchlist)
            )
            log.info("新着 %d 件", len(new_articles))
            try:
                send_keyword_alerts(session)
            except MailNotConfigured as exc:
                log.info("キーワード通知をスキップ: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("キーワード通知の送信に失敗しました")
            return len(new_articles)
    finally:
        _fetch_lock.release()


def digest_job() -> None:
    with SessionLocal() as session:
        try:
            sent = send_digest(session)
            log.info("ダイジェスト送信: %d 件", sent)
        except MailNotConfigured as exc:
            log.info("ダイジェストをスキップ: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("ダイジェストの送信に失敗しました")


def cleanup_job() -> None:
    with SessionLocal() as session:
        removed = purge_old_articles(session, settings.retention_days)
        log.info("古い記事を %d 件削除しました", removed)


def schedule_digests() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("digest-"):
            job.remove()
    with SessionLocal() as session:
        times = parse_times(get_setting(session, "digest_times", settings.digest_times))
    for hour, minute in times:
        scheduler.add_job(
            digest_job,
            CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            id=f"digest-{hour:02d}{minute:02d}",
            replace_existing=True,
        )


def start() -> None:
    scheduler.add_job(
        fetch_job,
        "interval",
        minutes=settings.fetch_interval_minutes,
        id="fetch",
        replace_existing=True,
        next_run_time=None,
    )
    scheduler.add_job(cleanup_job, CronTrigger(hour=4, minute=0), id="cleanup", replace_existing=True)
    schedule_digests()
    scheduler.start()
    # 起動直後に 1 回取得する
    threading.Thread(target=fetch_job, daemon=True).start()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
