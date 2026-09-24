#!/usr/bin/env python3
"""
dashboard_contracts.py —— 看板能力内核 v1：数据字典 + 生成闸门
四支柱之①数据字典 + ②契约校验：治"字段错位 / 丢字段 / 中文映射缺失"
落地形态（用户拍板）：独立规则模块（与渲染器分离，dbt/MCP 同构）+ 混合选型（AI 查表 + 闸门打回）

用法：
  python dashboard_contracts.py <case_dir> --config <cfg.yaml>          # 跑闸门校验（错误则退出码 1）
  python dashboard_contracts.py <case_dir> --export-dict <out.json>    # 导出数据字典
  python dashboard_contracts.py <case_dir> --config <cfg> --report <out.md>  # 校验 + 报告
"""
import argparse, json, re, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render_universal_dashboard import COL_CN, parse_caliber_cards  # 复用现有翻译字典与口径卡解析

# ===== 字段中文业务名补录注册表（人工补录落点；与渲染器 COL_CN 分离，字典层唯一事实源） =====
# 2026-09-22 用户确认补录：MovieLens 案例缺失项
FIELD_CN = {
    "rating_year": "评分年份",
    "genre_code": "类型代码",
    "users_ge1_full": "完整年活跃用户数",
    "active_user_cnt": "活跃用户数",
    # 2026-09-22 补录：鸟撞案例（闸门首次执勤抓出）
    "model_name": "机型名称",
}

# ===== 语义类型推断（vega-lite 启发：字段语义是选型原子输入） =====
def infer_semantic(name, sql_type):
    n = (name or "").lower(); t = (sql_type or "").upper()
    if re.search(r"(_id|_key)$", n): return "id"            # 技术键（下钻表默认隐藏的候选）
    if re.search(r"(_rank|_rnk)$", n): return "rank"        # 排名序数（展示用维度）
    if re.search(r"(date|_ts$|_time$|_year$|_month$|_dt$)", n) or "DATE" in t: return "date"
    if re.match(r"(is_|has_)", n) or "BOOL" in t: return "bool"
    if re.search(r"rate|pct|ratio|share", n): return "rate"  # 度量-率（0-1 自适应 ×100 的现有种子）
    if re.search(r"(_cnt|_count|_total|_sum|_avg|_score|_num|_amount|_gmv|_value)$", n): return "measure"
    if t in ("INTEGER", "REAL", "DOUBLE", "FLOAT", "NUMERIC"): return "measure"
    return "dimension"

def infer_visible(semantic):
    # Metabase 可见性三档：everywhere / detail（只在明细展开时显示）/ hidden
    return "detail" if semantic == "id" else "everywhere"

# ===== 数据字典提取（从 warehouse.db 的 ADS 层） =====
def extract_dictionary(case_dir, cfg=None):
    conn = sqlite3.connect(f"file:{case_dir/'warehouse.db'}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'ads_%' OR name LIKE 'dwd_%') ORDER BY name")]
    # 口径卡与 config 指标名对齐：metric 中文名 → source.column 的显示名兜底
    metric_cn = {}
    if cfg:
        for m in cfg.get("metrics", []):
            src = m.get("source") or {}
            if src.get("column") and m.get("name"):
                metric_cn[src["column"]] = m["name"]
    dictionary = {}
    for t in tables:
        fields = {}
        for _cid, name, sql_type, *_ in conn.execute(f'PRAGMA table_info("{t}")'):
            sem = infer_semantic(name, sql_type)
            cn = FIELD_CN.get(name) or COL_CN.get(name) or metric_cn.get(name) or ""
            fields[name] = {"sql_type": sql_type or "", "semantic": sem,
                            "cn": cn, "visible": infer_visible(sem)}
        dictionary[t] = fields
    conn.close()
    return dictionary

# ===== 引用收集：config 里所有 (表, 字段) 引用 =====
def _refs_of_component(c):
    t = c.get("type"); r = []; d = c
    def add(table, *cols):
        for col in cols:
            if col: r.append((table, col, f"components[{d.get('id','?')}].{col}"))
    if t in ("bar", "pie"):
        add(d.get("table"), d.get("dimension"), d.get("value_col"))
    elif t == "heatmap":
        add(d.get("table"), d.get("dimension"))
        for e in d.get("metrics", []):
            if isinstance(e, dict): add(d.get("table"), e.get("column"))
    elif t == "table":
        add(d.get("table"), *(d.get("columns") or []))
    elif t == "histogram":
        add(d.get("table"), d.get("column"), d.get("count_col"))
    elif t == "area_stack":
        add(d.get("table"), d.get("dimension"), d.get("time_col"), d.get("metric"))
    elif t == "scatter":
        add(d.get("table"), d.get("x"), d.get("y"))
    elif t == "radar":
        add(d.get("table"), d.get("dimension"))
    return r

def collect_references(cfg):
    refs = []  # (table, field, loc)
    for m in cfg.get("metrics", []):
        src = m.get("source") or {}
        refs.append((src.get("table"), src.get("column"), f"metrics[{m.get('id','?')}].source.column"))
        for k in (src.get("key") or {}):
            refs.append((src.get("table"), k, f"metrics[{m.get('id','?')}].source.key.{k}"))
    tcfg = cfg.get("time") or {}
    if tcfg: refs.append((tcfg.get("table"), tcfg.get("column"), "time.column"))
    comps = []
    for dom in cfg.get("domains", []) or [{"components": cfg.get("components", [])}]:
        comps.extend(dom.get("components", []))
    for c in comps:
        refs.extend(_refs_of_component(c))
    drill = cfg.get("drill") or {}
    for s in (drill.get("l2") or {}).get("slices", []):
        src = s.get("source") or {}
        refs.append((src.get("table"), src.get("column"), "drill.l2.slices.source.column"))
    for dd in (drill.get("l2") or {}).get("dimensions", []):
        refs.append((dd.get("table"), dd.get("dimension"), "drill.l2.dimensions.dimension"))
    l3 = drill.get("l3") or {}
    if l3:
        for col in l3.get("columns", []):
            refs.append((l3.get("table"), col, "drill.l3.columns"))
        for _dim, col in (l3.get("dimension_map") or {}).items():
            refs.append((l3.get("table"), col, "drill.l3.dimension_map"))
    return refs, comps

# ===== 展示字段集（中文映射完整性校验的范围） =====
def displayed_fields(cfg, dictionary):
    """返回 [(table, field, loc, 场景)]：所有会以列头形式展示给用户的字段"""
    out = []
    drill = cfg.get("drill") or {}
    for dd in (drill.get("l2") or {}).get("dimensions", []):
        t = dd.get("table")
        for f in dictionary.get(t, {}):
            out.append((t, f, f"drill.l2.dimensions[{t}]", "L2 下钻表（SELECT * 全列展示）"))
    l3 = drill.get("l3") or {}
    if l3:
        for col in l3.get("columns", []):
            out.append((l3.get("table"), col, "drill.l3.columns", "L3 明细表"))
    comps = []
    for dom in cfg.get("domains", []) or [{"components": cfg.get("components", [])}]:
        comps.extend(dom.get("components", []))
    for c in comps:
        if c.get("type") == "table":
            for col in (c.get("columns") or []):
                out.append((c.get("table"), col, f"components[{c.get('id','?')}].columns", "L1 表格"))
    return out

# ===== 选型规则表（数据形态 → 组件映射） =====
# 来源：Metabase sensibility（行数×指标数×维度类型三判定）+ AntV 选型指南（硬约束数字）+ vega-lite（字段语义类型为原子输入）
# 双用：①闸门校验（选型不当=警告，不阻断——用户拍板分级②）②Skill/config 指南规则源（对话终态教育 AI，同一套规则两期复用）
SELECTION_RULES = {
    # 意图: (数据形态要求, 推荐组件按优先级)
    "趋势":     ("时序列 + 度量", ["line", "area_stack"]),
    "构成":     ("分类维 + 度量", ["pie", "bar"]),
    "排名":     ("分类维 + 度量", ["bar"]),
    "分布":     ("单个数值列", ["histogram"]),
    "交叉":     ("分类维 + ≥2 指标", ["heatmap"]),
    "明细":     ("任意", ["table"]),
    "流向":     ("≥2 阶段度量", ["funnel", "sankey"]),
    "目标":     ("单值 + 目标值", ["bullet", "gauge"]),
    "相关":     ("两个数值列", ["scatter"]),
    "多维对比": ("分类维 + ≥3 指标", ["radar"]),
}

# 每组件类型的数据形态前置条件（闸门校验用；违反=警告不阻断）
TYPE_PREREQUISITES = {
    "line":       "需要时序数据（全局 time 配置，或指标源表含日期语义列）",
    "area_stack": "需要 时间列 + 分类维 + 度量",
    "pie":        "需要 分类维 + 度量；构成 >5 块自动合并'其他'（AntV 上限 9，从严取 5）",
    "bar":        "需要 分类维 + 度量；分类数 ≤20（AntV 硬约束）",
    "histogram":  "需要数值列（measure 语义）",
    "scatter":    "需要两个数值列（x/y 都是 measure）",
    "heatmap":    "需要 分类维 + ≥2 指标",
    "radar":      "需要 ≥3 个指标（两维雷达退化为折线）",
    "funnel":     "需要 ≥2 个阶段",
    "bullet":     "需要目标值 target",
    "gauge":      "需要单值 + 量程 max（默认 100）",
    "kpi":        "需要单值指标", "table": "任意数据形态",
    "text": "无数据要求", "heading": "无数据要求",
    "waterfall": "字面 items（增量构成）", "sankey": "字面 nodes/links（流向）",
}

def _sem(dictionary, table, field):
    return (dictionary.get(table) or {}).get(field, {}).get("semantic", "")

def check_selection(cfg, dictionary):
    """选型合规校验（警告级，不阻断）：组件类型 vs 数据形态的匹配核对"""
    findings = []
    tcfg = cfg.get("time") or {}
    metrics_src = {}
    for m in cfg.get("metrics", []):
        src = m.get("source") or {}
        metrics_src[m.get("id")] = src

    def has_time(table):
        if tcfg.get("table") == table and tcfg.get("column"): return True
        return any(_sem(dictionary, table, f) == "date" for f in dictionary.get(table, {}))

    comps = []
    for dom in cfg.get("domains", []) or [{"components": cfg.get("components", [])}]:
        comps.extend(dom.get("components", []))

    for c in comps:
        t, cid = c.get("type"), c.get("id", "?")
        def warn(msg): findings.append({"level": "warning", "loc": f"components[{cid}]", "msg": msg})
        if t == "line":
            if not tcfg and not any(has_time(s.get("table")) for s in metrics_src.values() if s.get("table")):
                warn(f"选型提醒：折线需要时序数据，但全局无 time 配置且指标源表无日期语义列。{TYPE_PREREQUISITES['line']}")
        elif t in ("bar", "pie"):
            dim_sem = _sem(dictionary, c.get("table"), c.get("dimension"))
            if dim_sem in ("measure", "rate"):
                warn(f"选型提醒：{t} 的分类轴字段 '{c.get('dimension')}' 是数值型（{dim_sem}），分类轴应是分类字段")
            top = c.get("top", 8 if t == "pie" else 5)
            limit = 9 if t == "pie" else 20
            if top > limit:
                warn(f"选型提醒：{t} 的 top={top} 超过 AntV 硬约束 {limit}（分类过多可读性差）")
        elif t == "histogram":
            if _sem(dictionary, c.get("table"), c.get("column")) not in ("measure", "rate"):
                warn(f"选型提醒：直方图的列 '{c.get('column')}' 不是数值列。{TYPE_PREREQUISITES['histogram']}")
        elif t == "scatter":
            for ax in ("x", "y"):
                if _sem(dictionary, c.get("table"), c.get(ax)) not in ("measure", "rate"):
                    warn(f"选型提醒：散点 {ax} 轴字段 '{c.get(ax)}' 不是数值列。{TYPE_PREREQUISITES['scatter']}")
        elif t == "heatmap":
            if len(c.get("metrics", [])) < 2:
                warn(f"选型提醒：热力矩阵只有 {len(c.get('metrics', []))} 个指标。{TYPE_PREREQUISITES['heatmap']}")
        elif t == "radar":
            if len(c.get("metrics", [])) < 3:
                warn(f"选型提醒：雷达图只有 {len(c.get('metrics', []))} 个指标。{TYPE_PREREQUISITES['radar']}")
        elif t == "funnel":
            if len(c.get("stages", [])) < 2:
                warn(f"选型提醒：漏斗只有 {len(c.get('stages', []))} 个阶段。{TYPE_PREREQUISITES['funnel']}")
        elif t == "bullet":
            if c.get("target") is None:
                warn(f"选型提醒：子弹图缺目标值。{TYPE_PREREQUISITES['bullet']}")
    return findings

# ===== 布局行满校验（硬阻断级，按用户拍板分级②） =====
CARD_LIKE = {"kpi", "bullet", "gauge", "radar"}  # 孤卡保持档位宽，不强制拉伸

def check_layout(cfg):
    """跑与渲染器同款的归一化，验证每行 span 之和=24（行满算法的安全网）"""
    import render_universal_dashboard as R
    try:
        domains, _ = R.normalize_domains(cfg)
    except Exception as e:
        return [{"level": "error", "loc": "components", "msg": f"布局归一化失败：{e}"}]
    findings = []
    for dom in domains:
        rows = {}
        for c in dom["components"]:
            g = c.get("grid") or {}
            rows.setdefault(g.get("y"), []).append(c)
        for y, row in rows.items():
            if not row: continue
            total = sum((c.get("grid") or {}).get("w", 0) for c in row)
            if total != 24 and not (len(row) == 1 and row[0]["type"] in CARD_LIKE):
                names = ", ".join(str(c.get("id")) for c in row)
                findings.append({"level": "error", "loc": f"domains[{dom.get('id')}] 行 y={y}",
                    "msg": f"行未填满：[{names}] 列宽合计 {total} ≠ 24——行满算法未生效或被绕过"})
    return findings

# ===== 闸门 v1：三项校验（字段存在性 / 中文映射完整性 / 列与数据对齐） =====
def validate_config(cfg, case_dir):
    import yaml
    dictionary = extract_dictionary(case_dir, cfg)
    findings = []  # {level, loc, msg}  level: error=打回 / warning=放行但提示
    refs, comps = collect_references(cfg)

    # ① 字段存在性
    for table, field, loc in refs:
        if table is None: continue
        if table not in dictionary:
            findings.append({"level": "error", "loc": loc,
                "msg": f"表 '{table}' 不存在（可用 ADS 表：{', '.join(list(dictionary)[:8])}{'…' if len(dictionary)>8 else ''}）"})
        elif field not in dictionary[table]:
            cands = [f for f in dictionary[table] if field and field[:4].lower() in f.lower()][:5]
            hint = f"——是否想写：{', '.join(cands)}？" if cands else ""
            findings.append({"level": "error", "loc": loc,
                "msg": f"字段 '{field}' 不存在于表 '{table}'（该表共 {len(dictionary[table])} 个字段）{hint}"})

    # ② 中文映射完整性（展示字段必须有中文业务名）
    for table, field, loc, scene in displayed_fields(cfg, dictionary):
        if table not in dictionary or field not in dictionary.get(table, {}): continue  # ①已报
        meta = dictionary[table][field]
        if not meta["cn"]:
            findings.append({"level": "error", "loc": loc,
                "msg": f"字段 '{table}.{field}' 将在{scene}展示，但数据字典无中文业务名（用户会看到英文原始列名）。"
                       f"请在字典中补 cn，或在该 config 位置显式指定列显示名"})
        elif meta["semantic"] == "id" and scene.startswith("L2"):
            findings.append({"level": "warning", "loc": loc,
                "msg": f"字段 '{table}.{field}' 是技术键（id），在{scene}全列展示中建议隐藏（字典 visible=detail）"})

    # ③ 表格列与数据一一对齐（dry-run 查询 + 列头去重检查）
    conn = sqlite3.connect(f"file:{case_dir/'warehouse.db'}?mode=ro", uri=True)
    for c in comps:
        if c.get("type") == "table":
            t, cols = c.get("table"), c.get("columns")
            if not cols:  # 缺省 SELECT * 全列
                cols = list(dictionary.get(t, {}).keys())
            names = [COL_CN.get(x, x) for x in cols]
            dup = {n for n in names if names.count(n) > 1}
            if dup:
                findings.append({"level": "error", "loc": f"components[{c.get('id','?')}]",
                    "msg": f"表格列头重名：{', '.join(dup)}——用户会以为字段错位。请给重名列配不同中文显示名"})
            try:
                conn.execute(f'SELECT {", ".join(chr(34)+x+chr(34) for x in cols)} FROM "{t}" LIMIT 1')
            except sqlite3.Error as e:
                findings.append({"level": "error", "loc": f"components[{c.get('id','?')}]",
                    "msg": f"表格查询 dry-run 失败：{e}"})
    for dd in (cfg.get("drill") or {}).get("l2", {}).get("dimensions", []):
        try:
            conn.execute(f'SELECT * FROM "{dd.get("table")}" LIMIT 1')
        except sqlite3.Error as e:
            findings.append({"level": "error", "loc": f"drill.l2.dimensions[{dd.get('table')}]",
                "msg": f"下钻表查询 dry-run 失败：{e}"})
    conn.close()
    findings.extend(check_selection(cfg, dictionary))  # ④ 选型合规（警告级，不阻断）
    findings.extend(check_layout(cfg))  # ⑤ 布局行满（硬阻断级）
    seen, uniq = set(), []  # 去重：同 (级别, 位置, 消息前缀) 只报一次
    for f in findings:
        key = (f["level"], f["loc"], f["msg"][:40])
        if key not in seen:
            seen.add(key); uniq.append(f)
    return uniq, dictionary

def main():
    ap = argparse.ArgumentParser(description="看板能力内核 v1：数据字典 + 生成闸门")
    ap.add_argument("case_dir")
    ap.add_argument("--config", default=None)
    ap.add_argument("--export-dict", default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    case_dir = Path(args.case_dir)
    cfg = None
    if args.config:
        import yaml
        p = Path(args.config)
        if not p.is_absolute() and not p.exists():
            p = case_dir / args.config  # 相对路径先按 cwd 解析，不存在再按 case_dir
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    if args.export_dict:
        d = extract_dictionary(case_dir, cfg)
        Path(args.export_dict).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        nf = sum(len(v) for v in d.values())
        print(f"✅ 数据字典已导出：{args.export_dict}（{len(d)} 表 / {nf} 字段）")
    if args.config:
        findings, dictionary = validate_config(cfg, case_dir)
        errors = [f for f in findings if f["level"] == "error"]
        warns = [f for f in findings if f["level"] == "warning"]
        for f in errors: print(f"❌ [error] {f['loc']}: {f['msg']}")
        for f in warns: print(f"⚠️  [warning] {f['loc']}: {f['msg']}")
        print(f"\n闸门结果：{'❌ 打回（' + str(len(errors)) + ' 项错误）' if errors else '✅ 放行'} · {len(warns)} 项警告 · 字典 {len(dictionary)} 表")
        if args.report:
            lines = ["# 闸门校验报告（dashboard_contracts v1）", "",
                     f"- 校验对象：`{args.config}`",
                     f"- 结果：{'❌ 打回' if errors else '✅ 放行'}（错误 {len(errors)} / 警告 {len(warns)}）", ""]
            for f in errors: lines.append(f"## ❌ {f['loc']}\n\n{f['msg']}\n")
            for f in warns: lines.append(f"## ⚠️ {f['loc']}\n\n{f['msg']}\n")
            Path(args.report).write_text("\n".join(lines), encoding="utf-8")
            print(f"报告已写入：{args.report}")
        sys.exit(1 if errors else 0)

if __name__ == "__main__":
    main()
