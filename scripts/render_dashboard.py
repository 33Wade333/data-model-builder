#!/usr/bin/env python3
"""
分析看板渲染器 v2 —— warehouse.db 的 ADS 表 + 口径卡 → 单文件 dashboard.html（ECharts 内嵌离线）
视觉基准：飞书风格看板「Stripe 式柔和纵深」——浅灰画布 #f5f6f7 + 纯白卡片 14px 圆角
+ 三层柔和投影 + 品牌蓝 #1456f0 + 大数字 tabular-nums + KPI 卡内嵌 sparkline。
零 token：确定性脚本从建好的数仓静态生成。每个指标挂"怎么算的"口径气泡。
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

# ---------- 飞书风格设计 tokens ----------
CANVAS = "#f5f6f7"; SURFACE = "#fff"; INK = "#1f2329"
SUB = "#646a73"; MUTE = "#8f959e"; FAINT = "#aeb4bd"
BORDER = "rgba(16,24,40,.07)"; BORDER2 = "#dee0e3"; SPLIT = "#eff0f2"
BLUE = "#1456f0"; TEAL = "#00a6a6"; PURPLE = "#722ed1"; AMBER = "#ff7d00"
SERIES = [BLUE, TEAL, PURPLE, AMBER]
SHADOW = "0 1px 1px rgba(16,24,40,.04), 0 4px 12px rgba(16,24,40,.06), 0 16px 40px -12px rgba(16,24,40,.10)"
SHADOW_HOVER = "0 2px 2px rgba(16,24,40,.05), 0 8px 20px rgba(16,24,40,.09), 0 24px 56px -12px rgba(16,24,40,.16)"

TOPN = 15
ID_WORDS = ("id", "_id", "key", "code", "rank", "rnk", "year", "_at", "ts")

def is_idish(col):
    c = col.lower()
    return c.endswith(ID_WORDS) or c in ("id",)

def pick_measure(cols, prefer=("cnt", "count", "total", "cost", "amount")):
    numeric = [c for c, t in cols if ("INT" in (t or "").upper() or "REAL" in (t or "").upper())
               and not is_idish(c)]
    for p in prefer:
        for c in numeric:
            if p in c.lower():
                return c
    return numeric[0] if numeric else None

def pick_label(cols):
    for c, t in cols:
        if "TEXT" in (t or "").upper() and not is_idish(c):
            return c
    for c, t in cols:
        if not is_idish(c) and ("name" in c.lower() or "title" in c.lower() or "login" in c.lower()):
            return c
    return None

def find_year_col(cols):
    for c, t in cols:
        if "year" in c.lower():
            return c
    return None

def classify(tname, cols, nrows):
    names = [c for c, _ in cols]
    low = [n.lower() for n in names]
    if "kpi" in low and "value" in low:
        return "kpi_long"
    ycol = find_year_col(cols)
    if re.search(r"_(rank|top)$", tname) or any(n.endswith(("_rank", "_rnk")) for n in names):
        return "rank"
    if re.search(r"_trend$", tname) or (ycol and any("yoy" in n or "prev" in n for n in low)):
        return "trend"
    if re.search(r"_distribution$", tname) or ("pct" in " ".join(low) and nrows <= 15):
        return "distribution"
    if ycol and nrows >= 8:
        return "year_multi"
    if nrows <= 60:
        return "compare"
    return "skip"

def parse_caliber_cards(kal_path):
    """解析口径卡.md（兼容三种标题：### 口径卡 N：名 / ## 卡 N：名 / ## 口径卡：名）"""
    cards = []
    if not kal_path.exists():
        return cards
    text = kal_path.read_text(encoding="utf-8")
    for m in re.finditer(r"^#{2,3}\s*(?:口径卡\s*)?卡?\s*(?:\d+\s*)?[：:]\s*(.+?)\n(.*?)(?=\n#{2,3}\s|\Z)", text, re.S | re.M):
        name, body = m.group(1).strip(), m.group(2)
        ym = re.search(r"```(?:yaml)?\n(.*?)```", body, re.S)
        fields = {}
        if ym:
            for line in ym.group(1).splitlines():
                fm = re.match(r"^(指标名|分子|分母|分母状态过滤|只算什么|不算什么|豁免规则|更新周期)\s*[：:]\s*(.+)$", line.strip())
                if fm:
                    fields[fm.group(1)] = fm.group(2).strip()
        if fields:
            cards.append({"name": name, **fields})
    return cards

CN_TABLE_HINTS = {
    "genre": "类型", "tag": "标签", "distribution": "分布", "year": "年度", "movie": "电影",
    "popularity": "热度", "rating": "评分", "trend": "趋势", "overview": "总览", "kpi": "总览",
    "repo": "仓库", "contributor": "贡献者", "star": "Star", "fork": "Fork", "push": "推送",
    "strike": "撞击", "species": "物种", "airport": "机场", "state": "州", "phase": "阶段",
    "model": "机型", "military": "军用", "release": "发版", "issue": "Issue", "pr": "PR",
}

def table_hints(tname):
    return [cn for seg, cn in CN_TABLE_HINTS.items() if seg in tname.lower()]

def match_caliber(cards, *keywords):
    """卡名命中权重 3（名字最具体），分子/分母命中权重 1"""
    kws = [k for k in keywords if k and len(str(k)) >= 2]
    best, best_score = None, 0
    for card in cards:
        name, body = card["name"], card.get("分子", "") + card.get("分母", "")
        score = sum(3 for k in kws if str(k) in name) + sum(1 for k in kws if str(k) in body)
        if score > best_score:
            best, best_score = card, score
    return best if best_score > 0 else None

def read_ads(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    for (tname,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ads_%' ORDER BY name"):
        cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{tname}")')]
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{tname}"')]
        out.append((tname, cols, rows))
    conn.close()
    return out

def maybe_pct(colname, values):
    vs = [v for v in values if isinstance(v, (int, float))]
    if not vs or not re.search(r"rate|pct|ratio|share", colname, re.I):
        return False
    return max(vs) <= 1.0001

CN_METRICS = {
    "rating_cnt": "评分条数", "avg_rating": "平均分", "high_rating_rate": "好评率",
    "active_user_cnt": "活跃用户", "new_user_cnt": "新用户", "strike_cnt": "撞击次数",
    "serious_cnt": "严重撞击", "serious_rate_pct": "严重率", "cost_total": "维修成本",
    "star_cnt": "Star 数", "fork_cnt": "Fork 数", "pr_cnt": "PR 数",
    "commit_cnt": "提交数", "core_evt_cnt": "核心事件", "pr_merge_rate": "PR 合并率",
    "issue_cnt": "Issue 数", "tag_cnt": "标签次数", "user_cnt": "用户数", "movie_cnt": "电影数",
    "share_pct": "份额", "users_ge1_full": "有评分用户", "active_user_cnt_same_period": "同期活跃",
    "pct": "占比", "value": "数值", "repo_cnt": "仓库数", "contributor_cnt": "贡献者",
    "release_cnt": "发版数", "push_cnt": "推送量", "merged_cnt": "合并数",
}

def cn(col):
    return CN_METRICS.get(col, col)

def fmt_value(v):
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)

def spark_svg(values, color=BLUE, w=110, h=34, gid="sg0"):
    """KPI 卡内嵌 sparkline（飞书风格：渐变面积 + 1.8 描边）"""
    vs = [v for v in values if isinstance(v, (int, float))]
    if len(vs) < 2:
        return ""
    lo, hi = min(vs), max(vs)
    rng = (hi - lo) or 1
    pts = [f"{1 + i/(len(vs)-1)*(w-2):.1f},{h-3-(v-lo)/rng*(h-8):.1f}" for i, v in enumerate(vs)]
    poly = " ".join(pts)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{color}38"/><stop offset="1" stop-color="{color}00"/>'
            f'</linearGradient></defs>'
            f'<polygon points="1,{h-1} {poly} {w-1},{h-1}" fill="url(#{gid})"/>'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity=".85"/></svg>')

# ---------- ECharts 公共样式（飞书风格图表主题） ----------
def ec_base():
    return {
        "color": SERIES,
        "tooltip": {"trigger": "axis", "backgroundColor": "#fff", "borderColor": BORDER2,
                    "textStyle": {"color": INK, "fontSize": 11},
                    "extraCssText": "box-shadow:0 8px 24px rgba(16,24,40,.12);border-radius:10px;"},
        "legend": {"textStyle": {"color": SUB, "fontSize": 11}, "itemWidth": 14, "itemHeight": 8,
                   "icon": "roundRect"},
        "xAxis": {"axisLabel": {"color": MUTE, "fontSize": 10},
                  "axisLine": {"lineStyle": {"color": BORDER2}},
                  "axisTick": {"show": False}},
        "yAxis": {"splitLine": {"lineStyle": {"color": SPLIT}},
                  "axisLabel": {"color": MUTE, "fontSize": 10}},
    }

def merge(base, extra):
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out

def build_charts(ads_tables):
    """返回 (kpi_cards, charts)。KPI 卡带 spark 数据（year_multi 表可画历史小折线）。"""
    kpi_cards, charts = [], []
    spark_id = [0]
    for tname, cols, rows in ads_tables:
        ctype = classify(tname, cols, len(rows))
        names = [c for c, _ in cols]
        base = ec_base()
        if ctype == "kpi_long":
            for r in rows:
                kpi_cards.append({"label": r.get("kpi", ""), "value": fmt_value(r.get("value")),
                                  "remark": r.get("remark", ""), "spark": ""})
        elif ctype == "rank":
            label = pick_label(cols) or tname
            rcol = next((n for n in names if n.endswith(("_rank", "_rnk"))), None)
            ordered = sorted(rows, key=lambda r: (r.get(rcol) if r.get(rcol) is not None else 10**9,))
            top = ordered[:TOPN][::-1]
            meas = pick_measure(cols)
            if not meas:
                continue
            scale100 = maybe_pct(meas, [r[meas] for r in top])
            vals = [round(r[meas] * 100, 2) if scale100 else r[meas] for r in top]
            unit = "%" if scale100 else ""
            opt = merge(base, {
                "grid": {"left": 170, "right": 56, "top": 16, "bottom": 24},
                "xAxis": {"type": "value", "name": unit, "nameTextStyle": {"color": MUTE, "fontSize": 10}},
                "yAxis": {"type": "category", "data": [str(r[label])[:26] for r in top],
                          "axisLabel": {"color": INK, "fontSize": 11}},
                "series": [{"type": "bar", "name": cn(meas), "data": vals,
                            "barWidth": 14, "itemStyle": {"borderRadius": [0, 7, 7, 0],
                            "color": {"type": "linear", "x": 0, "y": 0, "x2": 1, "y2": 0,
                                      "colorStops": [{"offset": 0, "color": "#4d7dff"},
                                                     {"offset": 1, "color": BLUE}]}},
                            "label": {"show": True, "position": "right", "fontSize": 10.5,
                                      "color": SUB, "fontWeight": 600,
                                      "formatter": f"{{c}}{unit}"}}]})
            charts.append({"id": tname, "title": f"{tname} · Top{len(top)}",
                           "caliber_keys": [label, cn(label), meas, cn(meas), tname],
                           "option": opt})
        elif ctype in ("trend", "year_multi"):
            ycol = find_year_col(cols)
            if not ycol:
                continue
            rows_s = sorted([r for r in rows if r.get(ycol) is not None], key=lambda r: r[ycol])
            years = [r[ycol] for r in rows_s]
            series, ynames, sparkdata = [], [], {}
            for n in names:
                if is_idish(n) or n == ycol:
                    continue
                t = dict(cols).get(n) or ""
                if "INT" in t.upper() or "REAL" in t.upper():
                    vals = [r[n] for r in rows_s]
                    scaled = maybe_pct(n, vals)
                    if scaled:
                        vals = [round(v * 100, 2) if v is not None else None for v in vals]
                    series.append({"name": cn(n), "type": "line", "data": vals, "smooth": True,
                                   "symbol": "none", "lineStyle": {"width": 2.2},
                                   "areaStyle": {"opacity": 0.06}})
                    ynames.append(n)
                    sparkdata[n] = (vals, scaled)
                    if len(series) >= 6:
                        break
            if not series:
                continue
            if ctype == "year_multi" and rows_s:
                last = rows_s[-1]
                for n in ynames[:5]:
                    v = last[n]
                    disp = v
                    if v is not None and maybe_pct(n, [v]):
                        disp = f"{v*100:.1f}%"
                    spark_id[0] += 1
                    vals, scaled = sparkdata.get(n, ([], False))
                    kpi_cards.append({"label": f"{cn(n)} · {last[ycol]}年",
                                      "value": fmt_value(disp) if not isinstance(disp, str) else disp,
                                      "remark": "", "spark": spark_svg(vals, BLUE, gid=f"sg{spark_id[0]}")})
            opt = merge(base, {
                "legend": {"top": 0, "type": "scroll"},
                "grid": {"left": 56, "right": 30, "top": 36, "bottom": 42},
                "xAxis": {"type": "category", "data": years},
                "dataZoom": [{"type": "inside"}] if len(years) > 20 else [],
                "yAxis": {"type": "value"},
                "series": series})
            charts.append({"id": tname, "title": f"{tname} · 历年趋势",
                           "caliber_keys": [tname] + [x for n in ynames[:3] for x in (n, cn(n))],
                           "option": opt})
        elif ctype in ("distribution", "compare"):
            label = pick_label(cols)
            meas = pick_measure(cols) or (names[-1] if names else None)
            if not label:
                label = next((n for n in names if not is_idish(n) and n != meas), None)
            if not label or not meas or not rows:
                continue
            scale100 = maybe_pct(meas, [r[meas] for r in rows])
            vals = [round(r[meas] * 100, 2) if scale100 else r[meas] for r in rows]
            opt = merge(base, {
                "grid": {"left": 56, "right": 26, "top": 24, "bottom": 52},
                "xAxis": {"type": "category", "data": [str(r[label])[:14] for r in rows],
                          "axisLabel": {"rotate": 30, "fontSize": 10, "color": SUB}},
                "yAxis": {"type": "value", "name": "%" if scale100 else "",
                          "nameTextStyle": {"color": MUTE, "fontSize": 10}},
                "series": [{"type": "bar", "name": cn(meas), "data": vals, "barWidth": 22,
                            "itemStyle": {"borderRadius": [6, 6, 0, 0], "color": BLUE},
                            "label": {"show": len(rows) <= 15, "position": "top", "fontSize": 10.5,
                                      "color": SUB, "fontWeight": 600}}]})
            charts.append({"id": tname, "title": f"{tname}",
                           "caliber_keys": [label, cn(label), meas, cn(meas), tname],
                           "option": opt})
    return kpi_cards, charts

def main():
    ap = argparse.ArgumentParser(description="分析看板 v2：ADS + 口径卡 → 单文件 HTML（飞书风格）")
    ap.add_argument("case_dir")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    case_dir = Path(args.case_dir)
    db_path = case_dir / "warehouse.db"
    if not db_path.exists():
        print(f"错误：找不到 {db_path}", file=sys.stderr); sys.exit(1)

    echarts_path = Path(__file__).parent / "vendor" / "echarts.min.js"
    echarts_js = echarts_path.read_text(encoding="utf-8") if echarts_path.exists() else ""
    if not echarts_js:
        print("警告：vendor/echarts.min.js 缺失，看板将无图表", file=sys.stderr)

    cards = parse_caliber_cards(case_dir / "artifacts" / "口径卡.md")
    kpi_cards, charts = build_charts(read_ads(db_path))
    for ch in charts:
        ch["caliber"] = match_caliber(cards, *ch["caliber_keys"], *table_hints(ch["id"]))
        ch.pop("caliber_keys", None)

    kpi_html = "".join(
        f'<div class="kpi"><div class="kl">{c["label"]}</div>'
        f'<div class="kv">{c["value"]}</div>'
        + (f'<div class="ks">{c["spark"]}</div>' if c.get("spark") else "")
        + (f'<div class="kr">{c["remark"]}</div>' if c.get("remark") else "") + "</div>"
        for c in kpi_cards[:10])

    charts_html = []
    for ch in charts:
        cal = ch.get("caliber")
        tip = ""
        if cal:
            tip = (f'<span class="cal-tip">口径 ⓘ<span class="cal-pop">'
                   f'<b>{cal["name"]}</b><br>分子：{cal.get("分子","—")}<br>'
                   f'分母：{cal.get("分母","—")}<br>'
                   f'只算：{cal.get("只算什么","—")}<br>不算：{cal.get("不算什么","—")}'
                   + (f'<br>豁免：{cal["豁免规则"]}' if cal.get("豁免规则", "无") not in ("", "无") else "")
                   + "</span></span>")
        charts_html.append(
            f'<div class="panel"><div class="ptitle">{ch["title"]}{tip}</div>'
            f'<div class="chart" id="{ch["id"]}"></div></div>')
    cal_panels = "".join(
        f'<details class="cal-card"><summary>{c["name"]}</summary>'
        f'<div>分子：{c.get("分子","—")}<br>分母：{c.get("分母","—")}<br>'
        f'只算：{c.get("只算什么","—")}<br>不算：{c.get("不算什么","—")}'
        + (f'<br>豁免：{c["豁免规则"]}' if c.get("豁免规则", "无") not in ("", "无") else "")
        + f'<br>更新：{c.get("更新周期","—")}</div></details>'
        for c in cards)

    opts_js = json.dumps({ch["id"]: ch["option"] for ch in charts}, ensure_ascii=False)
    out = Path(args.output) if args.output else case_dir / "dashboard.html"
    out.write_text(f'''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{case_dir.name} · 分析看板</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"Inter","Microsoft YaHei",system-ui,sans-serif;background:{CANVAS};color:{INK};
 -webkit-font-smoothing:antialiased}}
.page{{max-width:1560px;margin:0 auto;padding:24px}}
header{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:18px}}
h1{{font-size:20px;font-weight:600;letter-spacing:-.01em}}
.hmeta{{font-size:12px;color:{MUTE}}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(236px,1fr));gap:16px;margin-bottom:16px}}
.kpi{{position:relative;overflow:hidden;padding:16px 18px 13px;background:{SURFACE};
 border:1px solid {BORDER};border-radius:14px;box-shadow:{SHADOW},inset 0 1px 0 rgba(255,255,255,.7);
 transition:transform .18s cubic-bezier(.4,0,.2,1),box-shadow .18s cubic-bezier(.4,0,.2,1)}}
.kpi::before{{content:"";position:absolute;inset:0;pointer-events:none;
 background:radial-gradient(circle at 100% 0,rgba(20,86,240,.05),transparent 65%)}}
.kpi:hover{{transform:translateY(-2px);box-shadow:{SHADOW_HOVER},inset 0 1px 0 rgba(255,255,255,.8)}}
.kl{{font-size:12px;color:{SUB}}}
.kv{{font-size:30px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums;
 color:{INK};margin-top:2px}}
.ks{{margin-top:6px}} .kr{{font-size:10.5px;color:{FAINT};margin-top:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:16px}}
.panel{{background:{SURFACE};border:1px solid {BORDER};border-radius:14px;padding:18px 22px 16px;
 box-shadow:{SHADOW},inset 0 1px 0 rgba(255,255,255,.7)}}
.ptitle{{font-size:14px;font-weight:600;color:{INK};display:flex;align-items:center;gap:10px;margin-bottom:6px}}
.chart{{width:100%;height:340px}}
.cal-tip{{position:relative;cursor:help;font-size:11px;font-weight:500;color:{BLUE};background:#eaf0ff;
 border-radius:999px;padding:2px 10px}}
.cal-pop{{display:none;position:absolute;top:26px;left:0;z-index:99;width:360px;background:#fff;
 color:{INK};border:1px solid {BORDER2};border-radius:12px;padding:14px 16px;font-size:12px;
 font-weight:400;line-height:1.9;box-shadow:0 12px 32px rgba(16,24,40,.16);text-align:left}}
.cal-tip:hover .cal-pop{{display:block}}
.cal-pop b{{color:{BLUE}}}
h2{{font-size:16px;font-weight:600;margin:28px 0 12px;color:{INK}}}
.cal-card{{background:{SURFACE};border:1px solid {BORDER};border-radius:12px;padding:12px 16px;
 margin-bottom:8px;font-size:12.5px;box-shadow:{SHADOW}}}
.cal-card summary{{cursor:pointer;font-weight:600;color:{INK}}}
.cal-card div{{margin-top:8px;color:{SUB};line-height:2}}
footer{{text-align:center;color:{FAINT};font-size:11.5px;margin-top:30px;padding-top:16px;
 border-top:1px solid {BORDER2}}}
</style></head>
<body>
<div class="page">
<header>
<h1>{case_dir.name} · 分析看板</h1>
<div class="hmeta">由 ADS 层确定性生成 · 零 token · 口径全部业务拍板（悬停 ⓘ 查看每个数怎么算的）</div>
</header>
{f'<div class="kpis">{kpi_html}</div>' if kpi_html else ''}
<div class="grid">{''.join(charts_html)}</div>
{f'<h2>指标口径 · 业务拍板记录（{len(cards)} 条）</h2>{cal_panels}' if cal_panels else ''}
<footer>python scripts/render_dashboard.py · 数据 warehouse.db/ADS · 口径 artifacts/口径卡.md ·
 {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</div>
<script>{echarts_js}</script>
<script>
const OPTS = {opts_js};
for (const [id, opt] of Object.entries(OPTS)) {{
    const el = document.getElementById(id);
    if (el && window.echarts) echarts.init(el).setOption(opt);
}}
window.addEventListener('resize', () => {{
    document.querySelectorAll('.chart').forEach(el => {{
        const c = echarts.getInstanceByDom(el); if (c) c.resize();
    }});
}});
</script>
</body></html>''', encoding="utf-8")
    n_cal = sum(1 for ch in charts if ch.get("caliber"))
    print(f"✅ 分析看板 v2 已生成：{out}（{len(charts)} 图表 / {len(kpi_cards)} 指标卡含 sparkline / "
          f"口径 {len(cards)} 条，挂口径 {n_cal} 张）")

if __name__ == "__main__":
    main()
