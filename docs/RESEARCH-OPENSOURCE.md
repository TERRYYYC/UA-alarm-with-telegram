# 开源生态调研报告:Telegram 战争消息 → 结构化事件 → 实时地图管线

> 调研日期:**2026-09-06**。所有 star 数、License、最近提交时间均来自当日对 GitHub 仓库页面与 commits.atom 的实际抓取(非记忆值);站点与 API 均经过当日 HTTP 访问验证。个别仓库(如 osm-boundaries 的 GitHub 仓库)已失效,已如实注明并给出替代。
>
> 项目背景:Telethon 采集 4–10 个乌/俄语 Telegram 战争群 → 词典 + 规则先行、后续接入 LLM 的结构化提取(地名/事件类型/时间)→ PostgreSQL(+PostGIS)+ Redis → FastAPI + SSE 实时推送 → MapLibre GL 暗色地图 → Playwright 截图 → 发布 Telegram 频道 / Discord。当前为设计讨论阶段,目标是借鉴开源成果、避免重复造轮子。

## TL;DR

1. **采集层**:Telethon 仍是事实标准,但 **GitHub 仓库已被标记 archived(最近提交 2026-02-21)**;Pyrogram 同样已归档(2024-12),GramJS 也已归档。生态正在收紧——需要把"客户端库选型与维护状态"当作一级风险来管理,tdl(Golang)是目前最活跃的同类工具。存档类项目(tg_history_dumper、telegram-download-chat)提供了现成的增量采集/去重思路。
2. **战争地图领域没有现成的开源"大而全"系统**,但周边全是宝:DeepStateMap 有**非官方但稳定可用的 GeoJSON API**(`deepstatemap.live/api/history/last`,实测 200,~628KB,三语状态名),alarms.in.ua 有需申请 token 的官方 API 及多个社区替代;VIINA 提供了**可复用的战况事件类型学 + 去重("one-per-day")+ GeoNames/KATOTTH 地名体系**的完整参考实现。
3. **地名层**:GeoNames UA + KATOTTH(乌克兰行政区划编码)+ OSM(Geofabrik/osm-boundaries)三源合一即可构建高质量 gazetteer;VIINA 已打包了 33,141 个 GeoNames 聚落和 29,724 个 KATOTTH 聚落的 tessellation GeoJSON,可直接用。乌/俄转写库成熟(transliterate、cyrtranslit、ALA-LC 的 domovyk)。
4. **提取层**:乌克兰语 NLP 生态完整(Stanza 乌语 NER F1 86.05、lang-uk 系列模型),但实战项目(TGeocoder、VIINA、telegram-monitor)都选择了 **LLM(GPT-4o/Claude)+ 结构化输出**路线。instructor/pydantic/outlines 是可复用的 schema 约束框架;"规则+词典先行、LLM 兜底"的分层策略与社区实践一致。
5. **去重融合**:VIINA 的"同地同日同类型 → 同事件"过滤器是最直接的参考;通用工具链是 RapidFuzz/文本指纹 + dedupe + 空间聚类(DBSCAN/HDBSCAN),新闻流去重有 MinHash LSH 实践(chronicle)。
6. **可视化**:MapLibre GL(11.5k★,BSD)主库 + supercluster 聚类 + 自带 heatmap layer + 前端时间过滤即可覆盖 90% 需求;时间轴回放可参考 OpenHistoricalMap 与 GeoLibre 的 time slider 实现;暗色底图直接用 CARTO Dark Matter(IRONSIGHT 已验证)。
7. **端到端参考**:TGeocoder(Telethon→GPT-4o→地图)与 IRONSIGHT/OSINT-War-Room(多源 OSINT 面板)分别代表"学术原型"与"工程化面板"两条路线,值得逐组件对标。
8. **部署**:单机 docker-compose 完全够用(postgis/postgis 官方镜像 + Redis + FastAPI),Telethon 建议按 psyb0t/docker-telethon-plus 的思路做成独立 HTTP 服务以便重启与扩容。

---

## 领域 1:Telegram 采集与存档

### 1.1 客户端库

#### Telethon — [github.com/LonamiWebs/Telethon](https://github.com/LonamiWebs/Telethon)
- **License**:MIT | **语言**:Python(纯 Python 3 MTProto)
- **活跃度**:12,057 stars;**GitHub 仓库标记 archived**,最近提交 2026-02-21;默认分支为 v1(数据 2026-09-06)
- **功能**:最流行的 Python MTProto 客户端库,支持用户/机器人两种身份、channel 监听(events.NewMessage)、媒体下载、session 持久化。
- **对我们的启示**:项目设计默认就选它,生态最全、示例最多;但 **archived 状态是新风险**——需在设计中固定版本、考虑 fork 或评估替代(见下)。它文档中的 [FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html) 是采集风控的权威起点(FloodWait、session 迁移、登录限制)。
- **坑**:仓库归档后 bug 修复会停滞;大批量监听(几百个 channel)时 update 事件需要自己做好去重与重连逻辑;手机号风控问题见 1.5。

#### Pyrogram — [github.com/pyrogram/pyrogram](https://github.com/pyrogram/pyrogram)
- **License**:LGPL-3.0 | **语言**:Python(async,MTProto)
- **活跃度**:4,617 stars;**archived**,最近提交 2024-12-23(数据 2026-09-06)
- **功能**:与 Telethon 齐名的另一 Python 客户端,装饰器式 handler、社区 bot 生态庞大。
- **对我们的启示**:API 风格更简洁,但同样已停止维护,且 LGPL 对商用分发有约束。**不建议新项目主选**,可作迁移备选。

#### GramJS — [github.com/gram-js/gramjs](https://github.com/gram-js/gramjs)
- **License**:MIT | **语言**:TypeScript(Node/Browser MTProto)
- **活跃度**:1,765 stars;**archived**,最近提交 2026-07-14(数据 2026-09-06)
- **功能**:JS 生态的 Telegram 客户端,浏览器内可用。
- **对我们的启示**:若前端(而非 Python 后端)直接采集才有价值;本项目管线在 Python 侧,参考意义有限。同样已归档,印证"MTProto 客户端库维护普遍收缩"的趋势。

#### tdl — [github.com/iyear/tdl](https://github.com/iyear/tdl)
- **License**:AGPL-3.0 | **语言**:Golang
- **活跃度**:8,037 stars,**活跃维护中**,最近提交 2026-08-29(数据 2026-09-06)
- **功能**:Telegram 工具包(下载/上传/备份),基于官方 TDLib,支持 channel 历史批量导出、媒体下载、增量续传。
- **对我们的启示**:**目前生态里最活跃的 Telegram 工具**;适合做"历史消息一次性灌库 + 定期增量备份"的旁路,而不是实时流。AGPL 需注意义务。

### 1.2 采集/存档项目

#### tg_history_dumper — [github.com/3bl3gamer/tg_history_dumper](https://github.com/3bl3gamer/tg_history_dumper)
- **License**:MIT | **语言**:Python(TDLib)
- **活跃度**:94 stars,活跃,最近提交 2025-12-26(数据 2026-09-06)
- **功能**:导出 Telegram 对话/群/频道全部消息与媒体,支持增量导出、JSON 输出、自定义导出参数。
- **对我们的启示**:基于 TDLib 而非 MTProto 原生,可靠性与官方同步;其增量导出与媒体命名方案可直接借鉴。适合做**历史语料冷启动**(先灌 4–10 个群的历史消息再切换实时监听)。

#### telegram-download-chat — [github.com/popstas/telegram-download-chat](https://github.com/popstas/telegram-download-chat)
- **License**:MIT | **语言**:Python
- **活跃度**:208 stars,活跃,最近提交 2026-08-29(数据 2026-09-06)
- **功能**:下载任意 Telegram 聊天历史,CLI/GUI/WebUI/MCP 多界面。
- **对我们的启示**:轻量、文档好;与 tg_history_dumper 二选一即可。

#### snscrape — [github.com/JustAnotherArchivist/snscrape](https://github.com/JustAnotherArchivist/snscrape)
- **License**:GPL-3.0 | **语言**:Python
- **活跃度**:5,444 stars,但**基本停更**(最近提交 2023-06-22)(数据 2026-09-06)
- **功能**:社交媒体抓取器,支持 Telegram(基于 t.me/s 网页预览)、Twitter/X、Reddit 等。
- **对我们的启示**:无需登录即可抓公开频道预览页——**风控上最安全,但功能受限**(只有预览文本、无媒体、有频率限制、可能被封 IP)。适合作为"账号被封时的降级通道"或轻量频道监控;不依赖它做主路。

#### telegram-analysis — [github.com/amiryousefi/telegram-analysis](https://github.com/amiryousefi/telegram-analysis)
- **License**:无 License | **语言**:Python
- **活跃度**:369 stars,停更(最近提交 2021-10-17)(数据 2026-09-06)
- **功能**:Telegram 群分析工具(抓取 + 文本分析),早期经典教学项目。
- **对我们的启示**:无 License 不能直接改代码;但作为"抓取+分析"单仓库范例仍有参考价值。**仅参考结构,不采用代码**。

#### tg-scraper — [github.com/Mnilax/tg-scraper](https://github.com/Mnilax/tg-scraper)
- **License**:MIT | **语言**:Python
- **活跃度**:6 stars,小众,最近提交 2026-06-15(数据 2026-09-06)
- **功能**:频道消息抓取。
- **对我们的启示**:生态里此类小项目极多、质量参差,进一步说明"自己用 Telethon 写 200 行采集器"通常优于依赖小众 scraper。

#### docker-telethon-plus — [github.com/psyb0t/docker-telethon-plus](https://github.com/psyb0t/docker-telethon-plus)
- **License**:WTFPL | **语言**:Python + Docker
- **活跃度**:2 stars,活跃,最近提交 2026-08-01(数据 2026-09-06)
- **功能**:把 Telethon userbot 包装成 JSON HTTP API + MCP 端点,跑在 Docker 里。
- **对我们的启示**:架构思想很好——**把账号客户端做成独立的、可独立重启的微服务**,与主管线解耦;采集器崩溃/换号不拖垮全系统。可借鉴其容器化与 session 卷挂载方案(领域 8 详述)。

### 1.3 采集实践教训(账号风控 / session / 多账号 / 限流)

综合 Telethon [FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html)、OSINT-War-Room 与 IRONSIGHT 的工程记录及社区通行做法,归纳如下:

- **账号风险是头号运维问题**:MTProto 用户客户端采集违反 Telegram ToS,存在封号风险;战争群多为俄乌敏感频道,账号一旦被举报风险更高。社区通行做法:专用账号(非主号)、低频率(每秒个位数请求级)、只做只读监听(不加入太多群、不发言)、**处理好 FloodWaitError**(Telethon 会抛出并给出等待秒数,必须退避而不是硬重试)。
- **Session 管理**:Telethon 支持 SQLite session 文件持久化(避免每次重新登录触发风控);生产环境把 session 挂进 Docker volume,并支持"换号"流程(替换 session 文件即可)。多人异地登录、频繁更换 IP 都会增加风控概率。
- **多账号策略**:4–10 个频道量级用 1–2 个账号足够(Telethon 单账号可监听成百上千频道);多账号仅在需要分组隔离(乌方群/俄方群)或互为备份时考虑。**宁可少账号低频,不要多账号高频**。
- **降级通道**:Telegram 公开频道预览页(`t.me/s/<channel>`)、[snscrape](https://github.com/JustAnotherArchivist/snscrape) 与 IRONSIGHT 的 embed 抓取方式都验证了"无登录采集"可行性——适合做容灾降级或公开大频道旁路,但有频控且无历史媒体。
- **抓历史 vs 实时**:先用 tg_history_dumper/tdl 一次性灌历史(冷启动 gazetteer 调优与规则开发),再用 Telethon 增量监听,两者互补。

---

## 领域 2:战争/冲突地图平台与开源克隆

### 2.1 生产级平台(闭源为主,但 API/数据部分开放)

#### Liveuamap — [liveuamap.com](https://liveuamap.com/);旧版开源仓库 [github.com/liveuamap/liveuamap](https://github.com/liveuamap/liveuamap)
- **License**:旧仓库 Apache-2.0 | **技术栈**:现行站点闭源;旧仓库为 PHP + MySQL
- **活跃度**:旧仓库 60 stars,最后提交 2014-07-30(数据 2026-09-06)——**现行站点已闭源**,GitHub 上只有 2014 年的遗址。
- **功能**:乌克兰危机事件地图鼻祖:志愿者/编辑人工录入事件 → 按类别图标(炮击、爆炸、防空、集结点…)打点 → 时间线与日期筛选 → 侧栏新闻流。
- **对我们的启示**:**UI/UX 信息设计的基准**。要点:①事件图标按类别分色;②marker 显示时间戳,旧的自动淡化;③左侧实时新闻列表与地图联动;④按日期回看历史。本项目应照抄这套交互骨架,但用自动采集替代人工录入。**不要 fork 那个 2014 仓库**(技术栈全废,且现行 UI 并不在其中)。

#### DeepStateMap — [deepstatemap.live](https://deepstatemap.live/)
- **License**:站点闭源;**API 非官方但长期稳定**
- **功能**:乌军战况地图,逐村/逐地块多边形着色(乌控/俄控/争夺/解放+日期),含事件标注与时间轴。
- **API(实测验证,2026-09-06)**:`GET https://deepstatemap.live/api/history/last` 返回 HTTP 200,~628KB GeoJSON FeatureCollection(多边形,properties.name 为三语:"Звільнено /// Liberated /// geoJSON.status.dismissed" 格式,含样式字段)。另确认存在 `https://deepstatemap.live/railways.json`(freepublicapis 收录的文档化端点,[freepublicapis.com/deepstatemap-api](https://www.freepublicapis.com/deepstatemap-api))。社区文档仓库:[github.com/l0v3m0n3y/deepstatemap](https://github.com/l0v3m0n3y/deepstatemap)(Swift 封装,0 stars,最近提交 2026-08-25)。
- **对我们的启示**:①**免费的前线态势底图数据源**——可作为"地面真值"与自家提取事件做交叉验证/图层叠加;②其"多边形按控制状态着色 + 事件日期标注"是最被认可的战争地图设计语言;③API 无官方保证,做容错与本地缓存(参见 [tak-feeder-deepstate](https://github.com/sgofferj/tak-feeder-deepstate),13 stars,GPL-3.0,TAK 系统 feeder,2024-06 后停更)。注意:DeepStateMap 官方 2024 年后对 API 使用有收紧迹象,商用/高频使用需自行评估。

#### alarms.in.ua(乌克兰防空警报地图)— [alarms.in.ua](https://alarms.in.ua/)
- **License**:闭源;**官方 API 存在但需 token**:`https://api.alerts.in.ua`(实测 `/v1/alerts/active.json` 返回 401 并提示"API token required,请联系 api@alerts.in.ua",2026-09-06)。
- **功能**:按 oblast 着色显示实时防空警报(红=警报、绿=解除),支持声音通知与历史统计。
- **社区替代/封装**:
  - [UAVXP/raid](https://github.com/UAVXP/raid) — "Air Raid Alerts API(Ukraine)",生产实例 alerts.com.ua,`GET /api/states` + X-API-Key;
  - [snappixd/alerts_api](https://github.com/snappixd/alerts_api) — 同类 Rust/其他实现的告警 API;
  - Rust crates [ralertsinua-models](https://docs.rs/crate/ralertsinua-models/0.4.0)、[ralertsinua-geo](https://docs.rs/crate/ralertsinua-geo/0.3.3) — 结构化解析该 API;
  - IRONSIGHT 使用 [alerts.com.ua](https://alerts.com.ua/)(EN 名称)免费端点做乌克兰各州警报(15 秒轮询)。
- **对我们的启示**:**"alarms.in.ua 风格"的州级警报图层 + 时间衰减闪烁**是可复用的 UI 模式;若项目需要把 Telegram 提取出的"警报类事件"与官方警报数据融合,走 alerts.com.ua 或申请官方 token。时间敏感度极高的事件(导弹警报)应配 SSE 直推 + 声音提醒。

### 2.2 开源战争数据/地图项目

#### VIINA — [github.com/zhukovyuri/VIINA](https://github.com/zhukovyuri/VIINA)
- **License**:数据 ODbL,代码随库(无整体代码 License 声明)| **语言**:R
- **活跃度**:356 stars,活跃(每日更新数据,最近提交 2026-09-04)(数据 2026-09-06)
- **功能**:近实时多源(乌俄新闻)战争事件数据系统:抓取(每 6 小时)→ 地名抽取与地理编码(Yandex + OSM,人工核查误报)→ BERT 事件分类(24 类事件类型 + 行动方)→ 每日发布 CSV/GeoJSON;含领土控制逐聚落日度面板(GeoNames 33,141 点 / KATOTTH 29,724 点)与"one-per-day"去重数据。
- **对我们的启示**:**全报告最值得精读的项目**。可直接复用:①事件类型学(t_airalert/t_airstrike/t_uav/t_armor/t_artillery/t_ied/t_control…24 类)——我们的规则引擎照此建模;②"同地+同日+同类 → 合并,记录 n_reports 与 sources"的去重设计;③GeoNames/KATOTTH 双地名体系 + GEO_PRECISION(STREET~ADM1)分层;④三语地名表(乌/俄/英)与 tessellation GeoJSON 直接可作为 gazetteer 种子数据。坑:其去重粒度(1 天)比 Telegram 实时场景粗;分类模型面向新闻标题而非短消息口语。

#### The Economist war-fire model — [github.com/TheEconomist/the-economist-war-fire-model](https://github.com/TheEconomist/the-economist-war-fire-model)
- **License**:代码 MIT,数据 CC-BY-4.0 | **语言**:R
- **活跃度**:278 stars,**每日多次更新数据**,最近提交 2026-09-06(数据 2026-09-06)
- **功能**:用卫星热异常(VIIRS/FIRMS)统计检测战争火力事件(累计 9 万+),输出事件级 CSV(经纬度/时间/置信)。
- **对我们的启示**:"遥感信号做战争事件检测"的权威开源实现;可作为独立交叉验证层,也示范了**事件级数据公开发布**的格式(时间+坐标+置信度+来源)。坑:依赖云量与季节,漏报不可避免。

#### NZZ 领土控制分析 — [github.com/conflict-investigations/nzz-maps](https://github.com/conflict-investigations/nzz-maps)
- **License**:无 License 声明 | **语言**:Python(Notebook)
- **活跃度**:12 stars,活跃,最近提交 2026-09-02(数据 2026-09-06)
- **功能**:分析 NZZ 的领土控制数据。
- **对我们的启示**:展示"领土控制数据"的获取与分析套路;无 License,只读参考。

#### ukraine-war-heatmap — [github.com/ruarq/ukraine-war-heatmap](https://github.com/ruarq/ukraine-war-heatmap)
- **License**:MIT | **语言**:Python(Reddit PRAW + Folium)
- **活跃度**:10 stars,2022-04 后停更,作者明确宣布**停止更新**(数据 2026-09-06)
- **功能**:聚合 r/CombatFootage 等 Reddit 战争板块帖子的地理热度图。
- **对我们的启示**:**反面教材兼教训**:作者自述"热力图不适合表现战争,且 Reddit 活动衰减"而关停。→ 我们的默认视图应优先**离散事件标记(图标+时间)**而非热力面;热力仅作为密度概览的可选图层。

#### OSINT-War-Room — [github.com/Hue-Jhan/OSINT-War-Room](https://github.com/Hue-Jhan/OSINT-War-Room)(详见领域 7)
- 85 stars,MIT,FastAPI + Leaflet,聚合 GDELT 冲突事件 + Telegram + 警报 + ADS-B 的战术面板(数据 2026-09-06)。

#### LA3MAP — [github.com/MnkyArts/LA3MAP](https://github.com/MnkyArts/LA3MAP)
- **License**:无 License | **语言**:—
- **活跃度**:1 star,2023-07 停更(数据 2026-09-06)
- **功能**:面向 Arma 游戏的 simplified liveuamap clone。
- **对我们的启示**:说明 liveuamap 交互模式的通用性(连游戏圈都克隆);但项目太小,仅作概念佐证。**搜不到高质量的"liveuamap 现代开源克隆"**——如实说明:liveuamap 现行版本无开源实现,最接近的是上述 OSINT-War-Room/IRONSIGHT 面板与本项目自身。

### 2.3 战争地图 UI/UX 信息设计小结(可直接落地的设计决策)

- **事件图标**:按事件类型分色/分形(liveuamap、DeepState 均如此),Telegram 高频事件(炮击/导弹/无人机/爆炸/伤亡)需有独立 glyph。
- **时间表达**:每条事件显示"多久前"(相对时间)+ 悬停绝对时间;**默认只显示最近 N 小时/天的事件**,旧事件自动降透明度(时间衰减)——liveuamap 与 DeepState 的核心模式。
- **状态图层**:控制区用**多边形按状态着色**(DeepState 方案),警报用**行政区色块**(alarms.in.ua 方案);二者与点状事件分层放置。
- **时间轴**:提供"历史回放/日期选择"而非只有实时——用户需要回看昨晚发生了什么(参考 VIINA 的 daily map 动画与 OpenHistoricalMap,见领域 6)。
- **信息密度控制**:侧栏事件流(标题+地点+时间)与地图联动点击定位;缩放分级聚合(marker cluster),避免 4–10 个频道消息刷屏。

---

## 领域 3:地理数据与地名

### 3.1 基础地理数据集

#### GeoNames 乌克兰 — [geonames.org/export](https://www.geonames.org/export/)
- **License**:CC-BY-4.0 | **格式**:CSV dump(UA.zip,含 name/asciiname/alternatenames/坐标/feature_code/PPL 等级)
- **活跃度**:数据持续维护(站点 2026-09-06 可达)
- **对我们的启示**:**gazetteer 的第一数据源**。VIINA 的 33,141 个 PPL* 聚落与 gn_UA_tess.geojson 就是从它派生;alternatenames 字段天然包含乌/俄/英多语别名,配合 feature_code(PPL/PPLA/PPLA2…)区分"市/区/镇"层级。我们的词典应以 GeoNames UA + 战事高频地名补丁为骨架。

#### KATOTTH(乌克兰行政区划编码)— 数据经 VIINA 打包:[katotth_UA_tess.geojson](https://github.com/zhukovyuri/VIINA/tree/master/Data/katotth_UA_tess.geojson)(29,724 聚落)
- **License**:随 VIINA(ODbL)| **格式**:GeoJSON tessellation(泰森多边形)
- **活跃度**:VIINA 持续更新(数据 2026-09-06)
- **对我们的启示**:乌克兰官方行政区划编码([KATOTTH 维基词条](https://en.wikipedia.org/wiki/Codifier_of_administrative-territorial_units_and_territories_of_territorial_communities)),可与政府统计对表;**泰森多边形(tessellation)是解决"点地名 → 区域着色"的现成方案**——DeepStateMap 的逐村多边形同此思路。可直接下载该 GeoJSON 作为 oblast/rayon/hromada 层级参照。

#### GADM(全球行政区划)— [gadm.org/download_country.html](https://gadm.org/download_country.html)
- **License**:免费非商用(学术用途宽松)
- **活跃度**:版本化发布(站点 2026-09-06 可达)
- **功能**:乌克兰 4 级行政区划边界(国/州/区/社区级)GeoPackage/Shapefile。
- **对我们的启示**:快速获得 oblast 边界做聚合着色;License 不适合商用分发,自用与研究无碍。

#### Geofabrik Ukraine extract — [download.geofabrik.de/europe/ukraine.html](https://download.geofabrik.de/europe/ukraine.html)
- **License**:ODbL | **格式**:OSM PBF/SHP 切片,每日更新(站点 2026-09-06 可达)
- **对我们的启示**:postgis 环境装 [osm2pgsql](https://osm2pgsql.org/) 导入 Ukraine 切片,获得完整的"地名+道路+边界+POI"本地查询能力;配合 `name:uk`/`name:ru`/`name:en` 标签构建多语索引。这是 gazetteer 的**第三数据源**(补 GeoNames 没有的村级与小地名)。

#### osm-boundaries — [osm-boundaries.com](https://osm-boundaries.com/)
- **License**:ODbL | **格式**:按 admin_level 导出的边界 GeoJSON
- **活跃度**:站点 2026-09-06 可达;其原 GitHub 仓库 wambacher/osm-boundaries 已 404(如实说明:仓库失效,以网站为准)
- **对我们的启示**:按需下载乌克兰各 admin_level 边界(含 hromada 级),免去自己从 OSM 提取的麻烦。

#### 其他补充数据
- [OSMNames](https://github.com/OSMNames/OSMNames):383 stars,GPL-2.0,OSM 地名的全文搜索/排名数据 dump(2023-11 后停更);适合离线做地名候选排序。
- [photon](https://github.com/komoot/photon):3,033 stars,Apache-2.0,基于 Elasticsearch 的 OSM 地理编码器(活跃,最近提交 2026-09-04);若需要"输入地名 → 候选坐标"的搜索服务,自建 photon 比调 Google/Yandex 更可控。
- [shal/ua-cities](https://github.com/shal/ua-cities):1 star,MIT,乌克兰城市 JSON API(2019 停更)——数据陈旧,仅作小型示例。
- Kaggle [Ukraine Regions Boundaries](https://www.kaggle.com/datasets/p0stscriptum/ukraine-geo-data/data):社区整理的乌克兰边界数据,可与 GADM 交叉。

### 3.2 转写与归一化库

| 项目 | URL | License | 说明 |
|---|---|---|---|
| transliterate | [github.com/barseghyanartur/transliterate](https://github.com/barseghyanartur/transliterate) | 无声明 | 314 stars;多语言转写包,含乌克兰语 pack,Python;2020 后停更但稳定 |
| cyrtranslit | [pypi.org/project/cyrtranslit](https://pypi.org/project/cyrtranslit/) | MIT | 双向西里尔转写(含乌/俄官方规则),Python |
| domovyk | [github.com/ian-nai/domovyk](https://github.com/ian-nai/domovyk) | GPL-3.0 | 2 stars;按 **ALA-LC 罗马化表**转写多种西里尔字母(图书馆界标准) |
| names-translator | [github.com/dchaplinsky/names-translator](https://github.com/dchaplinsky/names-translator) | MIT | 16 stars;乌克兰人名/地名→俄语/英语的自动翻译与转写(活跃,2026-07 更新) |

- **对我们的启示**:gazetteer 导入时预生成"乌/俄/英/罗马化"四列键(GeoNames asciiname + alternatenames + transliterate/cyrtranslit),消息侧只做**规范化(小写、去重音、统一 ё/е、乌俄拼写映射)后查表**,而不是实时转写。乌语/俄语地名在战争语境差异显著(Київ/Kiev/Киев、Бахмут/Артемовск),**别名表 + 官方现行名(乌语)为主键**是通行做法(DeepState 三语名、VIINA 双语同理)。

### 3.3 同名歧义消解实践

- **VIINA 的做法**(最完整):先对全部唯一地名做地理编码,**人工核查所有误报**(false positive/negative),并保留 GEO_PRECISION 字段;对外发布 tessellation 而非裸点。
- **TGeocoder 的做法**:LLM 抽取地名后交给 geocoder,再人工/自动校验。
- **我们的建议**:①层级上下文规则——消息里同时出现 oblast/rayon 名时,同名小地名优先归入该区(如 "Новоселівка, Покровський район");②**同一消息内坐标短语**(军事坐标、距前线 X km 描述)辅助锚定;③"前线走廊"先验:战争事件大概率落在接触线附近(可用 DeepStateMap API 的前线多边形做空间先验);④gazetteer 按人口/行政等级排序,默认取高等级;⑤存疑时标记 `location_confidence`,不硬猜。

---

## 领域 4:事件提取与结构化(NLP / LLM)

### 4.1 乌克兰语/俄语 NLP 模型

#### Stanza(Stanford NLP)— [github.com/stanfordnlp/stanza](https://github.com/stanfordnlp/stanza)
- **License**:Apache-2.0(仓库显示 NOASSERTION,以项目页为准)| **语言**:Python
- **活跃度**:7,873 stars,活跃,最近提交 2026-07-15(数据 2026-09-06)
- **功能**:多语言 NLP 管线;**乌语 NER 模型(languk 语料,F1 86.05,PER/LOC/ORG/MISC)与俄语 NER(WikiNER,F1 92.9)** 已内置(数据来自 [stanfordnlp.github.io/stanza/ner_models.html](https://stanfordnlp.github.io/stanza/ner_models.html),2026-09-06 验证)。
- **对我们的启示**:规则提取阶段**最好的通用 NER 兜底**;LOC 标签可做 gazetteer 未命中的召回通道。坑:F1 86 对口语化军事黑话偏低,别指望它直接出干净地名。

#### lang-uk 组织 — [github.com/lang-uk](https://github.com/lang-uk) | [huggingface.co/lang-uk](https://huggingface.co/lang-uk)
- **License**:各模型自定 | **语言**:Python/Transformers
- **活跃度**:非营利组织,HF 上 64 个模型(2026-09-06 抓取)
- **代表模型**:[lang-uk/roberta-large-ner-uk](https://huggingface.co/lang-uk/roberta-large-ner-uk)(乌语 NER 大模型)、[lang-uk/fasttext_uk](https://huggingface.co/lang-uk/fasttext_uk)(词向量)、配套 NER 数据集 [benjamin/ner-uk](https://huggingface.co/datasets/benjamin/ner-uk)。
- **对我们的启示**:**乌语专项模型的权威来源**;若规则+LLM 已覆盖,可仅用其数据集做规则引擎评测基准。

#### 俄语 NLP
- [natasha](https://github.com/natasha/natasha):1,349 stars,MIT,俄语 NER/形态/嵌入一体化库(2024-10 后停更但仍好用)。
- [DeepPavlov](https://github.com/DeepPavlov/DeepPavlov):6,987 stars,Apache-2.0,俄语对话/NER 框架(2024-11 后停更)。
- [facebookresearch/fastText](https://github.com/facebookresearch/fastText):26,529 stars,MIT(已归档,2024-03);训练好的乌语/俄语词向量适合做轻量文本分类(如"是否战报/是否含地名"二分类)。

### 4.2 LLM 结构化输出框架

| 项目 | URL | License | Stars(2026-09-06) | 特点 |
|---|---|---|---|---|
| instructor | [github.com/567-labs/instructor](https://github.com/567-labs/instructor) | MIT | 13,831 | Pydantic 模型驱动 LLM 结构化输出,支持 retry/校验循环,是当前提取层的事实标准之一 |
| outlines | [github.com/dottxt-ai/outlines](https://github.com/dottxt-ai/outlines) | Apache-2.0 | 15,756 | 正则/JSON schema/语法约束的**受限解码**(本地模型友好),保证输出合法 |
| pydantic | [github.com/pydantic/pydantic](https://github.com/pydantic/pydantic) | MIT | 28,723 | 数据校验基础件,事件 schema 的建模层 |
| langchain | [github.com/langchain-ai/langchain](https://github.com/langchain-ai/langchain) | MIT | 145,759 | 提取链/工具生态最大,但依赖重;本项目简单管线未必需要 |

- **对我们的启示**:设计上**事件 schema 用 Pydantic 定义**,LLM 提取用 **instructor**(云端 API 场景)或 **outlines**(若后续自托管本地模型);"LLM 只输出 JSON,校验/纠错在 Python 侧"是社区共识。低资源语言实践要点:few-shot 示例必须用**真实乌俄语短消息样例**(直接取材自前 500 条采集数据人工标注),并强制要求模型输出**规范化地名(与 gazetteer 键一致)** 而非任意字符串。

### 4.3 战况文本解析的开源尝试

- **VIINA**:BERT(ruBERT 系斯拉夫语预训练模型微调)对新闻标题做 24 类多标签分类——**"规则词典先行"时代最好的监督学习参照**,但其输入是新闻标题,迁移到 Telegram 口语需重训。
- **TGeocoder**([github.com/MJCruickshank/TGeocoder](https://github.com/MJCruickshank/TGeocoder),43 stars,MIT,2025-01):Telegram 消息 → GPT-4o 解析(事件摘要 + 地点)→ 地理编码 → Folium 地图,是"LLM 直接从消息提结构化事件"的最小可行原型,**代码可直接读**。
- **telegram-monitor**([github.com/zoharbabin/telegram-monitor](https://github.com/zoharbabin/telegram-monitor),8 stars,MIT):Telethon 流 → Claude 批量翻译/分类/摘要,带乌克兰配置档案;证明"批量攒消息再一次性 LLM 分析"可显著省钱(而非每条一调)。
- **结论**:没有现成的"乌语 Telegram 战报 → 结构化事件"生产级开源库(如实说明);最接近的组合是 TGeocoder 的 prompt 设计 + VIINA 的事件类型学 + instructor/pydantic 的 schema 约束。**"词典+规则先行、LLM 兜底"与上述所有项目实践方向一致,且是成本最优解。**

---

## 领域 5:去重与事件融合

### 5.1 通用工具

#### dedupe — [github.com/dedupeio/dedupe](https://github.com/dedupeio/dedupe)
- **License**:MIT | **语言**:Python
- **活跃度**:4,510 stars,活跃,最近提交 2025-07-29(数据 2026-09-06)
- **功能**:基于主动学习的记录链接/实体解析(fuzzy 匹配 + 聚类 + 人工标注训练)。
- **对我们的启示**:适合"冷启动期用少量人工标注训练出地名/事件匹配器";但其训练流程偏重,实时管线里通常退化为"规则 + 阈值"。

#### RapidFuzz — [github.com/rapidfuzz/RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)
- **License**:MIT | **语言**:Python/C++
- **活跃度**:4,112 stars,活跃,最近提交 2026-08-30(数据 2026-09-06)
- **功能**:极速模糊字符串匹配(Levenshtein/Jaro-Winkler/token_sort_ratio…)。
- **对我们的启示**:地名别名匹配与消息近似去重的**主力工具**;token_set_ratio 对"语序颠倒的乌俄短语"效果好。

#### libpostal — [github.com/openvenues/libpostal](https://github.com/openvenues/libpostal)
- **License**:MIT | **语言**:C(Python 绑定)
- **活跃度**:4,882 stars,活跃,最近提交 2025-12-06(数据 2026-09-06)
- **功能**:全球地址解析/规范化(统计 NLP)。
- **对我们的启示**:对"地址式地名"(村+区+州)规范化有帮助,但乌俄语覆盖一般;**可选组件**,对短消息提取非必需。

### 5.2 时空聚类

- [scikit-mobility](https://github.com/scikit-mobility/scikit-mobility):806 stars,BSD-3-Clause,含轨迹聚类/OD 分析(2023 后停更)——事件"轨迹化"后可复用其聚类与停驻点检测。
- [apache/sedona](https://github.com/apache/sedona):2,396 stars,Apache-2.0,**活跃**;`ST_ClusterDBSCAN` 等空间 SQL 可直接在(分布式)SQL 里做点聚类——**数据量大后可平滑迁移**;单机阶段用 PostGIS 的 [ST_ClusterDBSCAN](https://postgis.net/docs/ST_ClusterDBSCAN.html) 即可,不必上 Spark。
- sklearn 的 [DBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html) / [HDBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html):单机首选(无需另立条目)。

### 5.3 新闻/OSINT 领域的事件合并实践

- **VIINA 的 one-per-day 过滤器**(领域 2 已述):同地点+同日期+同"actor×tactic"组合 → 合并为单事件,保留 `n_reports`、`sources`、`event_ids` 溯源字段。**这是与我们的需求最贴合的既有设计,建议直接采用并把时间粒度收紧到"小时级窗口"**(Telegram 消息密度远高于新闻)。
- **chronicle**([github.com/dukeblue1994-glitch/chronicle](https://github.com/dukeblue1994-glitch/chronicle),2 stars,MIT,2026-02):语义 embedding + **MinHash LSH 去重** + HDBSCAN 聚类的新闻流时间线构建——小项目但完整演示了"embedding 级去重"管线,可作 LLM 时代的补充参考。
- **GDELT 相关**:GDELT 自身事件编码([gdeltproject.org](https://www.gdeltproject.org/))是"事件合并"的行业参考(OSINT-War-Room 用它做冲突事件源);[counter-news](https://github.com/hancee/counter-news)(内容相似度去重 GDELT 事件)演示了内容级去重。
- **我们的建议分层策略**:
  1. **强指纹层**(快):规范化文本(normalize 乌/俄拼写)+ 提取出的 (地名, 事件类型) + 时间桶(如 30 分钟)+ 转发关系(Telegram forwarded_from 字段)→ 直接判重,绝大多数重复在此消灭(战争群转发链极多);
  2. **空间层**:同一地名簇(同一聚落或 5km 半径)内同类型事件在短时间窗内 → 合并并累加 `report_count`、聚合来源;
  3. **语义层**(慢,异步):MinHash/embedding 相似度处理"不同地名措辞同一事件"(别名、错拼);仅在弱规则层无法决断时触发。

---

## 领域 6:地图可视化与时间序列地图

### 6.1 核心库

#### MapLibre GL JS — [github.com/maplibre/maplibre-gl-js](https://github.com/maplibre/maplibre-gl-js)
- **License**:BSD-3-Clause | **语言**:TypeScript
- **活跃度**:11,547 stars,极活跃,最近提交 2026-09-06(数据 2026-09-06)
- **功能**:矢量瓦片渲染引擎;自带 heatmap layer、symbol/circle layer、表达式驱动样式。
- **对我们的启示**:本项目已选定它,生态与维护状况都是同类最佳。**时间衰减实现**:给事件数据带 `ts` 字段,前端用 `heatmap-weight`/`circle-opacity` 的表达式按时间衰减(如 `interpolate` + `linear` 把 age 映射到 0–1 权重),或用 filter 只显示最近 N 小时;官方示例页 [maplibre.org/maplibre-gl-js/docs/examples](https://maplibre.org/maplibre-gl-js/docs/examples/) 有 heatmap/cluster 样例。社区讨论 [maplibre-gl-js #5188](https://github.com/maplibre/maplibre-gl-js/discussions/5188) 汇总了 time slider 方案。

#### supercluster — [github.com/mapbox/supercluster](https://github.com/mapbox/supercluster)
- **License**:ISC | **语言**:JavaScript
- **活跃度**:2,356 stars,活跃,最近提交 2026-09-03(数据 2026-09-06)
- **功能**:浏览器端极速点聚类(层次聚类,缩放自适应)。
- **对我们的启示**:MapLibre 内置 cluster 即基于它;海量事件标记(数万点)时开 cluster + 点击钻取。

#### Kepler.gl — [github.com/keplergl/kepler.gl](https://github.com/keplergl/kepler.gl)
- **License**:MIT | **语言**:TypeScript/React
- **活跃度**:11,995 stars,活跃,最近提交 2026-09-05(数据 2026-09-06)
- **功能**:Uber 的大数据地理分析组件,**自带时间过滤器 + 回放动画**(time playback)、热力/聚类/弧线图层。
- **对我们的启示**:**做原型/探索性分析最快**;但嵌入自建 UI 的定制成本高(React 依赖、体积大)。**建议:探索阶段用 Kepler.gl 验证"事件数据长什么样",生产界面仍用 MapLibre 自绘。**

#### deck.gl — [github.com/visgl/deck.gl](https://github.com/visgl/deck.gl)
- **License**:MIT | **语言**:TypeScript
- **活跃度**:14,558 stars,极活跃,最近提交 2026-09-05(数据 2026-09-06)
- **功能**:WebGL2 大数据可视化框架,HeatmapLayer/TripsLayer(轨迹回放)等。
- **对我们的启示**:若事件量达到十万级或需要"导弹轨迹动画",deck.gl 与 MapLibre 可混用([maplibre-gl 集成](https://deck.gl/docs/api-reference/geo-layers/map-layers/maplibre-overlay));初期不需要。

#### maplibre-gl-interpolate-heatmap — [github.com/geoql/maplibre-gl-interpolate-heatmap](https://github.com/geoql/maplibre-gl-interpolate-heatmap)
- **License**:MIT | **语言**:TypeScript
- **活跃度**:13 stars,活跃,最近提交 2026-08-15(数据 2026-09-06)
- **功能**:MapLibre 自定义 WebGL 图层,按点值做 **IDW 插值热力**(区别于原生"密度"热力),支持自定义配色 GLSL 函数。
- **对我们的启示**:需要"按强度/置信度加权"的战争活动面(而非纯点密度)时的现成方案;也可自行写 custom layer 做时间衰减面。

### 6.2 时间轴/回放参考

- **OpenHistoricalMap** — [openhistoricalmap.org](https://www.openhistoricalmap.org/);引擎仓库 [OpenHistoricalMap/ohm-website](https://github.com/OpenHistoricalMap/OpenHistoricalMap)(23 stars,GPL-2.0,OSM Rails 的定制镜像;issue 仓库 [OpenHistoricalMap/openhistoricalmap](https://github.com/OpenHistoricalMap/OpenHistoricalMap) 已归档):**"地图+时间轴"的旗舰开源实践**——URL 带日期参数、按时间过滤要素、时间滑条 UI,是"事件时间回放"最值得抄的交互范式。
- **GeoLibre** — [github.com/opengeos/GeoLibre](https://github.com/opengeos/GeoLibre):7,161 stars,MIT,活跃;浏览器/桌面/Jupyter 通用 GIS 平台,内置 time slider 与 COG 时间序列支持,可参考其时间控件实现。
- **leaftime**(R)— [timelyportfolio.r-universe.dev/leaftime](https://timelyportfolio.r-universe.dev/leaftime/doc/manual.html):Leaflet 时间滑条插件,小而美,交互可借鉴(与 MapLibre 无代码复用)。
- **暗色底图**:IRONSIGHT 生产验证了 **CARTO Dark Matter**(`https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png`,数据 © OSM,ODbL)作为暗色战争面板底图——直接采用即可,配合 MapLibre 自绘叠加层,无需自建瓦片服务。

---

## 领域 7:端到端类似项目("Telegram → 地图" / OSINT 告警)

| 项目 | URL | License | Stars | 状态(2026-09-06) | 技术栈 | 对我们的启示 |
|---|---|---|---|---|---|---|
| **TGeocoder** | [github.com/MJCruickshank/TGeocoder](https://github.com/MJCruickshank/TGeocoder) | MIT | 43 | 2025-01 停更 | Telethon + GPT-4o + Folium | **与我们最接近的原型**:Telegram 新闻频道 → LLM 解析出事件+地名 → 地理编码 → 地图打点。直接读其 prompt 与解析流程;缺点是 Notebook 形态、非实时、无去重与持久化——恰好是我们项目要补齐的部分 |
| **IRONSIGHT** | [github.com/NoblerWorks-HQ/IRONSIGHT](https://github.com/NoblerWorks-HQ/IRONSIGHT) | MIT | 633 | 活跃(2026-08-31) | Next.js 16 + TS + Leaflet + CARTO 暗色瓦片 | **工程化面板标杆**:双战区(伊朗/以色列、俄/乌)切换、Telegram 公开频道实时抓取(60s 轮询)+自动翻译、alerts.com.ua 州警报(15s)、Neptun 无人机/导弹轨迹、NASA FIRMS 火点;docker-compose 部署。参考其面板布局、数据源表与免责声明写法;坑:Telegram 走公开 embed 抓取(无历史、易频控),地图用 Leaflet 而非 MapLibre |
| **OSINT-War-Room** | [github.com/Hue-Jhan/OSINT-War-Room](https://github.com/Hue-Jhan/OSINT-War-Room) | MIT | 85 | 活跃(2026-06-04) | FastAPI + 原生 JS + Leaflet | FastAPI + asyncio 后台抓取 + 内存缓存 + JSON 平文件的轻后端范本;GDELT 冲突事件、Telegram 预览页抓取(15s)、警报轮询。其 README 明列已知坑(Telegram 预览频控、OpenSky 限流)——与我们的限流策略互相印证 |
| **telegram-monitor** | [github.com/zoharbabin/telegram-monitor](https://github.com/zoharbabin/telegram-monitor) | MIT | 8 | 活跃(2026-03-01) | Telethon + Claude + Textual TUI | Telethon 99+ 频道实时流 + **消息批量攒批再交 Claude 分析**(省钱关键)+ 乌克兰/以色列等 6 套配置档案;无地图无持久化,但"采集→LLM 简报"段可直接学 |
| **Telegram_grabber** | [github.com/michailz/Telegram_grabber](https://github.com/michailz/Telegram_grabber) | MIT | 0 | 2023-12 停更 | Telegram + PostgreSQL | 极简"消息入库 PG + Web 搜索"示例,展示采集层与 PG 的对接;太小,仅参考表结构思路 |
| **UkraineTracker** | [github.com/spectator8/UkraineTracker](https://github.com/spectator8/UkraineTracker) | GPL-3.0 | 6 | 2025-05 停更 | 链接集 | 乌克兰战况追踪资源链接大全(地图/数据源清单),做"数据源竞品清单"时有用 |
| **VIINA** | [github.com/zhukovyuri/VIINA](https://github.com/zhukovyuri/VIINA) | ODbL | 356 | 每日更新 | R + BERT | 新闻端到端(采集→地名→分类→去重→地图+时间线→每日发布),**方法论上最完整**,详见领域 2/4/5 |
| **ruarq/ukraine-war-heatmap** | [github.com/ruarq/ukraine-war-heatmap](https://github.com/ruarq/ukraine-war-heatmap) | MIT | 10 | 已停(2022-04) | Reddit PRAW + Folium | 端到端反面教材:纯热力图表现力不足+单一来源衰减→项目死亡;教训已吸收(领域 2) |

**如实说明**:未搜到任何"Telegram 战争群 → 结构化事件 → 实时地图 → 自动截图发布"的完整开源系统;最接近的组合即上表。这意味着本项目的**差异化价值真实存在**,但每一段都有成熟组件可拼装,不必从零造轮子。

---

## 领域 8:部署与运维参考

### 8.1 数据库/容器模板

- [postgis/docker-postgis](https://github.com/postgis/docker-postgis):1,596 stars,MIT,官方 PostGIS 镜像(16/17 多版本 tag + 初始化脚本钩子 `/docker-entrypoint-initdb.d`)——**直接采用**,init 脚本里建表、建索引、导入行政区划。
- [bengotow/postgis-with-pgvector](https://github.com/bengotow/postgis-with-pgvector):0 stars,MIT,基于官方 postgis 镜像叠加 pgvector——若后续 LLM 去重要用向量检索,直接换这个镜像(近期活跃,2026-08)。
- [JBris/postgis-docker](https://github.com/JBris/postgis-docker):2 stars,独立 PostGIS compose 配置(2023 停更)——仅作最小示例参考。

### 8.2 实时管线参考

- [amirtds/svelte-fastapi-dashboard](https://github.com/amirtds/svelte-fastapi-dashboard):19 stars,MIT,**FastAPI + SSE 实时仪表盘**完整示例(Svelte 前端 + SSE 流 + 阈值告警)——**直接作为我们 FastAPI/SSE 段的骨架参考**(前端我们换 MapLibre)。
- [psyb0t/docker-telethon-plus](https://github.com/psyb0t/docker-telethon-plus):2 stars,WTFPL,Telethon 客户端容器化为 HTTP 服务——**采集器微服务化参考**:session 挂 volume、healthcheck、与主服务解耦重启。
- [IRONSIGHT](https://github.com/NoblerWorks-HQ/IRONSIGHT) 的 [docker-compose.yml](https://github.com/NoblerWorks-HQ/IRONSIGHT):多阶段 Dockerfile(产物 ~215MB、非 root 运行)+ compose 单命令起——单页面板工程的打包范式。
- [End-to-End-Data-Pipeline](https://github.com/hoangsonww/End-to-End-Data-Pipeline)(Kafka/Spark/Airflow/K8s 教学模板):说明"重流式架构"有现成模板,但**对我们属于过度设计**。

### 8.3 给本项目的部署建议(综合社区实践)

1. **单机 docker-compose 起步**:`postgis/postgis` + `redis` + `app`(采集器 + FastAPI 同进程或两容器)+ 可选 `photon`/`playwright` 容器。**不上 Kafka/K8s**——4–10 个频道的消息量(每分钟几十条)Redis pub/sub 或直接 PG LISTEN/NOTIFY 都绰绰有余。
2. **采集器独立容器 + session volume + healthcheck**:账号出问题时单独重启,不拖垮 API。
3. **Playwright 容器**:用 `mcr.microsoft.com/playwright` 官方镜像跑截图任务(Cron 或事件驱动),输出图挂 volume 供发布器读取;截图失败不影响主链路。
4. **SSE 网关**:FastAPI + `sse-starlette`,加心跳与断线重连;前端 EventSource 自带重连,服务端按频道去重推送。
5. **备份与恢复**:PG 定时 dump + session 文件备份;历史消息(tg_history_dumper 导出)保留原始 JSONL 作为"事件重算"的 source of truth——规则升级后可全量重放,这是规则先行架构的最大红利。

---

## 结论与建议采纳清单(按优先级)

### A. 直接采用(有成熟开源实现,不要自研)

1. **Telethon 做采集主路**(接受其 archived 现状:锁定版本 + 独立容器 + 定期评估替代;配合 tdl/tg_history_dumper 做历史冷启动,snscrape/公开预览页做降级)。
2. **gazetteer 三源合一**:GeoNames UA(多语别名)+ KATOTTH(VIINA 的 [katotth_UA_tess.geojson](https://github.com/zhukovyuri/VIINA/tree/master/Data/katotth_UA_tess.geojson))+ OSM Geofabrik 乌克兰切片;转写用 transliterate/cyrtranslit 预生成键。
3. **事件类型学**直接沿用 VIINA 的 24 类(裁剪为 Telegram 高频子集)+ 保留 `location_confidence`/`GEO_PRECISION` 字段。
4. **去重三层法**:规范化指纹+转发链 → (地名,类型,时间窗)合并并保留 n_reports/sources(VIINA 模式,窗口收紧到小时级)→ MinHash/embedding 兜底;工具用 RapidFuzz + PostGIS/DBSCAN。
5. **数据层**:postgis/postgis 官方镜像(+pgvector 变体)、Redis、SQLAlchemy+GeoAlchemy2。
6. **前端**:MapLibre GL JS + CARTO Dark Matter 底图 + supercluster/heatmap 时间衰减表达式;SSE 用 FastAPI + sse-starlette。
7. **LLM 提取**(二期)用 instructor + Pydantic schema;"攒批分析"借鉴 telegram-monitor 以降成本。
8. **交叉验证图层**:接入 DeepStateMap API(容错+缓存)与 alerts.com.ua/alarms.in.ua 官方 API,作为事件置信度校验与"警报类"事件的权威源。

### B. 重点参考(读代码/抄设计,不全盘采用)

1. **Liveuamap 的 UI 骨架**(类别图标 + 相对时间 + 时间线筛选)与 **DeepStateMap 的状态多边形 + 三语名**设计语言。
2. **TGeocoder** 的"Telegram→LLM→地理编码"prompt 流程;**telegram-monitor** 的批量分析架构。
3. **VIINA** 的 geocoding 人工核查流程与每日数据发布格式;**The Economist war-fire model** 的事件数据公开格式。
4. **IRONSIGHT / OSINT-War-Room** 的面板布局、数据源清单与限流参数(15s/60s 轮询级),以及它们的免责声明写法(第三方端点合规风险)。
5. **OpenHistoricalMap / GeoLibre** 的时间轴交互;kepler.gl 用于探索期快速验证。

### C. 先不碰(现阶段性价比低)

1. **Kepler.gl / deck.gl 嵌入生产 UI**(体积与定制成本高;探索期可用,生产用 MapLibre 自绘)。
2. **Apache Sedona / Spark / Kafka / Airflow / K8s**(量级不需要;PostGIS 单机即可,待数据量真到百万事件/天再迁移)。
3. **outlines 受限解码**(本地模型阶段才需要;云端 API 阶段 instructor + JSON schema 足够)。
4. **Pyrogram / GramJS / DeepPavlov / scikit-mobility**(已归档或停更,或与主栈重复)。
5. **liveuamap 2014 开源仓库、Mnilax/tg-scraper、shal/ua-cities 等小/旧项目**(质量与 License 风险大于收益)。

### 主要风险提示

- **账号合规**:MTProto 用户端采集违反 Telegram ToS,存在封号风险;生产部署必须做多账号隔离、FloodWait 退避、降级通道与法律评估。
- **数据源非官方**:DeepStateMap/alarms 相关 API 无 SLA,随时可能变更;统一做"抓取→缓存→重试→降级"四件套,并保留原始消息 JSONL 以便重算。
- **维护状态**:本报告引用的多个基础库(Telethon/Pyrogram/GramJS/fastText/natasha 等)在 2024–2026 年间陆续归档,选型需以"锁定版本 + 升级窗口"方式管理,不要把"生态永久活跃"当默认假设。
- **License 审查**:ODbL(VIINA/OSM 派生数据)与 AGPL(tdl)在再分发场景有义务要求;商用发布前需逐项确认。
