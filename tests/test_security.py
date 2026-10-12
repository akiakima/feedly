import socket

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.fetcher import UnsafeURLError, check_public_url
from app.opml import parse_opml


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _fake_resolve(ip):
    def _resolve(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    return _resolve


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"],
)
def test_private_addresses_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve(ip))
    with pytest.raises(UnsafeURLError):
        check_public_url("https://internal.example/feed")


def test_public_address_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve("93.184.216.34"))
    check_public_url("https://example.com/feed")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/feed", "javascript:alert(1)"])
def test_non_http_schemes_blocked(url):
    with pytest.raises(UnsafeURLError):
        check_public_url(url)


def test_security_headers(client):
    res = client.get("/")
    assert "script-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "no-referrer"


def test_cross_site_post_rejected(client):
    res = client.post("/categories", data={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    res = client.post("/categories", data={"name": "x"}, headers={"Referer": "https://evil.example/page"})
    assert res.status_code == 403


def test_same_site_post_allowed(client):
    res = client.post("/categories", data={"name": "x"}, headers={"Origin": "http://testserver"})
    assert res.status_code == 200


def test_opml_rejects_entity_expansion():
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<opml><body><outline text="&lol2;" xmlUrl="https://example.com/rss"/></body></opml>"""
    with pytest.raises(Exception):
        parse_opml(bomb)


def test_opml_import_skips_non_http(client):
    opml = b"""<opml><body>
      <outline text="ok" xmlUrl="https://example.com/rss"/>
      <outline text="bad" xmlUrl="file:///etc/passwd"/>
    </body></opml>"""
    res = client.post("/opml", files={"file": ("f.opml", opml, "text/xml")})
    assert "1 件のフィードを取り込みました" in res.text


def test_open_redirect_guard():
    assert main.safe_next("//evil.example") == "/"
    assert main.safe_next("https://evil.example") == "/"
    assert main.safe_next("/feeds") == "/feeds"
