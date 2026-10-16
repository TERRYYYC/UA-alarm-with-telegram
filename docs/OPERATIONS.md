---
feature_ids: []
topics: [operations, runbook, telegram, deployment]
doc_kind: runbook
created: 2026-09-07
---

# 运维手册(本地开发与单机部署)

> 面向运营者与后续 agent。命令均以 `backend/` 为工作目录,虚拟环境为 `.venv`。

## 一、首次 Telegram 登录(由运营者本人完成验证码输入)

```sh
cd backend
cp .env.example .env            # 填入 TELEGRAM_API_ID / TELEGRAM_API_HASH
# 写入手机号后执行:
.venv/bin/python scripts/telegram_login.py send      # 发送验证码
# 把验证码写入临时文件(脚本读取后自动删除):
echo "12345" > data/.login-code.txt
.venv/bin/python scripts/telegram_login.py code      # 完成登录(两步验证则用 password 阶段)
.venv/bin/python scripts/telegram_login.py status    # 检查登录状态并列出可见群/频道
```

- session 文件:`data/telegram.session`(已 gitignore,权限敏感,勿提交/分享)。
- 在 `.env` 里填 `TELEGRAM_CHATS`:`chat_id:显示名;...`(登录脚本会列出账号可见群的 ID)。

## 二、启动服务

```sh
# API(端口 8000;检测到 TELEGRAM_API_ID/HASH 时自动启动采集 Runner)
cd backend && .venv/bin/uvicorn app.main:app --port 8000

# 前端(端口 5173,代理 /api)
cd web && npm run dev -- --host 127.0.0.1

# 可选:发布器(事件触发 + 最小间隔;渲染 ?export=1 视图 → 本地目录)
PUBLISH_OUT_DIR=data/publish PUBLISH_MIN_INTERVAL_S=300 .venv/bin/uvicorn app.main:app --port 8000
```

## 三、测试与数据

```sh
.venv/bin/pytest tests -q                          # 全量测试(94 项)
.venv/bin/python scripts/replay_fixtures.py        # 重放合成样本(时间平移到当前)
.venv/bin/python scripts/build_gazetteer.py        # 重建真实地名库(GeoNames + KATOTTH)
.venv/bin/python scripts/scan_unknown_places.py    # 扫描历史报文,回填"待补地名"列表
./scripts/backup.sh                                # 备份(Postgres dump / sqlite + session)
```

## 四、核验与地名维护(运营日常)

事件默认是"未核实",但**不需要人工逐条确认**——系统按多源交叉印证自动升级:

| 状态 | 含义 | 触发方式 |
|---|---|---|
| `unverified` | 仅单一来源、单一报道 | 默认 |
| `auto_confirmed` | 自动确认(多源印证) | ≥ `AUTO_CONFIRM_MIN_SOURCES` 个独立来源,或 ≥ `AUTO_CONFIRM_MIN_REPORTS` 条报道(按"来源 + 消息 id"去重,编辑修订不重复计数) |
| `auto_confirmed`(波次) | 自动确认(同一波攻击) | 另一来源在 `WAVE_WINDOW_MIN` 分钟、`WAVE_RADIUS_KM` 公里内报了同类型事件 → 双方互相印证,不合并点位 |
| `confirmed` / `rejected` | 人工判定 | 运营在详情面板点"确认/拒绝",自动规则不再覆盖 |

环境变量:`AUTO_CONFIRM_MIN_SOURCES`(默认 2)、`AUTO_CONFIRM_MIN_REPORTS`(默认 3)、`WAVE_CORROBORATION`(默认开)、`WAVE_RADIUS_KM`(默认 50)、`WAVE_WINDOW_MIN`(默认 60)。
改完阈值后跑一次 `POST /api/v1/admin/reverify`(或让 DSH 调 `alarm_maintenance`)即可对历史事件重算核验。
人工只在两种情况下介入:① 明显误报 → 详情面板"拒绝";② 待处理队列里出现系统无法自行消解的歧义。

地名维护闭环(界面上"地名库"按钮):

1. **待补地名**页签:列出报文中出现、但地名库未收录的专名(按出现次数排序,带原文片段)。
   只收"像地名"的上下文(介词短语、行政词搭配、标题式行首),普通大写词不会刷屏。
2. 点"补录" → 表单预填原名 → 在**地图上点击**该地位置(或直接填经纬度)、补中文名与别名(变格形式)、选级别 → 保存。
   也可以从"待处理"队列里点"补录地名"直接跳过来。
3. 点顶部"**重新匹配**":把能解析的待补地名标为已解决、回填历史报文、重试待处理候选、重跑交叉印证。
   返回的计数会直接显示在面板上(补齐 N 个 / 回填 M 条 / 自动确认 K 条)。
4. 检索/编辑页签:按中文名、乌克兰语原名、别名或州检索;点结果进入编辑,改坐标后地图上的历史事件会立即跟着移动。

## 五、常见问题

| 现象 | 处理 |
|---|---|
| Runner 日志出现 floodwait | 自动指数退避(封顶 300s);连续限流说明账号行为可疑,暂停采集并观察 |
| 断线后数据有缺口 | 重连后自动按游标补采;`/api/v1/sources` 的 gap_notes 字段记录不可确认缺口 |
| 重新登录(换号/换机) | 删除 `data/telegram.session` 后重走第一节流程 |
| 端口占用(3003/3004/6399 为 Clowder 保留) | 只使用 5173/8000;若需 Redis 仅用 6398 |
| 地图无标记 | 先查 `/api/v1/events` 与浏览器控制台;发布截图依赖 `?export=1` 标签出现"事件 N" |
| 报道数虚高 / 单频道被"多源印证" | 同一消息的编辑修订曾被重复计数;已修复。历史数据用 `.venv/bin/python scripts/repair_report_counts.py --apply` 重算 |
| 发布图有 "API KEY REQUIRED" 水印 | CARTO 公共瓦片已开始要求 Key:用 `VITE_BASEMAP=esri-dark`(默认)或 `?basemap=none` 离线纯色底图 |
| 发布图是"无法访问此网站" | 渲染兜底已拦截(不会发布错误页),任务记为 failed 并重试;先确认前端 5173 在跑 |
| Postgres 上 ingest 报 500:`ForeignKeyViolation: events_place_id_fkey` | 地名目录主存是 JSON 文件(启动载入内存),`gazetteer_places` 表只是运行时/管理端条目的叠加层,因此 `events/candidates.place_id`、`unknown_places.resolved_place_id` 不应有外键。SQLite 默认不强制外键所以本地看不出来。已由迁移 `d4e5f6a7b8c9_drop_place_fk.py` 移除;回退到旧迁移会复现 |
| Postgres 部署下容器日志里**完全没有应用日志**(看不到采集器启动/掉线、发布器 tick) | `backend/alembic/env.py` 调用 `fileConfig()` 时默认 `disable_existing_loggers=True`,会把 `alembic.ini` 未声明的 `smon.*` 日志器全部禁用;而 Postgres 路径每次启动 lifespan 都先跑迁移,于是应用日志整体静默(SQLite 路径不跑 alembic,所以本地/测试看不出来)。已显式传 `disable_existing_loggers=False`,回归测试 `backend/tests/test_alembic_logging.py` |

## 六、安全与合规纪律

- 采集仅限运营者提供的群清单;采集账号与发布 bot 分离。
- 消息原文属于不可信输入:前端只做纯文本渲染,任何升级(富文本/模型直通)前必须先过转义/沙箱评审。
- 未获明确授权:不对外发布真实数据、不公开部署、不做 git 提交。
- 发布数据前核查许可:GeoNames CC-BY-4.0(署名)、KATOTTH/VIINA 派生数据 ODbL(共享义务)、Esri/CARTO 底图 © OSM。

## 七、生产部署(Docker,远端 ubuntu-remote)

目标机已在 `~/.ssh/config` 配置为 `ubuntu-remote`(`terry@69.5.20.240`),装好 Docker + Compose。**服务默认只绑 127.0.0.1**,通过 SSH 隧道访问,不对公网暴露。

当前状态(2026-09-21):三容器在 69.5.20.240 上运行,`db`/`api` healthy,**真实 Telegram 采集已启用**,消息持续写入 `raw_messages`/`events`。

```sh
# 本机执行:同步代码 + 地名库,远端构建并启动
./scripts/deploy.sh                # 加 --dry-run 可先预览同步内容

# 验证
ssh ubuntu-remote 'curl -s http://127.0.0.1:8000/api/v1/healthz'   # status/places/events
ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose ps'       # 三容器 healthy

# 本地开隧道后浏览器访问 http://127.0.0.1:5173
ssh -N -L 5173:127.0.0.1:5173 ubuntu-remote

# 灌演示样本(合成数据,时间平移到当前时刻)
ssh ubuntu-remote 'cd ~/telegram-alarm && python3 backend/scripts/replay_fixtures.py --api http://127.0.0.1:8000'
```

部署纪律与已知缺口:

- 已在 69.5.20.240 实测通过:`docker compose down -v` + `up -d` 从空卷跑完全部 5 个迁移,
  样本回放 13/13 成功、events=9(全新安装可用)。
- 凭据默认不上服务器:主 rsync 永久排除 `backend/.env` 与 `backend/data/telegram.session*`
  (排除项也受 `--delete` 保护,普通部署不会冲掉服务器上已同步的凭据);只有 `--with-credentials` 才显式同步一次。
- `backend/.dockerignore` 阻止 `.env`/session/本地 sqlite 进入镜像。
- **真实 Telegram 采集已可用**(2026-09-21 在 69.5.20.240 实测):`docker-compose.yml` 的 api 服务通过
  `env_file: ./backend/.env(required: false)` 注入 `TELEGRAM_*`,并把 `./backend/data` 挂到容器 `/app/data`,
  同时把 `TELEGRAM_SESSION_PATH` 钉为 `/app/data/telegram.session`。凭据缺失时 `required: false` 让服务照常启动,
  采集器写一行 "runner disabled" 后不工作。
  ```sh
  scripts/deploy.sh --with-credentials      # 同步 backend/.env + telegram.session 后重建 api
  # 验证采集器真的起来了:
  ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose logs api | grep smon.runner'
  #   → 2026-09-20 23:51:37,364 INFO [smon.runner] telethon client started (chats=2)
  ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose exec -T db psql -U smon -d smon \
      -c "select s.id, s.name, count(*), max(m.received_at) from raw_messages m join sources s on s.id=m.source_id group by 1,2;"'
  ```
  实测行为:连接成功后每个频道回填约 200 条历史(2 个频道共 390 条),随后转入实时;
  事件经 `/api/v1/stream`(SSE,经 nginx 5173)正常推送,未识别地名进入 `/api/v1/unknown-places`。
  注意 `events` 表**没有** `source_id` 列,要按频道统计请查 `raw_messages`(有 `source_id`)。
- 同一份 session 不要在本机和服务器上同时跑,否则可能互相踢下线;`deploy.sh` 的凭据同步会先
  `rm -f` 远端 session(容器内以 root 运行,Telethon 会改写属主,直接覆盖会 permission denied)。
- 本机 `backend/data/app.db` 的历史事件(1515 条)**不会**自动进入远端 Postgres(远端库现在装的是演示样本 + 服务器真实采集到的数据);无 sqlite→pg 导入脚本。
- `add_cors` 只允许 `localhost:5173`/`127.0.0.1:5173`(`backend/app/api.py:496`):走 nginx 同源代理不受影响,
  但"公网 IP 直连 API"会 CORS 失败。若要公网访问,用 `API_BIND=0.0.0.0 WEB_BIND=0.0.0.0 docker compose up -d`,并同步改 CORS。

