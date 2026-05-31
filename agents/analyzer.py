import anthropic
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlist.yaml"

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


@dataclass
class Signal:
    ticker: str
    market: str
    company: str
    direction: str
    score: float
    reason: str
    article_url: str = ""
    analyzed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _load_watchlist() -> dict:
    if not WATCHLIST_PATH.exists():
        return {"tickers": [], "us_tickers": [], "sectors": []}
    with open(WATCHLIST_PATH) as f:
        return yaml.safe_load(f) or {}


def _build_watchlist_context(watchlist: dict) -> str:
    tickers = [t["code"] for t in watchlist.get("tickers", [])]
    us_tickers = [t["code"] for t in watchlist.get("us_tickers", [])]
    sectors = watchlist.get("sectors", [])
    all_tickers = tickers + us_tickers
    if not all_tickers and not sectors:
        return ""
    return (
        f"\n\n監視リスト（これらの銘柄・セクターが含まれる場合はスコアを0.1加算してください）:\n"
        f"ティッカー: {', '.join(all_tickers)}\n"
        f"セクター: {', '.join(sectors)}"
    )


def _extract_signals(response: anthropic.types.Message) -> list[dict]:
    for block in response.content:
        if block.type == "text":
            text = block.text.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                return data.get("signals", [])
    return []


def analyze(articles: list[dict]) -> list[Signal]:
    if not articles:
        return []

    watchlist = _load_watchlist()
    watchlist_context = _build_watchlist_context(watchlist)
    client = anthropic.Anthropic()
    signals: list[Signal] = []

    for article in articles:
        title = article.get("title", "")
        body = article.get("body", "")
        url = article.get("url", "")
        if not title and not body:
            continue

        content = f"タイトル: {title}\n本文: {body}"
        system = ANALYZER_SYSTEM_PROMPT + watchlist_context

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": content}],
            )

            raw_signals = _extract_signals(response)
            for s in raw_signals:
                score = float(s.get("score", 0.0))
                ticker = s.get("ticker", "")
                watchlist_tickers = [t["code"] for t in watchlist.get("tickers", [])] + \
                                    [t["code"] for t in watchlist.get("us_tickers", [])]
                if ticker not in watchlist_tickers and score < 0.7:
                    continue

                signals.append(Signal(
                    ticker=ticker,
                    market=s.get("market", ""),
                    company=s.get("company", ""),
                    direction=s.get("direction", "NEUTRAL"),
                    score=min(score, 1.0),
                    reason=s.get("reason", ""),
                    article_url=url,
                ))

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析エラー ({url}): {e}")
        except Exception as e:
            logger.error(f"分析エラー ({url}): {e}")

    return signals
