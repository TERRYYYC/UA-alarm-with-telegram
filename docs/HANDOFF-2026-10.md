# 项目完整交接（2026-10-17）

本文是**当前真实状态**的交接主文档，取代 `docs/HANDOFF.md`（那是 2026-09 设计收敛阶段的文档，保留为历史资料）。凡本文与旧文档冲突，以本文为准。

---

## 0. 一句话现状

**项目已经在生产运行**：三容器跑在 `69.5.20.240` 上，真实 Telegram 采集已启用并连续工作约 4 周，实时地图、SSE、待复核队列、地名库维护、发布导出链路均已可用；唯一对外通道是 SSH 隧道，公网不可达。

| 项目 | 状态 |
| --- | --- |
| 后端 API | ✅ `telegram-alarm-api-1` Up (healthy)，`127.0.0.1:8000` |
| Postgres/PostGIS | ✅ `telegram-alarm-db-1` Up (healthy)，仅容器网络内 5432 |
| 前端 | ✅ `telegram-alarm-web-1` Up，`127.0.0.1:5173` |
| 真实采集 | ✅ 2 个 Telegram 频道，已从历史回填转入实时 |
| 本机测试 | ✅ `105 passed` |
| 发布器（自动截图转发） | ⛔ 未启用（需 `PUBLISH_OUT_DIR` + 容器内可用的 Chrome，见 §11.3） |
| Git 版本控制 | ⛔ **零提交**（头号工程风险，见 §13.1） |
| 人工复核队列 | ⚠️ 积压 12000+ 条 `needs_review`（见 §13.2） |

### 实时快照（2026-10-16 20:13 UTC，实测）

```
telegram-alarm-api-1  Up 3 weeks (healthy)  127.0.0.1:8000->8000/tcp
telegram-alarm-db-1   Up 3 weeks (healthy)  5432/tcp
telegram-alarm-web-1  Up 3 weeks            127.0.0.1:5173->80/tcp

GET /api/v1/healthz → {"status":"ok","places":35627,"events":3887,"pending":12102}

raw_messages:  tg:Віраж Києва 9114 (最新 19:28:01) | tg:Тіні Ракет 3873 (最新 19:09:00) | fixture 13
Telethon 连接:  ESTABLISHED 149.154.167.51:443  → 采集存活，当时是频道安静期
```

吞吐量级：约 400–500 条原始消息/天、80–110 个事件/天，来自这 2 个频道。

---

## 1. 交接阅读顺序

1. 本文（现状、架构、风险、待办）。
2. `docs/OPERATIONS.md`（运维手册：启停、日志、回放、地名维护、常见问题表）。
3. `docs/DESIGN.md`（架构与数据模型的设计理由、决议记录）。
4. `docs/HANDOFF.md` + `docs/REVIEW-AND-START.md`（历史：设计收敛过程、验收边界）。
5. `docs/RESEARCH-OPENSOURCE.md`、`docs/PLUGIN-ARCHITECTURE-PROPOSAL.md`（调研与未采纳的扩展提案，历史资料）。
6. 代码入口：`backend/app/main.py`（启动序列）→ `backend/app/service.py`（管线核心）→ `backend/app/api.py`（接口）。

---

## 2. 系统架构与运行拓扑

```
Telegram 频道 ──(MTProto/Telethon)──> runner 线程
                                        │ ingest_message()
                                        v
                              提取 → 地名解析 → 候选 → 合并 → 事件
                                        │
                                        ├─> Postgres (events/candidates/evidence/…)
                                        ├─> EventBus ──> SSE /api/v1/stream ──> React 前端
                                        └─> publisher 线程（默认关闭）──> 截图 + 转发 TG/Discord
```

生产拓扑（远端 `69.5.20.240`，compose 项目名 `telegram-alarm`）：

| 服务 | 镜像 | 端口 | 说明 |
| --- | --- | --- | --- |
| `db` | `postgis/postgis:16-3.4` | 5432/tcp（仅容器网络） | 用户/库/密码均为 `smon`；卷 `telegram-alarm_pgdata`；healthcheck `pg_isready` |
| `api` | `build ./backend`（`telegram-alarm-api:latest`，440MB） | `127.0.0.1:8000->8000` | uvicorn；启动时跑 alembic 迁移 + 加载地名库 + 起采集线程 |
| `web` | `build ./web`（nginx:alpine，94.9MB） | `127.0.0.1:5173->80` | 静态前端 + `/api/` 反代到 `api:8000`（为 SSE 关缓冲） |

网络 `telegram-alarm_default`。**所有端口只绑回环**，靠 SSH 隧道访问：

```sh
ssh -N -L 15173:127.0.0.1:5173 -L 18000:127.0.0.1:8000 ubuntu-remote
# 然后浏览器打开 http://127.0.0.1:15173
```

数据流要点：
- **前端全部走相对路径** `/api/v1/…`（`web/src/api.ts`），因此同源 + nginx 反代即可，不需要配置 API 地址。
- SSE 与 60 秒轮询**并行**：SSE 负责秒级增量，轮询负责兜底与统计刷新。
- 采集、发布都在 API 进程内的独立线程里，没有额外容器。

---

## 3. 代码地图

### 3.1 后端 `backend/`

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | 启动序列（lifespan）、日志配置、迁移、加载地名库、起发布/采集线程 |
| `app/api.py` | 全部 HTTP 路由（`APIRouter(prefix="/api/v1")`）、SSE、CORS |
| `app/service.py` | 管线核心：地名发现、候选生成、事件合并、自动确认、人工纠正、拆分、重匹配（最大文件） |
| `app/extractor.py` | 事件类型、否定/撤回/解除警报关键词、时间解析 |
| `app/gazetteer.py` | 内存地名目录：normalize、别名/词形/模糊匹配、歧义消解、`is_reportable` 过滤 |
| `app/geocoder.py` | Nominatim 地理编码（限速 1.05s，15s 超时，异常吞掉返回 None） |
| `app/telegram_adapter.py` | Telegram **事件语义层**（不持网络连接）：修订/删除/转发 → ingest 载荷，维护游标与退避 |
| `app/runner.py` | 采集生命周期：Telethon 线程、重连退避、缺口补采、DB 写入、checkpoint 落库 |
| `app/publisher.py` | 发布器：tick 调度、任务租约、headless Chrome 截图（CDP）、TG/Discord/文件三种发送 |
| `app/models.py` | 9 张表的 SQLAlchemy 模型 |
| `app/schemas.py` | Pydantic 请求体：`IngestRequest`、`CorrectRequest`、`ResolveRequest`、`RejectRequest`、`PlaceUpsert` |
| `app/config.py` | 全部配置项（模块级 `os.getenv`，启动后不可变） |
| `app/db.py` | engine/Session 工厂；sqlite 路径下开 WAL + `busy_timeout=10000` |
| `app/state.py` | 进程内单例槽位：`gazetteer / bus / publisher / publisher_stop / runner / geocoder` |

`backend/scripts/`：`backup.sh`（pg_dump 或 sqlite `.backup` + session 复制）、`build_gazetteer.py`（GeoNames+KATOTTH 合成地名库，需网络）、`fetch_zh_names.py`（Wikidata 抓中文名，需网络）、`repair_report_counts.py`（修历史报道重复计数，默认 dry-run）、`replay_fixtures.py`（回放固定样本）、`scan_unknown_places.py`（回填待补地名）、`telegram_login.py`（首次登录四阶段）。

### 3.2 前端 `web/`（React 18.3 + TS 5.5 + MapLibre GL 4.7 + Vite 5）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `src/main.tsx` | 10 | 入口 |
| `src/App.tsx` | 284 | 顶层状态机与数据编排 |
| `src/api.ts` | 133 | 全部 REST + SSE 封装（16 个函数） |
| `src/types.ts` | 108 | 前后端契约类型 |
| `src/basemap.ts` | 76 | 底图预设：`esri-dark`(默认) / `carto-dark`(需 key) / `none`(离线兜底) |
| `src/util.ts` | 51 | 类型色、中文文案、时间与新鲜度分级 |
| `src/components/MapView.tsx` | 233 | MapLibre 实例、GeoJSON 圆点层、DOM 标签、地图拾取 |
| `src/components/Sidebar.tsx` | 284 | 侧栏三视图 Feed / Detail / Pending |
| `src/components/Toolbar.tsx` | 67 | 品牌状态灯、时间窗预设 `[15,60,120,180]`、类型筛选 |
| `src/components/Geodata.tsx` | 344 | 地名库维护：待补地名 + 检索/编辑/地图拾取 |
| `src/styles.css` | 570 | 唯一全局样式表（含 10 个 `:root` 设计令牌） |

构建：`npm run build` = `tsc -b && vite build`；产物体积 `index-*.js` ≈ 973KB（主因 maplibre-gl，**未做代码分割**，必然触发 500KB chunk 警告）、`index-*.css` ≈ 74KB。**前端没有 test/lint 脚本**。

### 3.3 其它

- `fixtures/sample_messages.jsonl`：13 行 / 11 个 `msg-00x`，覆盖 8 类场景（否定、转发合并、一消息双事件、未知地点、同名歧义、上下文消歧、时钟/昨夜时间、编辑链含撤回、俄语写法）。全部为合成数据。
- `docs/`：本文 + `OPERATIONS.md`、`DESIGN.md`、`HANDOFF.md`、`REVIEW-AND-START.md`、`RESEARCH-OPENSOURCE.md`、`PLUGIN-ARCHITECTURE-PROPOSAL.md`。
- `scripts/deploy.sh`：一键部署（见 §10）。
- `docker-compose.yml`：56 行，三服务编排。
- `.tools/`：自建 CDP 校验工具 `check-geodata.js`、`check-place-flow.js`、`verify-png.js` + 实例截图 + **Telegram session 备份（凭据级敏感，勿外传）**。
- `prototype/hifi/situation-monitor.html`：视觉基线原型。
- `TG_Message_processor_from_databse.json`：n8n 导出的旧流程（历史参考，未接入）。
- `dsh-plug/`：空目录。

---

## 4. 数据模型（`backend/app/models.py`，9 张表）

无任何 ORM `relationship()`，关系只靠外键；全部无 `server_default`；时间统一 naive UTC（`utcnow_naive()`，`models.py:22-23`）。

| 表 | 关键字段 | 备注 |
| --- | --- | --- |
| `sources` | `adapter`、`name`、`config_json`(含 `admin1_bias`)、`enabled`、`last_success_at`、`checkpoint_json`、`gap_notes` | 每个 Telegram 频道一行，`name=f"tg:{显示名}"` |
| `raw_messages` | `source_id`、`external_id`、`revision`、`body_text`、`published_at`、`received_at`、`forward_json`、`edit_json`、`original_url` | 唯一键 `(source_id,external_id,revision)`；索引 `(source_id,external_id)` 为「消息族」 |
| `gazetteer_places` | `canonical_name`、`names_json`、`lat/lng`、`admin1/admin2`、`precision`、`source`、`name_zh`、`version` | 只存**运行时地理编码 + 管理端编辑**的条目；启动时与 JSON 文件目录合并 |
| `candidates` | `message_id`、`event_type`、`place_id`（**无 FK**）、`place_raw`、`occurred_at`、`uncertainty_seconds`、`time_basis`、`confidence`、`status`、`payload_json` | `status ∈ accepted / ignored / needs_review / failed`；**无 `created_at`** |
| `events` | `seq`、`event_type`、`place_id`（**无 FK**）、`geometry_json`、`occurred_at`、`ended_at`、`verification`、`report_count`、`version`、`merged_into_id`、`reason_json` | `seq` 供 SSE 单调补读；`ended_at IS NULL` 表示未解除；**无 `source_id`**（按频道统计请查 `raw_messages`） |
| `evidence` | `message_id`、`event_id`、`role` | 唯一键 `(message_id,event_id,role)`；`role ∈ report / merge_basis / manual` |
| `unknown_places` | `raw`、`display_name`、`context`、`report_count`、`source_id`、`sample_message_id`、`status`、`resolved_place_id`（**无 FK**）、`message_ids_json` | `status ∈ open / resolved / ignored` |
| `tasks` | `kind`、`status`、`lease_until`、`attempts`、`max_attempts`、`params_json`、`result_json`、`error` | 发布任务队列与租约 |

### 迁移链（`backend/alembic/versions/`，单头线性）

```
(None) → 03129c836c0f  initial schema（建 8 表 + 索引，初版含 2 个 place FK）
       → 8333654b6a34  event ended_at
       → 3a612aa69328  gazetteer name_zh
       → b07f49e510c4  unknown places
       → d4e5f6a7b8c9  drop place_id foreign keys   ← 当前 head
```

`alembic.ini` 里 `sqlalchemy.url` 只是占位，真实 URL 由 `backend/alembic/env.py` 从 `DATABASE_URL` 注入。

**迁移 5（本次新增）的由来**：Postgres 强制外键，而内存地名目录里的 3.5 万条地名从不写入 `gazetteer_places`，导致任何引用文件目录地名的 ingest 都以 `ForeignKeyViolation` 500 失败。SQLite 默认不强制外键（`app/db.py` 也没开 `PRAGMA foreign_keys`），所以本机 100+ 测试全过、问题只在 Postgres 上暴露。修复方式 = 按 SQLite 的实际语义移除这三个外键（列保留）。
回归守卫：`backend/tests/test_schema_place_fk.py`。

---

## 5. 配置项（`backend/app/config.py`，全部模块级读取，启动后不可变）

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/app.db` | 生产为 `postgresql+psycopg://smon:smon@db:5432/smon`（compose 注入） |
| `GAZETTEER_SEED` | `data/gazetteer_seed.json` | 主种子；同目录自动再载 `gazetteer_uk.json`、`place_names_zh.json`、`place_names_zh_gn.json` |
| `MERGE_WINDOW_MINUTES` | 60 | 合并窗口：同类型同地点且 `abs(Δoccurred_at) <= 3600s` 才合并 |
| `FUZZY_THRESHOLD` | 92 | 单 token 模糊匹配下限（`rapidfuzz.fuzz.ratio`） |
| `GEOCODER_ENABLED` | `1`（真） | 是否启用 Nominatim |
| `GEOCODER_MIN_IMPORTANCE` | 0.15 | 地理编码命中重要度下限 |
| `AUTO_CONFIRM_MIN_SOURCES` | 2 | 自动确认所需独立来源数 |
| `AUTO_CONFIRM_MIN_REPORTS` | 3 | 自动确认所需报告数 |
| `WAVE_CORROBORATION` | 真 | 波次级印证总开关 |
| `WAVE_RADIUS_KM` | 50 | 波次印证距离上限 |
| `WAVE_WINDOW_MIN` | 60 | 波次印证时间半窗 |
| `PUBLISH_OUT_DIR` | `""` | **空 = 发布器不启用** |
| `PUBLISH_MIN_INTERVAL_S` | 300 | 发布最小间隔 |
| `PUBLISH_RENDER_URL` | `http://127.0.0.1:5173/?export=1` | 截图目标地址 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_PUBLISH_CHAT` | `""` | 同时存在则用 TelegramSender |
| `DISCORD_WEBHOOK_URL` | `""` | 无 TG 时用 DiscordSender |
| `TELEGRAM_API_ID` / `_API_HASH` / `_PHONE` / `_CHATS` / `_SESSION_PATH` | — | 采集器（见 §7） |

---

## 6. 处理管线（原始消息 → 事件）

入口 `ingest_message(...)`（`backend/app/service.py:584`），HTTP 由 `backend/app/api.py:47` 调用，采集器由 `backend/app/runner.py:307` 调用。

1. **幂等查重**：按 `(source_id, external_id, revision)` 命中即返回 `{"status":"duplicate"}`。
2. **落 raw**：写 `raw_messages` 后 `flush()`。
3. **提取** `analyze_message()`（`backend/app/extractor.py:89`）：先 `gazetteer.normalize()`，返回 `types / negated / retracted / all_clear / occurred_at / uncertainty / time_basis`。
   - 类型模式：`shelling / explosion / drone / missile / air_alert`（`extractor.py:6-12`）。`приліт` 与具体类型并存时丢弃 shelling；无类型词时用 emoji 与词根兜底（💥→explosion、🚀→missile、⚠/кружля/ппо/тривожн→drone、реактивн→missile）。
   - 时间解析 `parse_time()`（`extractor.py:62`）：`N хв тому`→published−N 分钟（uncertainty 300s）；`о HH:MM`→当日时刻，若晚于 published 则减一天（1800s）；`вночі`→前一日 23:30（36000s）；`вчора`→−1 天（43200s）；否则取 published（3600s）。
4. **撤回**：`retracted` 或 `edit_json.deleted` → `_apply_retract()`：候选置 ignored、删证据、`report_count -= n`，归零则 `verification="rejected"`。
5. **解除警报**：`all_clear` → `_end_active_alerts()`：结束同地点的 `air_alert`；无地点时按该来源的 `admin1_bias` 结束整州警报。
6. **处理消息** `_process_message()`（`service.py:821`）：
   - 地名发现 `_find_places()`（`service.py:143`）：先双 token 词组（保证 `Кривий Ріг` 不被拆开）→ 未解析的大写专名对 → 单 token（未解析且首字母大写、非停用词、下一词非小写才进待补）。
   - 待补地名：仅当有事件类型且未被否定/撤回 → `_record_unknown_places()`，按 normalize 键聚合，`report_count = len(message_ids)`。
   - 否定 → 每地点建 `status="ignored"` 候选。
   - 无类型 → 只为已解析地点建 `needs_review` 候选（reason `unknown_type`）。
   - 有类型 → 地点 × 类型建候选；**`precision=="region"` 的地点只承载 `air_alert`**；已解析且非模糊 → `confidence=1.0`，否则 0.5。
   - 未解析且有 geocoder → 先置 accepted，调 `auto_resolve_place()`；命中写 place_id，否则回落 `needs_review`，reason 为 `ambiguous_place`（自带 `ambiguous_ids`）或 `unresolved_place`。
7. **接受候选** `_accept_candidate()`：`find_merge_target()` 命中则合并，否则新建 `Event(verification="unverified", report_count=1, seq=bus.next_seq())`；随后对每个事件跑 `promote_if_corroborated()`。
8. **提交后发布**：`db.commit()` 后逐个 `bus.publish(seq, serialize_event(...))` 推给 SSE。

### 地名解析（`backend/app/gazetteer.py`）

`resolve(raw, context="", admin_bias=None)`（`:404`）顺序：精确别名 → pymorphy3 词形还原（可选依赖）→ 仅单 token 的模糊匹配（桶 =（首字符, 长度 ±2），`ratio >= 92`）→ 形容词州名（`-ськ/-цьк/-зьк`，词干唯一命中）。

多候选消解顺序（`:440-483`）：唯一 region 胜 → 上下文 `admin1/admin2` 词干唯一 → `admin_bias` 唯一 → 同州多个按 `source="curated"` > `precision ∈ city/town` > 首个 → 形容词州名 → 否则返回 `{place_id: None, ambiguous_ids}`（进待复核）。

`normalize()`（`:89`）：小写、拉丁→西里尔折叠、`ё→е`/`ґ→г`、NFKD 后再 NFC。`is_reportable()`（`:158`）过滤自然地貌/纪念物/描述性短语/通用词。

地理编码 `Geocoder.search()`（`app/geocoder.py:22`）：Nominatim `countrycodes=ua, limit=1, accept-language=uk`，最小间隔 1.05s。`auto_resolve_place()`（`service.py:446`）新建地名时 `id = max(现有最大 id, 1_000_000_000)`，`source="nominatim"`，并写回 `gazetteer_places`。

### 合并规则（`service.py:204` `find_merge_target`）

候选必须是：`event_type` 相等 + `place_id` 相等 + `merged_into_id IS NULL` + `verification != "rejected"` + 两者 `occurred_at` 均非空且 `abs(Δ) <= MERGE_WINDOW_MINUTES*60`。

- 命中且已人工确认 → 候选置 ignored（reason `event_manually_confirmed`）。
- 命中且未确认：**同族**（同 `(source_id, external_id)`）证据已存在 → 只把证据指向新 revision，`report_count` 不变；否则 `report_count += 1` 并插一条 `role="report"` 证据。
- **也就是说：同一条消息的多次编辑既不增加报告数，也不触发多源确认。**

### 自动确认规则（`service.py:320-374`）

仅 `verification == "unverified"` 可被自动推进，人工判定不被覆盖。

- **路径 A**：`corroboration_stats()` 统计**按 `(source_id, external_id)` 去重**的独立来源数与报告数，`sources >= 2` 或 `reports >= 3` → 置 **`auto_confirmed`**。
- **路径 B（波次印证）**：对端须同 `event_type`、未合并、未 rejected、`place_id` 非空、`abs(Δoccurred_at) <= 60min`、haversine `<= 50km`，且**两者来源集合无交集**；命中则两者都置 `auto_confirmed`。只影响核验，不合并点位。
- `"confirmed"` **只能由人工** `correct_event()` 写入。
- ⚠️ `models.py:107` 的注释只列 `unverified|confirmed|rejected`，**漏了实际大量使用的 `auto_confirmed`**（文档/类型层面待补）。

---

## 7. 采集器（Telegram）

- 装配 `build_runner_from_env()`（`backend/app/runner.py:342`）：缺 `TELEGRAM_API_ID/HASH` → 记 `telegram runner disabled (TELEGRAM_API_ID/HASH not set)` 并返回 None（API 照常起）。
- `TELEGRAM_CHATS` 格式 `chat_id:显示名:偏置;chat_id:显示名:偏置`；每项对应 `sources` 一行，`name=f"tg:{显示名}"`，偏置写进 `config_json.admin1_bias`。非法项记日志后跳过。
- `TELEGRAM_SESSION_PATH` 默认 `data/telegram.session`；compose 里显式钉为 `/app/data/telegram.session`。
- **首次连接会回填**：`RunnerConfig.gap_fetch_limit = 200`，新来源 `last_msg_id=0`，故每频道最多回填最近 200 条；此后转实时。实测两台频道各回填约 200 条。
- **checkpoint 持久化**：写 `sources.checkpoint_json` 的 `{last_msg_id, last_seen_at, gap_notes}`，与 `last_success_at` 同事务提交。
- ⚠️ `sources.gap_notes` **列没有任何写入点**（只有读取），所以 `/sources`、`/stats` 返回的 `gap_notes` 恒为空字符串；缺口信息实际只在 `checkpoint_json` 里。
- **重连**：`FloodWait` → 所有 adapter 标记退避，睡 `min(seconds, 60)`，**不重连**；其它异常 → 退避 `min(5 * 2^n, 300)` 秒后重试并补缺口，成功后计数归零。
- **异常会被吞掉**：事件队列满（maxsize 5000）丢消息并 warn；DB `OperationalError` 重试 4 次后丢消息；`TelethonProvider.start()` 失败（session 未授权等）会落进通用 except → **无限重连，进程不退出**。所以「容器 healthy」不等于「采集正常」，**必须查 DB 新鲜度**。

### 判断采集是否活着（两个检查一起做）

**检查 1 — DB 新鲜度**（能发现异常，但「长时间无新消息」既可能是掉线，也可能只是两个频道安静）：

```sh
ssh ubuntu-remote "cd ~/telegram-alarm && docker compose exec -T db psql -U smon -d smon -c \
  \"select s.name, max(r.received_at) as latest, count(*) from raw_messages r \
    join sources s on s.id=r.source_id group by s.name order by 2 desc;\""
```

**检查 2 — Telethon 长连接是否还在**（用来区分「掉线」和「频道安静」）。容器内没有 `ss`/`netstat`，直接解析 `/proc/net/tcp`（注意端口是十六进制、IP 是小端序，**不能用 `grep 149.154`**）：

```sh
ssh ubuntu-remote "cd ~/telegram-alarm && docker compose exec -T api python3 -c \"
import socket,struct
def parse(p):
    for line in open(p).read().splitlines()[1:]:
        f=line.split()
        if len(f)<4: continue
        ip=socket.inet_ntoa(struct.pack('<I',int(f[2].split(':')[0],16)))
        yield ip,int(f[2].split(':')[1],16),f[3]
for ip,port,st in list(parse('/proc/net/tcp'))+list(parse('/proc/net/tcp6')):
    if port==443 and st=='01': print('ESTABLISHED',ip,port)
\""
```

出现 `ESTABLISHED 149.154.167.51 443`（Telegram DC 网段 `149.154.x.x`）即说明采集连接仍在。**2026-10-16 20:13 UTC 实测**：DB 最新消息停在 19:28（45 分钟前），但连接仍 ESTABLISHED → 属频道安静，不是掉线。

`latest` 距离当前时间若持续 > 30 分钟且检查 2 也没有连接，才是真的掉线（API 仍会显示 healthy）。

---

## 8. HTTP 接口清单

全部注册在 `backend/app/api.py:16`（前缀 `/api/v1`）。除注明外都先 `_require_state()`（未初始化 → 503）。

**写接口的本机限制**：`_require_local()`（`api.py:27`）只放行 `127.0.0.1 / ::1 / localhost / testclient`，否则 403。

| 方法 路径 | 作用 | 本机限制 |
| --- | --- | --- |
| GET `/healthz` | 地名数 / 事件数 / 待复核数 | 否 |
| POST `/ingest` | 外部报文入库，失败回滚并返 500 | 否（**开放写接口**） |
| GET `/events` | 事件列表（`window_minutes/types/verification/include_ended/limit/include_evidence`） | 否 |
| GET `/events/{id}` | 事件详情含逐条证据与 reason | 否 |
| GET `/pending` | `needs_review` 候选列表 | 否 |
| POST `/events/{id}/correct` | 人工纠正（核验/时间/地名/理由） | 否 |
| POST `/events/{id}/split` | 拆分被错误合并的事件 | 否 |
| GET `/sources` | 来源列表与 `last_success_at` / `gap_notes` | 否 |
| GET `/stats` | 一屏概览：核验/类型分布、来源健康、待补地名 | 否 |
| GET `/places` / `/places/{id}` | 地名检索（乌语/中文/州/别名，相关度排序）/ 详情 | 否 |
| POST `/places` | 新建地名 | 是 |
| PUT `/places/{id}` | 改地名并立即重推受影响事件 | 是 |
| GET `/unknown-places` | 待补地名（按出现次数排序） | 否 |
| POST `/unknown-places/{id}/ignore` | 忽略待补地名 | 是 |
| POST `/admin/reverify` | 对既有事件重跑交叉印证（含波次） | 是 |
| POST `/admin/rematch` | 补地名后重跑匹配 | 是 |
| POST `/pending/{id}/resolve` | 复核通过转事件 | 否 |
| POST `/admin/reprocess` | 规则升级后全量重放（幂等） | 是 |
| POST `/pending/{id}/reject` | 复核驳回 | 否 |
| POST `/admin/publish-now` | 手动触发发布会（`force=True` 绕过最小间隔） | 是 |
| GET `/stream` | SSE 事件流 | 否 |

**SSE 语义**（`api.py:461-490`）：连接时先按 `Event.seq > last_id`（查询参数，默认 0）升序补发**最多 1000 条**，之后只推新事件（`seq > seen`）；事件名固定 `event`，data 为 `serialize_event()` 的 JSON（`ensure_ascii=False`）；队列空时每 0.2s 轮询，客户端断开时 `bus.unsubscribe`。代码未配置心跳。

**CORS**（`api.py:493` `add_cors`）：`allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]`，`allow_methods=["*"]`，`allow_headers=["*"]`，未设 `allow_credentials`。生产走 nginx 同源反代，不受影响；**公网直连 API 会 CORS 失败**。

---

## 9. 前端行为要点

- **App 状态机**（`web/src/App.tsx`）：`eventsById` 只增量合并不清空，时间窗裁剪在前端做；`windowMin` 初值取 URL `?window=N`，否则 120；`types` 空集 = 全部。
- **刷新**：SSE（`App.tsx:92-95`，单条合并）+ **60 秒轮询**（`App.tsx:96-99`，同时刷新 events/pending/sources/unknown-places 与 `now`）。依赖 `[windowMin, types]`，任一变化会重建 SSE 连接与定时器。写操作后显式 `reloadAll()`。
- **交互**：时间窗 15/60/120/180 分钟；5 类类型筛选；待处理队列（展开项内做 250ms 防抖地名搜索、选地点、选类型、datetime-local 时间后 resolve；「忽略」用固定 reason `operator dismissed`；「补录地名」跳转地名库并预填）；事件详情可确认/拒绝（用 `window.prompt` 收理由）/拆分。
- **地图**：MapLibre，中心 `[31.6, 48.7]`、zoom 4.9、zoom 范围 4–11；默认底图 `esri-dark`（无需 key）；事件为 GeoJSON `circle` 层，半径按 precision（region 14 / district 10 / 其它 6）；中文标签用 DOM Marker（符号层需 glyph 服务且不含中文字形）。
- **地名库面板**：两个 tab（待补地名 / 检索编辑）；表单字段 = 乌克兰语原名、中文名、别名（逗号分隔）、所属州、纬度、经度、级别（region/district/city/town/village/settlement）；可保存条件 = 原名非空且坐标有限；支持「地图拾取」。
- **发布导出视图**：判定为「URL 存在 `export` 参数」（**不校验取值**，`?export=0` 也算）；该模式只渲染地图 + 左下角 `#export-tag`，供发布器截图定位。
- ⚠️ **`demoMode` 首屏误标**：初值 `true`（`App.tsx:36`），唯一翻转点 `App.tsx:80` 要等 `/api/v1/sources` 返回后才知道有没有 telegram 源。所以在真实采集环境下，首屏会短暂显示「本地样本数据 · 非真实事件」角标（实测抓到过）。修复方向：初值改为 `null`（未知）并延后渲染角标。
- ⚠️ **WebGL 不可用时整页全白**：`MapView` 没有 try/catch、没有 error boundary、没有降级 UI。实测在无 WebGL 的环境里 MapLibre 在 `Map._setupPainter` 抛异常 → React 整树卸载 → `#root` 变空、页面全白、无任何提示。修复方向：给地图加 error boundary + 降级到 `basemap=none` 或提示文案。

---

## 10. 部署（`scripts/deploy.sh`，97 行）

```sh
./scripts/deploy.sh                    # 同步 + 远端 docker compose up -d --build
./scripts/deploy.sh --dry-run          # 只打印 rsync 计划（注意：会在远端 mkdir，见下）
./scripts/deploy.sh --sync-only        # 只同步
./scripts/deploy.sh --with-credentials # 额外同步 backend/.env 与 telegram.session
```

- 环境变量：`REMOTE`（默认 `ubuntu-remote` = 69.5.20.240）、`REMOTE_DIR`（默认 `~/telegram-alarm` = `/home/terry/telegram-alarm`）。
- rsync：`-az --delete --human-readable --stats`。**排除项**：`.git/ .venv/ node_modules/ .npm-cache/ __pycache__/ *.pyc .pytest_cache/ .pip-cache/ .tools/ web/dist/ backend/data/app.db* backend/data/boot-test.db backend/data/publish/ backend/data/telegram.session* backend/data/.login-* backend/.env .cat-cafe/ .claude/ .codex/ .gemini/ .kimi/`。（地名库 JSON ≈10MB **不排除**，镜像构建需要它；**未排除 `.DS_Store`**。）
- `--with-credentials` 会先 `rm -f` 远端 session 再拷贝（容器内以 root 运行会把 session 改写成 root 属主，直接 rsync 覆盖会 permission denied），拷后 `chmod 600`；结尾警告**同一份 session 不要本机与服务器同时跑**，否则可能互相踢下线。
- 部署凭据默认**永不上服务器**：`.env` 与 session 在主 rsync 里被永久排除，且受 `--delete` 保护——普通部署不会冲掉服务器上已同步的凭据（已实测验证）。

### 已实测证据（2026-09-20/21 首次部署 + 2026-10-16 复查）

- 三容器 healthy；`curl 127.0.0.1:8000/api/v1/healthz` → `{"status":"ok","places":35627,...}`（地名库 35627 条加载成功）。
- `docker compose logs api` 里 5 条迁移全部成功（含升级路径 `b07f49e510c4 -> d4e5f6a7b8c9`）。
- **全新安装路径**已实测：`docker compose down -v` + `up -d` 从空卷跑完全部 5 个迁移，再回放样本 → `ok=13, duplicate=0, fail=0`，healthz `events=9, pending=1`。
- 经 nginx（5173）取 `/api/v1/events` 返回中文地名 + 坐标正常；`/api/v1/stream` 返回 `event: event` + 完整 payload。
- `ss -ltn` 确认远端只有 `127.0.0.1:8000` 与 `127.0.0.1:5173` 两条 LISTEN；公网 `69.5.20.240:5173` 不可达（HTTP 000）。
- 本机 → 远端隧道实测 `curl http://127.0.0.1:15173/` = HTTP 200，`<title>战况监控</title>`。

### SSH

`~/.ssh/config` 已有：

```
Host ubuntu-remote
  HostName 69.5.20.240
  User terry
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

远端 `terry` 在 `sudo` 与 `docker` 组内（docker 组等价 root 权限，已确认）。远端**还有别人的服务在跑**（`dpanel/dpanel:lite` 占 8807、`henrygd/beszel-agent`，另有 nginx-proxy-manager、new-api、warproxy 等镜像）——**不得触碰**。

⚠️ SSH 间歇性不稳定：多次出现 `Connection timed out during banner exchange`（TCP 22 可连但握手挂住），同时主机负载仅 0.17、8807 端口正常 → 属网络/防火墙层面的间歇问题，不是主机或应用问题。长任务用重试循环。

---

## 11. 运维

### 11.1 常用命令

```sh
ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose ps'
ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose logs api --tail 100'
ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose restart api'
curl -s http://127.0.0.1:18000/api/v1/healthz          # 经隧道
```

### 11.2 回放固定样本

```sh
ssh ubuntu-remote 'cd ~/telegram-alarm && python3 backend/scripts/replay_fixtures.py --api http://127.0.0.1:8000'
```

脚本只依赖标准库；默认把基准时间 `2026-09-07T12:00:00Z` 平移到当前（`--keep-base` 关闭）；按响应归类 ok/duplicate/fail，有 fail 则退出码 1。**幂等**：重复投递返回 `duplicate`。

### 11.3 发布器（当前未启用）

- 只有 `PUBLISH_OUT_DIR` 非空才启动；渠道优先级 TG Bot > Discord webhook > 落文件。
- 渲染用 headless Chrome（`publisher.py:50` 硬编码 macOS 路径 `/Applications/Google Chrome.app/…`，**无环境变量可覆盖**），通过 CDP 等 `#export-tag` 匹配 `事件 [1-9]\d*` + 校验 `.map canvas`，再等 4s 截图，png < 1000 字节判失败。
- ⚠️ **容器内没有 Chrome**，且镜像里有 `websockets` 但没有浏览器；默认渲染 URL 指向 `127.0.0.1:5173`（容器内是 api 容器自己，不是 web 容器）。要启用必须：装 Chrome/Chromium 到 api 镜像、让 `Renderer` 的路径可配置、把渲染 URL 改成 `http://web:80/?export=1`。
- 本机 `backend/data/publish/` 有 113 组历史产物（`work/*.png` + `published/*.png|.json`），时间跨度 2026-09-06 → 2026-09-11，是早期本机验证留下的。
- 相关回归：`backend/tests/test_publisher.py`（9 项，其中 1 项需本机 Chrome，缺失则 skip）。

### 11.4 备份

`backend/scripts/backup.sh`：Postgres 分支经 docker compose 的 db 容器 `pg_dump`，sqlite 分支用 `.backup`，并复制 `telegram.session`；`KEEP_DAYS` 默认 14。⚠️ 它会按 `-mtime +KEEP_DAYS` **删除**旧 `smon-*` 备份，有破坏性。

### 11.5 常见问题（详见 `docs/OPERATIONS.md` 第五节）

| 症状 | 首查 |
| --- | --- |
| 容器 healthy 但没有新事件 | DB `max(received_at)` 新鲜度（§7），API healthy 不代表采集活着 |
| **容器日志里完全没有应用日志** | 已修：`backend/alembic/env.py` 的 `fileConfig()` 默认 `disable_existing_loggers=True`，会在 Postgres 启动迁移时禁用 `smon.*` 全部日志器。现在显式传 `disable_existing_loggers=False`；回归守卫 `backend/tests/test_alembic_logging.py`。SQLite 路径不在启动时跑 alembic，所以本机与全部测试都发现不了 |
| `Postgres 上 ingest 报 500: ForeignKeyViolation` | 已修：迁移 5 移除三个 place 外键（§4） |
| 页面全白 | WebGL 不可用（§9），当前无兜底 |
| `rsync: unrecognized option '--info=stats1'` | macOS 自带 rsync 不支持，用 `--stats` |

---

## 12. 测试与质量现状

`cd backend && .venv/bin/python -m pytest -q` → **105 passed**（本机 python3.9 + `.venv`，约 40–70s）。

| 文件 | 测试数 | 覆盖 |
| --- | --- | --- |
| `tests/test_pipeline.py` | 25 | 消息→候选→校验→合并→事件→查询全链路 |
| `tests/test_extractor_real.py` | 21 | 真实地名库端到端提取（词形、地理编码成功/失败/歧义、警报生命周期） |
| `tests/test_admin_places.py` | 12 | 地名增改、中文/别名检索、改地名后坐标同步、重匹配、写接口仅本机 |
| `tests/test_gazetteer_uk.py` | 10 | 真实库规模（≥35000）、词形还原、种子碰撞、上下文消歧 |
| `tests/test_adapter.py` | 9 | Telegram 事件语义、幂等、游标、FloodWait 退避 |
| `tests/test_publisher.py` | 9 | 限流、租约/重试、渲染与发送解耦、三种发送器 |
| `tests/test_runner.py` | 7 | FakeProvider 注入事件/故障、检查点、缺口恢复、重连 |
| `tests/test_wave_corroboration.py` | 6 | 波次印证半径/时间窗/同类型/异来源 |
| `tests/test_runner_env.py` | 4 | 环境变量 → Runner 装配 |
| `tests/test_schema_place_fk.py` | 1 | 回归：三个 place_id 列不得有外键 |
| `tests/test_alembic_logging.py` | 1 | 回归：alembic `fileConfig` 不得禁用 `smon.*` |

**测试边界（重要）**：
- 全套件**离线**：不连真实 Postgres、不连 Telegram/外网；仅 1 个用例需本机 Chrome（缺失则 skip）。
- `backend/tests/conftest.py:22` 用内存 SQLite **绕过了 `app.db.make_engine`**，因此没有 WAL / `busy_timeout`。
- **外键约束在测试里从不开启**（仓库内无 `PRAGMA foreign_keys=ON`）——这正是迁移 5 那个 Postgres-only 缺陷能长期隐藏的原因。**任何新的 FK / Postgres 特有行为都必须在真实 Postgres 上验证。**
- 前端**没有任何自动化测试**。

---

## 13. 已知问题与风险（按优先级）

### 13.1 🔴 Git 零提交（头号风险）

`git rev-parse --show-toplevel` → **`/Users/terry/Desktop/coding`**，也就是**整个 `coding` 目录是仓库根**，项目只是其中一个子目录；`branch 'main' does not have any commits yet` —— **项目至今没有任何一次提交**，全部工作只存在于工作区文件里，没有可回溯的历史、没有可回滚的版本。

建议（需用户授权，未经许可不要擅自提交）：把仓库根收敛到项目目录（或加 `.gitignore` 排除兄弟项目），先做一次初始提交。

### 13.2 🔴 人工复核队列积压

实测 `candidates` 状态分布：**`needs_review` 12102 / `accepted` 9761 / `ignored` 53**。`needs_review ≈ raw_messages`，意味着几乎每一条原始消息都留下一条永不闭合的待复核候选，`/api/v1/pending` 会返回 11000+ 条。

这是**产品/工程问题，不是 bug**：`_process_message` 在「无事件类型」或「地名未解析」时都会建 `needs_review` 候选，而两个频道里大量消息本就不是战况事件。可选方向：给候选加噪音过滤/自动闭合策略、给 pending 接口加分页与筛选、或在 UI 上区分「待补地名」与「待确认事件」。

### 13.3 🟡 地名库数据质量问题

- 同名条目重复（实测页面上「赫雷宾卡」出现两次，指向不同条目）。
- 部分地名缺中文名（实测 `Іллінівка` 显示为原文）。
- `unknown_places` 有 193 条全为 `open`，从未被处理过。
- 中文映射只有 185 条精选 + 3954 条 Wikidata，覆盖远小于 35627 条目录。

### 13.4 🟡 前端两个真实缺陷

- `demoMode` 首屏误标（§9）。
- WebGL 不可用时整页全白（§9）。

### 13.5 🟡 文档过期

`README.md` 第 5、20、22、30、42 行与目录清单已过期（本次已修正，见 §15）。`docs/HANDOFF.md` 是 2026-09 阶段文档，历史价值保留但不再代表现状。

### 13.6 🟢 其它

- `sources.gap_notes` 列无写入点，`/sources`、`/stats` 恒返回空字符串。
- `models.py:107` 的 `verification` 注释漏了 `auto_confirmed`。
- `service.py:390` 对 `rejected` 的判断实际不可达（已被 `find_merge_target` 过滤）——是否属有意防御待确认。
- 后端容器**以 root 运行**（`backend/Dockerfile` 无 `USER`）。
- POST `/api/v1/ingest` **是开放写接口**（无鉴权）；目前只绑回环所以风险可控，一旦对外必须加鉴权。
- 前端单 chunk 973KB，无代码分割。
- `web/Dockerfile` 只 `COPY package.json`、不拷 `package-lock.json` → 构建非确定性。
- `scripts/deploy.sh --dry-run` 仍会在远端执行 `mkdir -p`（首次曾在远端留下空目录，无害）。
- 本机 `backend/data/app.db` 有 1515 条 sqlite 事件；**没有 sqlite → Postgres 的导入脚本**，本机历史数据不会自动进远端。

---

## 14. 待办与建议的下一步

按性价比排序：

1. **建立版本控制**（先做，风险最高、成本最低）——收敛 git 仓库根，加 `.gitignore`，做初始提交。
2. **给 API 加最基础的鉴权**，或明确记录「仅回环、永久不对外」的运维约束。
3. **处理 `needs_review` 积压**：给 `/pending` 加分页/筛选，并设计噪音候选的自动闭合策略。
4. **前端两处修复**：`demoMode` 初值改为未知态；给地图加 error boundary + 降级提示。
5. **补 `sources.gap_notes` 的写入**（或删掉这个从未被写入的列，避免误读）。
6. **补 `auto_confirmed` 到模型注释与前端文案**，统一核验状态枚举。
7. **若要用发布器**：给 api 镜像装 Chromium、把 `Renderer` 的 Chrome 路径与 `PUBLISH_RENDER_URL` 改成可配置，指向 `http://web:80/?export=1`，然后设 `PUBLISH_OUT_DIR`。
8. **考虑给前端加最小自动化测试**（当前为 0）。
9. **数据质量**：去重同名地名、用已有脚本继续补中文名、清理 193 条 `unknown_places`。
10. `web/vite.config.ts` 加 `manualChunks` 拆出 maplibre，消除 973KB 单 chunk。

---

## 15. 本次交接同步修正的内容

- 新增本文 `docs/HANDOFF-2026-10.md`。
- 修正 `README.md` 中过期的事实（阶段描述、测试数、部署状态、demoMode 说明、目录清单补 `docs/OPERATIONS.md` 与 `scripts/deploy.sh`）。
- 新增 `docs/OPERATIONS.md` 中的 Postgres 外键 500 与「容器日志完全静默」两条排查记录，以及远端实测结论（此前已在部署轮次完成）。

---

## 16. 环境与凭据备忘

- 本机：macOS，Python 3.9.6，`backend/.venv`（sqlalchemy 2.0.x），**无 Docker**；Chrome 155 在 `/Applications/Google Chrome.app/`；Node 24（自带全局 `WebSocket`/`fetch`，可写 CDP 脚本）；rsync 为 macOS 自带（不支持 `--info=stats1`）；**没有 `timeout` 命令**。
- 远端：Ubuntu 24.04.4 LTS，kernel 6.8，x86_64，4 vCPU / 15Gi RAM / 227G 盘（用 26G）；Docker 29.7.2 + Compose v5.5.0；宿主时区 UTC+8。
- 凭据（**敏感，不要写进任何文档或提交**）：`backend/.env` 含 `TELEGRAM_API_ID/API_HASH/PHONE/CHATS/SESSION_PATH`；`backend/data/telegram.session` 是账号登录态；`.tools/tg-session-backup-20260921-024642.session` 是它的备份。远端已同步一份（仅 `--with-credentials` 时更新）。
- ⚠️ **同一份 Telegram session 不要在本机与服务器同时运行**，会互相踢下线。

---

## 17. 纪律与约束（沿用项目规则）

- 不用端口 **3003（前端）/ 3004（API）**：属同机 Clowder AI 保留。
- 不用 Redis **6399**（被占），需要时用 **6398**。
- **未经授权不公开部署、不做 git 提交。**
- 「Done 需要证据」：任何完成声明都要附可复现的命令与输出。
- 远端宿主上还有**他人服务**（dpanel、beszel-agent 等），一律不触碰。
