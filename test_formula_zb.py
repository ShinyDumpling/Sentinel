# -*- coding: utf-8 -*-
"""补测：formula_zb 传股票代码参数的正确写法"""
import sys
import json

sys.path.insert(
    0, r"D:\【指标100】通达信《专业研究版》773\【指标100】通达信《专业研究版】773\PYPlugins\user"
)
from tqcenter import tq

tq.initialize(__file__)
print(">>> tq 初始化完成\n")

print("="*72)
print("一、测试 formula_zb 传股票代码参数")
print("="*72)

# 文档说 formula_arg 是公式参数，但股票代码要不要传？
# 测试三种写法：
test_cases = [
    ("ZLJE + 空参数", "ZLJE", ""),
    ("ZLJE + 股票代码", "ZLJE", "000001.SZ"),
    ("ZDT + 空参数", "ZDT", ""),
    ("ZDT + 股票代码", "ZDT", "000001.SZ"),
    ("MACD + 默认参数", "MACD", "12,26,9"),
    ("MACD + 股票代码", "MACD", "12,26,9,000001.SZ"),
]

for label, name, arg in test_cases:
    result = tq.formula_zb(formula_name=name, formula_arg=arg, xsflag=-1)
    print(f"\n【{label}】")
    if isinstance(result, dict):
        if result.get("Error"):
            print(f"  ❌ 失败: {result.get('Error')}")
        else:
            print(f"  ✅ 成功: {json.dumps(result, ensure_ascii=False, default=str)[:500]}")
    else:
        print(f"  返回: {str(result)[:200]}")

print("\n"+"="*72)
print("二、对比测试：formula_process_mul_zb（同参数）")
print("="*72)

for name in ["ZLJE", "ZDT", "MACD"]:
    result = tq.formula_process_mul_zb(
        formula_name=name,
        formula_arg="12,26,9" if name=="MACD" else "",
        return_count=1,
        return_date=True,
        xsflag=-1,
        stock_list=["000001.SZ"],
        stock_period="1d",
        count=1,
        dividend_type=0,
    )
    print(f"\n【{name}】")
    if result.get("ErrorId") == "0":
        data = {k:v for k,v in result.items() if k != "ErrorId"}
        print(f"  ✅ 成功: {json.dumps(data, ensure_ascii=False, default=str)[:500]}")
    else:
        print(f"  ❌ 失败: {result.get('Error')}")

try:
    tq.close()
except:
    pass
