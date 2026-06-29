# -*- coding: utf-8 -*-
"""全量下载 A 股日 K 数据到 SQLite。

从 TdxQuant 接口拉取全市场股票最近 150 个交易日的日 K 数据，
写入新建的 SQLite 数据库。支持断点续传（已下载的股票跳过）。

用法:
    python download_all_daily_k.py [--db PATH] [--days N] [--batch-size N]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# TdxQuant 路径
TDXQUANT_PATH = r"D:\【指标100】通达信《专业研究版》773\【指标100】通达信《专业研究版》773\PYPlugins\user"
sys.path.insert(0, TDXQUANT_PATH)
from tqcenter import tq  # noqa: E402

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------
DEFAULT_DB = "data/all_daily_k.db"
DEFAULT_DAYS = 150
DEFAULT_BATCH_SIZE = 200

# TdxQuant 返回的字段 → SQLite 列名
FIELD_MAP = {
    "Open": "open_price",
    "High": "high_price",
    "Low": "low_price",
    "Close": "close_price",
    "Volume": "volume",
    "Amount": "amount",
}


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db(db_path: str) -> sqlite3.Connection:
    """创建数据库和表。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_kline (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open_price REAL,
            high_price REAL,
            low_price REAL,
            close_price REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (code, trade_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_kline_code ON daily_kline (code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_kline_date ON daily_kline (trade_date)"
    )
    # 记录同步状态
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_status (
            code TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL DEFAULT 0,
            start_date TEXT,
            end_date TEXT,
            synced_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _strip_suffix(code: str) -> str:
    """去掉股票代码后缀 .SZ/.SH/.BJ，返回纯数字。"""
    return code.replace(".SZ", "").replace(".SH", "").replace(".BJ", "")


def get_synced_codes(conn: sqlite3.Connection) -> set[str]:
    """返回已同步的股票代码集合（纯数字）。"""
    rows = conn.execute("SELECT code FROM sync_status").fetchall()
    return {r[0] for r in rows}


def insert_daily_data(conn: sqlite3.Connection, code: str, df: pd.DataFrame) -> int:
    """将单只股票的日 K DataFrame 写入 SQLite，返回写入行数。"""
    if df.empty:
        return 0

    # 去掉后缀，统一用纯数字
    pure_code = _strip_suffix(code)

    # DataFrame 列名映射
    df = df.rename(columns=FIELD_MAP)
    df["code"] = pure_code
    df["trade_date"] = df.index.strftime("%Y-%m-%d")

    columns = ["code", "trade_date", "open_price", "high_price", "low_price", "close_price", "volume", "amount"]
    rows = df[columns].to_records(index=False).tolist()

    conn.executemany(
        """
        INSERT OR REPLACE INTO daily_kline
            (code, trade_date, open_price, high_price, low_price, close_price, volume, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    # 更新同步状态
    dates = sorted(df["trade_date"].unique())
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_status (code, row_count, start_date, end_date, synced_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (pure_code, len(df), dates[0], dates[-1], datetime.now().isoformat()),
    )
    conn.commit()
    return len(df)


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------
def download_all(
    db_path: str = DEFAULT_DB,
    days: int = DEFAULT_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """下载全市场 A 股日 K 数据。"""
    t0 = time.perf_counter()

    # 初始化 TdxQuant
    tq.initialize("download_all_daily_k")

    # 获取股票列表
    all_stocks = tq.get_stock_list()
    print(f"全市场股票: {len(all_stocks)} 只")

    # 初始化数据库
    conn = init_db(db_path)
    synced = get_synced_codes(conn)
    print(f"已同步: {len(synced)} 只")

    # 过滤掉已同步的（all_stocks 带后缀，synced 纯数字，统一用纯数字比对）
    todo = [s for s in all_stocks if _strip_suffix(s) not in synced]
    print(f"待下载: {len(todo)} 只")

    if not todo:
        print("全部已同步，无需下载。")
        tq.close()
        conn.close()
        return

    # 分批下载
    total_written = 0
    total_batches = (len(todo) + batch_size - 1) // batch_size

    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        batch_num = i // batch_size + 1

        try:
            res = tq.get_market_data(
                field_list=[],
                stock_list=batch,
                period="1d",
                count=days,
                dividend_type="front",
                fill_data=True,
            )
        except Exception as exc:
            print(f"  [{batch_num}/{total_batches}] 批次拉取失败: {exc}")
            continue

        if res is None or not isinstance(res, dict) or "Close" not in res:
            print(f"  [{batch_num}/{total_batches}] 批次返回为空")
            continue

        close_df = res["Close"]
        if close_df is None or close_df.empty:
            print(f"  [{batch_num}/{total_batches}] 批次无数据")
            continue

        # 逐只写入
        batch_written = 0
        for tdx_code in close_df.columns:
            # 构建单股票 DataFrame
            try:
                df = pd.DataFrame({
                    "Open": res["Open"][tdx_code],
                    "High": res["High"][tdx_code],
                    "Low": res["Low"][tdx_code],
                    "Close": res["Close"][tdx_code],
                    "Volume": res["Volume"][tdx_code],
                    "Amount": res["Amount"][tdx_code],
                })
                df.index.name = "date"
                df = df.dropna(subset=["Close"])
            except Exception:
                continue

            if df.empty:
                continue

            n = insert_daily_data(conn, tdx_code, df)
            batch_written += n

        total_written += batch_written
        elapsed = time.perf_counter() - t0
        print(
            f"  [{batch_num}/{total_batches}] {len(batch)} 只 → 写入 {batch_written} 行 "
            f"(累计 {total_written} 行, {elapsed:.0f}s)"
        )

    tq.close()
    conn.close()

    elapsed = time.perf_counter() - t0
    print(f"\n完成: {total_written} 行, 耗时 {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"数据库: {Path(db_path).absolute()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全量下载 A 股日 K 到 SQLite")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路径 (默认: {DEFAULT_DB})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"下载天数 (默认: {DEFAULT_DAYS})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"每批股票数 (默认: {DEFAULT_BATCH_SIZE})")
    args = parser.parse_args()

    download_all(db_path=args.db, days=args.days, batch_size=args.batch_size)
