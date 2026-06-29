---
name: sentinel-quant
description: Use when the user asks about A-share stock market dashboard, stock screening, daily K-line data sync, market review reports, or watchdog monitoring. Covers Sentinel, alphasift-fork, and daily_stock_analysis projects.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [a-share, quant, stock, dashboard, screening, market-review, tdxquant]
    related_skills: []
---

# Sentinel Quant — A股量化工具集

## Overview

整合三个本地项目的所有能力：大盘看板、选股、盯盘、日K数据管理、大盘复盘。所有项目基于通达信 TdxQuant 数据接口，需要通达信客户端运行。

## When to Use

- 用户说"看大盘"、"今天市场怎么样"、"大盘什么情况" → 大盘看板
- 用户说"选股"、"跑个选股"、"screen" → 选股
- 用户说"盯盘"、"watchdog"、"监控" → 盯盘
- 用户说"更新日K"、"下载日K"、"拉数据" → 全量下载日K
- 用户说"大盘复盘"、"market review"、"复盘报告" → 大盘复盘报告
- 用户说"分析这只股票"、"个股分析" → 个股分析（daily_stock_analysis）
- 用户问项目结构、路径、环境配置 → 参考本文档

## 项目全景

| 项目 | 路径 | Python | 说明 |
|------|------|--------|------|
| Sentinel | `D:\股神养成plan\Sentinel` | `D:\Python\Python313\python.exe` | 大盘看板、盯盘、日K下载 |
| alphasift-fork | `D:\股神养成plan\alphasift-fork` | `.venv\Scripts\python.exe` | 选股系统 |
| daily_stock_analysis | `D:\股神养成plan\daily_stock_analysis` | 系统 Python | 大盘复盘、个股分析 |
| TdxQuant | `D:\【指标100】通达信《专业研究版》773\【指标100】通达信《专业研究版》773\PYPlugins\user\tqcenter.py` | — | 数据接口 |
| all_daily_k.db | `D:\股神养成plan\Sentinel\all_daily_k.db` | — | 全市场日K数据库（113MB） |

---

## 一、每日大盘看板（market_dashboard.py）

**项目：** Sentinel  
**脚本：** `market_dashboard.py`  
**功能：** 拉取6大宽基指数 + 587个板块日K + 主力净流入 + 重点板块成分股验证 + LLM风格判断

```bash
# 完整看板（含 LLM 风格判断）
python D:/股神养成plan/Sentinel/market_dashboard.py

# 纯数据，跳过 LLM
python D:/股神养成plan/Sentinel/market_dashboard.py --no-llm
```

**输出内容：**
1. 6大宽基指数（上证/深成/创业板/科创50/沪深300/中证1000），含一句话解读
2. 板块热度榜：行业涨 Top15 → 概念涨 Top15 → 行业跌 Top15 → 概念跌 Top15
3. 重点板块成分股验证：涨幅前2行业 + 前2概念，每个板块显示涨跌比、龙头、中军、涨停股
4. LLM 市场风格判断（可选）

**注意事项：**
- 需要通达信客户端运行
- 板块代码显示为纯数字（去掉了 .SH/.SZ 后缀）
- 指数解读来自 Obsidian 知识库 `看大盘-六大指数解读.md`

---

## 二、选股（alphasift-fork）

**项目：** alphasift-fork  
**Python：** 必须用 `.venv\Scripts\python.exe`（虚拟环境）

### 2.1 本地选股（推荐）

```bash
cd D:\股神养成plan\alphasift-fork

# 放量突破策略
.venv/Scripts/python.exe -m alphasift.cli screen-local volume_breakout --snapshot-mode auto --full-pass --explain

# 双低策略
.venv/Scripts/python.exe -m alphasift.cli screen-local dual_low --snapshot-mode auto --full-pass --explain
```

**关键参数：**
- `--snapshot-mode auto`：自动使用本地快照缓存
- `--full-pass`：不限制输出数量
- `--explain`：LLM 排序并解释
- `--no-llm`：纯量化排序，不调 LLM

**数据源：**
- 快照：`data/snapshot/cn_snapshot_*.json`（efinance 拉取，自动缓存）
- 日K增强：`D:\股神养成plan\Sentinel\all_daily_k.db`（本地 SQLite）
- 板块信息：TdxQuant 实时接口

**输出表格列：** 排名、代码、名称、最终分、筛选分、风险、行业、概念、原因

### 2.2 远端选股

```bash
cd D:\股神养成plan\alphasift-fork
.venv/Scripts/python.exe -m alphasift.cli screen <策略> --explain
```

---

## 三、盯盘（watch_dog.py）

**项目：** Sentinel  
**脚本：** `watch_dog.py`  
**功能：** 解析 DSA 报告 → 提取股票池 → 拉实时快照 + 日K + 筹码分布 → LLM 判断买卖

```bash
python D:/股神养成plan/Sentinel/watch_dog.py
```

**日K 拉取方式：** 通过 TdxQuant `get_market_data` 实时拉取，每只股票最近 120 根日K，不依赖本地数据库。

---

## 四、全量下载日K（download_all_daily_k.py）

**项目：** Sentinel  
**脚本：** `download_all_daily_k.py`  
**功能：** 从 TdxQuant 拉取全市场 A 股最近 150 天日K，存入本地 SQLite

```bash
python D:/股神养成plan/Sentinel/download_all_daily_k.py
```

**输出：** `D:\股神养成plan\Sentinel\all_daily_k.db`（约 113MB，5500+ 只股票，80万+ 行）

**特性：**
- 断点续传：已同步的股票自动跳过
- 数据库路径为脚本所在目录，不受执行目录影响
- 被 alphasift-fork 选股项目的日K增强功能读取

**何时需要更新：**
- 每个交易日收盘后执行一次
- 选股项目依赖此数据库获取日K特征

---

## 五、大盘复盘报告（daily_stock_analysis）

**项目：** daily_stock_analysis  
**路径：** `D:\股神养成plan\daily_stock_analysis`

### 5.1 生成大盘复盘报告

```bash
cd D:\股神养成plan\daily_stock_analysis
python main.py
```

输出报告保存到 `reports/market_review_YYYYMMDD.md`。

**报告内容（7节）：**
1. 盘面总览（涨跌家数、涨跌停、成交额）
2. 指数结构（O/H/L/C/振幅/成交额）
3. 板块主线（领涨/领跌 Top5）
4. 资金与情绪
5. 消息催化（需搜索）
6. 明日交易计划（LLM 生成）
7. 风险提示

### 5.2 个股分析

通过 `server.py` / `webui.py` 启动 Web 服务触发分析，非 CLI 直接调用。

---

## 环境要求

- **通达信客户端：** 必须运行，TdxQuant 依赖其 DLL 连接
- **Python：** Sentinel 用 `D:\Python\Python313\python.exe`，alphasift 用项目 `.venv`
- **数据库：** `all_daily_k.db` 由 `download_all_daily_k.py` 维护，被 alphasift 选股读取
- **快照缓存：** alphasift 自动管理 `data/snapshot/` 目录

---

## Output Rules

执行脚本后，**直接输出脚本的完整原始结果，不要总结、不要省略、不要加工**。用户需要看到完整的输出内容，不要只提取关键信息或做概括。

---

## Common Pitfalls

1. **选股项目工作目录：** 必须在 `D:\股神养成plan\alphasift-fork` 下执行，否则找不到模块
2. **Python 解释器：** alphasift 必须用 `.venv\Scripts\python.exe`，Sentinel 用 `D:\Python\Python313\python.exe`，不要混用
3. **日K 数据过期：** 选股前先确认 `download_all_daily_k.py` 已更新到最新交易日
4. **TdxQuant 多线程不安全：** 同一进程内 `initialize` 只能调一次，多线程会跳过初始化
5. **快照缓存日期 ≠ 数据日期：** 快照文件名日期是缓存创建日，真实交易日从 DataFrame 提取
6. **板块代码格式：** TdxQuant 返回带后缀（如 `880081.SH`），显示时去掉后缀变纯数字
7. **大盘看板需要通达信客户端运行：** 否则 `tq.initialize()` 失败

---

## Verification Checklist

- [ ] 通达信客户端已启动
- [ ] `all_daily_k.db` 已更新到最新交易日
- [ ] 选股前确认在 `alphasift-fork` 目录下
- [ ] Sentinel 脚本使用正确的 Python 路径
- [ ] 大盘看板输出确认板块代码为纯数字
