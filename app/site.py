"""どこからでも見られる閲覧サイト（静的サイト）を作る。

記事データは合言葉（SITE_PASSWORD）で暗号化し、ブラウザ側で復号して表示する。
GitHub Pages のような誰でもアクセスできる場所に置いても、合言葉を知らなければ
記事・購読先・IR のウォッチリストは読めない。

暗号方式: PBKDF2-HMAC-SHA256（60 万回）で鍵を作り、AES-256-GCM で暗号化。
ブラウザ標準の Web Crypto API だけで復号できる。
"""

import base64
import gzip
import hashlib
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .db import Article, Category, Feed, get_setting, set_setting, utcnow

SITE_SRC = Path(__file__).resolve().parent.parent / "site"
KDF_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 16
MAX_ARTICLES = 3000
SALT_KEY = "site_salt"


class SitePasswordError(ValueError):
    pass


def article_id(article: Article) -> str:
    return hashlib.sha256(f"{article.feed_id}:{article.guid}".encode()).hexdigest()[:16]


def build_payload(session, days: int) -> dict:
    cutoff = utcnow() - timedelta(days=days)
    articles = list(
        session.scalars(
            select(Article)
            .options(joinedload(Article.feed).joinedload(Feed.category))
            .where(Article.published_at >= cutoff)
            .order_by(Article.published_at.desc())
            .limit(MAX_ARTICLES)
        ).unique()
    )
    categories = [c.name for c in session.scalars(select(Category).order_by(Category.position, Category.id))]
    return {
        "generated_at": utcnow().isoformat() + "Z",
        "categories": categories,
        "articles": [
            {
                "id": article_id(a),
                "t": a.title,
                "l": a.link,
                "s": a.summary,
                "p": a.published_at.isoformat() + "Z",
                "f": a.feed.title,
                "c": a.feed.category.name if a.feed.category else "未分類",
            }
            for a in articles
            if a.link.startswith(("http://", "https://"))
        ],
    }


def derive_key(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt_payload(payload: dict, password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> dict:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SitePasswordError(
            f"SITE_PASSWORD は {MIN_PASSWORD_LENGTH} 文字以上にしてください（推測されにくいランダムな文字列）。"
        )
    plain = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    iv = os.urandom(12)
    ct = AESGCM(derive_key(password, salt, iterations)).encrypt(iv, plain, None)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iterations, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt_payload(blob: dict, password: str) -> dict:
    """テスト・確認用（ブラウザの app.js と同じ処理）。"""
    salt = base64.b64decode(blob["salt"])
    key = derive_key(password, salt, blob["iter"])
    plain = AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None)
    return json.loads(gzip.decompress(plain))


def site_salt(session) -> bytes:
    """ソルトは一度作ったら使い続ける（ブラウザに鍵を記憶したとき、毎回入力し直さずに済むように）。"""
    value = get_setting(session, SALT_KEY)
    if not value:
        value = base64.b64encode(os.urandom(16)).decode()
        set_setting(session, SALT_KEY, value)
        session.commit()
    return base64.b64decode(value)


def build_site(session, out_dir: Path, password: str, days: int = 7) -> int:
    """out_dir に閲覧サイト一式を書き出す。載せた記事数を返す。"""
    payload = build_payload(session, days)
    blob = encrypt_payload(payload, password, site_salt(session))
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(SITE_SRC, out_dir)
    (out_dir / "data.json").write_text(json.dumps(blob), encoding="utf-8")
    return len(payload["articles"])
