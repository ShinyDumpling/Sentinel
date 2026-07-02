#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
个股池快速拉升监控器

核心监控数据：
1. 股票池短周期平均涨幅：1m / 3m / 5m
2. 股票池平均涨速：直接使用通达信实时字段 Zangsu
3. 股票池成交额突增：当前 60 秒成交额与前序 60 秒均值的比值
4. 股票池扩散度：上涨占比、站上分时均价占比
5. 同步转强数：短周期强势且站上均价的股票数
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sqlite3
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALPHASIFT_FORK_DIR = PROJECT_ROOT / "alphasift-fork"
if ALPHASIFT_FORK_DIR.exists():
    sys.path.insert(0, str(ALPHASIFT_FORK_DIR))

STOCK_DB_PATH = PROJECT_ROOT / "alphasift-fork" / "data" / "tdx_daily.db"
DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "logs" / "sector_surge_alerts.jsonl"
SNAPSHOT_FIELDS = ["Now", "LastClose", "Amount", "Average", "Zangsu"]

@dataclass
class QuotePoint:
    ts: float
    price: float
    last_close: float
    amount: float
    average: float | None
    zangsu: float | None


@dataclass
class BoardMetrics:
    sample_size: int
    change_pct: float
    ret_1m: float | None
    ret_3m: float | None
    ret_5m: float | None
    avg_zangsu: float | None
    up_ratio: float
    above_avg_ratio: float
    leaders_count: int
    amount_delta_60s: float | None
    amount_burst_ratio: float | None
    stock_rows: list[dict[str, Any]]
    active_signals: list[str]
    amount_delta_ready: bool
    amount_burst_ready: bool
    ret_1m_ready_count: int
    ret_3m_ready_count: int
    ret_5m_ready_count: int


def normalize_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        raw_code, suffix = text.split(".", 1)
        raw_code = raw_code.strip()
        suffix = suffix.strip()
        if raw_code.isdigit():
            raw_code = raw_code.zfill(6)
        return f"{raw_code}.{suffix}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return text
    if digits.startswith(("6", "5", "9")):
        suffix = "SH"
    elif digits.startswith(("4", "8")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{digits}.{suffix}"


def code_to_digits(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) == 6:
        return digits
    return str(code or "").strip().upper()


def safe_float(value: Any) -> float | None:
    if value in (None, "", "None", "null", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def median_or_none(values: Iterable[float | None]) -> float | None:
    cleaned = [item for item in values if isinstance(item, (int, float))]
    if not cleaned:
        return None
    return float(statistics.median(cleaned))


def mean_or_none(values: Iterable[float | None]) -> float | None:
    cleaned = [item for item in values if isinstance(item, (int, float))]
    if not cleaned:
        return None
    return float(sum(cleaned) / len(cleaned))


def pick_point_before(history: deque[QuotePoint], seconds_ago: int) -> QuotePoint | None:
    if not history:
        return None
    target_ts = history[-1].ts - seconds_ago
    candidate: QuotePoint | None = None
    for point in reversed(history):
        if point.ts <= target_ts:
            return point
        candidate = point
    return candidate if candidate and (history[-1].ts - candidate.ts) >= seconds_ago * 0.7 else None


def calc_return_pct(current_price: float, base_price: float | None) -> float | None:
    if base_price in (None, 0):
        return None
    return (current_price / base_price - 1.0) * 100.0


def window_amount_delta(history: deque[QuotePoint], seconds_ago: int) -> float | None:
    if not history:
        return None
    base = pick_point_before(history, seconds_ago)
    if base is None:
        return None
    delta = history[-1].amount - base.amount
    return delta if delta >= 0 else None


def load_tq():
    from alphasift.tdx_relation import _load_tdxquant  # type: ignore

    return _load_tdxquant()


def parse_csv_codes(raw: str | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    tokens = (
        str(raw or "")
        .replace("，", ",")
        .replace("；", ",")
        .replace(";", ",")
        .replace("\n", ",")
        .replace("\t", ",")
        .replace(" ", ",")
        .split(",")
    )
    for part in tokens:
        token = str(part or "").strip()
        if not token:
            continue
        parsed_codes: list[str] = []
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits and len(digits) % 6 == 0 and token == digits:
            parsed_codes = [digits[i:i + 6] for i in range(0, len(digits), 6)]
        else:
            code = code_to_digits(token)
            if code and code.isdigit() and len(code) == 6:
                parsed_codes = [code]
        for code in parsed_codes:
            if code not in seen:
                seen.add(code)
                result.append(code)
    return result


def load_stock_name_map() -> dict[str, str]:
    names: dict[str, str] = {}
    if not STOCK_DB_PATH.exists():
        return names
    conn = sqlite3.connect(str(STOCK_DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute("SELECT code, name FROM stocks")
        for code, name in cur.fetchall():
            code_text = code_to_digits(code)
            name_text = str(name or "").strip()
            if code_text and name_text:
                names[code_text] = name_text
    finally:
        conn.close()
    return names


def load_ma5_map(codes: list[str]) -> dict[str, float]:
    ma5_map: dict[str, float] = {}
    if not STOCK_DB_PATH.exists() or not codes:
        return ma5_map
    conn = sqlite3.connect(str(STOCK_DB_PATH))
    try:
        cur = conn.cursor()
        for code in codes:
            cur.execute(
                """
                SELECT close_price
                FROM daily_kline
                WHERE code = ?
                ORDER BY trade_date DESC
                LIMIT 5
                """,
                (code,),
            )
            rows = [safe_float(row[0]) for row in cur.fetchall()]
            closes = [value for value in rows if isinstance(value, (int, float))]
            if len(closes) == 5:
                ma5_map[code] = float(sum(closes) / 5.0)
    finally:
        conn.close()
    return ma5_map


def get_stock_name(
    code: str,
    *,
    stock_name_map: dict[str, str],
    tq: Any | None = None,
) -> str:
    code_text = code_to_digits(code)
    cached = stock_name_map.get(code_text)
    if cached:
        return cached
    if tq is not None:
        try:
            info = tq.get_stock_info(normalize_code(code_text), field_list=["Name"]) or {}
            name = str(info.get("Name") or "").strip()
            if name:
                stock_name_map[code_text] = name
                return name
        except Exception:
            pass
    return code_text


def format_stock_label(code: str, *, stock_name_map: dict[str, str], tq: Any | None = None) -> str:
    code_text = code_to_digits(code)
    name = get_stock_name(code_text, stock_name_map=stock_name_map, tq=tq)
    return f"{name}({code_text})" if name and name != code_text else code_text


def resolve_watchlist(manual_codes: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for code in manual_codes:
        if code not in seen:
            seen.add(code)
            merged.append(code)
    return merged


def fetch_snapshot(tq: Any, code: str) -> QuotePoint | None:
    stock_code = normalize_code(code)
    try:
        raw = tq.get_market_snapshot(stock_code=stock_code, field_list=SNAPSHOT_FIELDS)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] snapshot failed for {code}: {exc}")
        return None
    if not isinstance(raw, dict):
        return None

    now = safe_float(raw.get("Now"))
    last_close = safe_float(raw.get("LastClose"))
    amount = safe_float(raw.get("Amount"))
    if now is None or last_close in (None, 0) or amount is None:
        return None

    return QuotePoint(
        ts=time.time(),
        price=now,
        last_close=last_close,
        amount=amount,
        average=safe_float(raw.get("Average")),
        zangsu=safe_float(raw.get("Zangsu")),
    )


def compute_board_metrics(
    histories: dict[str, deque[QuotePoint]],
    *,
    leader_ret_1m: float,
    stock_name_map: dict[str, str],
    ma5_map: dict[str, float],
    code_order: dict[str, int],
    tq: Any | None = None,
) -> BoardMetrics:
    valid_codes = [code for code, hist in histories.items() if hist]
    current_points = {code: histories[code][-1] for code in valid_codes}

    if not current_points:
        return BoardMetrics(
            sample_size=0,
            change_pct=0.0,
            ret_1m=None,
            ret_3m=None,
            ret_5m=None,
            avg_zangsu=None,
            up_ratio=0.0,
            above_avg_ratio=0.0,
            leaders_count=0,
            amount_delta_60s=None,
            amount_burst_ratio=None,
            stock_rows=[],
            active_signals=[],
            amount_delta_ready=False,
            amount_burst_ready=False,
            ret_1m_ready_count=0,
            ret_3m_ready_count=0,
            ret_5m_ready_count=0,
        )

    change_pcts = [
        calc_return_pct(point.price, point.last_close)
        for point in current_points.values()
    ]
    ret_1m_map: dict[str, float | None] = {}
    ret_3m_map: dict[str, float | None] = {}
    ret_5m_map: dict[str, float | None] = {}

    for code, hist in histories.items():
        if not hist:
            continue
        current = hist[-1]
        ret_1m_map[code] = calc_return_pct(current.price, getattr(pick_point_before(hist, 60), "price", None))
        ret_3m_map[code] = calc_return_pct(current.price, getattr(pick_point_before(hist, 180), "price", None))
        ret_5m_map[code] = calc_return_pct(current.price, getattr(pick_point_before(hist, 300), "price", None))

    up_count = 0
    above_avg_count = 0
    leaders_count = 0
    stock_rows: list[dict[str, Any]] = []

    for code, point in current_points.items():
        change_pct = calc_return_pct(point.price, point.last_close)
        ret_1m = ret_1m_map.get(code)
        ret_3m = ret_3m_map.get(code)
        ret_5m = ret_5m_map.get(code)
        if isinstance(change_pct, (int, float)) and change_pct > 0:
            up_count += 1
        if point.average is not None and point.price >= point.average:
            above_avg_count += 1
        if (
            isinstance(ret_1m, (int, float))
            and ret_1m >= leader_ret_1m
            and point.average is not None
            and point.price >= point.average
        ):
            leaders_count += 1
        stock_rows.append({
            "code": code_to_digits(code),
            "name": get_stock_name(code, stock_name_map=stock_name_map, tq=tq),
            "current_price": round(point.price, 3),
            "change_pct": round(change_pct or 0.0, 3),
            "zangsu": round(point.zangsu, 3) if isinstance(point.zangsu, (int, float)) else None,
            "ma5_bias": calc_return_pct(point.price, ma5_map.get(code_to_digits(code))),
        })

    stock_rows.sort(
        key=lambda item: code_order.get(item["code"], 999999),
    )

    total_amount_history: deque[QuotePoint] = deque(maxlen=400)
    timestamps = sorted({point.ts for hist in histories.values() for point in hist})
    for ts in timestamps:
        amount_sum = 0.0
        found = False
        for hist in histories.values():
            for point in reversed(hist):
                if point.ts <= ts:
                    amount_sum += point.amount
                    found = True
                    break
        if found:
            total_amount_history.append(
                QuotePoint(ts=ts, price=0.0, last_close=0.0, amount=amount_sum, average=None, zangsu=None)
            )

    amount_delta_60s = window_amount_delta(total_amount_history, 60)
    previous_deltas: list[float] = []
    if total_amount_history:
        current_ts = total_amount_history[-1].ts
        for offset in (120, 180, 240, 300):
            virtual_history = deque(
                [point for point in total_amount_history if point.ts <= current_ts - (offset - 60)],
                maxlen=400,
            )
            if not virtual_history:
                continue
            delta = window_amount_delta(virtual_history, 60)
            if isinstance(delta, (int, float)) and delta > 0:
                previous_deltas.append(delta)

    baseline_amount = mean_or_none(previous_deltas)
    amount_burst_ratio = (
        amount_delta_60s / baseline_amount
        if isinstance(amount_delta_60s, (int, float))
        and isinstance(baseline_amount, (int, float))
        and baseline_amount > 0
        else None
    )
    amount_delta_ready = amount_delta_60s is not None
    amount_burst_ready = (
        amount_delta_ready
        and isinstance(baseline_amount, (int, float))
        and baseline_amount > 0
    )

    sample_size = len(current_points)
    up_ratio = up_count / sample_size if sample_size else 0.0
    above_avg_ratio = above_avg_count / sample_size if sample_size else 0.0

    ret_1m = mean_or_none(ret_1m_map.values())
    ret_3m = mean_or_none(ret_3m_map.values())
    ret_5m = mean_or_none(ret_5m_map.values())
    avg_zangsu = mean_or_none(point.zangsu for point in current_points.values())
    change_pct = mean_or_none(change_pcts) or 0.0

    return BoardMetrics(
        sample_size=sample_size,
        change_pct=change_pct,
        ret_1m=ret_1m,
        ret_3m=ret_3m,
        ret_5m=ret_5m,
        avg_zangsu=avg_zangsu,
        up_ratio=up_ratio,
        above_avg_ratio=above_avg_ratio,
        leaders_count=leaders_count,
        amount_delta_60s=amount_delta_60s,
        amount_burst_ratio=amount_burst_ratio,
        stock_rows=stock_rows,
        active_signals=[],
        amount_delta_ready=amount_delta_ready,
        amount_burst_ready=amount_burst_ready,
        ret_1m_ready_count=sum(1 for value in ret_1m_map.values() if value is not None),
        ret_3m_ready_count=sum(1 for value in ret_3m_map.values() if value is not None),
        ret_5m_ready_count=sum(1 for value in ret_5m_map.values() if value is not None),
    )


def format_watch_scope(
    *,
    manual_codes: list[str],
    members: list[str],
    stock_name_map: dict[str, str],
) -> list[str]:
    lines = ["监控范围"]
    lines.append(
        "  监控个股: " +
        (", ".join(format_stock_label(code, stock_name_map=stock_name_map) for code in manual_codes) if manual_codes else "-")
    )
    lines.append(f"  最终样本: {len(members)} 只")
    return lines


def evaluate_trigger(metrics: BoardMetrics, args: argparse.Namespace) -> tuple[bool, list[str]]:
    conditions: list[tuple[bool, str]] = [
        ((metrics.ret_1m or -999) >= args.min_ret_1m, f"1m涨幅={metrics.ret_1m:.3f}%") if metrics.ret_1m is not None else (False, "1m涨幅不足样本"),
        ((metrics.ret_3m or -999) >= args.min_ret_3m, f"3m涨幅={metrics.ret_3m:.3f}%") if metrics.ret_3m is not None else (False, "3m涨幅不足样本"),
        ((metrics.ret_5m or -999) >= args.min_ret_5m, f"5m涨幅={metrics.ret_5m:.3f}%") if metrics.ret_5m is not None else (False, "5m涨幅不足样本"),
        ((metrics.avg_zangsu or -999) >= args.min_avg_zangsu, f"平均涨速={metrics.avg_zangsu:.3f}") if metrics.avg_zangsu is not None else (False, "平均涨速缺失"),
        (metrics.up_ratio >= args.min_up_ratio, f"上涨占比={metrics.up_ratio:.1%}"),
        (metrics.above_avg_ratio >= args.min_above_avg_ratio, f"站上均价占比={metrics.above_avg_ratio:.1%}"),
        (metrics.leaders_count >= args.min_leaders, f"龙头同步数={metrics.leaders_count}"),
        ((metrics.amount_burst_ratio or -999) >= args.min_amount_burst, f"金额突增={metrics.amount_burst_ratio:.2f}x") if metrics.amount_burst_ratio is not None else (False, "金额突增样本不足"),
    ]
    positives = [text for matched, text in conditions if matched]
    core_ok = (
        (metrics.ret_1m is not None and metrics.ret_1m >= args.min_ret_1m)
        or (metrics.avg_zangsu is not None and metrics.avg_zangsu >= args.min_avg_zangsu)
    )
    triggered = core_ok and len(positives) >= args.min_trigger_score
    return triggered, positives


def format_metrics(metrics: BoardMetrics) -> str:
    lines = [
        f"样本数       : {metrics.sample_size}",
        f"股票池涨幅   : {fmt_pct(metrics.change_pct, ready=True)}",
        f"1m / 3m / 5m: {fmt_pct(metrics.ret_1m, ready=metrics.ret_1m_ready_count > 0)} / {fmt_pct(metrics.ret_3m, ready=metrics.ret_3m_ready_count > 0)} / {fmt_pct(metrics.ret_5m, ready=metrics.ret_5m_ready_count > 0)}",
        f"平均涨速     : {fmt_num(metrics.avg_zangsu)}",
        f"上涨占比     : {metrics.up_ratio:.1%}",
        f"均价上方占比 : {metrics.above_avg_ratio:.1%}",
        f"同步转强数   : {metrics.leaders_count}",
        f"60s成交额增量: {fmt_amount(metrics.amount_delta_60s, ready=metrics.amount_delta_ready)}",
        f"金额突增倍数 : {fmt_times(metrics.amount_burst_ratio, ready=metrics.amount_burst_ready)}",
    ]
    return "\n".join(lines)


def _pad(text: Any, width: int) -> str:
    return str(text)[:width].ljust(width)


def format_stock_table(metrics: BoardMetrics) -> str:
    if not metrics.stock_rows:
        return "监控个股: -"
    headers = [
        ("名称", 10),
        ("代码", 8),
        ("涨幅", 8),
        ("涨速", 8),
        ("现价", 8),
        ("相对MA5", 10),
    ]
    lines = []
    lines.append(" ".join(_pad(title, width) for title, width in headers))
    lines.append(" ".join("-" * width for _title, width in headers))
    for item in metrics.stock_rows:
        lines.append(
            " ".join(
                [
                    _pad(item.get("name") or item["code"], 10),
                    _pad(item["code"], 8),
                    _pad(f"{item['change_pct']:.2f}%", 8),
                    _pad(fmt_num(item["zangsu"]), 8),
                    _pad(fmt_price(item["current_price"]), 8),
                    _pad(fmt_signed_pct(item["ma5_bias"]), 10),
                ]
            )
        )
    return "\n".join(lines)


def format_signals(active_signals: list[str]) -> str:
    if not active_signals:
        return "触发信号: -"
    return "触发信号: " + " | ".join(active_signals)


def fmt_pct(value: float | None, *, ready: bool = True) -> str:
    if value is None:
        return "样本不足" if not ready else "-"
    return f"{value:.3f}%"


def fmt_num(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def fmt_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def fmt_signed_pct(value: float | None) -> str:
    if value is None:
        return "样本不足"
    return f"{value:+.2f}%"


def fmt_times(value: float | None, *, ready: bool = True) -> str:
    if value is None:
        return "样本不足" if not ready else "-"
    return f"{value:.2f}x"


def fmt_amount(value: float | None, *, ready: bool = True) -> str:
    if value is None:
        return "样本不足" if not ready else "-"
    abs_value = abs(value)
    if abs_value >= 100000000:
        return f"{value / 100000000:.2f}亿"
    if abs_value >= 10000:
        return f"{value / 10000:.2f}万"
    return f"{value:.0f}"


def emit_alert(message: str, *, popup: bool) -> None:
    print(f"\n[ALERT] {message}\n")
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass
    if popup:
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "股票池异动提醒", 0x40)
        except Exception:
            pass


def append_log(log_path: Path, payload: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通达信股票池快速拉升监控器")
    parser.add_argument("--watch-codes", default="", help="手动传入个股代码，传纯数字，支持逗号分隔多个")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="轮询间隔秒数，默认 5")
    parser.add_argument("--history-minutes", type=int, default=10, help="每只股票保留的分钟历史，默认 10")
    parser.add_argument("--cooldown-seconds", type=int, default=180, help="两次提醒最小间隔，默认 180 秒")
    parser.add_argument("--popup", action="store_true", help="触发时弹出 Windows 提示框")
    parser.add_argument("--once", action="store_true", help="只跑一轮，方便连通性测试")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="提醒日志路径")

    parser.add_argument("--min-ret-1m", type=float, default=0.25, help="股票池平均 1 分钟涨幅阈值，默认 0.25")
    parser.add_argument("--min-ret-3m", type=float, default=0.60, help="股票池平均 3 分钟涨幅阈值，默认 0.60")
    parser.add_argument("--min-ret-5m", type=float, default=0.90, help="股票池平均 5 分钟涨幅阈值，默认 0.90")
    parser.add_argument("--min-avg-zangsu", type=float, default=1.20, help="股票池平均涨速阈值，默认 1.20")
    parser.add_argument("--min-up-ratio", type=float, default=0.70, help="上涨家数占比阈值，默认 0.70")
    parser.add_argument("--min-above-avg-ratio", type=float, default=0.60, help="站上均价占比阈值，默认 0.60")
    parser.add_argument("--min-leaders", type=int, default=3, help="龙头同步数阈值，默认 3")
    parser.add_argument("--leader-ret-1m", type=float, default=0.50, help="定义龙头同步的个股 1m 涨幅阈值，默认 0.50")
    parser.add_argument("--min-amount-burst", type=float, default=1.80, help="60 秒成交额突增倍数阈值，默认 1.80")
    parser.add_argument("--min-trigger-score", type=int, default=4, help="满足多少条信号后触发，默认 4")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    manual_codes = parse_csv_codes(args.watch_codes)
    if not manual_codes:
        parser.error("至少传入一个 --watch-codes")

    tq = load_tq()
    tq.initialize(__file__)
    stock_name_map = load_stock_name_map()

    members = resolve_watchlist(manual_codes)
    members = sorted({code_to_digits(code) for code in members if code_to_digits(code)})
    if not members:
        raise SystemExit("未获取到任何可监控股票，请检查 --watch-codes")
    ma5_map = load_ma5_map(members)
    code_order = {code: index for index, code in enumerate(members)}

    maxlen = max(40, int((args.history_minutes * 60) / max(args.poll_seconds, 1)) + 20)
    histories: dict[str, deque[QuotePoint]] = {
        code: deque(maxlen=maxlen) for code in members
    }

    print(
        "\n".join(
            [
                "初始化完成",
                *format_watch_scope(
                    manual_codes=manual_codes,
                    members=members,
                    stock_name_map=stock_name_map,
                ),
                f"轮询间隔: {args.poll_seconds}s",
                f"提醒冷却: {args.cooldown_seconds}s",
                "监控指标: 1m/3m/5m涨幅, 平均涨速, 金额突增, 上涨占比, 均价上方占比, 同步转强数",
                "-" * 64,
            ]
        )
    )

    last_alert_ts = 0.0
    log_path = Path(args.log_path)

    while True:
        loop_start = time.time()
        for code in members:
            point = fetch_snapshot(tq, code)
            if point is not None:
                histories[code].append(point)

        metrics = compute_board_metrics(
            histories,
            leader_ret_1m=args.leader_ret_1m,
            stock_name_map=stock_name_map,
            ma5_map=ma5_map,
            code_order=code_order,
            tq=tq,
        )
        triggered, active_signals = evaluate_trigger(metrics, args)
        metrics.active_signals = active_signals

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            "\n".join(
                [
                    f"[{now_text}]",
                    format_stock_table(metrics),
                    "-" * 64,
                ]
            )
        )

        if triggered and (time.time() - last_alert_ts) >= args.cooldown_seconds:
            signal_text = " | ".join(active_signals)
            message = f"股票池快速拉升触发: {signal_text}"
            emit_alert(message, popup=args.popup)
            append_log(
                log_path,
                {
                    "ts": now_text,
                    "watch_codes": args.watch_codes,
                    "message": message,
                    "metrics": {
                        "sample_size": metrics.sample_size,
                        "change_pct": metrics.change_pct,
                        "ret_1m": metrics.ret_1m,
                        "ret_3m": metrics.ret_3m,
                        "ret_5m": metrics.ret_5m,
                        "avg_zangsu": metrics.avg_zangsu,
                        "up_ratio": metrics.up_ratio,
                        "above_avg_ratio": metrics.above_avg_ratio,
                        "leaders_count": metrics.leaders_count,
                        "amount_delta_60s": metrics.amount_delta_60s,
                        "amount_burst_ratio": metrics.amount_burst_ratio,
                        "stock_rows": metrics.stock_rows,
                    },
                    "signals": active_signals,
                },
            )
            last_alert_ts = time.time()

        if args.once:
            break

        elapsed = time.time() - loop_start
        sleep_seconds = max(0.5, args.poll_seconds - elapsed)
        time.sleep(sleep_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
