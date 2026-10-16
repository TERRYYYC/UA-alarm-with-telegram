# Telegram 战况消息地图可视化

面向乌克兰战争场景:监听多个 Telegram 群消息,提取地点与事件类型,渲染到实时暗色地图,并定时截图转发到 Telegram / Discord。

> 当前阶段：**已在生产运行**。三容器跑在 `69.5.20.240` 上，真实 Telegram 采集（2 个频道）已连续工作约 4 周，实时地图、SSE、待复核队列与地名库维护均可用；端口只绑回环，靠 SSH 隧道访问。发布器（自动截图转发）尚未启用。本机测试 105 项全部通过。
>
> ⚠️ 项目目录**尚无任何 git 提交**，这是当前头号工程风险。

## 接手入口

先读 [项目完整交接（2026-10）](docs/HANDOFF-2026-10.md)——这是当前状态的交接主文档；再读 [运维手册](docs/OPERATIONS.md)。[设计收敛与工程交接](docs/HANDOFF.md)与[评审结论](docs/REVIEW-AND-START.md)是 2026-09 阶段的历史资料，保留供追溯。视觉基线见 [深色高保真原型](prototype/hifi/situation-monitor.html)。发生冲突时，以 `docs/HANDOFF-2026-10.md` 和用户最新要求为准。

## 目录

- `docs/HANDOFF-2026-10.md` — 项目完整交接(当前状态、架构、数据模型、部署、风险与待办)
- `docs/OPERATIONS.md` — 运维手册(启停、日志、回放、地名维护、常见问题表、远端部署)
- `docs/DESIGN.md` — 设计文档(架构、数据模型、关键决策、决议记录、路线图)
- `docs/HANDOFF.md` — 设计收敛与工程交接(2026-09 阶段,历史资料)
- `docs/REVIEW-AND-START.md` — HANDOFF 评审结论、工作假设、验证证据
- `docs/RESEARCH-OPENSOURCE.md` — 开源生态调研报告(2026-09-06)
- `docs/PLUGIN-ARCHITECTURE-PROPOSAL.md` — 插件化架构提案(未采纳,历史资料)
- `prototype/hifi/situation-monitor.html` — 深墨色 UI 视觉基线原型(浏览器直接打开)
- `prototype/map-mockup.html` — 早期暗色地图草图(旧,仅供追溯)
- `fixtures/sample_messages.jsonl` — 脱敏固定样本(8 类场景)+ 预期结果说明
- `backend/` — FastAPI 事件管线(采集入口/提取/合并/查询/SSE),105 项测试
- `web/` — React + TypeScript + MapLibre 实时地图界面(深墨色视觉)
- `docker-compose.yml` — PostGIS + API + Web 三容器编排(端口默认只绑 127.0.0.1)
- `scripts/deploy.sh` — 一键部署到远端 `ubuntu-remote`(rsync + `docker compose up -d --build`)

## 本地运行

```sh
# 后端(端口 8000)
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests -q                      # 105 项测试,全部离线
.venv/bin/uvicorn app.main:app --port 8000

# 灌入演示样本(时间平移到当前时刻;样本是合成数据)
.venv/bin/python scripts/replay_fixtures.py

# 前端(端口 5173,代理 /api 到 8000)
cd ../web
npm install --cache .npm-cache
npm run dev
```

打开 http://127.0.0.1:5173/ 。样本回放阶段界面显示"本地样本数据 · 非真实事件"角标；一旦 `/api/v1/sources` 里存在 `adapter==='telegram'` 的来源，角标会切为实时采集（首屏短暂误标的问题见交接文档 §9）。

## 部署到远端

```sh
./scripts/deploy.sh                  # 同步到 ubuntu-remote:~/telegram-alarm 并 docker compose up -d --build
./scripts/deploy.sh --dry-run        # 只看 rsync 计划
./scripts/deploy.sh --with-credentials  # 额外同步 backend/.env 与 telegram.session(凭据级敏感)
```

服务只绑 `127.0.0.1`，用 SSH 隧道访问：

```sh
ssh -N -L 15173:127.0.0.1:5173 -L 18000:127.0.0.1:8000 ubuntu-remote
# 浏览器打开 http://127.0.0.1:15173
```

## 技术栈

Python 3.12(Docker)/3.9(本机兼容)+ FastAPI + SQLAlchemy + PostgreSQL/PostGIS + React + TypeScript + MapLibre GL(默认 Esri 暗色底图,可切 CARTO / 无底图)。采集用 Telethon,提取用规则 + pymorphy3 乌克兰语词形还原,模糊匹配用 rapidfuzz,地理编码用 Nominatim。v1 未引入 Redis/LLM;实时推送用 sse-starlette。
