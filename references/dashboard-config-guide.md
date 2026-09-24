# dashboard.config 生成指南 v2（组件自由拼接 · 实战版）

> 来源：三案例全链路实测 + BI 业界调研（Grafana gridPos / Metabase 默认尺寸+autoplace / Tableau·微软·IBM·Data-to-Viz 选型方法论）。
> 给生成 config 的 AI 看：照此选型、照此拼装、照此检查。

## 一、config v2 结构速查

```yaml
version: 2
title: XX · 指标看板
time: {table: 时间表, column: 时间列, grain: year, kpi_at: 最后完整年}   # 可空
metrics:                                                              # 同 v1
  - {id: xxx, name: 中文名, direction: up|down, unit: 单位,
     source: {table: 表, column: 列, key: {长表列: 行值}}, caliber: 口径卡名}
components:                # 平铺组件列表（顺序即自动放置顺序）
  - {id: k1, type: kpi, metric: xxx}                          # 单指标卡 6x3
  - {id: t1, type: line, metrics: [a, b]}                      # 折线 12x6
  - {id: r1, type: bar, metric: m, table: t, dimension: d, top: 5, label: 标题, order: "列 DESC"}
  - {id: p1, type: pie, metric: m, table: t, dimension: d, filter: "SQL", label: 标题}   # >5块自动合并"其他"
  - {id: h1, type: heatmap, table: t, dimension: d, metrics: [id 或 {metric, column}], top: 12, filter, order}
  - {id: tb, type: table, table: t, columns: [..], top: 15, order, title, dimension}
  - {id: hg, type: histogram, table: t, column: c, count_col: 已聚合计数列}   # count_col=聚合表模式
  - {id: hd, type: heading, content: 区块标题}                 # 24x1
  - {id: tx, type: text, content: 说明文字}                    # 24x2
  - {id: as, type: area_stack, table: t, dimension: d, time_col: c, metric: m, top: 5}
  - {id: sc, type: scatter, table: t, x: 列1, y: 列2, top: 300}
  - {id: fn, type: funnel, title: 标题, stages: [{label: 阶段名, metric: 指标id 或 value: 字面值}]}
  - {id: bl, type: bullet, metric: m, target: 目标值}
  - {id: wf, type: waterfall, unit: 单位, items: [{label, value, total: true|false}]}   # total=首尾合计柱；正增量绿/负增量红/合计蓝
  - {id: sk, type: sankey, nodes: [{name, col: 1|2|3, bad: true?}], links: [{source, target, value}]}   # 3列流向；节点≤12/连边≤24；col2灰/col3绿(bad红)
  - {id: rd, type: radar, table: t, dimension: d, metrics: [id...], top: 3}   # 各轴按全表最大值归一
  - {id: ga, type: gauge, metric: m, max: 100}   # 半环+分段底色，值取指标最新值
  # 可选 grid: {x, y, w, h}——省略则 first-fit 自动放置；对话精调时才给
drill:                      # 下钻（与布局无关）
  l2: {slices: [{label, color, unit, source{table,column,key/filter,agg}}], dimensions: [{table, dimension, label, filter, order}]}
  l3: {table, columns, dimension_map, time_col, limit, filters: [{column, label}], status: {column, bad, good}}
```

**v1 兼容**：老 config 的 kpi_band/trend/ranking/heatmap 四段自动翻译为组件（零改动）。

## 二、AI 选型三层逻辑（生成 components 时执行）

**第一层：意图路由**（问用户/读口径卡——这个数要回答什么问题）：

| 意图 | 组件 type |
|---|---|
| 单值 KPI（无目标） | `kpi` |
| 值 vs 目标 | `bullet`（多指标并排） |
| 趋势（随时间） | `line` |
| 构成/占比（静态） | `pie`（≤5块） |
| 构成随时间变化 | `area_stack` |
| 对比/排名 | `bar` |
| 分布 | `histogram` |
| 二维交叉健康全景 | `heatmap` |
| 相关性 | `scatter` |
| 流向/转化 | `funnel` |
| 明细 | `table` |
| 分区说明 | `heading` / `text` |

**第二层：形态检查**（查数据再定）：有无时间列（无→不能 line/area_stack）；类别数（饼>5 自动合并"其他"并警告）；有无目标值（有→bullet 优先于 kpi）；长表指标（source 带 key，进热力须显式 column）。

**第三层：反例硬约束**（渲染器自动执行，AI 生成时也应遵守）：
1. 饼图 ≤5 块（超出自动合并"其他"+警告）
2. 折线仅限时间轴（无 time 的 config 别放 line）
3. 柱/条形优于饼做精确对比
4. 热力矩阵适合二维交叉
5. 禁 3D；不截断坐标轴；量级悬殊别同轴
6. 未知 type 报错；x+w>24 自动收窄；坐标重叠自动挪位+警告
7. 排行/热力表按编号存储时必须声明 order（如 heat_score DESC）
8. 末年不完整必须 time.kpi_at（取最后完整年）
9. 率列 0-1 小数渲染器自适应（列名含 rate/pct/ratio/share 且值域≤1）
10. **看板填满一页（硬规则）**：每个业务域组件数 ≥6 且网格总行高 ≥17 行（约一屏 900px）。某域指标撑不起时合并小业务成一个域（不要留空旷域），或补组件：KPI 补齐 4 张、排行/构成/分布/表格各一、TopN 加大。多域参考：`dashboard.config.domains.yaml`（15/16/17 三案例各有 3 域样例）。
10. L3 明细选业务可读列 + filters（枚举列）+ status（好坏二值列→红绿行）

## 三、布局规则（Grafana/Metabase 模式）

- **自动行装填（零空白）**：省略 grid 时按声明顺序累积首选宽切行，行内按权重比例**恰好填满 24 列**（最大余数法），行高自动对齐行内最高组件——任何组件组合都不留空白
- **KPI 连续分组等分**：连续声明的 kpi 组件自动等分行宽（3 张→各 8 列、4 张→各 6 列）
- **首选宽**：kpi 6、line 16、bar 8、pie 8、histogram 16、heatmap 24、table 24、area_stack/scatter 24、funnel 16、bullet 8、heading/text 24——常用组合（line+bar、pie+histogram、funnel+bullet）恰好满行，其他组合由比例填满自动适应
- **对话改图 = 单字段 diff**："把饼图换成柱状图"→ `type: pie→bar`；"趋势图放左边放大"→ `grid: {x:0, w:16}`；"加一个分布直方图"→ components 追加一条；"这页只留 KPI 和排行"→ 删组件（自动上浮紧凑）
- **禁止重叠**（网格互斥）；分区用 heading 组件不用布局容器

## 四、渲染命令

```bash
python scripts/render_universal_dashboard.py <案例目录> [--config 路径] [--output 路径]
```
产出：单文件 HTML（ECharts 内嵌离线），组件画布 + L2/L3 下钻 + 卡片角标提醒（选型规则自动修正时，对应卡片右上角出现"⚠ 自动调整"小徽标，悬停看详情，不再用页顶横幅打扰版面）。


## 能力内核（2026-09-22 集成版）

### 生成闸门（渲染前自动执行，`dashboard_contracts.py`）
- **硬阻断（退出码 2，拒绝渲染）**：字段不存在 / 展示字段无中文业务名 / 表格列头重名 / 布局行未填满 / 未知组件类型
- **警告（放行但提示）**：选型不当（数值列当分类轴、雷达<3指标、漏斗<2阶段、top 超限）/ 技术键全列展示
- **开销红线**：单次生成最多 1 次渲染 + 2 轮打回自纠，超限降级模板
- 中文业务名补录落点：`render_universal_dashboard.py` 顶部的 `FIELD_CN` 注册表（单一事实源）

### 自动规范（模板内置，config 无需声明）
- **KPI 五段式**：标签行→大数字→说明（无口径时"越高越好·单位X"占位）→迷你折线(40px)→底部虚线+下钻提示；跨卡像素级对齐
- **L2 多维分解**：维度分段控件+摘要 chips（率最高/成本合计/行数，自动排除排名 ID 列）+固定 420px 单表区；数字右对齐+千分位、布尔列 ✓/—、表头 sticky
- **布局**：宽度档 6/8/12/24（24 列网格）；行满自动均摊（5KPI→5,5,5,5,4）；行高对齐；孤卡全宽（kpi/bullet/gauge/radar 除外）；空数据组件自动隐藏
- **响应式**：≥1025 坐标制 / 752~1024 两列 / <752 单列
- **美学**：系列色 AntV 10、图例 14×3、动画 300ms、环形内径 75%、均值虚线、分区标题透明壳+竖条

### 回归检查（改模板/渲染器后必跑）
`python scripts/dashboard_check.py <生成的看板.html>` —— A.JS 语法 / B.DATA 布局（行满+行高+重叠+转义）/ C.模板规范锚点（17 条），全绿才算改完。


## 视觉层级（AI 布局思考指南）

**核心原则：回答用户主要问题的图表给最大空间，辅助图表给小空间。**

生成 config 时，先问自己："用户最想看什么？"——那个图表设为 `grid: {w: 16}`（主角），
配对的辅助图表设为 `grid: {w: 8}`（配角）。不要全部用默认宽度。

### 怎么判断谁是主角

| 用户说的 | 主角（w:16） | 配角（w:8） |
|---|---|---|
| "看看趋势" | 折线图 | 排名/构成 |
| "哪个最火/最多" | 排名条形图 | 趋势/构成 |
| "构成是什么" | 饼图/堆叠面积 | 排名/趋势 |
| "转化怎么样" | 漏斗图 | 子弹图/仪表盘 |
| "明细给我看" | 表格（w:24 全宽） | — |
| "多维交叉看" | 热力矩阵（w:24 全宽） | — |

### 错落有致的排版节奏

```
KPI 行（等宽，一行排满）
  ↓
主角图（16 宽） + 配角图（8 宽）     ← 非对称，有主次
  ↓
配角图（8 宽） + 另一主角（16 宽）    ← 位置对调，视觉变化
  ↓
全宽组件（表格/热力，24 宽）         ← 收尾
```

不要让所有行都是"宽+窄"的同样排列——偶尔对调位置（窄+宽），让页面有节奏感。
