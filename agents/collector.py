import anthropic
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

load_dotenv()

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

MAX_RETRIES = 2
RETRY_WAIT = 5


def build_collector_prompt(source: dict) -> str:
    lang_instruction = (
        "記事タイトルと本文は日本語のまま収集すること。"
        if source.get("language") == "ja"
        else "Collect article titles and body text in English as-is."
    )
    return f"""
あなたはニュース収集エージェントです。
指定されたURLにアクセスし、マーケット・株式関連のニュース記事を収集してください。

{lang_instruction}

収集する情報:
- 記事タイトル
- 記事本文（要約可）
- 記事URL

ルール:
- ページが動的コンテンツを含む場合はスクロールして追加記事を読み込む
- ペイウォールで本文が読めない場合はタイトルとリード文のみ収集
- 広告・ナビゲーション・フッターのテキストは除外
- 最大15件まで収集

必ずJSON形式のみで返答すること:
{{
  "articles": [
    {{
      "title": "記事タイトル",
      "body": "記事本文または要約",
      "url": "記事のURL"
    }}
  ]
}}
"""


@dataclass
class CollectorResult:
    source: str
    url: str
    articles: list[dict] = field(default_factory=list)
    collected_at: str = ""
    success: bool = False
    error: str | None = None

    def __post_init__(self):
        if not self.collected_at:
            self.collected_at = datetime.now(timezone.utc).isoformat()


def _extract_json(response: anthropic.types.Message) -> dict:
    for block in response.content:
        if block.type == "text":
            text = block.text.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
    return {"articles": []}


async def _collect_once_async(source: dict) -> anthropic.types.Message:
    client = anthropic.AsyncAnthropic()
    system_prompt = build_collector_prompt(source)
    url = source["url"]

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "vibium", "mcp", "--screenshot-dir", str(SCREENSHOTS_DIR)],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = [async_mcp_tool(t, session) for t in tools_result.tools]

            last_message = None
            runner = client.beta.messages.tool_runner(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": f"このURLのニュースを収集してください: {url}"}],
                tools=tools,
            )
            async for message in runner:
                last_message = message

            return last_message


async def _collect_with_retry_async(source: dict) -> "CollectorResult":
    result = CollectorResult(source=source["name"], url=source["url"])

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await _collect_once_async(source)
            if response is None:
                raise RuntimeError("tool_runner からレスポンスが返りませんでした")

            data = _extract_json(response)
            articles = data.get("articles", [])

            if articles:
                result.articles = articles
                result.success = True
                logger.info(f"収集完了: {len(articles)} 件 ({source['url']})")
                return result

            logger.warning(f"記事0件 attempt={attempt + 1} ({source['url']})")

        except* Exception as eg:
            errors = [str(e) for e in eg.exceptions]
            result.error = "; ".join(errors)
            logger.error(f"収集エラー attempt={attempt + 1} ({source['url']}): {result.error}")

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_WAIT)

    return result


def collect(source: dict) -> CollectorResult:
    logger.info(f"収集開始: {source['name']} ({source['url']})")
    return asyncio.run(_collect_with_retry_async(source))
