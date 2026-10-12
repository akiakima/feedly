"""OPML（フィード一覧の標準フォーマット）の入出力。feedly などとの移行用。"""

from xml.sax.saxutils import quoteattr

from defusedxml import ElementTree as ET

from .db import Category, Feed


def export_opml(categories: list[Category], uncategorized: list[Feed]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head><title>ニュースまとめ フィード一覧</title></head>",
        "  <body>",
    ]

    def outline(feed: Feed, indent: str) -> str:
        return (
            f'{indent}<outline type="rss" text={quoteattr(feed.title)} title={quoteattr(feed.title)} '
            f"xmlUrl={quoteattr(feed.url)} htmlUrl={quoteattr(feed.site_url or '')}/>"
        )

    for cat in categories:
        lines.append(f"    <outline text={quoteattr(cat.name)} title={quoteattr(cat.name)}>")
        lines.extend(outline(f, "      ") for f in cat.feeds)
        lines.append("    </outline>")
    lines.extend(outline(f, "    ") for f in uncategorized)
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


def parse_opml(content: bytes | str) -> list[tuple[str | None, str, str, str]]:
    """(カテゴリ名, タイトル, フィード URL, サイト URL) の一覧を返す。"""
    root = ET.fromstring(content)
    body = root.find("body")
    if body is None:
        raise ValueError("OPML の body が見つかりません。")
    results = []

    def walk(node, category):
        for child in node.findall("outline"):
            url = child.get("xmlUrl")
            label = child.get("title") or child.get("text") or ""
            if url:
                results.append((category, label or url, url, child.get("htmlUrl") or ""))
            else:
                walk(child, label or category)

    walk(body, None)
    return results
