"""メール通知（定時ダイジェスト・キーワード通知）。"""

import logging
import smtplib
import ssl
from collections import OrderedDict
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .config import settings
from .db import Article, Feed, Keyword, get_setting
from .timeutil import format_local

log = logging.getLogger(__name__)

DIGEST_PER_CATEGORY = 10
UNCATEGORIZED = "未分類"

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)
_env.filters["localtime"] = format_local


class MailNotConfigured(RuntimeError):
    pass


def mail_recipient(session) -> str:
    return get_setting(session, "mail_to", settings.mail_to).strip()


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def send_mail(to: str, subject: str, text: str, html_body: str | None = None) -> None:
    if not smtp_configured():
        raise MailNotConfigured("SMTP_USER / SMTP_PASSWORD が設定されていません（.env を確認してください）。")
    if not to:
        raise MailNotConfigured("通知先メールアドレスが設定されていません（設定画面で入力してください）。")

    sender = settings.mail_from or settings.smtp_user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("ニュースまとめ", sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if settings.smtp_use_ssl or settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)


def group_by_category(articles: list[Article]) -> "OrderedDict[str, list[Article]]":
    def sort_key(a: Article):
        cat = a.feed.category
        return (cat is None, cat.position if cat else 0, cat.name if cat else "")

    groups: OrderedDict[str, list[Article]] = OrderedDict()
    for article in sorted(articles, key=sort_key):
        name = article.feed.category.name if article.feed.category else UNCATEGORIZED
        groups.setdefault(name, []).append(article)
    for name in groups:
        groups[name].sort(key=lambda a: a.published_at, reverse=True)
    return groups


def build_digest(articles: list[Article]) -> tuple[str, str, str]:
    groups = group_by_category(articles)
    shown = OrderedDict((name, items[:DIGEST_PER_CATEGORY]) for name, items in groups.items())
    counts = {name: len(items) for name, items in groups.items()}
    subject = f"【ニュースまとめ】新着 {len(articles)} 件"

    lines = [f"新着記事 {len(articles)} 件", ""]
    for name, items in shown.items():
        lines.append(f"■ {name}（{counts[name]} 件）")
        for a in items:
            lines.append(f"・{a.title}［{a.feed.title}］")
            lines.append(f"  {a.link}")
        if counts[name] > len(items):
            lines.append(f"  ほか {counts[name] - len(items)} 件")
        lines.append("")
    lines.append(f"すべての記事: {settings.app_base_url}/")
    text = "\n".join(lines)

    html_body = _env.get_template("email_digest.html").render(
        heading=f"新着記事 {len(articles)} 件",
        groups=shown,
        counts=counts,
        base_url=settings.app_base_url,
    )
    return subject, text, html_body


def send_digest(session, force: bool = False) -> int:
    """未送信の新着記事をカテゴリ別にまとめて送る。送った件数を返す。"""
    if not force and get_setting(session, "digest_enabled", "1") != "1":
        return 0
    articles = list(
        session.scalars(
            select(Article)
            .options(joinedload(Article.feed).joinedload(Feed.category))
            .where(Article.digested.is_(False))
        ).unique()
    )
    if not articles:
        return 0
    subject, text, html_body = build_digest(articles)
    send_mail(mail_recipient(session), subject, text, html_body)
    for a in articles:
        a.digested = True
    session.commit()
    return len(articles)


def match_keywords(article: Article, keywords: list[str]) -> list[str]:
    haystack = f"{article.title}\n{article.summary}".casefold()
    return [k for k in keywords if k.casefold() in haystack]


def send_keyword_alerts(session, keywords: list[str] | None = None) -> int:
    """登録キーワードに一致した新着記事をすぐに通知する。

    keywords を渡すと DB のキーワードの代わりに使う（GitHub Actions モード用）。
    """
    if get_setting(session, "keyword_alert_enabled", "1") != "1":
        return 0
    if keywords is None:
        keywords = list(session.scalars(select(Keyword.word)))
    keywords = [k.strip() for k in keywords if k.strip()]
    pending = list(
        session.scalars(
            select(Article)
            .options(joinedload(Article.feed).joinedload(Feed.category))
            .where(Article.alerted.is_(False))
        ).unique()
    )
    if not pending:
        return 0

    hits = []
    for article in pending:
        matched = match_keywords(article, keywords) if keywords else []
        if matched:
            hits.append((article, matched))

    if hits:
        words = sorted({w for _, ms in hits for w in ms})
        subject = f"【キーワード通知】{'・'.join(words[:3])} など {len(hits)} 件"
        lines = []
        for article, matched in hits:
            lines.append(f"・{article.title}［{article.feed.title}］（{'、'.join(matched)}）")
            lines.append(f"  {article.link}")
        text = "\n".join(lines)
        html_body = _env.get_template("email_alert.html").render(
            hits=hits, base_url=settings.app_base_url
        )
        send_mail(mail_recipient(session), subject, text, html_body)

    for article in pending:
        article.alerted = True
    session.commit()
    return len(hits)
