# stock-radar 開発指示書

## プロジェクト概要

ニュース・SNSを自動巡回し、株価上昇に影響しそな銘柄をユーザーに通知する
**投資判断パートナーツール**。

- ブラウザ操作: Vibium MCP（API未提供サイトも対応）
- エージェント制御: Claude API（claude-sonnet-4-20250514）
- スケジューリング: Prefect
- インターフェース: まずCLI、後にWeb UI追加

---

## ワークディレクトリ

```
/Users/yoshiakigoto/work/stock-radar/
```

---

## 最終的なディレクトリ構成

```
stock-radar/
├── .env                        # APIキー等（コミット禁止）
├── .gitignore
├── requirements.txt
├── CLAUDE.md                   # この指示書
│
├── config/
│   ├── sources.yaml            # 巡回サイト設定
│   └── watchlist.yaml          # 監視セクター・ティッカー
│
├── agents/
│   ├── __init__.py
│   ├── collector.py            # 収集エージェント（Vibium MCP呼び出し）
│   └── analyzer.py             # 分析エージェント（ティッカー抽出・スコアリング）
│
├── flows/
│   ├── __init__.py
│   └── pipeline.py             # Prefect フロー定義
│
├── db/
│   ├── __init__.py
│   ├── models.py               # SQLite スキーマ定義
│   └── stock_radar.db          # SQLiteデータベース（自動生成）
│
├── cli/
│   ├── __init__.py
│   └── main.py                 # CLIエントリーポイント（Rich使用）
│
└── screenshots/                # Vibiumスクリーンショット保存先
```

---

## 環境情報

| 項目 | バージョン |
|------|-----------|
| Python | 3.14.3（`python3` コマンド） |
| Node.js | v24.9.0 |
| Vibium MCP | v26.3.18（インストール済み） |
| 仮想環境 | `.venv/`（作成済み） |

### インストール済みパッケージ

```
anthropic
prefect
pyyaml
rich
sqlite-utils
python-dotenv
```

---

## Step 1: `requirements.txt` 作成

```
anthropic>=0.40.0
prefect>=3.0.0
pyyaml>=6.0
rich>=13.0
sqlite-utils>=3.35
python-dotenv>=1.0
```

---

## Step 2: `db/models.py` — SQLiteスキーマ

以下のテーブルを定義すること。

```python
# articles テーブル
# - id: INTEGER PRIMARY KEY
# - source: TEXT          # サイト名（例: 日経電子版）
# - url: TEXT UNIQUE
# - title: TEXT
# - body: TEXT
# - collected_at: TEXT    # ISO8601
# - lang: TEXT            # ja / en

# signals テーブル
# - id: INTEGER PRIMARY KEY
# - article_id: INTEGER   # articles.id への外部キー
# - ticker: TEXT          # 例: 7203, NVDA
# - market: TEXT          # JP / US
# - direction: TEXT       # UP / DOWN / NEUTRAL
# - score: REAL           # 0.0〜1.0
# - reason: TEXT          # Claudeによる根拠
# - analyzed_at: TEXT     # ISO8601
```

---

## Step 3: `agents/collector.py` — 収集エージェント

### 役割

Vibium MCP を subprocess 経由で起動し、
Claude API に MCP ツールとして渡して自律的にブラウザを操作させる。

### 実装方針

```python
import anthropic
import subprocess
import json
from pathlib import Path

# Vibium MCP サーバーを subprocess で起動
# claude-sonnet-4-20250514 に以下のシステムプロンプトを渡す

COLLECTOR_SYSTEM_PROMPT = """
あなたはニュース収集エージェントです。
指定されたURLにアクセスし、以下の情報を収集してください：
- 記事タイトル
- 記事本文（要約で可）
- 記事URL

ページが動的コンテンツを含む場合はスクロールして追加記事を読み込んでください。
ペイウォールで本文が読めない場合は、タイトルとリード文のみ収集してください。
収集した記事は必ずJSON形式で返してください。

返却フォーマット:
{
  "articles": [
    {
      "title": "記事タイトル",
      "body": "記事本文または要約",
      "url": "記事のURL"
    }
  ]
}
"""
```

### Vibium MCP の呼び出し方

```python
# anthropic SDK の MCP サポートを使う
# 参考: https://docs.anthropic.com/en/docs/agents-and-tools/mcp

client = anthropic.Anthropic()

# Vibium を MCP サーバーとして登録
# コマンド: npx -y vibium mcp --screenshot-dir ./screenshots

response = client.beta.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    system=COLLECTOR_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": f"このURLのニュースを収集してください: {url}"}],
    mcp_servers=[
        {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "vibium", "mcp", "--screenshot-dir", "./screenshots"],
        }
    ],
    betas=["mcp-client-2025-04-04"],
)
```

### 返却値

```python
# CollectorResult dataclass
@dataclass
class CollectorResult:
    source: str
    url: str
    articles: list[dict]  # title, body, url
    collected_at: str      # ISO8601
    success: bool
    error: str | None
```

---

## Step 4: `agents/analyzer.py` — 分析エージェント

### 役割

収集した記事テキストを受け取り、
株価に影響しそうなティッカーと方向性・スコアを返す。

### システムプロンプト

```python
ANALYZER_SYSTEM_PROMPT = """
あなたは株式市場アナリストです。
与えられたニュース記事を分析し、株価に影響を与えそうな銘柄を特定してください。

分析観点:
- 企業の業績・決算情報
- 規制・政策変更
- M&A・提携情報
- 災害・事故・不祥事
- マクロ経済指標

必ずJSON形式のみで返答してください。前置きや説明文は不要です。

返却フォーマット:
{
  "signals": [
    {
      "ticker": "7203",
      "market": "JP",
      "company": "トヨタ自動車",
      "direction": "UP",
      "score": 0.85,
      "reason": "北米販売台数が前年比15%増と発表。EV移行も順調"
    }
  ]
}

direction は UP / DOWN / NEUTRAL のいずれか。
score は 0.0〜1.0（1.0が最も影響大）。
関連銘柄がない場合は signals を空配列で返すこと。
"""
```

### watchlist.yaml との照合

- `watchlist.yaml` に登録されたセクター・ティッカーを優先してスコアを上げる
- 未登録銘柄でもスコア0.7以上なら結果に含める

---

## Step 5: `flows/pipeline.py` — Prefect フロー

```python
from prefect import flow, task
from prefect.schedules import IntervalSchedule
from datetime import timedelta

@task
def collect_from_source(source: dict) -> CollectorResult:
    # collector.py を呼び出す
    pass

@task
def analyze_articles(result: CollectorResult) -> list[Signal]:
    # analyzer.py を呼び出す
    pass

@task
def save_to_db(articles, signals):
    # db/models.py を使って保存
    pass

@flow(
    name="stock-radar-pipeline",
    schedule=IntervalSchedule(interval=timedelta(hours=2))  # 2時間ごと
)
def stock_radar_flow():
    sources = load_enabled_sources()  # config/sources.yaml から読み込み
    for source in sources:
        result = collect_from_source(source)
        signals = analyze_articles(result)
        save_to_db(result.articles, signals)
```

---

## Step 6: `cli/main.py` — CLIエントリーポイント

Rich ライブラリを使って見やすく表示すること。

### 実装すべきコマンド

```bash
# 最新シグナルを表示（デフォルト: 上位10件）
python3 -m cli.main show

# 特定ティッカーのシグナルを表示
python3 -m cli.main show --ticker 7203

# 特定セクターのシグナルを表示
python3 -m cli.main show --sector 半導体

# 今すぐ収集・分析を実行（手動トリガー）
python3 -m cli.main run

# 特定サイトだけ実行
python3 -m cli.main run --source 日経電子版

# watchlist に銘柄を追加
python3 -m cli.main watch add --ticker 6367 --name ダイキン工業

# Prefect スケジューラを起動
python3 -m cli.main start
```

### 出力イメージ（Rich テーブル）

```
╭─────────────────────────────────────────────╮
│  📈 Stock Radar — 最新シグナル              │
│  2026-05-30 14:00 JST                       │
╰─────────────────────────────────────────────╯

 ティッカー  企業名          方向  スコア  ソース
 ─────────────────────────────────────────────
 7203       トヨタ自動車    ↑UP   0.85   日経電子版
 NVDA       NVIDIA         ↑UP   0.91   Reuters
 8306       三菱UFJ        ↓DOWN 0.72   Bloomberg
```

---

## 実装の優先順位

1. `db/models.py` — DBスキーマ（基盤）
2. `agents/collector.py` — 収集エージェント（日経1サイトで動作確認）
3. `agents/analyzer.py` — 分析エージェント
4. `cli/main.py` の `run` と `show` コマンド（最小動作確認）
5. `flows/pipeline.py` — Prefect フロー化
6. 残りのCLIコマンド

---

## 重要な制約・注意事項

### セキュリティ
- `.env` は絶対にコミットしない
- APIキーはすべて `python-dotenv` 経由で読み込む

### Vibium MCP について
- `npx -y vibium mcp` で起動するMCPサーバー
- anthropic SDK の `mcp_servers` パラメータ + `betas=["mcp-client-2025-04-04"]` で使用
- スクリーンショット保存先: `./screenshots/`
- ペイウォールサイトはタイトル・リード文のみ取得（本文取得を強制しない）

### Python コマンド
- `python` ではなく `python3` を使うこと（`python` コマンドは存在しない）
- 仮想環境: `source .venv/bin/activate` してから実行

### エラーハンドリング
- 各サイトの収集失敗は握りつぶさずログに残す
- 1サイト失敗しても他サイトの処理は継続する
- DB保存はトランザクションで保護する

### レート制限
- 同一サイトへの連続アクセスは避ける（サイト間に3秒以上のsleep）
- Claude API の呼び出し間隔も適切に制御する

---

## 動作確認コマンド（開発中の確認用）

```bash
cd /Users/yoshiakigoto/work/stock-radar
source .venv/bin/activate

# Vibium MCP の単体動作確認
npx -y vibium mcp

# DB初期化確認
python3 -c "from db.models import init_db; init_db(); print('DB OK')"

# 収集エージェント単体テスト（日経1件）
python3 -c "
from agents.collector import collect
result = collect('https://www.nikkei.com/markets/')
print(result)
"

# CLI 動作確認
python3 -m cli.main show
```

---

## 将来フェーズ（今は実装不要）

- Web UI（Next.js + FastAPI）
- プッシュ通知（LINE / Slack）
- 米国株ソース追加（Reuters Global, CNBC等）
- X（Twitter）SNS巡回
- バックテスト機能
