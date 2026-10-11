"""初回起動時に登録するカテゴリとおすすめフィード。

ジャンルは後から画面で自由に追加・変更できる。URL が変わったサイトは
「フィード管理」画面でエラーが表示されるので、そこで修正・削除する。
"""

from urllib.parse import quote


def google_news(query: str) -> str:
    """Google ニュースの検索結果 RSS。公式 RSS がない分野の補完に使う。"""
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja"


CATEGORIES = [
    "経済",
    "ビジネス",
    "ベンチャー・スタートアップ",
    "テック",
    "AI",
    "株・マーケット",
    "仮想通貨",
]

# (カテゴリ, タイトル, フィード URL)
FEEDS = [
    ("経済", "NHK ニュース 経済", "https://www3.nhk.or.jp/rss/news/cat5.xml"),
    ("経済", "東洋経済オンライン", "https://toyokeizai.net/list/feed/rss"),
    ("ビジネス", "ダイヤモンド・オンライン", "https://diamond.jp/list/feed/rss/dol"),
    ("ビジネス", "PRESIDENT Online", "https://president.jp/list/rss"),
    ("ベンチャー・スタートアップ", "BRIDGE", "https://thebridge.jp/feed"),
    (
        "ベンチャー・スタートアップ",
        "Google ニュース: スタートアップ 資金調達",
        google_news("スタートアップ 資金調達"),
    ),
    ("テック", "ITmedia NEWS", "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"),
    ("テック", "GIGAZINE", "https://gigazine.net/news/rss_2.0/"),
    ("テック", "Publickey", "https://www.publickey1.jp/atom.xml"),
    ("テック", "はてなブックマーク テクノロジー", "https://b.hatena.ne.jp/hotentry/it.rss"),
    ("AI", "ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"),
    ("AI", "Google ニュース: 生成AI", google_news("生成AI")),
    ("株・マーケット", "Google ニュース: 日経平均 株式市場", google_news("日経平均 株式市場")),
    ("株・マーケット", "Google ニュース: 米国株", google_news("米国株")),
    ("仮想通貨", "CoinPost", "https://coinpost.jp/?feed=rss2"),
    ("仮想通貨", "あたらしい経済", "https://www.neweconomy.jp/feed"),
    ("仮想通貨", "CoinDesk JAPAN", "https://www.coindeskjapan.com/feed/"),
]
