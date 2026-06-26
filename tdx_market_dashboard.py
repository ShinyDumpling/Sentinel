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
from collections import Counter
from pathlib import Path

from tqcenter import tq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

N = 25  # 拉最近25根日K，保证能算20日涨幅
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


def pct(a, b):
    try:
        return (float(a) / float(b) - 1) * 100
    except (ZeroDivisionError, TypeError, ValueError):
        return float("nan")


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
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "你是一个克制、说大白话、面向A股新手的大盘解读助手。"},
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

你的任务不是解读今天涨跌，不是分析板块，不是分析资金抱团方向，也不是给交易建议。
你只需要根据6大宽基指数的表现，判断最近市场主风格偏向哪里。

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

请重点参考“5日%”和“20日%”的相对强弱关系，“当日%”和“量能比”只作为辅助判断。

请按以下原则判断：
- 如果科创50、创业板指明显强于沪深300、上证指数，优先判断为“科技成长”
- 如果中证1000明显强于沪深300，优先判断为“小盘题材”
- 如果沪深300、上证指数明显强于创业板指、科创50，优先判断为“权重蓝筹”
- 如果6大指数多数同步走强，可判断为“大盘普涨”
- 如果强弱关系分裂、轮动较快，可判断为“混合轮动”
- 如果没有清晰优势方向，可判断为“无明显主线”

注意：
- 不要引用板块、个股、消息面、政策面
- 不要补充输入中没有提供的信息
- 只能依据这6大指数的数据做判断
- 输出要克制、直接、少空话

请输出如下 JSON：
{{
  "最近主风格": "科技成长/小盘题材/权重蓝筹/大盘普涨/混合轮动/无明显主线",
  "风格强度": "强/中/弱",
  "指数依据": [
    "一句话说明主要依据1",
    "一句话说明主要依据2"
  ],
  "一句话结论": "一句大白话总结最近市场风格，20字以内"
}}

【原始数据】
{data_text}
"""

    client = OpenAI(api_key=s["api_key"], base_url=s["base_url"])
    resp = client.chat.completions.create(
        model=s["model"],
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "你是一个克制、直接、只根据指数数据判断A股风格的分析助手。"},
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


def main():
    use_llm = "--no-llm" not in sys.argv

    tq.initialize(__file__)
    print(">>> tq 初始化完成")

    # =========================================================================
    # 第一部分：6大宽基指数
    # =========================================================================
    print("\n" + "=" * 72)
    print(">>> 1. 6大宽基指数")
    print("=" * 72)

    index_codes = [
        ("000001.SH", "上证指数"),
        ("399001.SZ", "深证成指"),
        ("399006.SZ", "创业板指"),
        ("000688.SH", "科创50"),
        ("000300.SH", "沪深300"),
        ("000852.SH", "中证1000"),
    ]
    codes = [c for c, _ in index_codes]

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
    for code, name in index_codes:
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
        print(f"{r['名称']:<12}{r['收盘']:>10.2f}{r['当日%']:>8.2f}{chg5_str:>8}{chg20_str:>8}{r['成交额亿']:>12.1f}{vol_str:>8}")

    total_amt = sum(r["成交额亿"] for r in index_rows if r["成交额亿"])
    print(f"\n全市场总成交额: {total_amt:.1f} 亿")

    # 第一部分+：市场广度 —— 涨跌家数 / 涨跌停统计
    # 当前为提速已停用；后续如果要恢复市场广度分析，再放开下面这整段逻辑。
    market_breadth = {}

    # =========================================================================
    # 第二部分：587个板块批量拉日K + 主力净流入
    # =========================================================================
    print("\n" + "=" * 72)
    print(">>> 2. 板块热度榜 (计算中...)")
    print("=" * 72)

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
            chg20 = pct(c.iloc[-1], c.iloc[-21]) if len(c) >= 21 else float("nan")
            amt_yi = float(a.iloc[-1]) / 10000

            # 主力净流入
            fund = tq.get_more_info(stock_code=code, field_list=["Zjl_HB"])
            zjl_hb_str = fund.get("Zjl_HB")
            zjl_hb = float(zjl_hb_str) / 10000 if zjl_hb_str and zjl_hb_str != "" else float("nan")
            block_type = get_block_type(code, block_type_map)
            if block_type == "unknown":
                unknown_type_codes.append(code)

            rows.append({
                "代码": code,
                "名称": code_to_name.get(code, code),
                "类型代码": block_type,
                "类型": get_block_type_label(block_type),
                "当日涨幅%": round(chg1, 2),
                "20日涨幅%": round(chg20, 2) if chg20 == chg20 else None,
                "成交额亿": round(amt_yi, 1),
                "主力净流入亿": round(zjl_hb, 2) if zjl_hb == zjl_hb else None,
            })
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
    # 第三部分：行业板块热度榜 Top15
    # =========================================================================
    industry_rows = [r for r in rows_filtered if r["类型代码"] == "industry"]
    industry_top = sorted(industry_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else -999, reverse=True)[:TOP_N]

    print("\n" + "=" * 72)
    print(f">>> 🔥 行业板块热度榜 Top {TOP_N} (按当日涨幅排序)")
    print("=" * 72)
    header = f"{'排名':<4}{'板块':<20}{'当日%':>8}{'20日%':>8}{'主力净流入亿':>14}"
    print("-" * 72)
    print(header)
    print("-" * 72)
    for i, r in enumerate(industry_top, 1):
        chg20_str = f"{r['20日涨幅%']:.2f}" if r['20日涨幅%'] is not None else "-"
        net_str = f"{r['主力净流入亿']}" if r['主力净流入亿'] is not None else "-"
        name_with_type = f"{r['名称']}{r['类型']}"
        print(f"{i:<4}{name_with_type:<20}{r['当日涨幅%']:>8.2f}{chg20_str:>8}{net_str:>14}")

    # =========================================================================
    # 第四部分：概念板块热度榜 Top15
    # =========================================================================
    concept_rows = [r for r in rows_filtered if r["类型代码"] == "theme"]
    concept_top = sorted(concept_rows, key=lambda r: r["当日涨幅%"] if r["当日涨幅%"] is not None else -999, reverse=True)[:TOP_N]

    print("\n" + "=" * 72)
    print(f">>> 🚀 概念板块热度榜 Top {TOP_N} (按当日涨幅排序)")
    print("=" * 72)
    print("-" * 72)
    print(header)
    print("-" * 72)
    for i, r in enumerate(concept_top, 1):
        chg20_str = f"{r['20日涨幅%']:.2f}" if r['20日涨幅%'] is not None else "-"
        net_str = f"{r['主力净流入亿']}" if r['主力净流入亿'] is not None else "-"
        name_with_type = f"{r['名称']}{r['类型']}"
        print(f"{i:<4}{name_with_type:<20}{r['当日涨幅%']:>8.2f}{chg20_str:>8}{net_str:>14}")

    # 旧版 LLM 大白话解读逻辑已停用，后续如需对比可恢复：
    # verdict = judge_dashboard_with_llm(asof, index_rows, industry_top, concept_top, market_breadth)

    # =========================================================================
    # 第五部分：LLM 风格判断（只看6大指数）
    # =========================================================================
    if use_llm:
        print("\n" + "=" * 72)
        print(">>> 🤖 LLM 最近市场风格判断")
        print("=" * 72)
        verdict = judge_style_with_llm(asof, index_rows)
        if verdict.get("状态"):
            print(f"[{verdict.get('状态')}] {verdict.get('原因') or verdict.get('原始返回','')}")
        else:
            print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print("\n(已用 --no-llm 跳过 LLM 风格判断)")

    print("\n" + "=" * 72)
    print(">>> 完成")
    print("=" * 72)

    try:
        tq.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
