# Data Model Builder · AI 数据建模 Skill

**你说一句话，AI 引导你从零构建完整的数据模型——ODS→DWD→DWS→ADS 四层数仓、口径统一、指标体系。**

**Just describe your business — AI guides you through building a complete data model: ODS→DWD→DWS→ADS warehouse layers, unified metric calibers, indicator system.**

## 效果：从杂乱到有序 | From Chaos to Structure

**原始数据（3300 万条评分、8 万部电影、4 张散表）→ AI 引导建模 → 四层数仓 → 可交互看板**

**Raw data (33M ratings, 86K movies, 4 scattered tables) → AI-guided modeling → 4-layer warehouse → interactive dashboard**

### 数据模型地图 | Data Model Map

![Model Map](screenshots/model-map.png)
*MovieLens 开源数据：25 张表、6 个层级（ODS→DIM→DWD→DWM→DWS→ADS）| MovieLens open data: 25 tables, 6 layers*

### 建模后的看板输出 | Dashboard Output

![Dashboard](screenshots/dashboard-output.png)
*从杂乱数据到飞书风格可交互看板 | From raw data to Feishu-style interactive dashboard*

## 它做了什么 | What It Does

1. **AI 引导建模** — 不需要懂数据库，AI 用业务语言引导你拍板每个指标口径
   **AI-guided modeling** — No database knowledge needed; AI uses business language to guide metric caliber decisions

2. **四层数仓** — ODS→DWD→DWS→ADS，每层有明确的职责和验证规则
   **Four warehouse layers** — ODS→DWD→DWS→ADS, each with clear responsibilities and validation rules

3. **口径统一** — 每个指标都有业务方拍板的分子/分母定义，防止"同名不同数"
   **Unified calibers** — Every metric has a business-approved numerator/denominator definition

4. **19 个真实案例** — 从在线教育到电商到航空，全部端到端验证通过
   **19 real cases** — From e-commerce to aviation, all end-to-end verified

## 使用 | Use

将此 Skill 安装到你的 AI 助手（Claude Code / DSH / 其他），然后对话说：
"帮我从零建一个数据模型"

Install this Skill to your AI assistant, then say: "Help me build a data model from scratch"

## 搭配使用 | Companion

本 Skill 搭配 **dsh-dashboard-ai** 插件使用效果最佳——Skill 建好数据模型后，插件一键把数据变成飞书风格可交互看板（17 种图表+下钻明细）。

**Pair with the dsh-dashboard-ai plugin** for the full experience — after the Skill builds your data model, the plugin turns it into a Feishu-style interactive dashboard (17 chart types + drill-down).

👉 https://github.com/33Wade333/dsh-dashboard-ai

## License

MIT
