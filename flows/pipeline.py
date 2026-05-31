import logging
import time
from pathlib import Path

import yaml
from prefect import flow, task
from prefect.schedules import Interval
from datetime import timedelta

from agents.collector import collect, CollectorResult
from agents.analyzer import analyze, Signal
from db.models import init_db, save_article, save_signal

logger = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"


def load_enabled_sources() -> list[dict]:
    with open(SOURCES_PATH) as f:
        data = yaml.safe_load(f)
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


@task(retries=1, retry_delay_seconds=10)
def collect_from_source(source: dict) -> CollectorResult:
    return collect(source)


@task
def analyze_articles(result: CollectorResult) -> list[Signal]:
    if not result.success or not result.articles:
        return []
    return analyze(result.articles)


@task
def save_to_db(result: CollectorResult, signals: list[Signal]):
    db = init_db()
    lang = "ja"

    with db.conn:
        for article in result.articles:
            article_id = save_article(
                db=db,
                source=result.source,
                url=article.get("url", result.url),
                title=article.get("title", ""),
                body=article.get("body", ""),
                lang=lang,
            )
            if article_id is None:
                continue

            for sig in signals:
                if sig.article_url == article.get("url", ""):
                    save_signal(
                        db=db,
                        article_id=article_id,
                        ticker=sig.ticker,
                        market=sig.market,
                        direction=sig.direction,
                        score=sig.score,
                        reason=sig.reason,
                    )


@flow(name="stock-radar-pipeline")
def stock_radar_flow():
    sources = load_enabled_sources()
    for i, source in enumerate(sources):
        if i > 0:
            time.sleep(3)
        result = collect_from_source(source)
        signals = analyze_articles(result)
        save_to_db(result, signals)
        if not result.success:
            logger.warning(f"収集失敗: {source['name']} — {result.error}")


def run_once(source_name: str | None = None):
    sources = load_enabled_sources()
    if source_name:
        sources = [s for s in sources if s["name"] == source_name]
        if not sources:
            raise ValueError(f"ソースが見つかりません: {source_name}")

    db = init_db()
    all_signals = []

    for i, source in enumerate(sources):
        if i > 0:
            time.sleep(3)
        result = collect(source)
        if not result.success:
            logger.warning(f"収集失敗: {source['name']} — {result.error}")
            continue

        signals = analyze(result.articles)
        lang = source.get("language", "ja")
        with db.conn:
            for article in result.articles:
                article_id = save_article(
                    db=db,
                    source=result.source,
                    url=article.get("url", result.url),
                    title=article.get("title", ""),
                    body=article.get("body", ""),
                    lang=lang,
                )
                if article_id is None:
                    continue
                for sig in signals:
                    if sig.article_url == article.get("url", ""):
                        save_signal(
                            db=db,
                            article_id=article_id,
                            ticker=sig.ticker,
                            market=sig.market,
                            direction=sig.direction,
                            score=sig.score,
                            reason=sig.reason,
                        )
        all_signals.extend(signals)

    return all_signals
