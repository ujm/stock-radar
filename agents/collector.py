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


async def _collect_async(url: str) -> anthropic.types.Message:
    client = anthropic.AsyncAnthropic()
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
                system=COLLECTOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"このURLのニュースを収集してください: {url}"}],
                tools=tools,
            )
            async for message in runner:
                last_message = message

            return last_message


def collect(url: str, source_name: str = "") -> CollectorResult:
    result = CollectorResult(source=source_name, url=url)

    try:
        logger.info(f"収集開始: {url}")
        response = asyncio.run(_collect_async(url))
        if response is None:
            raise RuntimeError("tool_runner からレスポンスが返りませんでした")
        data = _extract_json(response)
        result.articles = data.get("articles", [])
        result.success = True
        logger.info(f"収集完了: {len(result.articles)} 件 ({url})")

    except* Exception as eg:
        errors = [str(e) for e in eg.exceptions]
        result.error = "; ".join(errors)
        logger.error(f"収集失敗 ({url}): {result.error}")

    return result
