import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TaskProgressColumn
from rich.table import Table

from db.models import init_db, get_latest_signals

console = Console()

WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlist.yaml"


def _direction_label(direction: str) -> str:
    if direction == "UP":
        return "[bold green]↑UP[/bold green]"
    elif direction == "DOWN":
        return "[bold red]↓DOWN[/bold red]"
    return "[yellow]→NEUTRAL[/yellow]"


@dataclass
class _SourceSummary:
    name: str
    article_count: int = 0
    success: bool = False
    error: str | None = None


def _print_run_summary(summaries: list[_SourceSummary], signal_count: int):
    table = Table(show_header=True, header_style="bold magenta", show_footer=True, box=None)
    table.add_column("ソース", footer="シグナル検出数")
    table.add_column("記事数", justify="right", footer=str(signal_count))
    table.add_column("ステータス", footer="")

    for s in summaries:
        if not s.success and s.error:
            status = "[bold red]FAIL[/bold red]"
        elif s.article_count == 0:
            status = "[yellow]記事なし[/yellow]"
        else:
            status = "[bold green]成功[/bold green]"
        table.add_row(s.name, f"{s.article_count}件", status)

    console.print(Panel(table, title="収集完了サマリー", expand=False))


def cmd_show(args):
    init_db()
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M JST")
    console.print(Panel(
        f"[bold cyan]Stock Radar — 最新シグナル[/bold cyan]\n{now}",
        expand=False,
    ))

    rows = get_latest_signals(
        limit=args.limit,
        ticker=args.ticker,
        source=getattr(args, "source", None),
    )

    if not rows:
        console.print("[yellow]シグナルがありません。まず `run` コマンドで収集・分析を実行してください。[/yellow]")
        return

    if args.detail:
        for row in rows:
            ticker, market, direction, score, reason, analyzed_at, title, source, url = row
            analyzed_dt = analyzed_at[:16].replace("T", " ") if analyzed_at else ""
            body_lines = (
                f"[bold]{ticker}[/bold] ({market})  {_direction_label(direction)}  "
                f"スコア: [bold]{score:.2f}[/bold]  [{source}]  {analyzed_dt}\n\n"
                f"[cyan]根拠:[/cyan] {reason}\n\n"
                f"[cyan]記事:[/cyan] {title}\n"
                f"[dim]{url}[/dim]"
            )
            console.print(Panel(body_lines, expand=False))
    else:
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("ティッカー", style="bold")
        table.add_column("根拠", max_width=32)
        table.add_column("方向")
        table.add_column("スコア", justify="right")
        table.add_column("ソース")
        table.add_column("日時")

        for row in rows:
            ticker, market, direction, score, reason, analyzed_at, title, source, url = row
            analyzed_dt = analyzed_at[:16].replace("T", " ") if analyzed_at else ""
            short_reason = reason[:30] + "…" if len(reason) > 30 else reason
            table.add_row(
                f"{ticker} ({market})",
                short_reason,
                _direction_label(direction),
                f"{score:.2f}",
                source,
                analyzed_dt,
            )

        console.print(table)


def cmd_run(args):
    from flows.pipeline import load_enabled_sources
    from agents.collector import collect
    from agents.analyzer import analyze
    from db.models import save_article, save_signal

    source_name = getattr(args, "source", None)
    sources = load_enabled_sources()
    if source_name:
        sources = [s for s in sources if s["name"] == source_name]
        if not sources:
            console.print(f"[red]エラー: ソースが見つかりません: {source_name}[/red]")
            sys.exit(1)

    db = init_db()
    all_signals = []
    summaries: list[_SourceSummary] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        source_task = progress.add_task("収集中...", total=len(sources))

        for i, source in enumerate(sources):
            if i > 0:
                time.sleep(3)

            summary = _SourceSummary(name=source["name"])
            progress.update(source_task, description=f"収集中... [{source['name']}]")
            result = collect(source)
            progress.advance(source_task)

            if not result.success:
                summary.error = result.error
                summaries.append(summary)
                console.print(f"[red]収集失敗: {source['name']} — {result.error}[/red]")
                continue

            summary.success = True
            summary.article_count = len(result.articles)
            article_count = len(result.articles)
            analyze_task = progress.add_task(
                f"分析中... [記事 0/{article_count}]",
                total=article_count,
            )

            signals = []
            for j, article in enumerate(result.articles):
                progress.update(
                    analyze_task,
                    description=f"分析中... [記事 {j + 1}/{article_count}]",
                )
                partial = analyze([article])
                signals.extend(partial)
                progress.advance(analyze_task)

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
            summaries.append(summary)

    _print_run_summary(summaries, len(all_signals))


def cmd_watch_add(args):
    watchlist = {}
    if WATCHLIST_PATH.exists():
        with open(WATCHLIST_PATH) as f:
            watchlist = yaml.safe_load(f) or {}

    tickers = watchlist.setdefault("tickers", [])
    for t in tickers:
        if t.get("code") == args.ticker:
            console.print(f"[yellow]{args.ticker} は既に watchlist に登録されています。[/yellow]")
            return

    tickers.append({"code": args.ticker, "name": args.name})
    with open(WATCHLIST_PATH, "w") as f:
        yaml.dump(watchlist, f, allow_unicode=True, default_flow_style=False)

    console.print(f"[green]{args.ticker} ({args.name}) を watchlist に追加しました。[/green]")


def cmd_start(_args):
    from flows.pipeline import stock_radar_flow
    from prefect.schedules import Interval
    from datetime import timedelta
    console.print("[cyan]Prefect スケジューラを起動します (2時間ごと)...[/cyan]")
    stock_radar_flow.serve(
        name="stock-radar-pipeline",
        schedules=[Interval(timedelta(hours=2))],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-radar", description="Stock Radar CLI")
    sub = parser.add_subparsers(dest="command")

    # show
    show_p = sub.add_parser("show", help="最新シグナルを表示")
    show_p.add_argument("--ticker", default=None, help="特定ティッカーで絞り込み")
    show_p.add_argument("--source", default=None, help="特定ソースで絞り込み")
    show_p.add_argument("--sector", default=None, help="特定セクターで絞り込み（未実装）")
    show_p.add_argument("--limit", type=int, default=10, help="表示件数")
    show_p.add_argument("--detail", action="store_true", help="根拠・記事タイトル・URLをフル表示")

    # run
    run_p = sub.add_parser("run", help="今すぐ収集・分析を実行")
    run_p.add_argument("--source", default=None, help="特定ソース名で実行")

    # watch
    watch_p = sub.add_parser("watch", help="watchlist 管理")
    watch_sub = watch_p.add_subparsers(dest="watch_command")
    add_p = watch_sub.add_parser("add", help="銘柄を追加")
    add_p.add_argument("--ticker", required=True)
    add_p.add_argument("--name", required=True)

    # start
    sub.add_parser("start", help="Prefect スケジューラを起動")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "show":
        cmd_show(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "watch":
        if args.watch_command == "add":
            cmd_watch_add(args)
        else:
            parser.parse_args(["watch", "--help"])
    elif args.command == "start":
        cmd_start(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
