#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DSA + TdxQuant 盯盘 Demo（最小闭环版）

当前能力：
1. 从 DSA 报告中自动解析股票池
2. 通过本地 tqcenter.py 获取股票实时数据
3. 将整份 DSA 报告全文、当前股票实时数据、当前持仓上下文一起交给 LLM
4. 输出固定结构的 JSON 判断结果
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from openai import OpenAI
from py_mini_racer import MiniRacer


TQCENTER_DIR = Path(
    r"D:\【指标100】通达信《专业研究版》773\【指标100】通达信《专业研究版》773\PYPlugins\user"
)
TQCENTER_FILE = TQCENTER_DIR / "tqcenter.py"
DEFAULT_REPORT_PATH = Path(r"D:\股神养成plan\daily_stock_analysis\reports\report_20260629.md")

DEFAULT_MARKET_REPORT_PATH = Path(r"D:\股神养成plan\daily_stock_analysis\reports\market_review_20260629.md")

CYQ_JS_CODE = r"""
// @ts-nocheck
function CYQCalculator(index, klinedata) {
    var maxprice = 0;
    var minprice = 0;
    var factor = 150;
    var start = this.range ? Math.max(0, index - this.range + 1) : 0;
    var kdata = klinedata.slice(start, Math.max(1, index + 1));
    if (kdata.length === 0) throw 'invaild index';
    for (var i = 0; i < kdata.length; i++) {
        var elements = kdata[i];
        maxprice = !maxprice ? elements.high : Math.max(maxprice, elements.high);
        minprice = !minprice ? elements.low : Math.min(minprice, elements.low);
    }

    var accuracy = Math.max(0.01, (maxprice - minprice) / (factor - 1));
    var yrange = [];
    for (var i = 0; i < factor; i++) {
        yrange.push((minprice + accuracy * i).toFixed(2) / 1);
    }
    var xdata = createNumberArray(factor);

    for (var i = 0; i < kdata.length; i++) {
        var eles = kdata[i];
        var open = eles.open,
            close = eles.close,
            high = eles.high,
            low = eles.low,
            avg = (open + close + high + low) / 4,
            turnoverRate = Math.min(1, eles.hsl / 100 || 0);

        var H = Math.floor((high - minprice) / accuracy),
            L = Math.ceil((low - minprice) / accuracy),
            GPoint = [high == low ? factor - 1 : 2 / (high - low), Math.floor((avg - minprice) / accuracy)];
        for (var n = 0; n < xdata.length; n++) {
            xdata[n] *= (1 - turnoverRate);
        }

        if (high == low) {
            xdata[GPoint[1]] += GPoint[0] * turnoverRate / 2;
        } else {
            for (var j = L; j <= H; j++) {
                var curprice = minprice + accuracy * j;
                if (curprice <= avg) {
                    if (Math.abs(avg - low) < 1e-8) {
                        xdata[j] += GPoint[0] * turnoverRate;
                    } else {
                        xdata[j] += (curprice - low) / (avg - low) * GPoint[0] * turnoverRate;
                    }
                } else {
                    if (Math.abs(high - avg) < 1e-8) {
                        xdata[j] += GPoint[0] * turnoverRate;
                    } else {
                        xdata[j] += (high - curprice) / (high - avg) * GPoint[0] * turnoverRate;
                    }
                }
            }
        }
    }

    var currentprice = klinedata[index].close;
    var totalChips = 0;
    for (var i = 0; i < factor; i++) {
        var x = xdata[i].toPrecision(12) / 1;
        totalChips += x;
    }
    var result = new CYQData();
    result.x = xdata;
    result.y = yrange;
    result.benefitPart = result.getBenefitPart(currentprice);
    result.avgCost = getCostByChip(totalChips * 0.5).toFixed(2);
    result.percentChips = {
        '90': result.computePercentChips(0.9),
        '70': result.computePercentChips(0.7)
    };
    return result;

    function getCostByChip(chip) {
        var result = 0, sum = 0;
        for (var i = 0; i < factor; i++) {
            var x = xdata[i].toPrecision(12) / 1;
            if (sum + x > chip) {
                result = minprice + i * accuracy;
                break;
            }
            sum += x;
        }
        return result;
    }

    function CYQData() {
        this.x = arguments[0];
        this.y = arguments[1];
        this.benefitPart = arguments[2];
        this.avgCost = arguments[3];
        this.percentChips = arguments[4];
        this.computePercentChips = function (percent) {
            if (percent > 1 || percent < 0) throw 'argument "percent" out of range';
            var ps = [(1 - percent) / 2, (1 + percent) / 2];
            var pr = [getCostByChip(totalChips * ps[0]), getCostByChip(totalChips * ps[1])];
            return {
                priceRange: [pr[0].toFixed(2), pr[1].toFixed(2)],
                concentration: pr[0] + pr[1] === 0 ? 0 : (pr[1] - pr[0]) / (pr[0] + pr[1])
            };
        };
        this.getBenefitPart = function (price) {
            var below = 0;
            for (var i = 0; i < factor; i++) {
                var x = xdata[i].toPrecision(12) / 1;
                if (price >= minprice + i * accuracy) {
                    below += x;
                }
            }
            return totalChips == 0 ? 0 : below / totalChips;
        };
    }
}

function createNumberArray(count) {
    var array = [];
    for (var i = 0; i < count; i++) {
        array.push(0);
    }
    return array;
}
"""

POSITION_CONTEXT: Dict[str, Dict[str, Any]] = {
    "002668.SZ": {
        "是否持仓": True,
        "持仓数量": 200,
        "持仓成本": 10.225,
        "当前持仓市值": 10255,
        "计划最大仓位": 0.4,
        "当前仓位": 0.2,
        "建仓阶段": "首仓",
    }
}

RECENT_DAILY_COUNT = 120

SNAPSHOT_FIELDS = [
    "Now",
    "LastClose",
    "Open",
    "Max",
    "Min",
    "Volume",
    "NowVol",
    "Amount",
    "Average",
    "Zangsu",
    "Before5MinNow",
    "Buyp",
    "Buyv",
    "Sellp",
    "Sellv",
    "Inside",
    "Outside",
]

MORE_INFO_FIELDS = [
    "ZAF",
    "fHSL",
    "fLianB",
    "MA5Value",
    "DynaPE",
    "PB_MRQ",
    "Zsz",
    "Ltsz",
    "Zjl_HB",
    "Zjl",
    "TotalBVol",
    "TotalSVol",
    "Wtb",
    "ZTPrice",
    "DTPrice",
    "FCAmo",
    "EverZTCount",
    "HisHigh",
    "HisLow",
    "HqDate",
]

STOCK_INFO_FIELDS = [
    "Name",
    "ActiveCapital",
    "J_zgb",
    "rs_hyname",
    "tdx_dyname",
]

DAILY_FIELDS = ["Open", "High", "Low", "Close", "Volume", "Amount"]

BAIDU_SENTIMENT_URL = "https://finance.pae.baidu.com/vapi/sentimentlist"
DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"

TODO_ITEMS = {
    "DSA策略要怎么改_是否有必要结构化": [
        "当前 demo 直接把整份 DSA 报告全文放进 prompt，优点是快，缺点是约束弱。",
        "后续建议评估是否改成双层输入：1) DSA 原始报告全文；2) 从报告里提炼出的结构化关键条件。",
        "如果后续触发判断需要更稳定、更少漂移，结构化大概率是有必要的，但第一版可以先保留自然语言策略。",
    ],
    "LLM_APIKey的安全": [
        "当前 demo 只从环境变量读取 API Key，便于快速打通。",
        "后续建议把本 demo 独立出自己的 .env，避免与其他项目配置耦合。",
        "后续建议不要把 API Key 写死在脚本里，也不要把包含密钥的文件提交到 Git。",
        "后续如要长期使用，可增加显式的环境变量检查和脱敏日志输出。",
    ],
    "快照保留机制": [
        "当前 demo 只做单次抓取，没有保留历史快照。",
        "后续建议增加本地快照缓存，至少保留最近 N 轮轮询结果。",
        "建议快照至少包含：抓取时间、股票代码、盯盘数据、LLM判断、是否触发事件。",
        "后续可以按日期分文件或写入 sqlite/jsonl，便于复盘和排查漏报。",
    ],
    "防漏框架建议": [
        "不要只依赖单次 LLM 判断，建议加入硬规则哨兵层。",
        "建议保留最近多轮快照，避免只看单帧导致漏掉盘中过程型信号。",
        "建议引入状态机：未触发 -> 候选触发 -> 已确认 -> 已通知 -> 冷却中。",
        "建议对持仓股提高轮询频率，对观察股使用较低频率，形成分层盯盘。",
        "建议买点类信号允许二次确认，止损/风险类信号允许一轮直接报警。",
    ],
    "盘前_盘中_盘后行为思考": [
        "盘前：如果最新价、成交量、量比等关键盘中字段为 0 或明显无效，不应直接给出是否触发买卖点的结论，而应输出“待开盘确认”状态或 no_action，并明确下一观察条件。",
        "盘前：可以继续使用前一日 DSA 报告作为策略底稿，但盘前阶段更适合输出“开盘后重点观察什么”，而不是判定已经触发。",
        "盘中：这是主判断阶段，应以实时价格、量比、换手、盘口、120 日日线衍生指标等为核心，结合 DSA 策略判断是否真正触发买点、卖点或风险提示。",
        "盘中：如果关键字段缺失但不是全部缺失，应在输出中明确数据限制，避免把低质量数据误判成未触发。",
        "盘后：更适合做复盘、记录触发历史、更新下一交易日策略，不应再把盘后结果当成盘中执行信号。",
        "后续可考虑给不同时间段单独设计模式：premarket / intraday / postmarket，让 LLM 和规则层按时段切换不同判断标准。",
        "后续可考虑先由程序预判当前市场阶段，再决定是否调用 LLM；例如盘前仅生成观察清单，盘中才做触发判断，盘后只做总结与归档。",
    ],
    "持仓上下文": [
        "当前 demo 已补入最小持仓上下文，但仍是写死数据，后续需要改成从真实持仓源读取。",
        "持仓场景下，盯盘判断重点应从“能不能买”切到“持有、加仓、减仓、止盈、止损、风险提示”。",
        "后续可扩展动作枚举，例如 hold / add_position / trim_position / stop_loss / take_profit，以减少 watch_sell / risk_alert 语义过粗的问题。",
    ],
    "预期分析与监控执行拆分": [
        "当股票数量变多时，当前脚本会为每只股票同时做分析和监控判断，输出结果容易过多、过乱，后续需要先整理清楚边界。",
        "需要明确哪些内容属于盘前/策略层的‘预期分析’，哪些内容属于盘中/执行层的‘监控判断’，避免在监控脚本里混入过多重新分析。",
        "后续建议把流程拆成两段：先生成精简、稳定的预期与观察点，再由监控脚本只围绕这些预期做触发检查、风险提示和状态更新。",
        "这个脚本整体定位应是监控脚本，因此输出应优先服务‘是否触发、为什么触发、下一步看什么’，而不是对每只股票重复展开完整分析。",
        "如果股票池较大，后续还需要设计结果分层与聚合方式，例如区分‘已触发 / 候选 / 继续观察 / 可忽略’，减少盯盘噪音。",
    ],
    "板块强度识别与轮动状态机": [
        "板块综合强度的定时计算已有其他脚本负责，watch_dog 当前暂不重复实现，只预留接收外部板块结果的接口。",
        "板块主线确认、竞争方向、切换迟滞等轮动状态机当前暂不实现，后续在板块信号源和切换规则稳定后再接入。",
        "当前阶段优先支持人工输入具体板块或上位方向，并在展开出的板块范围内完成股票筛选。",
    ],
}


def _ensure_tqcenter_importable() -> None:
    if not TQCENTER_FILE.exists():
        raise FileNotFoundError(f"tqcenter.py not found: {TQCENTER_FILE}")
    tqcenter_dir = str(TQCENTER_DIR)
    if tqcenter_dir not in sys.path:
        sys.path.insert(0, tqcenter_dir)


def _read_report_text(report_path: Path) -> str:
    if not report_path.exists():
        raise FileNotFoundError(f"DSA report not found: {report_path}")
    return report_path.read_text(encoding="utf-8")


def _normalize_a_share_code(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    if len(digits) != 6:
        raise ValueError(f"Unsupported stock code in report: {code}")
    suffix = ".SH" if digits.startswith(("5", "6", "9")) else ".SZ"
    return f"{digits}{suffix}"


def _extract_watchlist_from_report(report_text: str) -> List[str]:
    codes = re.findall(r"\((\d{6})\)", report_text)
    watchlist: List[str] = []
    for code in codes:
        normalized = _normalize_a_share_code(code)
        if normalized not in watchlist:
            watchlist.append(normalized)
    if not watchlist:
        raise ValueError("No A-share stock codes found in DSA report.")
    return watchlist


def _position_for_stock(code: str) -> Dict[str, Any]:
    return POSITION_CONTEXT.get(
        code,
        {
            "是否持仓": False,
            "持仓数量": 0,
            "持仓成本": None,
            "当前持仓市值": 0,
            "计划最大仓位": None,
            "当前仓位": 0,
            "建仓阶段": "未建仓",
        },
    )


def _get_llm_settings() -> Dict[str, Optional[str]]:
    api_key = os.getenv("ARK_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("ARK_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_LLM_MODEL
    base_url = os.getenv("ARK_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_LLM_BASE_URL
    if model and "/" in model and not model.startswith("ark-"):
        model = model.split("/", 1)[1]
    return {
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
    }


def _to_float(value: Any) -> Any:
    if value in (None, "", "None", "null", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _to_int(value: Any) -> Optional[int]:
    numeric = _to_float(value)
    if numeric is None:
        return None
    try:
        return int(float(numeric))
    except (TypeError, ValueError):
        return None


def _normalize_level_values(value: Any) -> List[Any]:
    if isinstance(value, list):
        return [_to_float(item) for item in value]
    if value in (None, ""):
        return []
    return [_to_float(value)]


def _todo(message: str) -> Dict[str, str]:
    return {"状态": "TODO", "说明": message}


def _baidu_benefit_label(value: Any) -> str:
    mapping = {
        "1": "利好",
        "0": "中性",
        "-1": "利空",
    }
    return mapping.get(str(value), "未知")


def _normalize_publish_time(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        ts = int(str(value))
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _fetch_baidu_sentiment_list(code: str) -> Dict[str, Any]:
    normalized_code = str(code or "").split(".")[0].strip()
    params = {
        "code": normalized_code,
        "market": "ab",
        "financeType": "stock",
    }
    try:
        response = requests.get(
            BAIDU_SENTIMENT_URL,
            params=params,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://finance.baidu.com/",
            },
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "状态": "unavailable",
            "说明": f"百度财经舆情接口请求失败: {exc}",
        }

    if response.status_code != 200:
        return {
            "状态": "unavailable",
            "说明": f"百度财经舆情接口返回异常状态码: {response.status_code}",
        }

    if str(payload.get("ResultCode")) != "0":
        return {
            "状态": "unavailable",
            "说明": f"百度财经舆情接口 ResultCode 异常: {payload.get('ResultCode')}",
        }

    result = payload.get("Result") or []
    root = result[0] if isinstance(result, list) and result else {}
    info = (
        (root.get("TplData") or {})
        .get("aiSentimentXcxListInfo", {})
    )
    sentiment_items = info.get("sentimentListInfo") or []

    normalized_items: List[Dict[str, Any]] = []
    stats = {"利好条数": 0, "中性条数": 0, "利空条数": 0, "未知条数": 0}

    for item in sentiment_items:
        benefit_type = str(item.get("benefitType"))
        label = _baidu_benefit_label(benefit_type)
        if label == "利好":
            stats["利好条数"] += 1
        elif label == "中性":
            stats["中性条数"] += 1
        elif label == "利空":
            stats["利空条数"] += 1
        else:
            stats["未知条数"] += 1

        normalized_items.append(
            {
                "标题": item.get("title"),
                "摘要": item.get("abstract"),
                "舆情方向": label,
                "舆情方向值": _to_int(benefit_type),
                "来源": item.get("provider"),
                "发布时间": _normalize_publish_time(item.get("publishTime")),
                "原文链接": item.get("originUrl"),
                "消息类型": item.get("messageType"),
            }
        )

    return {
        "状态": "ok",
        "数据源": "百度财经 sentimentlist",
        "股票代码": normalized_code,
        "舆情条数": len(normalized_items),
        "舆情统计": stats,
        "舆情列表": normalized_items,
    }


def _compute_ma(daily_data: Dict[str, pd.DataFrame], code: str, window: int) -> Optional[float]:
    close_df = daily_data.get("Close")
    if close_df is None or code not in close_df.columns or close_df.empty:
        return None
    series = pd.to_numeric(close_df[code], errors="coerce").dropna()
    if len(series) < window:
        return None
    return round(float(series.tail(window).mean()), 4)


def _compute_return(
    daily_data: Dict[str, pd.DataFrame], code: str, current_price: Any, lookback: int
) -> Optional[float]:
    close_df = daily_data.get("Close")
    if close_df is None or code not in close_df.columns or close_df.empty:
        return None
    series = pd.to_numeric(close_df[code], errors="coerce").dropna()
    if len(series) < lookback:
        return None
    base_price = _to_float(series.iloc[-lookback])
    now_price = _to_float(current_price)
    if base_price in (None, 0) or now_price is None:
        return None
    return round((now_price / base_price - 1.0) * 100.0, 4)


def _compute_prev_high_low(
    daily_data: Dict[str, pd.DataFrame], code: str, window: int = 20
) -> Dict[str, Optional[float]]:
    high_df = daily_data.get("High")
    low_df = daily_data.get("Low")
    prev_high = None
    prev_low = None

    if high_df is not None and code in high_df.columns:
        series = pd.to_numeric(high_df[code], errors="coerce").dropna()
        if len(series) >= window:
            prev_high = round(float(series.tail(window).max()), 4)

    if low_df is not None and code in low_df.columns:
        series = pd.to_numeric(low_df[code], errors="coerce").dropna()
        if len(series) >= window:
            prev_low = round(float(series.tail(window).min()), 4)

    return {"前高": prev_high, "前低": prev_low}


def _normalize_trade_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:8]
    return None


def _get_daily_series(
    daily_data: Dict[str, pd.DataFrame], field: str, code: str
) -> pd.Series:
    df = daily_data.get(field)
    if df is None or code not in df.columns or df.empty:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(df[code], errors="coerce")
    series.index = [_normalize_trade_date(idx) for idx in series.index]
    series = series[series.index.notna()]
    series = series[~series.index.duplicated(keep="last")]
    return series.dropna()


def _fetch_gb_history(
    tq: Any, code: str, daily_data: Dict[str, pd.DataFrame]
) -> List[Dict[str, Any]]:
    close_series = _get_daily_series(daily_data, "Close", code)
    if close_series.empty:
        return []

    start_date = str(close_series.index.min())
    end_date = str(close_series.index.max())
    try:
        result = tq.get_gb_info_by_date(
            stock_code=code,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception:
        return []

    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        if isinstance(result.get("Value"), list):
            return [item for item in result["Value"] if isinstance(item, dict)]
        return [result]
    return []


def _pick_effective_float_capital(record: Dict[str, Any]) -> Optional[float]:
    candidate_keys = [
        "ActiveCapital",
        "activecapital",
        "Ltgb",
        "ltgb",
        "Ltg",
        "LTG",
        "ltg",
        "liutongguben",
        "娴侀€氳偂鏈?",
        "娴侀€氳偂",
        "娴侀€氳偂鏈暟",
    ]
    for key in candidate_keys:
        value = _to_float(record.get(key))
        if isinstance(value, (int, float)) and value and value > 0:
            return float(value)
    return None


def _pick_effective_date(record: Dict[str, Any]) -> Optional[str]:
    candidate_keys = [
        "Date",
        "date",
        "Rq",
        "RQ",
        "EndDate",
        "end_date",
        "StartDate",
        "start_date",
        "GQDJR",
        "BGRQ",
    ]
    for key in candidate_keys:
        normalized = _normalize_trade_date(record.get(key))
        if normalized:
            return normalized
    return None


def _infer_turnover_scale(
    daily_data: Dict[str, pd.DataFrame],
    code: str,
    current_turnover_rate: Any,
    reference_float_capital: Any,
    snapshot_volume: Any,
) -> float:
    turnover = _to_float(current_turnover_rate)
    float_capital = _to_float(reference_float_capital)
    if not isinstance(turnover, (int, float)) or turnover <= 0:
        return 100.0
    if not isinstance(float_capital, (int, float)) or float_capital <= 0:
        return 100.0

    volume_series = _get_daily_series(daily_data, "Volume", code)
    latest_daily_volume = _to_float(volume_series.iloc[-1]) if not volume_series.empty else None

    for candidate in (latest_daily_volume, _to_float(snapshot_volume)):
        if not isinstance(candidate, (int, float)) or candidate <= 0:
            continue
        raw_ratio = float(candidate) / float(float_capital)
        if raw_ratio <= 0:
            continue
        scale = float(turnover) / raw_ratio
        if 0.000001 < scale < 1000000:
            return float(scale)
    return 100.0


def _compute_daily_turnover_history(
    daily_data: Dict[str, pd.DataFrame],
    code: str,
    gb_history: List[Dict[str, Any]],
    current_float_capital: Any,
    current_turnover_rate: Any,
    snapshot_volume: Any,
) -> Dict[str, Any]:
    volume_series = _get_daily_series(daily_data, "Volume", code)
    if volume_series.empty:
        return {
            "daily_turnover_history": [],
            "daily_turnover_meta": {"error": "missing_daily_volume"},
        }

    records: List[Dict[str, Any]] = []
    for item in gb_history:
        effective_date = _pick_effective_date(item)
        float_capital = _pick_effective_float_capital(item)
        if effective_date and float_capital:
            records.append({"date": effective_date, "float_capital": float_capital})
    records.sort(key=lambda item: item["date"])

    fallback_float_capital = _to_float(current_float_capital)
    reference_float_capital = records[-1]["float_capital"] if records else fallback_float_capital
    scale = _infer_turnover_scale(
        daily_data=daily_data,
        code=code,
        current_turnover_rate=current_turnover_rate,
        reference_float_capital=reference_float_capital,
        snapshot_volume=snapshot_volume,
    )

    history: List[Dict[str, Any]] = []
    record_idx = 0
    active_float_capital = fallback_float_capital

    for trade_date, volume in volume_series.items():
        while record_idx < len(records) and records[record_idx]["date"] <= trade_date:
            active_float_capital = records[record_idx]["float_capital"]
            record_idx += 1

        turnover_rate = None
        if (
            isinstance(volume, (int, float))
            and volume >= 0
            and isinstance(active_float_capital, (int, float))
            and active_float_capital > 0
        ):
            turnover_rate = round(float(volume) / float(active_float_capital) * scale, 4)

        history.append(
            {
                "date": trade_date,
                "volume": round(float(volume), 4) if pd.notna(volume) else None,
                "float_capital": round(float(active_float_capital), 4)
                if isinstance(active_float_capital, (int, float))
                else None,
                "turnover_rate": turnover_rate,
            }
        )

    return {
        "daily_turnover_history": history,
        "daily_turnover_meta": {
            "formula": "turnover_rate = volume / float_capital * scale",
            "scale": round(scale, 6),
            "gb_record_count": len(records),
            "fallback_float_capital": fallback_float_capital,
        },
    }


def _chip_status_from_concentration(concentration_90: Optional[float]) -> str:
    if concentration_90 is None:
        return "未知"
    if concentration_90 < 0.08:
        return "高度集中"
    if concentration_90 < 0.15:
        return "较集中"
    if concentration_90 < 0.25:
        return "中等"
    return "较分散"


def _build_cyq_kline_records(
    daily_data: Dict[str, pd.DataFrame],
    code: str,
    daily_turnover_history: Dict[str, Any],
) -> List[Dict[str, Any]]:
    open_series = _get_daily_series(daily_data, "Open", code)
    high_series = _get_daily_series(daily_data, "High", code)
    low_series = _get_daily_series(daily_data, "Low", code)
    close_series = _get_daily_series(daily_data, "Close", code)
    volume_series = _get_daily_series(daily_data, "Volume", code)
    amount_series = _get_daily_series(daily_data, "Amount", code)

    turnover_map = {
        item.get("date"): item.get("turnover_rate")
        for item in daily_turnover_history.get("daily_turnover_history", [])
        if isinstance(item, dict) and item.get("date")
    }

    common_dates = [
        trade_date
        for trade_date in close_series.index.tolist()
        if trade_date in open_series.index
        and trade_date in high_series.index
        and trade_date in low_series.index
        and trade_date in volume_series.index
        and trade_date in amount_series.index
        and trade_date in turnover_map
    ]

    records: List[Dict[str, Any]] = []
    prev_close: Optional[float] = None
    for trade_date in common_dates:
        open_price = _to_float(open_series.loc[trade_date])
        high_price = _to_float(high_series.loc[trade_date])
        low_price = _to_float(low_series.loc[trade_date])
        close_price = _to_float(close_series.loc[trade_date])
        volume = _to_float(volume_series.loc[trade_date])
        amount = _to_float(amount_series.loc[trade_date])
        hsl = _to_float(turnover_map.get(trade_date))
        if not all(isinstance(v, (int, float)) for v in [open_price, high_price, low_price, close_price, volume, amount, hsl]):
            prev_close = close_price if isinstance(close_price, (int, float)) else prev_close
            continue

        amplitude = None
        change_pct = None
        change_amount = None
        if prev_close and prev_close != 0:
            amplitude = (float(high_price) - float(low_price)) / float(prev_close) * 100.0
            change_pct = (float(close_price) / float(prev_close) - 1.0) * 100.0
            change_amount = float(close_price) - float(prev_close)

        records.append(
            {
                "date": trade_date,
                "open": round(float(open_price), 4),
                "close": round(float(close_price), 4),
                "high": round(float(high_price), 4),
                "low": round(float(low_price), 4),
                "volume": round(float(volume), 4),
                "amount": round(float(amount), 4),
                "zf": round(float(amplitude), 4) if amplitude is not None else 0.0,
                "zdf": round(float(change_pct), 4) if change_pct is not None else 0.0,
                "zde": round(float(change_amount), 4) if change_amount is not None else 0.0,
                "hsl": round(float(hsl), 4),
            }
        )
        prev_close = float(close_price)

    return records


def _compute_profit_ratio_from_distribution(
    current_price: float,
    x_values: List[Any],
    y_values: List[Any],
) -> Optional[float]:
    if not isinstance(current_price, (int, float)) or not x_values or not y_values:
        return None
    total = 0.0
    below = 0.0
    for chip, price in zip(x_values, y_values):
        chip_value = _to_float(chip)
        price_value = _to_float(price)
        if not isinstance(chip_value, (int, float)) or not isinstance(price_value, (int, float)):
            continue
        total += float(chip_value)
        if float(price_value) <= float(current_price):
            below += float(chip_value)
    if total <= 0:
        return None
    return round(below / total, 6)


def _compute_chip_distribution(
    daily_data: Dict[str, pd.DataFrame],
    code: str,
    daily_turnover_history: Dict[str, Any],
    current_price: Any,
) -> Dict[str, Any]:
    records = _build_cyq_kline_records(daily_data, code, daily_turnover_history)
    if len(records) < 30:
        return _todo("筹码分布暂无法计算：有效日线/换手率样本不足 30 条。")

    js_engine = MiniRacer()
    js_engine.eval(CYQ_JS_CODE)
    result = js_engine.call("CYQCalculator", len(records) - 1, records)

    price_now = _to_float(current_price)
    if not isinstance(price_now, (int, float)):
        price_now = _to_float(records[-1]["close"])

    profit_ratio = _compute_profit_ratio_from_distribution(
        float(price_now) if isinstance(price_now, (int, float)) else 0.0,
        result.get("x", []),
        result.get("y", []),
    )
    if profit_ratio is None:
        profit_ratio = _to_float(result.get("benefitPart"))

    percent_90 = result.get("percentChips", {}).get("90", {})
    percent_70 = result.get("percentChips", {}).get("70", {})
    concentration_90 = _to_float(percent_90.get("concentration"))
    concentration_70 = _to_float(percent_70.get("concentration"))

    return {
        "获利比例": round(float(profit_ratio), 6) if isinstance(profit_ratio, (int, float)) else None,
        "平均成本": _to_float(result.get("avgCost")),
        "90成本区间": {
            "低": _to_float((percent_90.get("priceRange") or [None, None])[0]),
            "高": _to_float((percent_90.get("priceRange") or [None, None])[1]),
        },
        "90集中度": round(float(concentration_90), 6) if isinstance(concentration_90, (int, float)) else None,
        "70成本区间": {
            "低": _to_float((percent_70.get("priceRange") or [None, None])[0]),
            "高": _to_float((percent_70.get("priceRange") or [None, None])[1]),
        },
        "70集中度": round(float(concentration_70), 6) if isinstance(concentration_70, (int, float)) else None,
        "筹码状态": _chip_status_from_concentration(concentration_90),
        "样本K线数": len(records),
    }


def _fetch_daily_data(tq: Any, code: str) -> Dict[str, pd.DataFrame]:
    try:
        data = tq.get_market_data(
            field_list=DAILY_FIELDS,
            stock_list=[code],
            start_time="",
            end_time="",
            count=RECENT_DAILY_COUNT,
            dividend_type="none",
            period="1d",
            fill_data=True,
        )
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}
    normalized: Dict[str, pd.DataFrame] = {}
    for field, df in data.items():
        if isinstance(df, pd.DataFrame):
            normalized[field] = df.copy()
    return normalized


def _normalize_stock_payload(
    code: str,
    snapshot: Dict[str, Any],
    more_info: Dict[str, Any],
    stock_info: Dict[str, Any],
    daily_data: Dict[str, pd.DataFrame],
    daily_turnover_history: Dict[str, Any],
    chip_distribution: Dict[str, Any],
    sentiment_payload: Dict[str, Any],
) -> Dict[str, Any]:
    now_price = _to_float(snapshot.get("Now"))
    ma5 = _to_float(more_info.get("MA5Value")) or _compute_ma(daily_data, code, 5)
    ma10 = _compute_ma(daily_data, code, 10)
    ma20 = _compute_ma(daily_data, code, 20)
    prev_high_low = _compute_prev_high_low(daily_data, code, window=20)

    return {
        "股票代码": code,
        "股票名称": stock_info.get("Name"),
        "最新价": now_price,
        "昨收": _to_float(snapshot.get("LastClose")),
        "开盘价": _to_float(snapshot.get("Open")),
        "最高价": _to_float(snapshot.get("Max")),
        "最低价": _to_float(snapshot.get("Min")),
        "涨跌幅": _to_float(more_info.get("ZAF")),
        "总成交量": _to_float(snapshot.get("Volume")),
        "现手": _to_float(snapshot.get("NowVol")),
        "成交额": _to_float(snapshot.get("Amount")),
        "量比": _to_float(more_info.get("fLianB")),
        "换手率": _to_float(more_info.get("fHSL")),
        "均价": _to_float(snapshot.get("Average")),
        "涨速": _to_float(snapshot.get("Zangsu")),
        "5分钟前价格": _to_float(snapshot.get("Before5MinNow")),
        "买一到买五价量": {
            "买价": _normalize_level_values(snapshot.get("Buyp")),
            "买量": _normalize_level_values(snapshot.get("Buyv")),
        },
        "卖一到卖五价量": {
            "卖价": _normalize_level_values(snapshot.get("Sellp")),
            "卖量": _normalize_level_values(snapshot.get("Sellv")),
        },
        "内盘/外盘": {
            "内盘": _to_float(snapshot.get("Inside")),
            "外盘": _to_float(snapshot.get("Outside")),
        },
        "委比": _to_float(more_info.get("Wtb")),
        "主力净流入": _to_float(more_info.get("Zjl_HB")),
        "主买净额": _to_float(more_info.get("Zjl")),
        "总买量/总卖量": {
            "总买量": _to_float(more_info.get("TotalBVol")),
            "总卖量": _to_float(more_info.get("TotalSVol")),
        },
        "MA5": ma5,
        "MA10": ma10,
        "MA20": ma20,
        "\u6362\u624b\u7387\u8ba1\u7b97\u8bf4\u660e": daily_turnover_history.get("daily_turnover_meta"),
        "前高/前低": prev_high_low,
        "5/10/20/60日涨幅": {
            "5日涨幅": _compute_return(daily_data, code, now_price, 5),
            "10日涨幅": _compute_return(daily_data, code, now_price, 10),
            "20日涨幅": _compute_return(daily_data, code, now_price, 20),
            "60日涨幅": _compute_return(daily_data, code, now_price, 60),
        },
        "动态PE": _to_float(more_info.get("DynaPE")),
        "PB": _to_float(more_info.get("PB_MRQ")),
        "总市值": _to_float(more_info.get("Zsz")),
        "流通市值": _to_float(more_info.get("Ltsz")),
        "流通股本": _to_float(stock_info.get("ActiveCapital")),
        "总股本": _to_float(stock_info.get("J_zgb")),
        "所属行业/地域": {
            "行业": stock_info.get("rs_hyname"),
            "地域": stock_info.get("tdx_dyname"),
        },
        "所属板块列表": _todo("暂不接入，后续可通过 get_relation / 板块相关接口补齐。"),
        "涨停价/跌停价": {
            "涨停价": _to_float(more_info.get("ZTPrice")),
            "跌停价": _to_float(more_info.get("DTPrice")),
        },
        "封单额": _to_float(more_info.get("FCAmo")),
        "连板天数": _to_int(more_info.get("EverZTCount")),
        "52周高/低": {
            "52周高": _to_float(more_info.get("HisHigh")),
            "52周低": _to_float(more_info.get("HisLow")),
        },
        "行情日期": more_info.get("HqDate"),
        "行情时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "新闻/公告/舆情": sentiment_payload,
        "筹码分布": chip_distribution,
        "板块热度排行": _todo("暂不接入，后续可接板块热度数据源。"),
    }


def _fetch_one(tq: Any, code: str) -> Dict[str, Any]:
    snapshot = tq.get_market_snapshot(stock_code=code, field_list=SNAPSHOT_FIELDS)
    more_info = tq.get_more_info(stock_code=code, field_list=MORE_INFO_FIELDS)
    stock_info = tq.get_stock_info(stock_code=code, field_list=STOCK_INFO_FIELDS)
    daily_data = _fetch_daily_data(tq, code)
    gb_history = _fetch_gb_history(tq, code, daily_data)

    if not isinstance(snapshot, dict):
        raise RuntimeError(f"snapshot for {code} is not dict: {type(snapshot)}")
    if not isinstance(more_info, dict):
        raise RuntimeError(f"more_info for {code} is not dict: {type(more_info)}")
    if not isinstance(stock_info, dict):
        stock_info = {}

    daily_turnover_history = _compute_daily_turnover_history(
        daily_data=daily_data,
        code=code,
        gb_history=gb_history,
        current_float_capital=stock_info.get("ActiveCapital"),
        current_turnover_rate=more_info.get("fHSL"),
        snapshot_volume=snapshot.get("Volume"),
    )
    chip_distribution = _compute_chip_distribution(
        daily_data=daily_data,
        code=code,
        daily_turnover_history=daily_turnover_history,
        current_price=snapshot.get("Now"),
    )
    sentiment_payload = _fetch_baidu_sentiment_list(code)

    return _normalize_stock_payload(
        code,
        snapshot,
        more_info,
        stock_info,
        daily_data,
        daily_turnover_history,
        chip_distribution,
        sentiment_payload,
    )


def _build_llm_prompt(
    report_text: str,
    market_report_text: str,
    focus_code: str,
    market_payload: Dict[str, Any],
    position_context: Dict[str, Any],
) -> str:
    compact_market_payload = json.dumps(market_payload, ensure_ascii=False, indent=2)
    compact_position_context = json.dumps(position_context, ensure_ascii=False, indent=2)
    sentiment_payload = market_payload.get("新闻/公告/舆情") or {}
    sentiment_items = sentiment_payload.get("舆情列表") or []
    sentiment_summary = {
        "状态": sentiment_payload.get("状态"),
        "数据源": sentiment_payload.get("数据源"),
        "舆情条数": sentiment_payload.get("舆情条数"),
        "舆情统计": sentiment_payload.get("舆情统计"),
        "最近重要舆情": sentiment_items[:3],
    }
    compact_sentiment_summary = json.dumps(
        sentiment_summary, ensure_ascii=False, indent=2
    )
    return f"""你现在是一个严格执行既有交易策略的盘中判断器，不要重新发明策略。
你的任务不是重新分析股票，而是判断：
1. 当前是否已经触发原策略中的买点、卖点、加仓点、减仓点、止盈点、止损点或风险提示
2. 如果没有触发，就明确说未触发
3. 输出必须是 JSON，且只能输出 JSON
4. 下面会给你整份 DSA 个股报告全文，你只能聚焦当前这只股票 `{focus_code}` 的策略内容做判断，不要把其他股票的建议混进当前结论
5. 下面还会给你当前股票的持仓上下文；如果已经持仓，请优先从“持有、加仓、减仓、止盈、止损、风险提示”角度判断，而不是只讨论能不能建仓
6. 如果未持仓，才主要从“观察、低吸、建仓”角度判断
7. 下面还会给你当日大盘分析报告全文；你在判断是否触发时，必须同时考虑大盘、板块、资金、情绪这些市场环境因素
8. 如果个股结构看起来接近触发，但大盘、板块、资金或情绪明显不支持，可以输出 no_action 或 risk_alert，并在原因中说明是哪一层环境约束了执行
9. 当前盯盘数据中已经包含“筹码分布”，判断时必须额外考虑获利比例、平均成本、70/90集中度、筹码状态这些信息，不能忽略
10. 当前盯盘数据中已经包含“新闻/公告/舆情”，其中“舆情方向”来自百度财经 sentimentlist 的 benefitType 映射（1=利好，0=中性，-1=利空），判断时必须把这部分作为辅助证据一起考虑
11. 你必须先阅读我单独给你的“舆情摘要”，再判断这些舆情对本次动作是增强、削弱、中性，还是提示风险
12. 不论是否触发，都必须用一句话说明筹码分布对本次判断的影响
13. 不论是否触发，都必须单独输出“舆情修正”字段，里面至少要写明：舆情整体偏多/偏空/中性，最近最重要的一条消息是什么，它对本次判断是增强还是削弱

动作枚举只能是：
- no_action
- watch_buy
- watch_sell
- risk_alert

动作解释：
- no_action：当前不触发新的买卖动作；若已持仓，可理解为继续持有观察
- watch_buy：偏向买入观察；若已持仓，可理解为加仓观察
- watch_sell：偏向卖出观察；若已持仓，可理解为减仓、止盈或卖出观察
- risk_alert：偏向破位、止损、异常波动、策略失效等风险提示

请使用如下 JSON 结构：
{{
  "是否触发": true,
  "动作": "watch_buy",
  "置信度": 0.82,
  "触发原因": "一句到两句中文说明",
  "风险提示": "一句中文说明，没有则写无",
  "建议下次观察": "一句中文说明",
  "市场环境修正": "说明大盘、板块、资金、情绪对本次判断的影响",
  "筹码分布修正": "说明获利比例、平均成本、70/90集中度、筹码状态对本次判断的影响",
  "舆情修正": "必须单独说明舆情整体偏多、偏空还是中性，最近最重要的一条消息是什么，以及它对本次动作是增强、削弱还是中性"
}}

【当日 DSA 个股报告全文】
{report_text}

【当日大盘分析报告全文】
{market_report_text}

【当前关注股票】
{focus_code}

【当前持仓上下文】
{compact_position_context}

【舆情摘要】
{compact_sentiment_summary}

【当前盯盘数据】
{compact_market_payload}
"""


def _strip_markdown_json(text: str) -> str:
    """Remove markdown code fences that some LLMs wrap around JSON output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _judge_with_llm(
    report_text: str,
    market_report_text: str,
    focus_code: str,
    market_payload: Dict[str, Any],
    position_context: Dict[str, Any],
) -> Dict[str, Any]:
    settings = _get_llm_settings()
    api_key = settings.get("api_key")
    model = settings.get("model")
    base_url = settings.get("base_url")

    if not api_key or not model or not base_url:
        return {
            "状态": "未执行",
            "原因": "缺少 LLM 配置，请检查环境变量。",
        }

    client = OpenAI(api_key=api_key, base_url=base_url)
    prompt = _build_llm_prompt(
        report_text,
        market_report_text,
        focus_code,
        market_payload,
        position_context,
    )

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": "你是一个严格、克制、只做盘中触发判断的交易策略执行助手。",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        timeout=60,
    )

    content = response.choices[0].message.content or "{}"
    content = _strip_markdown_json(content)
    parsed = json.loads(content)
    parsed["LLM模型"] = model
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DSA + TdxQuant watch demo"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM judgement and only output fetched/normalized data.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _ensure_tqcenter_importable()
    report_text = _read_report_text(DEFAULT_REPORT_PATH)
    market_report_text = _read_report_text(DEFAULT_MARKET_REPORT_PATH)
    watchlist = _extract_watchlist_from_report(report_text)

    from tqcenter import tq  # type: ignore

    print(">>> 初始化通达信 TdxQuant 连接")
    tq.initialize(__file__)
    print(">>> 初始化完成\n")
    print(f">>> DSA 报告: {DEFAULT_REPORT_PATH}")
    print(f">>> 大盘报告: {DEFAULT_MARKET_REPORT_PATH}")
    print(f">>> 从报告解析出的股票池: {watchlist}\n")

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for code in watchlist:
        print("=" * 72)
        print(f">>> 获取股票数据并执行 LLM 判断: {code}")
        print("=" * 72)
        try:
            market_payload = _fetch_one(tq, code)
            position_context = _position_for_stock(code)
            if args.no_llm:
                llm_result = {
                    "状态": "已跳过",
                    "原因": "本次使用 --no-llm，未调用大模型。",
                }
            else:
                llm_result = _judge_with_llm(
                    report_text,
                    market_report_text,
                    code,
                    market_payload,
                    position_context,
                )
            combined = {
                "股票代码": code,
                "DSA报告路径": str(DEFAULT_REPORT_PATH),
                "大盘报告路径": str(DEFAULT_MARKET_REPORT_PATH),
                "持仓上下文": position_context,
                "盯盘数据": market_payload,
                "LLM判断": llm_result,
            }
            results.append(combined)
            print(json.dumps(combined, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            error = {
                "股票代码": code,
                "错误": str(exc),
            }
            errors.append(error)
            print(json.dumps(error, ensure_ascii=False, indent=2))
        print()

    final_output = {
        "说明": {
            "demo范围": "当前已接入最小 LLM 闭环：DSA 报告全文 + 持仓上下文 + 当前盯盘数据 -> LLM执行判断。",
            "股票池": watchlist,
            "策略模式": "当前使用 DSA 报告驱动股票池，并把报告全文传给 LLM。",
            "持仓模式": "当前使用脚本内写死的最小持仓上下文，后续可替换为真实持仓源。",
            "未完成项": ["所属板块列表", "板块热度排行", "循环轮询", "推送"],
        },
        "TODO": TODO_ITEMS,
        "结果": results,
        "错误": errors,
    }

    print("=" * 72)
    print(">>> 最终完整 JSON")
    print("=" * 72)
    print(json.dumps(final_output, ensure_ascii=False, indent=2))

    try:
        tq.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
