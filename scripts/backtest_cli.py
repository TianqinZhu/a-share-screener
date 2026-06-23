from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import DEFAULT_UNIVERSE, get_daily_history, normalize_symbol, score_rejuvenation


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_UNIVERSE)
    return [normalize_symbol(part.strip()) for part in value.split(",") if part.strip()]


def load_histories(symbols: list[str], days: int) -> tuple[list[dict[str, Any]], int]:
    histories: list[dict[str, Any]] = []
    errors = 0
    for symbol in symbols:
        try:
            daily = get_daily_history(symbol, days)
            histories.append(
                {
                    "symbol": normalize_symbol(symbol),
                    "name": daily.get("name") or normalize_symbol(symbol),
                    "points": daily.get("points") or [],
                }
            )
        except Exception:
            errors += 1
    return histories, errors


def build_backtest_row(history: dict[str, Any], entry_index: int, hold_days: int) -> dict[str, Any]:
    points = history["points"]
    signal = score_rejuvenation(points[: entry_index + 1])
    entry = points[entry_index]
    exit_point = points[entry_index + hold_days]
    after_points = points[entry_index + 1 : entry_index + hold_days + 1] or [exit_point]
    entry_price = float(entry["close"])
    exit_price = float(exit_point["close"])
    max_high = max(float(point["high"]) for point in after_points)
    min_low = min(float(point["low"]) for point in after_points)
    return {
        "symbol": history["symbol"],
        "name": history.get("name") or history["symbol"],
        "entry_date": entry["time"],
        "exit_date": exit_point["time"],
        "entry_price": round(entry_price, 3),
        "exit_price": round(exit_price, 3),
        "return_pct": round((exit_price - entry_price) / entry_price * 100, 2) if entry_price else 0,
        "max_gain_pct": round((max_high - entry_price) / entry_price * 100, 2) if entry_price else 0,
        "max_drawdown_pct": round(min(0, (min_low - entry_price) / entry_price * 100), 2) if entry_price else 0,
        "score": signal.get("score", 0),
        "status": signal.get("status", ""),
        "reason": signal.get("reason", ""),
    }


def summarize_rows(rows: list[dict[str, Any]], entry_days: int) -> dict[str, Any]:
    returns = [float(row["return_pct"]) for row in rows]
    drawdowns = [float(row["max_drawdown_pct"]) for row in rows]
    wins = [value for value in returns if value > 0]
    return {
        "sample_count": len(rows),
        "entry_days": entry_days,
        "avg_daily_picks": round(len(rows) / entry_days, 2) if entry_days else 0,
        "avg_return_pct": round(average(returns), 2),
        "median_return_pct": round(median(returns), 2),
        "win_rate_pct": round(len(wins) / len(returns) * 100, 2) if returns else 0,
        "best_return_pct": round(max(returns), 2) if returns else 0,
        "worst_return_pct": round(min(returns), 2) if returns else 0,
        "avg_max_drawdown_pct": round(average(drawdowns), 2),
    }


def run_rolling_backtest(
    histories: list[dict[str, Any]],
    top: int = 10,
    hold_days: int = 30,
    window_days: int = 120,
) -> dict[str, Any]:
    entry_dates: set[str] = set()
    for history in histories:
        points = history.get("points") or []
        last_entry = len(points) - hold_days - 1
        first_entry = max(79, last_entry - window_days + 1)
        if last_entry < first_entry:
            continue
        for entry_index in range(first_entry, last_entry + 1):
            entry_dates.add(points[entry_index]["time"])

    rows: list[dict[str, Any]] = []
    latest_picks: list[dict[str, Any]] = []
    ordered_dates = sorted(entry_dates)
    for entry_date in ordered_dates:
        candidates = []
        for history in histories:
            points = history.get("points") or []
            entry_index = next((idx for idx, point in enumerate(points) if point["time"] == entry_date), -1)
            if entry_index < 79 or entry_index + hold_days >= len(points):
                continue
            candidates.append(build_backtest_row(history, entry_index, hold_days))
        ranked = sorted(
            candidates,
            key=lambda row: (
                row["status"] in {"buy_watch", "watch"},
                row["score"],
                row["return_pct"],
            ),
            reverse=True,
        )[:top]
        rows.extend(ranked)
        if entry_date == ordered_dates[-1]:
            latest_picks = ranked

    return {
        "summary": summarize_rows(rows, len(ordered_dates)),
        "rows": rows,
        "latest_picks": latest_picks,
        "meta": {
            "top": top,
            "hold_days": hold_days,
            "window_days": window_days,
            "entry_days": len(ordered_dates),
            "symbols": len(histories),
            "entry_rule": "entry day close",
            "exit_rule": f"{hold_days} trading days later close",
        },
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "entry_date",
        "exit_date",
        "symbol",
        "name",
        "score",
        "status",
        "entry_price",
        "exit_price",
        "return_pct",
        "max_gain_pct",
        "max_drawdown_pct",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run rolling 30-trading-day backtest outside the website.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols, for example sh600000,sz000001")
    parser.add_argument("--hold-days", type=int, default=30)
    parser.add_argument("--window-days", type=int, default=120)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--output-csv", default="", help="Optional CSV output path")
    args = parser.parse_args(argv)

    hold_days = max(5, min(args.hold_days, 90))
    window_days = max(20, min(args.window_days, 240))
    top = max(1, min(args.top, 50))
    symbols = parse_symbols(args.symbols)
    histories, errors = load_histories(symbols, max(180, hold_days + window_days + 90))
    result = run_rolling_backtest(histories, top=top, hold_days=hold_days, window_days=window_days)
    result["meta"]["requested_symbols"] = len(symbols)
    result["meta"]["load_errors"] = errors

    if args.output_csv:
        write_csv(Path(args.output_csv), result["rows"])

    print(json.dumps({"summary": result["summary"], "meta": result["meta"], "latest_picks": result["latest_picks"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
