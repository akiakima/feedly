# ニュースまとめ（RSS リーダー + メール通知）

feedly のように RSS / Atom フィードを集めて読む Web アプリです。新着記事はメールで受け取れます。

- **ジャンル別の記事一覧**：経済・ビジネス・ベンチャー・テック・AI・株・仮想通貨のおすすめフィードを最初から登録済み
- **未読管理と「あとで読む」**：記事を開くと既読になり、★を付けた記事は「あとで読む」に残る。キーワード検索もできる
- **フィード追加**：サイトの URL を入れるだけで RSS を自動で探す。ジャンルはいつでも追加・変更できる
- **メール通知**
  - 定時ダイジェスト：指定した時刻（初期値は 7:00 と 18:00）に、前回以降の新着をジャンル別にまとめて送る
  - キーワード通知：登録したキーワード（例：「ビットコイン」「上場」）を含む記事が出たらすぐ送る
- **OPML の入出力**：feedly など他の RSS リーダーとフィード一覧をやり取りできる
- スマホ表示とダークモードに対応

## 仕組み

```
[RSS フィード] --(30分ごと)--> 取得・重複除去 --> SQLite
                                          |--> キーワード一致 → すぐメール
                                          |--> 定時 → ダイジェストメール
                                     Web 画面（FastAPI）で閲覧・管理
```

Python（FastAPI + SQLAlchemy + feedparser + APScheduler）で作った 1 プロセスのアプリです。DB は SQLite のファイル 1 つだけです。

## はじめかた

### 1. Gmail の送信設定（無料）

1. Google アカウントで [2 段階認証](https://myaccount.google.com/security) を有効にする
2. [アプリパスワード](https://myaccount.google.com/apppasswords) を発行する（16 文字）
3. `.env.example` をコピーして `.env` を作り、`SMTP_USER`（Gmail アドレス）と `SMTP_PASSWORD`（アプリパスワード）、`MAIL_TO`（通知先）を入れる

Gmail 以外の SMTP（Outlook、さくらのメールなど）も、`SMTP_HOST` と `SMTP_PORT` を変えれば使えます。ポート 465 なら SSL、それ以外は STARTTLS で接続します。

### 2. 起動

**Python で起動する場合**（Python 3.11 以上）

```bash
python -m venv .venv
source .venv/bin/activate          # Windows は .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

**Docker で起動する場合**

```bash
docker compose up -d
```

ブラウザで http://localhost:8000 を開きます。起動するとすぐ 1 回目の取得が始まります。

### 3. 画面での設定

- **通知設定**：通知先アドレス、ダイジェストの時刻、キーワードを設定し、「テストメールを送る」で届くか確認する
- **フィード管理**：フィードの追加・削除・ジャンル変更を行う。URL が変わって取れなくなったフィードには「取得エラー」が表示される

> 初回の取得分は通知済みとして扱います（登録直後に数百件のメールが届かないようにするため）。メールで届くのは 2 回目以降の取得で見つかった新着です。

## ジャンル・サイトを追加するには

- 画面の「フィード管理」で、サイトの URL を入力して追加します。「または新しいジャンル」に名前を入れると、ジャンルも同時に作れます。
- 公式 RSS がない分野は、**Google ニュースの検索 RSS** を使うのが手軽です。
  `https://news.google.com/rss/search?q=キーワード&hl=ja&gl=JP&ceid=JP:ja`
- 初期登録するおすすめフィードを変えたいときは、`app/presets.py` を編集します。

## サーバーで常時動かす場合

メール通知はアプリが動いている間だけ送られます。PC を閉じても通知を受け取りたい場合は、常時起動のサーバーに置いてください。

- 自宅の常時起動 PC や Raspberry Pi で `docker compose up -d`
- 無料枠の VM（Oracle Cloud Always Free など）や安価な VPS で `docker compose up -d`

インターネットに公開するときは、`.env` の `APP_USERNAME` と `APP_PASSWORD` を必ず設定してください（Basic 認証がかかります）。`APP_BASE_URL` も公開した URL に変えると、メール内のリンクが正しくなります。

## 開発

```bash
pip install -r requirements-dev.txt
pytest
```

| ファイル | 役割 |
|---|---|
| `app/main.py` | Web 画面とルーティング |
| `app/fetcher.py` | フィードの取得・解析・保存と、RSS の自動検出 |
| `app/notifier.py` | ダイジェストメールとキーワード通知 |
| `app/scheduler.py` | 定期実行（取得・通知・古い記事の削除） |
| `app/presets.py` | 初期登録するジャンルとおすすめフィード |
| `app/db.py` | DB のモデル |
| `app/templates/` | 画面とメールのテンプレート |

## 注意

- RSS で取得するのはタイトル・概要・リンクだけです。本文は元のサイトで読みます。
- おすすめフィードの URL は、サイトの都合で変わることがあります。取得エラーになったフィードは「フィード管理」で修正するか削除してください。
