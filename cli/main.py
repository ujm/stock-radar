import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
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
    )

    if not rows:
        console.print("[yellow]シグナルがありません。まず `run` コマンドで収集・分析を実行してください。[/yellow]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("ティッカー", style="bold")
    table.add_column("企業名 / 根拠", max_width=50)
    table.add_column("方向")
    table.add_column("スコア", justify="right")
    table.add_column("ソース")
    table.add_column("日時")

    for row in rows:
        ticker, market, direction, score, reason, analyzed_at, title, source = row
        analyzed_dt = analyzed_at[:16].replace("T", " ") if analyzed_at else ""
        table.add_row(
            f"{ticker} ({market})",
            reason[:48] + "…" if len(reason) > 48 else reason,
            _direction_label(direction),
            f"{score:.2f}",
            source,
            analyzed_dt,
        )

    console.print(table)


def cmd_run(args):
    from flows.pipeline import run_once
    source_name = getattr(args, "source", None)
    label = f"[bold]{source_name}[/bold]" if source_name else "全ソース"
    console.print(f"[cyan]収集・分析を開始します ({label})...[/cyan]")

    try:
        signals = run_once(source_name=source_name)
        if signals:
            console.print(f"[green]{len(signals)} 件のシグナルを検出・保存しました。[/green]")
        else:
            console.print("[yellow]シグナルは検出されませんでした。[/yellow]")
    except ValueError as e:
        console.print(f"[red]エラー: {e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]実行エラー: {e}[/red]")
        sys.exit(1)


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
    show_p.add_argument("--sector", default=None, help="特定セクターで絞り込み（未実装）")
    show_p.add_argument("--limit", type=int, default=10, help="表示件数")

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
