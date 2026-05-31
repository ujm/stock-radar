import sqlite_utils
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "stock_radar.db"


def get_db() -> sqlite_utils.Database:
    return sqlite_utils.Database(DB_PATH)


def init_db():
    db = get_db()

    if "articles" not in db.table_names():
        db["articles"].create({
            "id": int,
            "source": str,
            "url": str,
            "title": str,
            "body": str,
            "collected_at": str,
            "lang": str,
        }, pk="id", not_null={"url", "collected_at"})
        db["articles"].create_index(["url"], unique=True)

    if "signals" not in db.table_names():
        db["signals"].create({
            "id": int,
            "article_id": int,
            "ticker": str,
            "market": str,
            "direction": str,
            "score": float,
            "reason": str,
            "analyzed_at": str,
        }, pk="id", foreign_keys=[("article_id", "articles", "id")])
        db["signals"].create_index(["ticker"])
        db["signals"].create_index(["analyzed_at"])
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_article_ticker "
            "ON signals(article_id, ticker)"
        )

    return db


def save_article(db: sqlite_utils.Database, source: str, url: str, title: str, body: str, lang: str) -> int | None:
    collected_at = datetime.now(timezone.utc).isoformat()
    try:
        db["articles"].insert({
            "source": source,
            "url": url,
            "title": title,
            "body": body,
            "collected_at": collected_at,
            "lang": lang,
        }, ignore=True)
        row = db.execute("SELECT id FROM articles WHERE url = ?", [url]).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def save_signal(db: sqlite_utils.Database, article_id: int, ticker: str, market: str,
                direction: str, score: float, reason: str):
    analyzed_at = datetime.now(timezone.utc).isoformat()
    db["signals"].insert({
        "article_id": article_id,
        "ticker": ticker,
        "market": market,
        "direction": direction,
        "score": score,
        "reason": reason,
        "analyzed_at": analyzed_at,
    }, ignore=True)


def get_latest_signals(limit: int = 10, ticker: str | None = None, sector: str | None = None):
    db = get_db()
    query = """
        SELECT s.ticker, s.market, s.direction, s.score, s.reason, s.analyzed_at,
               a.title, a.source, a.url
        FROM signals s
        JOIN articles a ON s.article_id = a.id
    """
    params = []
    conditions = []

    if ticker:
        conditions.append("s.ticker = ?")
        params.append(ticker)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY s.analyzed_at DESC, s.score DESC LIMIT ?"
    params.append(limit)

    return db.execute(query, params).fetchall()
