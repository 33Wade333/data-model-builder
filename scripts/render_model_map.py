#!/usr/bin/env python3
"""
数据模型地图渲染器 —— warehouse.db + build/*.sql → 单文件 model-map.html
零 token：确定性脚本从建好的数仓静态生成，AI 的活建仓时已干完，看图是后处理。

用法: python render_model_map.py <案例目录> [--output model-map.html]
读取: <案例目录>/warehouse.db（只读）+ <案例目录>/build/*.sql
内容: 六层泳道（层职责+比喻）→ 表卡片（表名/中文备注/行数/主键/上游血缘）
      → 点击展开字段字典（含中文注释）→ 顶部层间血缘流图 → 统计条
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

LAYERS = [
    ("ods", "ODS 原始层", "原样照搬源表，一个字不改", "复印件", "#64748b"),
    ("dim", "DIM 维表层", "加工维表，映射修正", "户口本", "#0d9488"),
    ("dwd", "DWD 明细层", "打判定标签，口径只写一次（0/1）", "贴标签", "#2563eb"),
    ("dwm", "DWM 拉宽层", "只加列不聚合，补齐维度", "加列不翻表", "#7c3aed"),
    ("dws", "DWS 汇总层", "按粒度聚合，只存整数不存率", "算了一半", "#d97706"),
    ("ads", "ADS 应用层", "算率+排名，薄层", "最终答案", "#16a34a"),
]

def layer_of(table):
    m = re.match(r"^(ods|dim|dwd|dwm|dws|ads)_", table)
    return m.group(1) if m else "other"

def parse_build_sqls(build_dir):
    """从 build/*.sql 解析：表中文备注 + 血缘（INSERT INTO target ... FROM/JOIN source）"""
    remarks, lineage = {}, {}   # lineage: {target: set(sources)}
    src_tables = set()          # 源库表（src.xxx）
    sql_files = sorted(build_dir.glob("*.sql")) if build_dir.exists() else []
    for f in sql_files:
        text = f.read_text(encoding="utf-8")
        # 表备注：DROP/CREATE TABLE 前最近的 -- 注释行
        lines = text.splitlines()
        pending_remark = None
        for line in lines:
            cm = re.match(r"^--\s*(.+)$", line.strip())
            if cm and not cm.group(1).startswith("==="):
                pending_remark = cm.group(1).strip()
            tm = re.match(r"^(?:DROP TABLE IF EXISTS|CREATE TABLE)\s+(\w+)", line.strip(), re.IGNORECASE)
            if tm:
                tname = tm.group(1).lower()
                if pending_remark and tname not in remarks:
                    remarks[tname] = pending_remark
                pending_remark = None
        # 血缘：按语句切分，找 INSERT INTO target + FROM/JOIN source
        for stmt in re.split(r";\s*\n", text):
            im = re.search(r"INSERT\s+INTO\s+(\w+)", stmt, re.IGNORECASE)
            if not im:
                continue
            target = im.group(1).lower()
            sources = set()
            for sm in re.finditer(r"(?:FROM|JOIN)\s+(?:src\.)?(\w+)", stmt, re.IGNORECASE):
                s = sm.group(1).lower()
                if s != target:
                    sources.add(s)
            if re.search(r"FROM\s+src\.", stmt, re.IGNORECASE):
                for sm in re.finditer(r"FROM\s+src\.(\w+)", stmt, re.IGNORECASE):
                    src_tables.add(sm.group(1).lower())
            lineage.setdefault(target, set()).update(sources)
    return remarks, lineage, src_tables

def read_warehouse(db_path):
    """读 warehouse.db：表清单/行数/主键/字段（类型+DDL中文注释）"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = {}
    for (tname, ddl) in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        cols, pk = [], []
        for cid, cname, ctype, notnull, dflt, is_pk in conn.execute(f'PRAGMA table_info("{tname}")'):
            cols.append({"name": cname, "type": ctype})
            if is_pk:
                pk.append(cname)
        # DDL 行内中文注释: col_name TYPE ..., -- 注释
        comments = {}
        if ddl:
            for line in ddl.splitlines():
                m = re.match(r"\s*(\w+)\s+\w+.*?--\s*(.+)$", line)
                if m:
                    comments[m.group(1)] = m.group(2).strip()
        for c in cols:
            c["comment"] = comments.get(c["name"], "")
        cnt = conn.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
        tables[tname] = {"rows": cnt, "pk": pk, "cols": cols}
    conn.close()
    return tables

def fmt_rows(n):
    return f"{n:,}"

def build_html(case_name, tables, remarks, lineage, src_tables, db_path):
    wh_tables = {t: info for t, info in tables.items() if layer_of(t) != "other"}
    other_tables = {t: info for t, info in tables.items() if layer_of(t) == "other"}
    total_rows = sum(info["rows"] for info in tables.values())
    n_layers = sum(1 for p, *_ in LAYERS if any(layer_of(t) == p for t in wh_tables))

    # 层间血缘计数（含 源库→ODS）
    edge_counts = {}
    for target, sources in lineage.items():
        tl = layer_of(target)
        if tl == "other" or target not in wh_tables:
            continue
        for s in sources:
            if s in src_tables:
                sl = "src"
            elif s in wh_tables:
                sl = layer_of(s)
            else:
                continue
            if sl != tl:
                edge_counts[(sl, tl)] = edge_counts.get((sl, tl), 0) + 1

    css_vars = ":root{--bg:#f6f8fa;--card:#fff;--ink:#1a2332;--sub:#5a6b85;--blue:#2563eb;--border:#e2e8f0}"
    layer_colors = {p: c for p, _, _, _, c in LAYERS}

    # ---- 顶部层间流图（SVG）----
    flow_order = ["src"] + [p for p, *_ in LAYERS if any(layer_of(t) == p for t in wh_tables)]
    box_w, box_h, gap = 118, 54, 46
    svg_w = len(flow_order) * box_w + (len(flow_order) - 1) * gap + 40
    labels = {"src": ("源库", "只读挂载")}
    for p, name, duty, metaphor, color in LAYERS:
        labels[p] = (name.split()[0], metaphor)
    boxes, arrows = [], []
    for i, p in enumerate(flow_order):
        x = 20 + i * (box_w + gap)
        cnt = len(src_tables) if p == "src" else sum(1 for t in wh_tables if layer_of(t) == p)
        color = "#94a3b8" if p == "src" else layer_colors[p]
        boxes.append(
            f'<g transform="translate({x},30)">'
            f'<rect width="{box_w}" height="{box_h}" rx="10" fill="{color}"/>'
            f'<text x="{box_w/2}" y="24" text-anchor="middle" fill="#fff" font-size="15" font-weight="700">{labels[p][0]}</text>'
            f'<text x="{box_w/2}" y="42" text-anchor="middle" fill="#fff" font-size="10" opacity=".9">{labels[p][1]} · {cnt}表</text></g>')
        if i < len(flow_order) - 1:
            nxt = flow_order[i + 1]
            ec = edge_counts.get((p, nxt), 0)
            # 非相邻层也有血缘时画弧线（简化：相邻画直线，非相邻画上方弧线）
            x1, x2 = x + box_w, x + box_w + gap
            if nxt in flow_order[i + 1:i + 2]:
                arrows.append(
                    f'<line x1="{x1}" y1="57" x2="{x2}" y2="57" stroke="#94a3b8" stroke-width="2" marker-end="url(#arr)"/>'
                    + (f'<text x="{(x1+x2)/2}" y="50" text-anchor="middle" fill="{layer_colors.get(nxt, "#64748b")}" font-size="10">{ec}</text>' if ec else ""))
    # 非相邻层血缘（弧线）
    for (sl, tl), ec in edge_counts.items():
        if sl not in flow_order or tl not in flow_order:
            continue
        i1, i2 = flow_order.index(sl), flow_order.index(tl)
        if i2 - i1 > 1:
            x1 = 20 + i1 * (box_w + gap) + box_w / 2
            x2 = 20 + i2 * (box_w + gap) + box_w / 2
            arrows.append(
                f'<path d="M{x1},28 C{x1},6 {x2},6 {x2},28" fill="none" stroke="{layer_colors.get(tl, "#64748b")}" '
                f'stroke-width="1.6" stroke-dasharray="4 3" marker-end="url(#arr)"/>'
                f'<text x="{(x1+x2)/2}" y="12" text-anchor="middle" fill="{layer_colors.get(tl, "#64748b")}" font-size="10">{ec} 跳层</text>')

    # ---- 六层泳道 ----
    lanes = []
    for p, name, duty, metaphor, color in LAYERS:
        lt = {t: info for t, info in wh_tables.items() if layer_of(t) == p}
        if not lt:
            continue
        cards = []
        for t, info in sorted(lt.items(), key=lambda kv: -kv[1]["rows"]):
            ups = sorted(s for s in lineage.get(t, set()) if s in wh_tables or s in src_tables)
            up_chips = "".join(
                f'<a class="chip" href="#tbl-{s}">{s}</a>' if s in wh_tables
                else f'<span class="chip src">src.{s}</span>'
                for s in ups)
            col_rows = "".join(
                f'<tr><td><code>{c["name"]}</code></td><td>{c["type"]}</td>'
                f'<td>{"🔑 " if c["name"] in info["pk"] else ""}{c["comment"]}</td></tr>'
                for c in info["cols"])
            cards.append(f'''<details class="tcard" id="tbl-{t}">
<summary><b class="tname">{t}</b>
<span class="trows">{fmt_rows(info["rows"])} 行</span>
<span class="tremark">{remarks.get(t, "")}</span></summary>
<div class="tbody">
<div class="meta">主键：{("、".join(info["pk"]) if info["pk"] else "无（ODS 原样照搬）")}</div>
{f'<div class="meta">上游：{up_chips}</div>' if up_chips else '<div class="meta">上游：源库</div>'}
<table><tr><th>字段</th><th>类型</th><th>说明</th></tr>{col_rows}</table>
</div></details>''')
        lanes.append(f'''<div class="lane" style="--lc:{color}">
<div class="lane-head"><div class="lane-name">{name}</div>
<div class="lane-duty">{duty}</div><div class="lane-meta">{metaphor} · {len(lt)} 表</div></div>
{''.join(cards)}</div>''')

    other_html = ""
    if other_tables:
        ocards = "".join(
            f'<span class="chip">{t}（{fmt_rows(i["rows"])} 行）</span>' for t, i in sorted(other_tables.items()))
        other_html = f'<div class="card"><b>非分层辅助表</b><div style="margin-top:8px">{ocards}</div></div>'

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{case_name} · 数据模型地图</title>
<style>
{css_vars}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:var(--bg);color:var(--ink);line-height:1.7}}
.wrap{{max-width:1280px;margin:0 auto;padding:0 20px 60px}}
header{{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;padding:32px 0 24px;margin-bottom:22px}}
h1{{font-size:24px}} .sub{{opacity:.85;font-size:13.5px;margin-top:4px}}
.stats{{display:flex;gap:14px;margin-top:14px;flex-wrap:wrap}}
.stats span{{background:rgba(255,255,255,.15);padding:3px 14px;border-radius:16px;font-size:13px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
h2{{font-size:18px;margin:26px 0 10px;color:var(--blue)}}
.flowsvg{{width:100%;height:auto;display:block}}
.lanes{{display:flex;gap:14px;align-items:flex-start;overflow-x:auto;padding-bottom:12px}}
.lane{{flex:1;min-width:215px;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}}
.lane-head{{background:var(--lc);color:#fff;padding:10px 14px}}
.lane-name{{font-weight:700;font-size:14.5px}} .lane-duty{{font-size:11.5px;opacity:.92;margin-top:2px}}
.lane-meta{{font-size:11px;opacity:.85;margin-top:2px}}
.tcard{{border-bottom:1px solid var(--border)}}
.tcard:last-child{{border-bottom:none}}
.tcard summary{{padding:9px 12px;cursor:pointer;font-size:12.5px;list-style:none;display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}}
.tcard summary::-webkit-details-marker{{display:none}}
.tcard summary:hover{{background:#f1f5f9}}
.tname{{font-size:12.5px;color:var(--blue)}}
.trows{{font-size:11px;color:var(--sub);background:#f1f5f9;border-radius:10px;padding:1px 8px}}
.tremark{{font-size:11px;color:var(--sub);flex-basis:100%}}
.tbody{{padding:8px 12px 12px;background:#fafbfd}}
.meta{{font-size:11.5px;color:var(--sub);margin:3px 0}}
.chip{{display:inline-block;font-size:11px;background:#eef2ff;color:var(--blue);border-radius:10px;padding:1px 9px;margin:1px 3px 1px 0;text-decoration:none;border:1px solid #dbe4ff}}
.chip.src{{background:#f1f5f9;color:var(--sub);border-color:var(--border)}}
table{{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:6px}}
th{{background:#eef2ff;padding:4px 8px;border:1px solid var(--border);text-align:left}}
td{{padding:3px 8px;border:1px solid var(--border)}}
code{{font-family:Consolas,monospace;font-size:11px;color:#334155}}
footer{{text-align:center;color:var(--sub);font-size:12px;margin-top:30px;padding-top:14px;border-top:1px solid var(--border)}}
</style></head>
<body>
<header><div class="wrap">
<h1>{case_name} · 数据模型地图</h1>
<div class="sub">六层数仓全景：层职责 · 表血缘 · 字段字典（点击表卡片展开）—— 由 warehouse.db 确定性生成，零 token</div>
<div class="stats">
<span>🏗 {n_layers} 层</span><span>📋 {len(tables)} 表</span><span>🔢 {fmt_rows(total_rows)} 行</span>
<span>🔗 血缘 {sum(len(v) for v in lineage.values())} 条</span><span>📂 {db_path.name}</span>
</div></div></header>
<div class="wrap">
<div class="card"><h2 style="margin-top:0">层间血缘流</h2>
<svg class="flowsvg" viewBox="0 0 {svg_w} 120" xmlns="http://www.w3.org/2000/svg">
<defs><marker id="arr" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><polygon points="0 0, 7 3.5, 0 7" fill="#94a3b8"/></marker></defs>
{''.join(arrows)}{''.join(boxes)}
</svg>
<div style="font-size:11.5px;color:var(--sub)">直线=相邻层血缘（数字为表间引用数）；虚线弧=跳层血缘；源库以只读挂载（src），ODS 一个字不改照搬。</div></div>
<h2>六层泳道 · 点击表卡片看字段字典</h2>
<div class="lanes">{''.join(lanes)}</div>
{other_html}
<footer>数据模型地图 · {datetime.now().strftime('%Y-%m-%d %H:%M')} 生成 · python scripts/render_model_map.py &lt;案例目录&gt;<br>
口径与分层纪律见 artifacts/口径卡.md · 验证见 build/verify_output.log</footer>
</div></body></html>'''

def main():
    ap = argparse.ArgumentParser(description="数据模型地图：warehouse.db → 单文件 HTML")
    ap.add_argument("case_dir", help="案例目录（含 warehouse.db 与 build/）")
    ap.add_argument("--output", default=None, help="输出 HTML 路径（默认 <案例目录>/model-map.html）")
    args = ap.parse_args()

    case_dir = Path(args.case_dir)
    db_path = case_dir / "warehouse.db"
    if not db_path.exists():
        print(f"错误：找不到 {db_path}", file=sys.stderr); sys.exit(1)

    remarks, lineage, src_tables = parse_build_sqls(case_dir / "build")
    tables = read_warehouse(db_path)
    out = Path(args.output) if args.output else case_dir / "model-map.html"
    html = build_html(case_dir.name, tables, remarks, lineage, src_tables, db_path)
    out.write_text(html, encoding="utf-8")
    n = sum(1 for t in tables if layer_of(t) != "other")
    print(f"✅ 模型地图已生成：{out}（{n} 张分层表 / {len(tables)} 张全表）")

if __name__ == "__main__":
    main()
