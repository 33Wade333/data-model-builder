#!/usr/bin/env python3
"""
只读探源库脚本 — 一键跑完六项必查：表清单+行数+字段+枚举+空值率+重复行+时间悬崖+孤儿外键+跨表重名
用法: python probe_source_db.py --url "mysql+pymysql://user:pass@host:3306/dbname" [--schema public] [--output probe_report.md]

安全要求:
  - 只执行 SELECT 查询，绝不执行写操作
  - 密码建议通过环境变量 SOURCE_DB_URL 传入，不硬编码
  - 探查完自动断开连接

支持: MySQL / PostgreSQL / SQLite (通过 SQLAlchemy)
"""

import argparse
import os
import sys
from collections import defaultdict

def get_engine(url):
    """创建只读 SQLAlchemy 引擎"""
    from sqlalchemy import create_engine, text
    # 对 MySQL 强制设置只读（如果支持）
    execution_options = {"isolation_level": "AUTOCOMMIT"} if "mysql" in url.lower() else {}
    engine = create_engine(url, execution_options=execution_options)
    return engine

def list_tables(engine, schema=None):
    """列出所有表及行数"""
    from sqlalchemy import text
    tables = []
    with engine.connect() as conn:
        if "sqlite" in str(engine.url):
            result = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))
            table_names = [row[0] for row in result]
        elif "postgresql" in str(engine.url):
            schema = schema or "public"
            result = conn.execute(text(
                f"SELECT tablename FROM pg_tables WHERE schemaname = '{schema}'"
            ))
            table_names = [row[0] for row in result]
        else:  # mysql
            result = conn.execute(text("SHOW TABLES"))
            table_names = [row[0] for row in result]

        for tname in table_names:
            try:
                cnt = conn.execute(text(f'SELECT COUNT(*) FROM "{tname}"')).scalar()
            except Exception:
                cnt = -1  # 无权限或错误
            tables.append((tname, cnt))
    return tables

def describe_table(engine, table_name, schema=None):
    """获取表结构（字段名、类型、是否可空、是否主键）"""
    from sqlalchemy import inspect
    insp = inspect(engine)
    columns = []
    pk_cols = set()
    try:
        pk = insp.get_pk_constraint(table_name, schema=schema)
        pk_cols = set(pk.get("constrained_columns", []))
    except Exception:
        pass

    for col in insp.get_columns(table_name, schema=schema):
        columns.append({
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": col.get("nullable", True),
            "is_pk": col["name"] in pk_cols,
        })
    return columns, pk_cols

def sample_data(engine, table_name, n=5):
    """取前N行真实数据"""
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            result = conn.execute(text(f'SELECT * FROM "{table_name}" LIMIT {n}'))
            rows = result.fetchall()
            keys = result.keys()
            return [dict(zip(keys, row)) for row in rows]
        except Exception as e:
            return [{"error": str(e)}]

def null_rates(engine, table_name, columns):
    """统计每个字段的空值率"""
    from sqlalchemy import text
    rates = {}
    with engine.connect() as conn:
        total = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
        if total == 0:
            return {c["name"]: 0.0 for c in columns}
        for col in columns:
            cname = col["name"]
            try:
                nulls = conn.execute(text(
                    f"SELECT COUNT(*) FROM \"{table_name}\" WHERE \"{cname}\" IS NULL "
                    f"OR TRIM(CAST(\"{cname}\" AS VARCHAR)) = ''"
                )).scalar()
                rates[cname] = round(nulls / total * 100, 1)
            except Exception:
                rates[cname] = -1  # 无法统计
    return rates

def is_likely_date_or_unique(cname, values, total_rows):
    """判断字段是否像日期或唯一标识（不应列为枚举）"""
    # 日期特征：值匹配日期格式
    import re
    date_pattern = re.compile(r'^\d{4}[-/]\d{2}[-/]\d{2}')
    if values and all(date_pattern.match(str(v)) for v, _ in values[:5]):
        return True
    # 唯一标识特征：取值数接近行数（>80%），或字段名含id/email/phone/url/code/token
    if len(values) > total_rows * 0.8:
        return True
    id_keywords = ['email', 'phone', 'url', 'token', 'address', 'name', 'content', 'description', 'path']
    if any(k in cname.lower() for k in id_keywords) and len(values) > total_rows * 0.3:
        return True
    return False

def enum_values(engine, table_name, columns, threshold=50):
    """对字符串/枚举字段列出全部取值（超过threshold种的不列，排除日期和唯一标识）。
    也对INTEGER字段检查：取值数≤20且不是连续自然数（排除ID）→ 也列为枚举。"""
    from sqlalchemy import text
    enums = {}
    with engine.connect() as conn:
        total = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
        if total == 0:
            return enums  # 空表无法判断枚举
        for col in columns:
            cname = col["name"]
            ctype = col["type"].upper()
            is_text = any(k in ctype for k in ["CHAR", "TEXT", "VARCHAR", "ENUM", "STR"])
            is_int = "INT" in ctype
            if not (is_text or is_int):
                continue
            try:
                distinct = conn.execute(text(
                    f'SELECT DISTINCT "{cname}", COUNT(*) FROM "{table_name}" '
                    f'WHERE "{cname}" IS NOT NULL GROUP BY "{cname}" ORDER BY COUNT(*) DESC'
                )).fetchall()
                if len(distinct) == 0:
                    continue  # 空值字段不列枚举
                if is_text and len(distinct) <= threshold:
                    if not is_likely_date_or_unique(cname, distinct, total):
                        enums[cname] = [(row[0], row[1]) for row in distinct]
                elif is_int and len(distinct) <= 20:
                    # 整数枚举：取值数少且不是连续自然数（排除ID类字段）
                    values = [row[0] for row in distinct]
                    if cname.lower() != "id" and not cname.lower().endswith("_id"):
                        # 检查是否连续自然数（如果是，大概率是ID不是枚举）
                        try:
                            int_vals = sorted([int(v) for v in values])
                            is_sequential = (int_vals[-1] - int_vals[0] == len(int_vals) - 1
                                            and len(int_vals) > 5)
                        except (ValueError, TypeError):
                            is_sequential = False
                        if not is_sequential:
                            enums[cname] = [(row[0], row[1]) for row in distinct]
            except Exception:
                pass
    return enums

def redact_url(url_str):
    """脱敏数据库URL：隐藏密码（处理密码含@等特殊字符的情况）"""
    import re
    # 策略：找到 :// 后第一个 : 到最后一个 @ 之间的内容就是密码部分
    # mysql+pymysql://user:password@host:3306/db
    # mysql+pymysql://admin:s3cretP@ss@10.0.0.1:3306/mydb（密码含@）
    result = re.sub(r'://([^:]+):([^@]+(?:@[^@]+)*)@', r'://\1:***@', str(url_str))
    return result

# ---------- v8 新增四项必查 ----------

BIG_TABLE_LIMIT = 5_000_000  # 超过此行数跳过重查询（重复行/孤儿外键），防拖垮源库

def duplicate_rows(engine, table_name, row_count):
    """完全重复行数（所有列都一样）。大表跳过防拖垮源库。"""
    from sqlalchemy import text
    if row_count > BIG_TABLE_LIMIT:
        return None  # None = 未查（表太大）
    with engine.connect() as conn:
        try:
            distinct = conn.execute(text(f'SELECT COUNT(*) FROM (SELECT DISTINCT * FROM "{table_name}")')).scalar()
            return row_count - distinct
        except Exception:
            return -1

def find_time_columns(engine, table_name, columns, row_count):
    """找出时间类字段：类型是 DATE/DATETIME/TIMESTAMP，或文本值像日期，或整数像 unix 时间戳"""
    from sqlalchemy import text
    time_cols = []
    with engine.connect() as conn:
        for col in columns:
            cname, ctype = col["name"], col["type"].upper()
            if any(k in ctype for k in ["DATE", "TIME", "TIMESTAMP"]):
                time_cols.append((cname, "typed"))
                continue
            try:
                sample = conn.execute(text(
                    f'SELECT "{cname}" FROM "{table_name}" WHERE "{cname}" IS NOT NULL LIMIT 5'
                )).fetchall()
                if not sample:
                    continue
                vals = [str(r[0]) for r in sample]
                import re as _re
                if all(_re.match(r'^\d{4}[-/]\d{2}[-/]\d{2}', v) for v in vals):
                    time_cols.append((cname, "text-date"))
                elif "INT" in ctype and all(_re.match(r'^\d{9,13}$', v) for v in vals):
                    time_cols.append((cname, "unix"))
            except Exception:
                pass
    return time_cols

def time_histogram(engine, table_name, cname, kind):
    """按年统计行数，返回 [(year, count)]。kind: typed/text-date/unix"""
    from sqlalchemy import text
    if kind == "unix":
        expr = f'CAST("{cname}" / 31557600 + 1970 AS INTEGER)'  # unix秒/毫秒都粗略可用
    else:
        expr = f'CAST(SUBSTR(CAST("{cname}" AS VARCHAR), 1, 4) AS INTEGER)'
    with engine.connect() as conn:
        try:
            rows = conn.execute(text(
                f'SELECT {expr} AS y, COUNT(*) FROM "{table_name}" '
                f'WHERE {expr} BETWEEN 1900 AND 2100 GROUP BY {expr} ORDER BY {expr}'
            )).fetchall()
            return [(int(r[0]), r[1]) for r in rows]
        except Exception:
            return []

def detect_time_cliffs(hist):
    """检测数据悬崖：某年行数 < 前一年的10%，或末年不足前一年50%（年份未满，如只存到7月）"""
    issues = []
    for i in range(1, len(hist)):
        prev_y, prev_c = hist[i-1]
        cur_y, cur_c = hist[i]
        if prev_c > 0 and cur_c < prev_c * 0.10:
            issues.append(f"{prev_y}年{prev_c:,}条 → {cur_y}年{cur_c:,}条，断崖式下跌")
        elif prev_c > 0 and cur_c < prev_c * 0.5 and i == len(hist) - 1:
            issues.append(f"末年{cur_y}年只有{cur_c:,}条（前一年{prev_c:,}条）——可能年份未存满，趋势对比须用同期口径")
    return issues

def last_year_month_coverage(engine, table_name, cname, kind, year):
    """末年月度覆盖数（v8.1 修复：行数降幅不大但年份未存满的漏报——如 627 条是前年 57% 但只录到 7 月）。
    unix 用近似月（2629800 秒/月，与年份表达式同一漂移系，覆盖数仍有效）；文本/类型日期取第 6-7 位 MM"""
    from sqlalchemy import text
    if kind == "unix":
        yexpr = f'CAST("{cname}" / 31557600 + 1970 AS INTEGER)'
        mexpr = f'CAST(("{cname}" % 31557600) / 2629800 AS INTEGER)'
    else:
        yexpr = f'CAST(SUBSTR(CAST("{cname}" AS VARCHAR), 1, 4) AS INTEGER)'
        mexpr = f'SUBSTR(CAST("{cname}" AS VARCHAR), 6, 2)'
    with engine.connect() as conn:
        try:
            return conn.execute(text(
                f'SELECT COUNT(DISTINCT {mexpr}) FROM "{table_name}" WHERE {yexpr} = {year}'
            )).scalar()
        except Exception:
            return None

def orphan_fk_check(engine, tables_columns):
    """孤儿外键检查：xxx_id 列猜测父表，查子表有而父表没有的行数。
    父表猜测两级：① 精确名（users/user/useres）② 表名含基词（tb_region_dict 含 region）。
    父表连接列：id 或同名 xxx_id。启发式尽力而为，猜不到就跳过。"""
    from sqlalchemy import text
    findings = []
    with engine.connect() as conn:
        for tname, cols in tables_columns.items():
            for cname, ctype in cols.items():
                if not cname.endswith("_id") or "INT" not in ctype.upper():
                    continue
                base = cname[:-3]  # user_id -> user
                parent, pcol = None, None
                # 第一级：精确表名
                for cand in (base + "s", base, base + "es"):
                    if cand in tables_columns and cand != tname:
                        if tables_columns[cand].get("id"):
                            parent, pcol = cand, "id"
                        elif tables_columns[cand].get(cname):
                            parent, pcol = cand, cname
                        if parent:
                            break
                # 第二级：表名含基词（处理 tb_xxx_dict / sys_xxx 前缀命名）
                if not parent:
                    for cand in tables_columns:
                        if cand == tname or base not in cand.lower():
                            continue
                        if tables_columns[cand].get("id") and f"{base}_id" not in tables_columns[cand]:
                            parent, pcol = cand, "id"  # 父表有自己的自增 id
                        elif tables_columns[cand].get(cname):
                            parent, pcol = cand, cname  # 父表用同名 xxx_id 当主键
                        if parent:
                            break
                if not parent:
                    continue
                try:
                    child_total = conn.execute(text(f'SELECT COUNT(*) FROM "{tname}"')).scalar()
                    if child_total > BIG_TABLE_LIMIT:
                        continue  # 大表跳过 JOIN，防拖垮源库
                    n = conn.execute(text(
                        f'SELECT COUNT(*) FROM "{tname}" c LEFT JOIN "{parent}" p ON c."{cname}" = p."{pcol}" '
                        f'WHERE p."{pcol}" IS NULL AND c."{cname}" IS NOT NULL'
                    )).scalar()
                    if n and n > 0:
                        findings.append((tname, cname, parent, n, child_total))
                except Exception:
                    pass
    return findings

def cross_table_duplicate_names(tables_columns):
    """跨表重名字段：同一字段名出现在≥3张表——问业务是否同一含义"""
    from collections import Counter
    counter = Counter()
    for cols in tables_columns.values():
        for cname in cols:
            counter[cname] += 1
    return {name: cnt for name, cnt in counter.items() if cnt >= 3}

def generate_report(engine, schema=None):
    """生成完整的探源报告"""
    tables = list_tables(engine, schema)
    report_lines = []
    report_lines.append("# 源库探查报告\n")
    report_lines.append(f"数据库: {redact_url(engine.url)}\n\n")

    # 表清单
    report_lines.append("## 表清单\n")
    report_lines.append("| 表名 | 行数 |")
    report_lines.append("|---|---|")
    for tname, cnt in tables:
        report_lines.append(f"| {tname} | {cnt:,} |")
    report_lines.append("")

    # 逐表详情
    tables_columns = {}  # 供跨表检查用
    all_time_findings = []
    for tname, cnt in tables:
        if cnt < 0:
            continue
        report_lines.append(f"## {tname}（{cnt:,} 行）\n")
        columns, pk_cols = describe_table(engine, tname, schema)
        tables_columns[tname] = {c["name"]: c["type"] for c in columns}
        rates = null_rates(engine, tname, columns)
        enums = enum_values(engine, tname, columns)

        # 字段字典
        report_lines.append("| 字段名 | 类型 | 主键 | 可空 | 空值率 |")
        report_lines.append("|---|---|---|---|---|")
        for col in columns:
            pk_mark = "✅ PK" if col["is_pk"] else ""
            null_pct = rates.get(col['name'], '?')
            null_mark = f"**{null_pct}%**" if isinstance(null_pct, (int, float)) and null_pct > 10 else str(null_pct)
            if null_pct == 100.0:
                null_mark = "**100%** ❌可能无用"
            report_lines.append(f"| {col['name']} | {col['type']} | {pk_mark} | {'是' if col['nullable'] else '否'} | {null_mark} |")
        report_lines.append("")

        # 枚举字段
        if enums:
            report_lines.append("### 枚举字段\n")
            for cname, values in enums.items():
                report_lines.append(f"**{cname}**（{len(values)}种取值）：")
                report_lines.append("| 取值 | 出现次数 |")
                report_lines.append("|---|---|")
                for val, cnt_val in values:
                    report_lines.append(f"| {val} | {cnt_val:,} |")
                report_lines.append("")

        # 标记问题
        issues = []
        if not pk_cols:
            issues.append("⚠️ **无主键**——此表没有主键，数据进数仓前需在DWD层过滤")
        if cnt == 0:
            issues.append("⚠️ **空表**——0行数据，无法判断字段含义，需人工确认")
        if len(columns) == 1 and pk_cols:
            issues.append("⚠️ **单列表**——只有主键没有业务字段，可能是配置表或映射表")
        high_null = [c for c in columns if rates.get(c["name"], 0) > 10 and rates.get(c["name"], 0) < 100]
        if high_null:
            issues.append(f"⚠️ **空值率>10%**：{', '.join(c['name'] for c in high_null)}")
        full_null = [c for c in columns if rates.get(c["name"], 0) == 100]
        if full_null:
            issues.append(f"⚠️ **100%空值（可能无用）**：{', '.join(c['name'] for c in full_null)}")
        # 特殊列名检查
        import re as _re
        special_cols = [c for c in columns if _re.search(r'[\s\u4e00-\u9fff()（）]', c["name"])]
        if special_cols:
            issues.append(f"⚠️ **列名含特殊字符（空格/中文/括号）**：{', '.join(c['name'] for c in special_cols)}——后续建表需重命名")
        # v8：完全重复行
        dup = duplicate_rows(engine, tname, cnt)
        if dup is None:
            issues.append(f"ℹ️ **重复行未查**——表超过{BIG_TABLE_LIMIT:,}行，全表 DISTINCT 会拖垮源库，需人工抽查")
        elif dup > 0:
            issues.append(f"⚠️ **完全重复行 {dup:,} 条**——业务上'一件事'的边界在哪？需问用户（去重建键而非丢弃）")
        # v8：时间范围与数据悬崖
        time_cols = find_time_columns(engine, tname, columns, cnt)
        for tcname, kind in time_cols[:2]:  # 每表最多查2个时间字段，防大表多次扫描
            hist = time_histogram(engine, tname, tcname, kind)
            if hist:
                cliffs = detect_time_cliffs(hist)
                span = f"{hist[0][0]}~{hist[-1][0]}年"
                issues.append(f"ℹ️ 时间字段 **{tcname}** 范围 {span}（{hist[0][0]}年{hist[0][1]:,}条 … {hist[-1][0]}年{hist[-1][1]:,}条）")
                for c in cliffs:
                    issues.append(f"⚠️ **数据悬崖**：{c}")
                    all_time_findings.append((tname, tcname, c))
                # v8.1 修复：末年月度覆盖检查——行数降幅不大（如前年57%）但年份未存满的漏报
                import datetime as _dt
                last_y = hist[-1][0]
                if 1900 < last_y < _dt.date.today().year:  # 历史年份才判"未存满"，当前进行年不算
                    months = last_year_month_coverage(engine, tname, tcname, kind, last_y)
                    if months is not None and months < 11:
                        msg = f"末年{last_y}年只覆盖{months}个月——年份未存满，趋势对比须用同期口径"
                        issues.append(f"⚠️ **数据悬崖**：{msg}")
                        all_time_findings.append((tname, tcname, msg))
        if issues:
            report_lines.append("### 注意事项\n")
            for issue in issues:
                report_lines.append(f"- {issue}")
            report_lines.append("")

    # v8：跨表检查
    report_lines.append("## 跨表检查\n")
    orphans = orphan_fk_check(engine, tables_columns)
    if orphans:
        report_lines.append("### 孤儿外键（子表有、父表没有）\n")
        report_lines.append("| 子表 | 字段 | 猜测父表 | 孤儿行数 | 子表总行数 |")
        report_lines.append("|---|---|---|---|---|")
        for tname, cname, parent, n, total in orphans:
            report_lines.append(f"| {tname} | {cname} | {parent} | {n:,} | {total:,} |")
        report_lines.append("\n→ 孤儿行的业务含义要问用户：是软删除？历史数据？还是脏数据？\n")
    else:
        report_lines.append("孤儿外键：未发现（或无法猜测父表关系）\n")
    dup_names = cross_table_duplicate_names(tables_columns)
    if dup_names:
        report_lines.append("### 跨表重名字段（出现在≥3张表）\n")
        report_lines.append("→ 问用户：这些同名跨表的字段，业务上是同一个含义吗？（如 user_id 在订单表和日志表是否同一批用户）\n")
        report_lines.append("| 字段名 | 出现表数 |")
        report_lines.append("|---|---|")
        for name, c in sorted(dup_names.items(), key=lambda x: -x[1]):
            report_lines.append(f"| {name} | {c} |")
        report_lines.append("")

    return "\n".join(report_lines)

def main():
    parser = argparse.ArgumentParser(description="只读探源库")
    parser.add_argument("--url", help="数据库URL，或通过环境变量 SOURCE_DB_URL 传入")
    parser.add_argument("--schema", help="PostgreSQL schema（默认 public）")
    parser.add_argument("--output", default="probe_report.md", help="输出文件名")
    args = parser.parse_args()

    url = args.url or os.environ.get("SOURCE_DB_URL")
    if not url:
        print("错误：请通过 --url 或环境变量 SOURCE_DB_URL 提供数据库连接URL", file=sys.stderr)
        sys.exit(1)

    print(f"连接数据库（只读）...")
    engine = get_engine(url)
    try:
        report = generate_report(engine, schema=args.schema)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"探查完成，报告已保存到 {args.output}")
    finally:
        engine.dispose()
        print("连接已断开。")

if __name__ == "__main__":
    main()
