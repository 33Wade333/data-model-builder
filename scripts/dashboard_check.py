#!/usr/bin/env python3
"""
dashboard_check.py —— 看板回归检查（能力内核·防退化机制）
每次改模板/渲染器后必跑；全绿才算改完。三层检查：
  A. JS 语法（防模板编辑引入语法错误→整页白屏）
  B. DATA 布局（防行满/重叠回归——行合计=24、零重叠、零尺寸组件）
  C. 模板规范锚点（防编辑静默丢失——色板/五段式/分段控件/响应式/居中）
用法：python dashboard_check.py <生成的看板.html> [--template <模板路径>]
退出码：0=全绿 1=有回归
"""
import argparse, json, re, subprocess, sys, tempfile
from pathlib import Path

CARD_LIKE = {"kpi", "bullet", "gauge", "radar"}

def check_js(html):
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if len(scripts) < 2:
        return [f"script 块数量异常：{len(scripts)}（应为 2：echarts+主脚本）"]
    issues = []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(scripts[-1]); tmp = f.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        issues.append(f"主脚本语法错误：{r.stderr.strip()[:200]}")
    return issues

def check_data(html):
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    if not m:
        return ["DATA 未找到（占位符未替换或结构破坏）"]
    raw = m.group(1)
    if "</script" in raw:
        return ["DATA 含裸 </script（转义防御失效——会截断整页脚本）"]
    d = json.loads(raw)
    issues = []
    for dom in d.get("domains", []):
        comps = [(c["id"], c["type"], c["grid"]) for c in dom["components"]]
        rows = {}
        for cid, t, g in comps:
            rows.setdefault(g["y"], []).append((cid, t, g))
            if g["w"] < 1 or g["h"] < 1:
                issues.append(f"[{dom['id']}] 组件 {cid} 零尺寸 {g}")
        for y, row in rows.items():
            total = sum(g["w"] for _, _, g in row)
            if total != 24 and not (len(row) == 1 and row[0][1] in CARD_LIKE):
                issues.append(f"[{dom['id']}] 行 y={y} 未填满：合计 {total} ≠ 24（{', '.join(c for c,_,_ in row)}）")
            hs = {g["h"] for _, _, g in row}
            if len(hs) > 1:
                issues.append(f"[{dom['id']}] 行 y={y} 高度不齐：{sorted(hs)}（行带空白回归）")
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                (ia, _, ga), (ib, _, gb) = comps[i], comps[j]
                ox = min(ga["x"]+ga["w"], gb["x"]+gb["w"]) - max(ga["x"], gb["x"])
                oy = min(ga["y"]+ga["h"], gb["y"]+gb["h"]) - max(ga["y"], gb["y"])
                if ox > 0 and oy > 0:
                    issues.append(f"[{dom['id']}] 组件重叠：{ia} × {ib}")
    return issues

# 模板规范锚点（值=当前定稿规范；改规范时同步改这里）
TEMPLATE_ANCHORS = [
    ("系列色板 AntV 10", 'const S = ["#5B8FF9","#61DDAA","#65789B","#F6BD16","#7262fd","#78D3F8","#9661BC","#F6903D","#008685","#F08BB4"];'),
    ("KPI 五段式·说明块单行截断", "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:18px"),
    ("KPI 五段式·折线块", ".kspark{min-height:40px"),
    ("L2 分段控件·恒定高度", ".dimtablewrap{height:420px"),
    ("L2 分段控件·摘要chips", ".sumchip{"),
    ("布尔列 ✓/—", 'const val = bools.has(c.key) ? (r[c.key] ? "✓" : "—") : fmt(r[c.key]);'),
    ("明细表数字右对齐", ".dimtbl td.num-r{text-align:right}"),
    ("主表居中", ".dt th{text-align:center"),
    ("分区标题透明壳", ".cell-heading{background:none;border:0"),
    ("响应式双断点", "@media (max-width:1024px)"),
    ("响应式单列", "@media (max-width:752px)"),
    ("均值参考线", "markLine: i === 0 ?"),
    ("动画 300ms", "animationDuration:300"),
    ("图例 14×3", "itemWidth:14, itemHeight:3"),
]

def check_template(html):
    return [f"锚点丢失：{name}" for name, anchor in TEMPLATE_ANCHORS if anchor not in html]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    args = ap.parse_args()
    html = Path(args.html).read_text(encoding="utf-8")
    results = [("A. JS 语法", check_js(html)), ("B. DATA 布局", check_data(html)), ("C. 模板规范锚点", check_template(html))]
    failed = 0
    for section, issues in results:
        if issues:
            for i in issues:
                print(f"❌ {section}｜{i}")
                failed += 1
        else:
            print(f"✅ {section} 全绿")
    total = sum(len(i) for _, i in results)
    print(f"\n回归检查：{'❌ ' + str(failed) + ' 项回归' if failed else '✅ 全绿（0 回归）'}")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
