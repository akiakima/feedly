import os
import sys
import tempfile
from pathlib import Path

import pytest

# app を import する前にテスト用の設定にする
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["SEED_PRESETS"] = "false"
os.environ["SMTP_USER"] = "sender@example.com"
os.environ["SMTP_PASSWORD"] = "dummy"
os.environ["MAIL_TO"] = "me@example.com"
os.environ["APP_USERNAME"] = ""
os.environ["APP_PASSWORD"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, SessionLocal, engine  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def rss_bytes():
    return (FIXTURES / "sample_rss.xml").read_bytes()


@pytest.fixture
def atom_bytes():
    return (FIXTURES / "sample_atom.xml").read_bytes()


@pytest.fixture
def sent_mails(monkeypatch):
    """実際には送らず、送信内容を記録する。"""
    sent = []

    def fake_send(to, subject, text, html_body=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html_body})

    import app.main as main
    import app.notifier as notifier

    monkeypatch.setattr(notifier, "send_mail", fake_send)
    monkeypatch.setattr(main, "send_mail", fake_send)
    return sent
