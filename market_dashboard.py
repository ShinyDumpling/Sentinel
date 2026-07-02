# -*- coding: utf-8 -*-
"""
每日大盘看板 —— 纯通达信量化接口实现，大盘指数 + 市场广度 + 板块热度榜

已完成:
  1. ✅ 涨跌家数/涨跌比统计 —— 用 get_stock_list() 拉全市场 5536 只股票的 ZAF 涨跌幅自己统计
  2. ✅ 涨跌停家数统计 —— 同上，从 ZAF 中统计 >9.5% (涨停) 和 < -9.5% (跌停) 的数量

前提: 通达信客户端开着、登录着；本文件放在 tqcenter.py 同目录下
跑法: D:/Python/Python313/python.exe tdx_market_dashboard.py --no-llm
"""
import os
import sys
import json
import time
from collections import Counter
from pathlib import Path

from tqcenter import tq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

N = 120  # 拉最近120根日K，供20/60/120日涨幅和中期趋势判断使用
TOP_N = 15  # 每个榜单取前15名

BASE_DIR = Path(__file__).resolve().parent
BLOCK_TYPE_MAP_PATH = BASE_DIR / "tdx_block_type_map.json"
BLOCK_TYPE_LABELS = {
    "industry": "【行业】",
    "region": "【地域】",
    "theme": "【概念】",
    "style": "【风格】",
    "holding": "【持仓】",
    "event": "【事件】",
    "unknown": "【未分类】",
}
RANKABLE_BLOCK_TYPES = {"industry", "theme"}
KEY_BLOCKS_PER_BUCKET = 2
LEADER_CANDIDATE_COUNT = 3
MIDDLE_ARMY_CANDIDATE_COUNT = 3
LIMIT_UP_THRESHOLD = 9.5
LIMIT_DOWN_THRESHOLD = -9.5


def pct(a, b):
    try:
        return (float(a) / float(b) - 1) * 100
    except (ZeroDivisionError, TypeError, ValueError):
        return float("nan")


def rolling_mean(values, window):
    if len(values) < window or window <= 0:
        return None
    subset = values.iloc[-window:]
    if len(subset) < window:
        return None
    return float(subset.mean())


def classify_slope(current, previous, eps=0.3):
    if current is None or previous is None:
        return None
    delta = current - previous
    if delta > eps:
        return "向上"
    if delta < -eps:
        return "向下"
    return "走平"


def max_drawdown_pct(series):
    if len(series) < 2:
        return None
    running_high = series.cummax()
    drawdowns = series / running_high - 1
    return float(drawdowns.min()) * 100


def summarize_block_trend(close_series, amount_series):
    values = close_series.astype(float).dropna().tail(N)
    amount_values = amount_series.astype(float).dropna().tail(N)
    if len(values) < 2 or len(amount_values) < 1:
        return {
            "5日涨幅%": None,
            "20日涨幅%": None,
            "60日涨幅%": None,
            "120日涨幅%": None,
            "MA20": None,
            "MA60": None,
            "MA120": None,
            "收盘站上MA20": None,
            "收盘站上MA60": None,
            "MA20方向": None,
            "MA60方向": None,
            "距120日新高回撤%": None,
            "近20日最大回撤%": None,
            "近10日活跃天数": None,
            "近20日放量天数": None,
            "中期趋势": "数据不足",
            "阶段判断": "数据不足",
        }
    last = float(values.iloc[-1])
    ma20 = rolling_mean(values, 20)
    ma60 = rolling_mean(values, 60)
    ma120 = rolling_mean(values, 120)
    prev_ma20 = rolling_mean(values.iloc[:-5], 20) if len(values) >= 25 else None
    prev_ma60 = rolling_mean(values.iloc[:-5], 60) if len(values) >= 65 else None
    slope20 = classify_slope(ma20, prev_ma20)
    slope60 = classify_slope(ma60, prev_ma60)
    change5 = pct(values.iloc[-1], values.iloc[-6]) if len(values) >= 6 else float("nan")
    change20 = pct(values.iloc[-1], values.iloc[-21]) if len(values) >= 21 else float("nan")
    change60 = pct(values.iloc[-1], values.iloc[-61]) if len(values) >= 61 else float("nan")
    change120 = pct(values.iloc[-1], values.iloc[0]) if len(values) >= 120 else float("nan")
    high120 = float(values.max()) if len(values) >= 1 else None
    drawdown_from_high = pct(last, high120) if high120 else float("nan")
    drawdown20 = max_drawdown_pct(values.tail(20)) if len(values) >= 20 else None
    avg_amt20 = rolling_mean(amount_values, 20)
    active_days10 = int((values.pct_change().tail(10) > 0.02).sum()) if len(values) >= 10 else None
    strong_amt_days20 = (
        int((amount_values.tail(20) > avg_amt20 * 1.2).sum()) if avg_amt20 and len(amount_values) >= 20 else None
    )

    if (
        ma20 is not None and ma60 is not None
        and last >= ma20 >= ma60
        and slope20 == "向上"
        and slope60 in ("向上", "走平")
    ):
        trend_stage = "中期上升"
    elif ma20 is not None and ma60 is not None and last >= ma20 and slope20 == "向上":
        trend_stage = "上升回流"
    elif ma20 is not None and ma60 is not None and last >= ma20 and last < ma60:
        trend_stage = "反抽修复"
    elif ma20 is not None and ma60 is not None and last < ma20 and last >= ma60:
        trend_stage = "高位整理"
    else:
        trend_stage = "偏弱震荡"

    if (
        change60 == change60 and change120 == change120
        and change60 >= 15 and change120 >= 20
        and drawdown_from_high == drawdown_from_high and drawdown_from_high >= -12
    ):
        mid_term_trend = "主线候选"
    elif change60 == change60 and change60 >= 8 and slope20 == "向上":
        mid_term_trend = "趋势活跃"
    elif change20 == change20 and change20 > 0:
        mid_term_trend = "短线活跃"
    else:
        mid_term_trend = "趋势一般"

    return {
        "5日涨幅%": round(change5, 2) if change5 == change5 else None,
        "20日涨幅%": round(change20, 2) if change20 == change20 else None,
        "60日涨幅%": round(change60, 2) if change60 == change60 else None,
        "120日涨幅%": round(change120, 2) if change120 == change120 else None,
        "MA20": round(ma20, 2) if ma20 is not None else None,
        "MA60": round(ma60, 2) if ma60 is not None else None,
        "MA120": round(ma120, 2) if ma120 is not None else None,
        "收盘站上MA20": ma20 is not None and last >= ma20,
        "收盘站上MA60": ma60 is not None and last >= ma60,
        "MA20方向": slope20,
        "MA60方向": slope60,
        "距120日新高回撤%": round(drawdown_from_high, 2) if drawdown_from_high == drawdown_from_high else None,
        "近20日最大回撤%": round(drawdown20, 2) if drawdown20 is not None else None,
        "近10日活跃天数": active_days10,
        "近20日放量天数": strong_amt_days20,
        "中期趋势": mid_term_trend,
        "阶段判断": trend_stage,
    }


def load_block_type_map():
    """读取板块分类映射表。"""
    with BLOCK_TYPE_MAP_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_block_type(code, block_type_map):
    """优先按本地映射表判断板块类型。"""
    meta = block_type_map.get(code)
    if not meta:
        return "unknown"
    return meta.get("type", "unknown")


def get_block_type_label(type_code):
    return BLOCK_TYPE_LABELS.get(type_code, f"【{type_code}】")


def get_block_type_display(code, block_type_map):
    meta = block_type_map.get(code, {})
    type_code = meta.get("type", "unknown")
    return get_block_type_label(type_code)


def safe_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def sort_key_desc(value, default=-999999):
    return value if value is not None else default


def pick_key_blocks(industry_top, concept_top):
    """从涨幅榜中选取重点板块用于成分股验证。"""
    ordered = []
    seen = set()
    groups = [
        ("行业Top", industry_top[:KEY_BLOCKS_PER_BUCKET]),
        ("概念Top", concept_top[:KEY_BLOCKS_PER_BUCKET]),
    ]
    for source, rows in groups:
        for row in rows:
            code = row["代码"]
            if code in seen:
                continue
            seen.add(code)
            copied = dict(row)
            copied["来源榜单"] = source
            ordered.append(copied)
    return ordered


def brief_stock_row(row, name_cache):
    code = row["代码"]
    name = name_cache.get(code, code)
    chg = row.get("涨跌幅%")
    amt = row.get("成交额亿")
    volume_ratio = row.get("量比")
    consecutive_boards = row.get("连板天数")
    parts = [name]
    if chg is not None:
        parts.append(f"{chg:+.2f}%")
    if amt is not None:
        parts.append(f"{amt:.1f}亿")
    if volume_ratio is not None:
        parts.append(f"量比{volume_ratio:.2f}")
    if consecutive_boards is not None and consecutive_boards >= 2:
        parts.append(f"{int(round(consecutive_boards))}连板")
    return " / ".join(parts)


def classify_block_action(block_row, member_rows):
    board_chg = block_row.get("当日涨幅%")
    board_net = block_row.get("主力净流入亿")
    total = len(member_rows)
    if total == 0:
        return "数据不足", ["成分股数据为空，无法判断板块内部结构"]

    up = sum(1 for row in member_rows if (row.get("涨跌幅%") or 0) > 0)
    down = sum(1 for row in member_rows if (row.get("涨跌幅%") or 0) < 0)
    flat = total - up - down
    limit_up = sum(1 for row in member_rows if (row.get("涨跌幅%") or -999) >= LIMIT_UP_THRESHOLD)
    limit_down = sum(1 for row in member_rows if (row.get("涨跌幅%") or 999) <= LIMIT_DOWN_THRESHOLD)
    up_ratio = up / total if total else 0
    down_ratio = down / total if total else 0

    reasons = [
        f"成分股上涨 {up} 家，下跌 {down} 家，平盘 {flat} 家",
        f"涨停 {limit_up} 家，跌停 {limit_down} 家",
    ]
    if board_net is not None:
        reasons.append(f"板块主力净流入 {board_net:+.2f} 亿")

    if board_chg is None:
        return "数据不足", reasons

    if board_chg >= 0:
        if up_ratio >= 0.6 and (board_net is None or board_net >= 0) and limit_up >= 1:
            return "真上涨", reasons
        if up_ratio <= 0.45 or (board_net is not None and board_net < 0):
            return "疑似虚涨", reasons
        if up_ratio >= 0.5:
            return "分化上涨", reasons
        return "偏弱上涨", reasons

    if down_ratio >= 0.6 and (board_net is None or board_net <= 0):
        return "真下跌", reasons
    if up_ratio >= 0.35:
        return "分化下跌", reasons
    if board_net is not None and board_net > 0:
        return "疑似承接下跌", reasons
    return "偏弱下跌", reasons


def analyze_key_block_members(key_blocks):
    if not key_blocks:
        return []

    block_members = {}
    all_member_codes = []
    seen_member_codes = set()

    for block in key_blocks:
        members = tq.get_stock_list_in_sector(block["代码"]) or []
        members = [code for code in members if isinstance(code, str) and code]
        block_members[block["代码"]] = members
        for code in members:
            if code not in seen_member_codes:
                seen_member_codes.add(code)
                all_member_codes.append(code)

    amount_df = None
    if all_member_codes:
        try:
            market_res = tq.get_market_data(
                field_list=["Amount"],
                stock_list=all_member_codes,
                period="1d",
                count=1,
                dividend_type="none",
                fill_data=True,
            )
            amount_df = market_res.get("Amount")
        except Exception:
            amount_df = None

    member_cache = {}
    for code in all_member_codes:
        extra = tq.get_more_info(stock_code=code, field_list=["ZAF", "fLianB", "EverZTCount"]) or {}
        amount_yi = None
        if amount_df is not None and code in amount_df.columns:
            try:
                amount_yi = float(amount_df[code].sort_index().iloc[-1]) / 10000
            except Exception:
                amount_yi = None
        member_cache[code] = {
            "代码": code,
            "涨跌幅%": safe_float(extra.get("ZAF")),
            "量比": safe_float(extra.get("fLianB")),
            "连板天数": safe_float(extra.get("EverZTCount")),
            "成交额亿": round(amount_yi, 2) if amount_yi is not None else None,
        }

    display_codes = []
    analyses = []
    for block in key_blocks:
        members = [dict(member_cache[code]) for code in block_members.get(block["代码"], []) if code in member_cache]
        members_sorted = sorted(
            members,
            key=lambda row: (
                sort_key_desc(row.get("涨跌幅%")),
                sort_key_desc(row.get("成交额亿"), 0),
                sort_key_desc(row.get("连板天数"), 0),
                sort_key_desc(row.get("量比"), 0),
            ),
            reverse=True,
        )
        middle_army_sorted = sorted(
            members,
            key=lambda row: (
                sort_key_desc(row.get("成交额亿"), 0),
                sort_key_desc(row.get("涨跌幅%")),
                sort_key_desc(row.get("连板天数"), 0),
                sort_key_desc(row.get("量比"), 0),
            ),
            reverse=True,
        )
        limit_up_rows = [row for row in members_sorted if (row.get("涨跌幅%") or -999) >= LIMIT_UP_THRESHOLD]
        limit_down_rows = [row for row in members_sorted if (row.get("涨跌幅%") or 999) <= LIMIT_DOWN_THRESHOLD]
        action_label, reasons = classify_block_action(block, members)

        selected = (
            limit_up_rows[:LEADER_CANDIDATE_COUNT]
            + members_sorted[:LEADER_CANDIDATE_COUNT]
            + middle_army_sorted[:MIDDLE_ARMY_CANDIDATE_COUNT]
        )
        seen_display = set()
        for row in selected:
            code = row["代码"]
            if code not in seen_display:
                seen_display.add(code)
                display_codes.append(code)

        total = len(members)
        up = sum(1 for row in members if (row.get("涨跌幅%") or 0) > 0)
        down = sum(1 for row in members if (row.get("涨跌幅%") or 0) < 0)
        analyses.append({
            "代码": block["代码"],
            "名称": block["名称"],
            "类型代码": block["类型代码"],
            "类型": block["类型"],
            "来源榜单": block["来源榜单"],
            "当日涨幅%": block.get("当日涨幅%"),
            "20日涨幅%": block.get("20日涨幅%"),
            "主力净流入亿": block.get("主力净流入亿"),
            "成分股数": total,
            "上涨家数": up,
            "下跌家数": down,
            "平盘家数": total - up - down,
            "上涨占比%": round(up * 100 / total, 1) if total else None,
            "下跌占比%": round(down * 100 / total, 1) if total else None,
            "涨停家数": len(limit_up_rows),
            "跌停家数": len(limit_down_rows),
            "状态判断": action_label,
            "判断依据": reasons,
            "_涨停候选": limit_up_rows[:LEADER_CANDIDATE_COUNT],
            "_涨停候选_raw": limit_up_rows[:LEADER_CANDIDATE_COUNT],
            "_龙头候选": members_sorted[:LEADER_CANDIDATE_COUNT],
            "_龙头候选_raw": members_sorted[:LEADER_CANDIDATE_COUNT],
            "_中军候选": middle_army_sorted[:MIDDLE_ARMY_CANDIDATE_COUNT],
            "_中军候选_raw": middle_army_sorted[:MIDDLE_ARMY_CANDIDATE_COUNT],
        })

    name_cache = {}
    for code in dict.fromkeys(display_codes):
        info = tq.get_stock_info(code, field_list=["Name"]) or {}
        name_cache[code] = info.get("Name", code)

    for analysis in analyses:
        analysis["涨停股"] = [brief_stock_row(row, name_cache) for row in analysis.pop("_涨停候选")]
        analysis["涨停股名"] = [
            f"{name_cache.get(row['代码'], row['代码'])}({row['代码'].replace('.SH','').replace('.SZ','').replace('.BJ','')})"
            for row in analysis.pop("_涨停候选_raw")
        ]
        analysis["龙头候选"] = [brief_stock_row(row, name_cache) for row in analysis.pop("_龙头候选")]
        analysis["龙头候选名"] = [
            f"{name_cache.get(row['代码'], row['代码'])}({row['代码'].replace('.SH','').replace('.SZ','').replace('.BJ','')})"
            for row in analysis.pop("_龙头候选_raw")
        ]
        analysis["中军候选"] = [brief_stock_row(row, name_cache) for row in analysis.pop("_中军候选")]
        analysis["中军候选名"] = [
            f"{name_cache.get(row['代码'], row['代码'])}({row['代码'].replace('.SH','').replace('.SZ','').replace('.BJ','')})"
            for row in analysis.pop("_中军候选_raw")
        ]

    return analyses


def _get_llm_settings():
    """从环境变量读 LLM 配置（ARK_API_KEY / ARK_MODEL / ARK_BASE_URL）"""
    api_key = os.getenv("ARK_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("ARK_MODEL") or os.getenv("OPENAI_MODEL")
    base_url = os.getenv("ARK_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if model and "/" in model and not model.startswith("ark-"):
        model = model.split("/", 1)[1]
    return {"api_key": api_key, "model": model, "base_url": base_url}


def judge_dashboard_with_llm(asof, index_rows, industry_top, concept_top, market_breadth):
    """旧版逻辑：把大盘+板块+市场广度数据喂给LLM，输出大白话解读（当前停用）"""
    s = _get_llm_settings()
    if not s["api_key"] or not s["model"] or not s["base_url"]:
        return {"状态": "未执行", "原因": "缺少 LLM 配置，已跳过解读。(设置 ARK_API_KEY / ARK_MODEL / ARK_BASE_URL 环境变量即可)"}

    try:
        from openai import OpenAI
    except ImportError:
        return {"状态": "未执行", "原因": "未安装 openai 库 (pip install openai)。"}

    data_text = json.dumps({
        "数据截止交易日": asof,
        "大盘指数": index_rows,
        "市场广度": market_breadth,
        "行业板块Top15": industry_top,
        "概念板块Top15": concept_top,
    }, ensure_ascii=False, indent=2)

    prompt = f"""你是一个面向A股新手的每日大盘解读助手。下面给你 {asof} 收盘的完整看板数据：
1. 6大宽基指数（上证指数、深证成指、创业板指、科创50、沪深300、中证1000）
2. 市场广度：涨跌家数、涨跌比、涨跌停家数
3. 行业板块热度榜Top15（按当日涨幅排序，带主力净流入）
4. 概念板块热度榜Top15（按当日涨幅排序，带主力净流入）

请你从「新手视角」做解读，输出如下JSON：
{{
  "今日一句话总结": "用一句话说清楚今天市场到底是涨是跌、主线在哪里，<=50字",
  "市场情绪判断": "根据涨跌比和涨跌停家数，判断是赚钱效应还是亏钱效应，一句话",
  "市场风格判断": "是权重行情还是小票行情？是科技成长还是价值蓝筹？一句话",
  "真有资金的板块": "列2-3个主力净流入为正且涨幅靠前的板块名字",
  "虚涨警示板块": "列1-2个看起来涨得凶但主力净流出的板块名字，没有就写无",
  "新手观察建议": "给新手一句明天的观察点，一句话",
  "风险提示": "提醒一句追高风险或仓位控制"
}}

【原始数据】
{data_text}
"""

    client = OpenAI(api_key=s["api_key"], base_url=s["base_url"])
    resp = client.chat.completions.create(
        model=s["model"],
        temperature=0.2,
        messages=[
            {"role": "system", "content": "你是一个克制、说大白话、面向A股新手的大盘解读助手。你只输出 JSON，不要输出任何其他文字、解释或 markdown 标记。"},
            {"role": "user", "content": prompt},
        ],
        timeout=120,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"状态": "解析失败", "原始返回": content}
    parsed["_模型"] = s["model"]
    return parsed


def judge_style_with_llm(asof, index_rows):
    """只根据6大指数判断最近市场主风格。"""
    s = _get_llm_settings()
    if not s["api_key"] or not s["model"] or not s["base_url"]:
        return {"状态": "未执行", "原因": "缺少 LLM 配置，已跳过风格判断。(设置 ARK_API_KEY / ARK_MODEL / ARK_BASE_URL 环境变量即可)"}

    try:
        from openai import OpenAI
    except ImportError:
        return {"状态": "未执行", "原因": "未安装 openai 库 (pip install openai)。"}

    data_text = json.dumps({
        "数据截止交易日": asof,
        "6大宽基指数": index_rows,
    }, ensure_ascii=False, indent=2)

    prompt = f"""你是一个A股市场风格分析助手。

你的任务不是分析板块，不是分析资金抱团方向，也不是给交易建议。
你只需要根据6大宽基指数的表现，区分“最近主风格”和“今天的盘面动作”。

你会看到以下6个指数的数据：
1. 上证指数
2. 深证成指
3. 创业板指
4. 科创50
5. 沪深300
6. 中证1000

每个指数都包含：
- 当日%
- 5日%
- 20日%
- 成交额亿
- 量能比

请按下面的时间框架做判断：
- 20日%：判断最近一段时间的主风格
- 5日%：判断最近短线是否仍在延续该风格
- 当日%：判断今天是在强化、分歧、回撤，还是发生切换
- 量能比：只作为辅助，不要过度解读

请按以下原则判断：
- 如果科创50、创业板指在20日和5日维度明显强于沪深300、上证指数，最近主风格偏“科技成长”
- 如果中证1000在20日和5日维度明显强于沪深300，最近主风格偏“小盘题材”
- 如果沪深300、上证指数在20日和5日维度明显强于创业板指、科创50，最近主风格偏“权重蓝筹”
- 如果多数宽基在20日和5日维度都同步走强，可判断为“大盘普涨”
- 如果20日主风格清晰，但当日多数指数普跌或短线强势指数明显转弱，应判断为“主风格仍在，但今天处于分歧/回撤”
- 如果20日、5日、当日三个维度互相冲突且没有稳定优势方向，可判断为“混合轮动”或“无明显主线”

注意：
- 不要引用板块、个股、消息面、政策面
- 不要补充输入中没有提供的信息
- 只能依据这6大指数的数据做判断
- 不要把“最近主风格”和“今天的盘面动作”混成一句空泛结论
- 输出要克制、直接、少空话

请输出如下 JSON：
{{
  "最近主风格": "科技成长/小盘题材/权重蓝筹/大盘普涨/混合轮动/无明显主线",
  "最近风格强度": "强/中/弱",
  "近5日延续性": "强延续/弱延续/开始分歧/已经走弱",
  "今日盘面状态": "强化/分歧/回撤/切换/普跌",
  "今日是否与主风格一致": "一致/部分一致/不一致",
  "指数依据": [
    "一句话说明20日和5日维度的依据",
    "一句话说明当日维度的依据"
  ],
  "一句话结论": "一句大白话总结最近风格和今天状态，30字以内"
}}

【原始数据】
{data_text}
"""

    client = OpenAI(api_key=s["api_key"], base_url=s["base_url"])
    resp = client.chat.completions.create(
        model=s["model"],
        temperature=0.1,
        messages=[
            {"role": "system", "content": "你是一个克制、直接、只根据指数数据判断A股风格的分析助手。你只输出 JSON，不要输出任何其他文字、解释或 markdown 标记。"},
            {"role": "user", "content": prompt},
        ],
        timeout=120,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"状态": "解析失败", "原始返回": content}
    parsed["_模型"] = s["model"]
    return parsed


def judge_sector_intent_with_llm(
    asof,
    style_verdict,
    industry_top,
    industry_bottom,
    concept_top,
    concept_bottom,
    key_block_analyses,
):
    """Use post-market data to infer next-session sector direction expectations."""
    s = _get_llm_settings()
    if not s["api_key"] or not s["model"] or not s["base_url"]:
        return {
            "status": "skipped",
            "reason": "Missing LLM settings: set ARK_API_KEY / ARK_MODEL / ARK_BASE_URL",
        }

    try:
        from openai import OpenAI
    except ImportError:
        return {
            "status": "skipped",
            "reason": "openai package is not installed (pip install openai)",
        }

    system_prompt, prompt = build_sector_intent_prompt(
        asof,
        style_verdict,
        industry_top,
        industry_bottom,
        concept_top,
        concept_bottom,
        key_block_analyses,
    )

    client = OpenAI(api_key=s["api_key"], base_url=s["base_url"])
    resp = client.chat.completions.create(
        model=s["model"],
        temperature=0.1,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        timeout=120,
    )
    content = resp.choices[0].message.content or ""
    return {"_model": s["model"], "content": content}


def build_sector_intent_prompt(
    asof,
    style_verdict,
    industry_top,
    industry_bottom,
    concept_top,
    concept_bottom,
    key_block_analyses,
):
    def _fmt(val, decimals=2):
        if val is None or val != val:
            return "-"
        return f"{val:.{decimals}f}"

    def _pure_code(code):
        return code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")

    lines = []
    lines.append(f"交易日: {asof}")
    if style_verdict:
        lines.append(f"市场风格: {json.dumps(style_verdict, ensure_ascii=False)}")

    header = f"{'排名':<4}{'板块':<20}{'当日%':>8}{'20日%':>8}{'60日%':>8}{'120日%':>8}{'主力净流入亿':>14}{'中期趋势':>12}"

    def _append_rank_table(title, rows_to_print):
        lines.append("")
        lines.append(title)
        lines.append("-" * 72)
        lines.append(header)
        lines.append("-" * 72)
        for i, r in enumerate(rows_to_print, 1):
            name_with_type = f"{r['名称']}({_pure_code(r['代码'])})"
            trend_str = r.get("中期趋势") or "-"
            lines.append(
                f"{i:<4}{name_with_type:<20}"
                f"{r['当日涨幅%']:>8.2f}"
                f"{_fmt(r.get('20日涨幅%')):>8}"
                f"{_fmt(r.get('60日涨幅%')):>8}"
                f"{_fmt(r.get('120日涨幅%')):>8}"
                f"{_fmt(r.get('主力净流入亿'), 1):>14}"
                f"{trend_str:>12}"
            )

    _append_rank_table(f"行业板块热度榜 Top{len(industry_top)} (按当日涨幅排序)", industry_top)
    _append_rank_table(f"概念板块热度榜 Top{len(concept_top)} (按当日涨幅排序)", concept_top)
    _append_rank_table(f"行业板块跌幅榜 Bottom{len(industry_bottom)} (按当日涨幅排序)", industry_bottom)
    _append_rank_table(f"概念板块跌幅榜 Bottom{len(concept_bottom)} (按当日涨幅排序)", concept_bottom)

    if key_block_analyses:
        lines.append("")
        lines.append("重点板块成分股验证:")
        for analysis in key_block_analyses:
            chg = analysis["当日涨幅%"]
            chg_str = f"{chg:+.2f}%" if chg is not None else "-"
            total = analysis["成分股数"]
            up = analysis["上涨家数"]
            down = analysis["下跌家数"]
            flat = analysis["平盘家数"]
            limit_up = analysis["涨停家数"]
            parts = [
                f"🔥 {analysis['名称']}({_pure_code(analysis.get('代码',''))})",
                chg_str,
                f"成分股{total}只",
                f"上涨{up}",
                f"下跌{down}",
                f"平盘{flat}",
            ]
            if limit_up > 0:
                parts.append(f"涨停{limit_up}")
            lines.append("  " + "  ".join(parts))
            leaders = "、".join(analysis.get("龙头候选名", [])[:3]) or "-"
            middles = "、".join(analysis.get("中军候选名", [])[:3]) or "-"
            limit_up_names = analysis.get("涨停股名", [])
            if limit_up_names:
                lu_str = "、".join(limit_up_names[:5]) + (f"等{len(limit_up_names)}只" if len(limit_up_names) > 5 else "")
            else:
                lu_str = "-"
            lines.append(f"     龙头: {leaders}    中军: {middles}    涨停股: {lu_str}")

    data_text = "\n".join(lines)

    system_prompt = """
你是一个A股方向预期分析助手。
你的任务是基于盘后结构化数据，生成次日盘前可用的方向预案。

硬性规则：
1. 你必须只输出合法 JSON。
2. 不允许输出 markdown。
3. 不允许输出代码块标记。
4. 不允许输出任何 JSON 之外的解释、前言、总结或备注。
5. 所有字段必须完整返回。
6. 如果某项没有内容，返回空数组 [] 或字符串 "无"。
7. 不要编造输入中没有出现的消息、政策或外部信息。
8. 重点是方向预期，不是个股推荐。
9. 要区分中期主线与短期轮动强点。
10. 要区分延续、观察、分歧、回避几种状态。
11. 如果输出不是合法 JSON，视为回答失败。
""".strip()

    prompt = f"""
请基于今天的盘后数据，生成一个“次日方向预期预案”。

任务背景：
- 当前是盘后场景。
- 目标是支持次日盘前准备。
- 输出应帮助用户判断：明天哪些方向优先看，哪些方向只观察，哪些方向需要回避。

判断原则：
1. 可以参考 style_verdict 作为市场背景，但不能让它覆盖板块本身证据。
2. 行业板块更接近配置层和中期资金偏好。
3. 概念板块更接近短线主题活跃和情绪层。
4. 要结合 20日/60日/120日涨幅、MA20/MA60 方向、距120日高点回撤、中期趋势标签，区分“中期主线”与“单日脉冲”。
5. 如果行业和概念同时指向同一个方向，可以适当提高强度判断。
6. 如果板块今天上涨，但成分股广度、涨停数、龙头/中军结构配合较弱，要谨慎处理。
7. 如果某个方向今天很强，但中期趋势偏弱，应更倾向归类为“轮动尝试”或“短期脉冲”，而不是直接定义为新主线。
8. 如果多个信号冲突，不要模糊带过，必须明确写出矛盾点。

请严格输出如下 JSON，不要输出任何其他文字：

{{
  "market_overview": {{
    "style_background": "",
    "structure": "",
    "summary": ""
  }},
  "mid_term_leaders": [
    {{
      "name": "",
      "status": "",
      "reason": "",
      "evidence": [
        ""
      ]
    }}
  ],
  "short_term_hotspots": [
    {{
      "name": "",
      "type": "",
      "reason": "",
      "evidence": [
        ""
      ]
    }}
  ],
  "next_day_focus": [
    {{
      "name": "",
      "status": "",
      "bias": "",
      "reason": "",
      "confirm_signals": [
        ""
      ],
      "risk_signals": [
        ""
      ]
    }}
  ],
  "avoid_directions": [
    {{
      "name": "",
      "reason": "",
      "risk_signals": [
        ""
      ]
    }}
  ],
  "conflicts": [
    {{
      "topic": "",
      "detail": ""
    }}
  ],
  "watchlist": [
    {{
      "item": "",
      "why": ""
    }}
  ]
}}

字段要求：
- 所有股票名、板块名、方向名，只要引用到具体对象，必须写成 `中文名(纯代码)` 格式，例如：`半导体(881121)`、`中国卫星(600118)`
- 不允许只写名字不写代码
- 不允许输出带 `.SH`、`.SZ`、`.BJ` 后缀的代码
- `market_overview.style_background`: 用中文概括市场风格背景
- `market_overview.structure`: 只能从这些值中选一个：`老主线轮动`、`主线切换尝试`、`混合轮动`、`防守主导`
- `mid_term_leaders[].name`: 必须为 `中文名(纯代码)` 格式
- `mid_term_leaders[].status`: 只能从这些值中选：`完整`、`动摇`、`走弱`
- `short_term_hotspots[].name`: 必须为 `中文名(纯代码)` 格式
- `short_term_hotspots[].type`: 只能从这些值中选：`轮动`、`主线延伸`、`可能新主线`
- `next_day_focus[].name`: 必须为 `中文名(纯代码)` 格式
- `next_day_focus[].status`: 只能从这些值中选：`主攻`、`观察`、`分歧`
- `next_day_focus[].bias`: 只能从这些值中选：`进攻`、`防守`、`轮动`、`修复`
- `avoid_directions[].name`: 必须为 `中文名(纯代码)` 格式
- `watchlist[].item`: 如果提到具体股票或板块，必须为 `中文名(纯代码)` 格式
- 每个 `reason` 尽量一句话说清
- `evidence`、`confirm_signals`、`risk_signals` 都用短句数组表达，不要写成长段

空值要求：
- 没有内容的数组返回 []
- 没有明显矛盾时，`conflicts` 返回 [{{"topic":"无","detail":"无"}}]
- 不要补充 schema 之外的字段

原始输入数据：
{data_text}
""".strip()
    return system_prompt, prompt


def main():
    use_llm = "--no-llm" not in sys.argv

    t0_total = time.perf_counter()

    t0 = time.perf_counter()
    tq.initialize(__file__)
    print(f">>> tq 初始化完成 [⏱ {time.perf_counter() - t0:.1f}s]")

    # =========================================================================
    # 第一部分：6大宽基指数
    # =========================================================================
    print("\n" + "=" * 72)
    print(">>> 1. 6大宽基指数")
    print("=" * 72)

    t0 = time.perf_counter()

    index_codes = [
        ("000001.SH", "上证指数", "沪市整体冷暖，被银行石油等大块头主导，比较迟钝"),
        ("399001.SZ", "深证成指", "深市整体，科技制造股多，比上证活跃"),
        ("399006.SZ", "创业板指", "成长股代表（新能源、医药、科技制造）"),
        ("000688.SH", "科创50", "硬科技代表（半导体、AI、芯片），弹性最大"),
        ("000300.SH", "沪深300", "大盘蓝筹，机构主战场，它强=资金抱团偏防御"),
        ("000852.SH", "中证1000", "小盘股，题材股土壤，它强=游资活跃风险偏好高"),
    ]
    codes = [c for c, _, _ in index_codes]

    res = tq.get_market_data(
        field_list=["Close", "Amount"],
        stock_list=codes,
        period="1d",
        count=N,
        dividend_type="none",
        fill_data=True,
    )
    close = res["Close"]
    amount = res["Amount"]

    asof = ""
    index_rows = []
    index_notes = {}  # code → 一句话解释
    for code, name, note in index_codes:
        index_notes[name] = note
        try:
            c = close[code].sort_index()
            a = amount[code].sort_index()
            last = float(c.iloc[-1])
            chg1 = pct(c.iloc[-1], c.iloc[-2])
            chg5 = pct(c.iloc[-1], c.iloc[-6]) if len(c) >= 6 else float("nan")
            chg20 = pct(c.iloc[-1], c.iloc[-21]) if len(c) >= 21 else float("nan")
            amt_yi = float(a.iloc[-1]) / 10000
            avg5_amt = sum(a.iloc[-6:-1]) / 5 if len(a) >= 6 else float("nan")
            vol_ratio = (a.iloc[-1] / avg5_amt) if avg5_amt else float("nan")

            index_rows.append({
                "名称": name,
                "收盘": round(last, 2),
                "当日%": round(chg1, 2),
                "5日%": round(chg5, 2) if chg5 == chg5 else None,
                "20日%": round(chg20, 2) if chg20 == chg20 else None,
                "成交额亿": round(amt_yi, 1),
                "量能比": round(vol_ratio, 2) if vol_ratio == vol_ratio else None,
            })
            asof = str(c.index[-1])[:10]
        except Exception as e:
            print(f"  {name}: 数据异常 {e}")

    header = f"{'指数':<12}{'收盘':>10}{'当日%':>8}{'5日%':>8}{'20日%':>8}{'成交额亿':>12}{'量能比':>8}"
    print(f"\n数据截止: {asof}\n")
    print("-" * 72)
    print(header)
    print("-" * 72)
    for r in index_rows:
        chg5_str = f"{r['5日%']:.2f}" if r['5日%'] is not None else "-"
        chg20_str = f"{r['20日%']:.2f}" if r['20日%'] is not None else "-"
        vol_str = f"{r['量能比']:.2f}" if r['量能比'] is not None else "-"
        note = index_notes.get(r["名称"], "")
        print(f"{r['名称']:<12}{r['收盘']:>10.2f}{r['当日%']:>8.2f}{chg5_str:>8}{chg20_str:>8}{r['成交额亿']:>12.1f}{vol_str:>8}  ← {note}")

    total_amt = sum(r["成交额亿"] for r in index_rows if r["成交额亿"])
    print(f"\n全市场总成交额: {total_amt:.1f} 亿")
    print(f"[⏱ 6大宽基指数耗时 {time.perf_counter() - t0:.1f}s]")

    # 第一部分+：市场广度 —— 涨跌家数 / 涨跌停统计
    # 当前为提速已停用；后续如果要恢复市场广度分析，再放开下面这整段逻辑。
    market_breadth = {}

    # =========================================================================
    # 第二部分：587个板块批量拉日K + 主力净流入
    # =========================================================================
    print("\n" + "=" * 72)
    print(">>> 2. 板块热度榜")
    print("=" * 72)

    t0 = time.perf_counter()

    block_type_map = load_block_type_map()
    blocks = tq.get_sector_list(list_type=1)  # 全部587个板块
    print(f"全部板块总数: {len(blocks)}")

    # 批量拉所有板块日K
    all_codes = [b["Code"] for b in blocks]
    res = tq.get_market_data(
        field_list=["Close", "Amount"],
        stock_list=all_codes,
        period="1d",
        count=N,
        dividend_type="none",
        fill_data=True,
    )
    close = res["Close"]
    amount = res["Amount"]
    print(f"日K数据拉取完成")

    # 计算每个板块的涨跌幅 + 主力净流入
    rows = []
    code_to_name = {b["Code"]: b["Name"] for b in blocks}
    fail = 0
    unknown_type_codes = []

    for code in all_codes:
        try:
            c = close[code].sort_index()
            a = amount[code].sort_index()
            if len(c) < 2:
                fail += 1
                continue

            last = float(c.iloc[-1])
            chg1 = pct(c.iloc[-1], c.iloc[-2])
            amt_yi = float(a.iloc[-1]) / 10000
            trend_summary = summarize_block_trend(c, a)

            block_type = get_block_type(code, block_type_map)
            if block_type == "unknown":
                unknown_type_codes.append(code)

            row = {
                "代码": code,
                "名称": code_to_name.get(code, code),
                "类型代码": block_type,
                "类型": get_block_type_display(code, block_type_map),
                "当日涨幅%": round(chg1, 2),
                "成交额亿": round(amt_yi, 1),
                "主力净流入亿": None,  # 先占位，排序后再补
            }
            row.update(trend_summary)
            rows.append(row)
        except Exception as e:
            fail += 1
            continue

    print(f"计算完成: 有效 {len(rows)} 个，数据不足 {fail} 个")
    type_counter = Counter(r["类型代码"] for r in rows)
    print("板块分类统计: " + ", ".join(f"{k}={type_counter[k]}" for k in sorted(type_counter)))
    if unknown_type_codes:
        print(f"未分类板块数: {len(unknown_type_codes)}")

    # 过滤：只保留行业和概念主题进入热度榜
    rows_filtered = [r for r in rows if r["类型代码"] in RANKABLE_BLOCK_TYPES]

    print(f"过滤后剩余: {len(rows_filtered)} 个")

    # =========================================================================
    # 第三部分：行业 / 概念板块榜单
    # =========================================================================
    industry_rows = [r for r in rows_filtered if r["类型代码"] == "industry"]
    concept_rows = [r for r in rows_filtered if r["类型代码"] == "theme"]
    industry_top = sorted(industry_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else -999, reverse=True)[:TOP_N]
    industry_bottom = sorted(industry_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else 999)[:TOP_N]
    concept_top = sorted(concept_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else -999, reverse=True)[:TOP_N]
    concept_bottom = sorted(concept_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else 999)[:TOP_N]

    ranked_blocks = industry_top + industry_bottom + concept_top + concept_bottom
    top_codes = {r["代码"] for r in ranked_blocks}
    print(f"\n上榜板块共 {len(top_codes)} 个，补拉主力净流入...")
    fund_map = {}
    for code in top_codes:
        try:
            fund = tq.get_more_info(stock_code=code, field_list=["Zjl_HB"])
            zjl_hb_str = fund.get("Zjl_HB")
            fund_map[code] = float(zjl_hb_str) / 10000 if zjl_hb_str and zjl_hb_str != "" else float("nan")
        except Exception:
            fund_map[code] = float("nan")

    for r in ranked_blocks:
        zjl = fund_map.get(r["代码"])
        r["主力净流入亿"] = round(zjl, 2) if zjl == zjl else None

    header = f"{'排名':<4}{'板块':<20}{'当日%':>8}{'20日%':>8}{'60日%':>8}{'120日%':>8}{'主力净流入亿':>14}{'中期趋势':>12}"

    def print_block_rank(title, rows_to_print):
        print("\n" + "=" * 72)
        print(title)
        print("=" * 72)
        print("-" * 72)
        print(header)
        print("-" * 72)
        for i, r in enumerate(rows_to_print, 1):
            chg20_str = f"{r['20日涨幅%']:.2f}" if r['20日涨幅%'] is not None else "-"
            chg60_str = f"{r['60日涨幅%']:.2f}" if r['60日涨幅%'] is not None else "-"
            chg120_str = f"{r['120日涨幅%']:.2f}" if r['120日涨幅%'] is not None else "-"
            net_str = f"{r['主力净流入亿']}" if r['主力净流入亿'] is not None else "-"
            pure_code = r["代码"].replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
            name_with_type = f"{r['名称']}({pure_code})"
            trend_str = r.get("中期趋势") or "-"
            print(f"{i:<4}{name_with_type:<20}{r['当日涨幅%']:>8.2f}{chg20_str:>8}{chg60_str:>8}{chg120_str:>8}{net_str:>14}{trend_str:>12}")

    print_block_rank(f">>> 🔥 行业板块热度榜 Top {TOP_N} (按当日涨幅排序)", industry_top)
    print_block_rank(f">>> 🚀 概念板块热度榜 Top {TOP_N} (按当日涨幅排序)", concept_top)
    print_block_rank(f">>> 📉 行业板块跌幅榜 Bottom {TOP_N} (按当日涨幅排序)", industry_bottom)
    print_block_rank(f">>> 📉 概念板块跌幅榜 Bottom {TOP_N} (按当日涨幅排序)", concept_bottom)

    key_blocks = pick_key_blocks(industry_top, concept_top)
    print(f"\n重点板块成分股验证: 计划分析 {len(key_blocks)} 个板块...")
    key_block_analyses = analyze_key_block_members(key_blocks)

    print("\n" + "=" * 72)
    print(">>> 3. 重点板块成分股验证")
    print("=" * 72)
    for analysis in key_block_analyses:
        code = analysis.get("代码", "")
        pure_code = code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        chg = analysis["当日涨幅%"]
        chg_str = f"{chg:+.2f}%" if chg is not None else "-"
        total = analysis["成分股数"]
        up = analysis["上涨家数"]
        down = analysis["下跌家数"]
        flat = analysis["平盘家数"]
        limit_up = analysis["涨停家数"]
        # 第1行：板块名(代码) + 涨幅 + 成分股 + 涨跌平 + 涨停
        parts = [
            f"🔥 {analysis['名称']}({pure_code})",
            chg_str,
            f"成分股{total}只",
            f"上涨{up}",
            f"下跌{down}",
            f"平盘{flat}",
        ]
        if limit_up > 0:
            parts.append(f"涨停{limit_up}")
        print(f"\n  {'  '.join(parts)}")
        # 第2行：龙头 + 中军 + 涨停股（纯名字）
        leaders = "、".join(analysis.get("龙头候选名", [])[:3]) or "-"
        middles = "、".join(analysis.get("中军候选名", [])[:3]) or "-"
        limit_up_names = analysis.get("涨停股名", [])
        if limit_up_names:
            if len(limit_up_names) <= 5:
                lu_str = "、".join(limit_up_names)
            else:
                lu_str = "、".join(limit_up_names[:5]) + f"等{len(limit_up_names)}只"
        else:
            lu_str = "-"
        print(f"     龙头: {leaders}    中军: {middles}    涨停股: {lu_str}")

    print(f"\n[⏱ 板块热度榜耗时 {time.perf_counter() - t0:.1f}s]")

    # 旧版 LLM 大白话解读逻辑已停用，后续如需对比可恢复：
    # verdict = judge_dashboard_with_llm(asof, index_rows, industry_top, concept_top, market_breadth)

    style_verdict = None

    # =========================================================================
    # 第五部分：LLM 风格判断（只看6大指数）
    # =========================================================================
    if use_llm:
        print("\n" + "=" * 72)
        print(">>> 🤖 LLM 最近市场风格判断")
        print("=" * 72)
        t0 = time.perf_counter()
        style_verdict = judge_style_with_llm(asof, index_rows)
        if style_verdict.get("状态"):
            print(f"[{style_verdict.get('状态')}] {style_verdict.get('原因') or style_verdict.get('原始返回','')}")
        else:
            print(json.dumps(style_verdict, ensure_ascii=False, indent=2))
        print(f"[⏱ LLM风格判断耗时 {time.perf_counter() - t0:.1f}s]")

        print("\n" + "=" * 72)
        print(">>> 🤖 LLM 市场资金意图判断")
        print("=" * 72)
        t0 = time.perf_counter()
        intent_verdict = judge_sector_intent_with_llm(
            asof,
            style_verdict if isinstance(style_verdict, dict) else {},
            industry_top,
            industry_bottom,
            concept_top,
            concept_bottom,
            key_block_analyses,
        )
        if intent_verdict.get("content"):
            print(intent_verdict["content"])
        elif intent_verdict.get("status"):
            print(f"[{intent_verdict.get('status')}] {intent_verdict.get('reason') or intent_verdict.get('raw_response','')}")
        else:
            print("(LLM 返回为空)")
        print(f"[⏱ LLM资金意图判断耗时 {time.perf_counter() - t0:.1f}s]")
    else:
        print("\n(已用 --no-llm 跳过 LLM 风格判断)")

        # 打印构建好的全量 prompt（不实际调用 LLM）
        system_prompt, prompt = build_sector_intent_prompt(
            asof,
            {},
            industry_top,
            industry_bottom,
            concept_top,
            concept_bottom,
            key_block_analyses,
        )
        print("\n" + "=" * 72)
        print(">>> 📋 板块方向预案 Prompt（未调用 LLM）")
        print("=" * 72)
        print("\n--- SYSTEM ---")
        print(system_prompt)
        print("\n--- USER ---")
        print(prompt)

    print("\n" + "=" * 72)
    print(f">>> 完成 [总耗时 {time.perf_counter() - t0_total:.1f}s]")
    print("=" * 72)

    try:
        tq.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
