"""Web アプリ本体（FastAPI）。"""

import base64
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import joinedload, selectinload

from . import presets, scheduler
from .config import settings
from .db import Article, Category, Feed, Keyword, SessionLocal, get_setting, init_db, set_setting
from .fetcher import discover_feed, fetch_feeds, store_entries
from .notifier import (
    mail_recipient,
    send_digest,
    send_mail,
    smtp_configured,
)
from .opml import export_opml, parse_opml
from .timeutil import format_local, relative_time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
PAGE_SIZE = 50


def seed_presets(session, only_missing: bool = False) -> int:
    """プリセットのカテゴリとフィードを登録する。追加したフィード数を返す。"""
    if not only_missing and session.scalar(select(func.count(Feed.id))):
        return 0
    cats = {c.name: c for c in session.scalars(select(Category))}
    for i, name in enumerate(presets.CATEGORIES):
        if name not in cats:
            cats[name] = Category(name=name, position=i)
            session.add(cats[name])
    session.flush()
    existing = set(session.scalars(select(Feed.url)))
    added = 0
    for cat_name, title, url in presets.FEEDS:
        if url in existing:
            continue
        session.add(Feed(title=title, url=url, category_id=cats[cat_name].id))
        added += 1
    session.commit()
    return added


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.seed_presets:
        with SessionLocal() as session:
            seed_presets(session)
    if settings.enable_scheduler:
        scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="ニュースまとめ", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["localtime"] = format_local
templates.env.filters["ago"] = relative_time


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """APP_USERNAME / APP_PASSWORD が設定されていれば Basic 認証をかける。"""
    if settings.app_username and settings.app_password and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, settings.app_username) and secrets.compare_digest(
                    pw, settings.app_password
                )
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="news"'})
    return await call_next(request)


def redirect(url: str, message: str = "", error: bool = False) -> RedirectResponse:
    if message:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'error' if error else 'msg': message})}"
    return RedirectResponse(url, status_code=303)


def safe_next(value: str | None, default: str = "/") -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else default


def sidebar(session) -> dict:
    unread = dict(
        session.execute(
            select(Article.feed_id, func.count(Article.id))
            .where(Article.is_read.is_(False))
            .group_by(Article.feed_id)
        ).all()
    )
    categories = list(
        session.scalars(
            select(Category).options(selectinload(Category.feeds)).order_by(Category.position, Category.id)
        )
    )
    uncategorized = list(session.scalars(select(Feed).where(Feed.category_id.is_(None)).order_by(Feed.title)))
    starred = session.scalar(select(func.count(Article.id)).where(Article.is_starred.is_(True)))
    cat_unread = {c.id: sum(unread.get(f.id, 0) for f in c.feeds) for c in categories}
    return {
        "categories": categories,
        "uncategorized": uncategorized,
        "unread": unread,
        "cat_unread": cat_unread,
        "total_unread": sum(unread.values()),
        "starred_count": starred or 0,
    }


def article_filters(category: str, feed: int | None, view: str, q: str):
    conds = []
    if feed:
        conds.append(Article.feed_id == feed)
    elif category == "none":
        conds.append(Feed.category_id.is_(None))
    elif category.isdigit():
        conds.append(Feed.category_id == int(category))
    if view == "unread":
        conds.append(Article.is_read.is_(False))
    elif view == "starred":
        conds.append(Article.is_starred.is_(True))
    for word in q.split():
        like = f"%{word}%"
        conds.append(or_(Article.title.ilike(like), Article.summary.ilike(like)))
    return conds


# ---------------------------------------------------------------- 記事一覧


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    category: str = "",
    feed: int | None = None,
    view: str = "unread",
    q: str = "",
    page: int = 1,
):
    view = view if view in ("unread", "all", "starred") else "unread"
    page = max(page, 1)
    with SessionLocal() as session:
        conds = article_filters(category, feed, view, q)
        base = select(Article).join(Feed).where(*conds)
        total = session.scalar(select(func.count()).select_from(base.subquery()))
        articles = list(
            session.scalars(
                base.options(joinedload(Article.feed).joinedload(Feed.category))
                .order_by(Article.published_at.desc(), Article.id.desc())
                .limit(PAGE_SIZE)
                .offset((page - 1) * PAGE_SIZE)
            )
        )
        title = "すべての記事"
        if view == "starred":
            title = "あとで読む"
        if feed:
            f = session.get(Feed, feed)
            title = f.title if f else title
        elif category == "none":
            title = "未分類"
        elif category.isdigit():
            c = session.get(Category, int(category))
            title = c.name if c else title
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **sidebar(session),
                "articles": articles,
                "title": title,
                "category": category,
                "feed": feed,
                "view": view,
                "q": q,
                "page": page,
                "total": total or 0,
                "has_next": page * PAGE_SIZE < (total or 0),
                "page_size": PAGE_SIZE,
            },
        )


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@app.get("/articles/{article_id}/open")
def open_article(article_id: int):
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(404)
        article.is_read = True
        session.commit()
        return RedirectResponse(article.link, status_code=302)


@app.post("/articles/{article_id}/{action}")
def article_action(request: Request, article_id: int, action: str, next: str = Form("/")):
    with SessionLocal() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(404)
        if action == "read":
            article.is_read = True
        elif action == "unread":
            article.is_read = False
        elif action == "toggle-read":
            article.is_read = not article.is_read
        elif action == "toggle-star":
            article.is_starred = not article.is_starred
        else:
            raise HTTPException(404)
        session.commit()
        state = {"id": article.id, "is_read": article.is_read, "is_starred": article.is_starred}
    if _wants_json(request):
        return JSONResponse(state)
    return redirect(safe_next(next))


@app.post("/mark-read")
def mark_read(
    category: str = Form(""),
    feed: str = Form(""),
    q: str = Form(""),
    next: str = Form("/"),
):
    feed_id = int(feed) if feed.isdigit() else None
    with SessionLocal() as session:
        conds = article_filters(category, feed_id, "unread", q)
        ids = select(Article.id).join(Feed).where(*conds)
        res = session.execute(update(Article).where(Article.id.in_(ids)).values(is_read=True))
        session.commit()
    return redirect(safe_next(next), f"{res.rowcount} 件を既読にしました")


@app.post("/refresh")
def refresh(next: str = Form("/")):
    if scheduler._fetch_lock.locked():
        return redirect(safe_next(next), "更新中です。しばらくしてから再読み込みしてください")
    threading.Thread(target=scheduler.fetch_job, daemon=True).start()
    return redirect(safe_next(next), "更新を開始しました。数十秒後に再読み込みしてください")


# ---------------------------------------------------------------- フィード管理


@app.get("/feeds", response_class=HTMLResponse)
def feeds_page(request: Request):
    with SessionLocal() as session:
        counts = dict(
            session.execute(select(Article.feed_id, func.count(Article.id)).group_by(Article.feed_id)).all()
        )
        return templates.TemplateResponse(request, "feeds.html", {**sidebar(session), "counts": counts})


def get_or_create_category(session, name: str) -> Category | None:
    name = (name or "").strip()
    if not name:
        return None
    cat = session.scalar(select(Category).where(Category.name == name))
    if cat:
        return cat
    position = (session.scalar(select(func.max(Category.position))) or 0) + 1
    cat = Category(name=name, position=position)
    session.add(cat)
    session.flush()
    return cat


@app.post("/feeds")
def add_feed(
    url: str = Form(...),
    category_id: str = Form(""),
    new_category: str = Form(""),
    title: str = Form(""),
):
    url = url.strip()
    try:
        feed_url, result = discover_feed(url)
    except Exception as exc:  # noqa: BLE001
        return redirect("/feeds", f"追加できませんでした: {exc}", error=True)

    with SessionLocal() as session:
        if session.scalar(select(Feed).where(Feed.url == feed_url)):
            return redirect("/feeds", "このフィードは登録済みです", error=True)
        cat = get_or_create_category(session, new_category)
        if cat is None and category_id.isdigit():
            cat = session.get(Category, int(category_id))
        feed = Feed(
            title=(title.strip() or result.title or feed_url)[:300],
            url=feed_url,
            site_url=result.site_url[:1000],
            category_id=cat.id if cat else None,
        )
        session.add(feed)
        session.flush()
        new = store_entries(session, feed, result)
        session.commit()
        return redirect("/feeds", f"「{feed.title}」を追加しました（{len(new)} 件）")


@app.post("/feeds/{feed_id}/edit")
def edit_feed(
    feed_id: int,
    title: str = Form(...),
    url: str = Form(...),
    category_id: str = Form(""),
):
    with SessionLocal() as session:
        feed = session.get(Feed, feed_id)
        if not feed:
            raise HTTPException(404)
        feed.title = title.strip()[:300] or feed.title
        new_url = url.strip()
        if new_url and new_url != feed.url:
            if session.scalar(select(Feed).where(Feed.url == new_url)):
                return redirect("/feeds", "同じ URL のフィードが既にあります", error=True)
            feed.url = new_url
            feed.etag = feed.modified = feed.last_error = ""
        feed.category_id = int(category_id) if category_id.isdigit() else None
        session.commit()
    return redirect("/feeds", "保存しました")


@app.post("/feeds/{feed_id}/toggle")
def toggle_feed(feed_id: int):
    with SessionLocal() as session:
        feed = session.get(Feed, feed_id)
        if not feed:
            raise HTTPException(404)
        feed.enabled = not feed.enabled
        session.commit()
        state = "有効" if feed.enabled else "停止"
        return redirect("/feeds", f"「{feed.title}」を{state}にしました")


@app.post("/feeds/{feed_id}/refresh")
def refresh_feed(feed_id: int):
    with SessionLocal() as session:
        feed = session.get(Feed, feed_id)
        if not feed:
            raise HTTPException(404)
        new = fetch_feeds(session, [feed], workers=1)
        if feed.last_error:
            return redirect("/feeds", f"取得に失敗しました: {feed.last_error}", error=True)
        return redirect("/feeds", f"「{feed.title}」を更新しました（新着 {len(new)} 件）")


@app.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int):
    with SessionLocal() as session:
        feed = session.get(Feed, feed_id)
        if not feed:
            raise HTTPException(404)
        title = feed.title
        session.delete(feed)
        session.commit()
    return redirect("/feeds", f"「{title}」を削除しました")


@app.post("/feeds/presets")
def add_presets():
    with SessionLocal() as session:
        added = seed_presets(session, only_missing=True)
    return redirect("/feeds", f"おすすめフィードを {added} 件追加しました")


@app.post("/categories")
def add_category(name: str = Form(...)):
    with SessionLocal() as session:
        cat = get_or_create_category(session, name)
        session.commit()
    return redirect("/feeds", f"ジャンル「{cat.name}」を追加しました" if cat else "")


@app.post("/categories/{category_id}/edit")
def edit_category(category_id: int, name: str = Form(...), position: int = Form(0)):
    with SessionLocal() as session:
        cat = session.get(Category, category_id)
        if not cat:
            raise HTTPException(404)
        name = name.strip()
        if name and name != cat.name:
            if session.scalar(select(Category).where(Category.name == name)):
                return redirect("/feeds", "同じ名前のジャンルがあります", error=True)
            cat.name = name
        cat.position = position
        session.commit()
    return redirect("/feeds", "保存しました")


@app.post("/categories/{category_id}/delete")
def delete_category(category_id: int):
    with SessionLocal() as session:
        cat = session.get(Category, category_id)
        if not cat:
            raise HTTPException(404)
        for f in cat.feeds:
            f.category_id = None
        session.delete(cat)
        session.commit()
    return redirect("/feeds", "ジャンルを削除しました（フィードは「未分類」に移動）")


@app.get("/opml")
def opml_export():
    with SessionLocal() as session:
        data = sidebar(session)
        body = export_opml(data["categories"], data["uncategorized"])
    return Response(
        body,
        media_type="text/x-opml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="feeds.opml"'},
    )


@app.post("/opml")
async def opml_import(file: UploadFile = File(...)):
    try:
        entries = parse_opml(await file.read())
    except Exception as exc:  # noqa: BLE001
        return redirect("/feeds", f"OPML を読み込めませんでした: {exc}", error=True)
    added = 0
    with SessionLocal() as session:
        existing = set(session.scalars(select(Feed.url)))
        for cat_name, title, url, site in entries:
            if url in existing:
                continue
            cat = get_or_create_category(session, cat_name or "")
            session.add(Feed(title=title[:300], url=url, site_url=site[:1000], category_id=cat.id if cat else None))
            existing.add(url)
            added += 1
        session.commit()
    return redirect("/feeds", f"{added} 件のフィードを取り込みました")


# ---------------------------------------------------------------- 設定・通知


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with SessionLocal() as session:
        keywords = list(session.scalars(select(Keyword).order_by(Keyword.word)))
        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("digest-")] if scheduler.scheduler.running else []
        ctx = {
            **sidebar(session),
            "mail_to": mail_recipient(session),
            "digest_times": get_setting(session, "digest_times", settings.digest_times),
            "digest_enabled": get_setting(session, "digest_enabled", "1") == "1",
            "keyword_alert_enabled": get_setting(session, "keyword_alert_enabled", "1") == "1",
            "keywords": keywords,
            "smtp_configured": smtp_configured(),
            "smtp_host": settings.smtp_host,
            "smtp_user": settings.smtp_user,
            "fetch_interval": settings.fetch_interval_minutes,
            "next_digests": sorted(j.next_run_time for j in jobs if j.next_run_time),
        }
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings")
def save_settings(
    mail_to: str = Form(""),
    digest_times: str = Form(""),
    digest_enabled: str = Form(""),
    keyword_alert_enabled: str = Form(""),
):
    times = scheduler.parse_times(digest_times)
    with SessionLocal() as session:
        set_setting(session, "mail_to", mail_to.strip())
        set_setting(session, "digest_times", ",".join(f"{h:02d}:{m:02d}" for h, m in times))
        set_setting(session, "digest_enabled", "1" if digest_enabled else "0")
        set_setting(session, "keyword_alert_enabled", "1" if keyword_alert_enabled else "0")
        session.commit()
    if scheduler.scheduler.running:
        scheduler.schedule_digests()
    return redirect("/settings", "設定を保存しました")


@app.post("/keywords")
def add_keyword(words: str = Form(...)):
    added = 0
    with SessionLocal() as session:
        existing = {k.casefold() for k in session.scalars(select(Keyword.word))}
        for word in words.replace("、", ",").split(","):
            word = word.strip()[:200]
            if word and word.casefold() not in existing:
                session.add(Keyword(word=word))
                existing.add(word.casefold())
                added += 1
        session.commit()
    return redirect("/settings", f"キーワードを {added} 件追加しました")


@app.post("/keywords/{keyword_id}/delete")
def delete_keyword(keyword_id: int):
    with SessionLocal() as session:
        kw = session.get(Keyword, keyword_id)
        if kw:
            session.delete(kw)
            session.commit()
    return redirect("/settings", "キーワードを削除しました")


@app.post("/settings/test-mail")
def test_mail():
    with SessionLocal() as session:
        to = mail_recipient(session)
    try:
        send_mail(to, "【ニュースまとめ】テストメール", "メール通知の設定は正常です。")
    except Exception as exc:  # noqa: BLE001
        return redirect("/settings", f"送信に失敗しました: {exc}", error=True)
    return redirect("/settings", f"{to} にテストメールを送信しました")


@app.post("/settings/send-digest")
def send_digest_now():
    with SessionLocal() as session:
        try:
            sent = send_digest(session, force=True)
        except Exception as exc:  # noqa: BLE001
            return redirect("/settings", f"送信に失敗しました: {exc}", error=True)
    if not sent:
        return redirect("/settings", "未送信の新着記事はありません")
    return redirect("/settings", f"{sent} 件をまとめて送信しました")


@app.get("/healthz")
def healthz():
    return {"ok": True}
