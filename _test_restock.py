#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试工具：把状态文件里某个"有货"的商品改成"售罄"，
这样下一次运行就会把它判定为【到货】，从而触发一条真实推送。
跑完记得提交推送，再手动触发一次工作流。
"""
import json
import sys
from pathlib import Path

p = Path("state/store_seen.json")
if not p.exists():
    print("找不到 state/store_seen.json，请在仓库目录下运行")
    sys.exit(1)

d = json.loads(p.read_text(encoding="utf-8"))
prods = d.get("products", {})

target = None
for sku, v in prods.items():
    if v.get("sold_out") is False:
        target = (sku, v)
        break

if not target:
    print("没有找到处于'有货'状态的商品，无法制造测试")
    sys.exit(1)

sku, v = target
v["sold_out"] = True
p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

print("已把这件商品在状态里标成【售罄】：")
print("  SKU :", sku)
print("  名称:", v.get("name"))
print("  链接:", v.get("url"))
print()
print("下一次运行会发现它其实有货 → 触发【到货了！】推送。")
