#!/usr/bin/env bash
# 部署 telegram alarm 到远端 Docker 主机(默认 ubuntu-remote = 69.5.20.240)。
#
# 用法:
#   scripts/deploy.sh              # 同步代码(含地名库)并在远端 docker compose up -d --build
#   scripts/deploy.sh --dry-run    # 只显示 rsync 会做什么,不改远端
#   scripts/deploy.sh --sync-only  # 只同步,不构建/启动
#
# 默认不传 .env 与 telegram.session(凭据留本机)。需要真实采集时加 --with-credentials。
#
# 服务默认只绑远端 127.0.0.1,本地用 SSH 隧道访问:
#   ssh -N -L 5173:127.0.0.1:5173 ubuntu-remote   # 然后打开 http://127.0.0.1:5173
set -euo pipefail

REMOTE="${REMOTE:-ubuntu-remote}"
REMOTE_DIR="${REMOTE_DIR:-~/telegram-alarm}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
SYNC_ONLY=0
WITH_CREDENTIALS=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --sync-only) SYNC_ONLY=1 ;;
    --with-credentials) WITH_CREDENTIALS=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo "==> 远端:$REMOTE  目录:$REMOTE_DIR"
if [ "$DRY_RUN" != "1" ]; then
  ssh -o BatchMode=yes "$REMOTE" "mkdir -p $REMOTE_DIR"
fi

# 排除本地开发产物与凭据;地名库(.json,约 10MB)必须同步,镜像构建依赖它。
RSYNC_ARGS=(-az --delete --human-readable --stats
  --exclude '.git/'
  --exclude '.venv/' --exclude 'node_modules/' --exclude '.npm-cache/'
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.pip-cache/'
  --exclude '.tools/' --exclude 'web/dist/'
  --exclude 'backend/data/app.db*' --exclude 'backend/data/boot-test.db'
  --exclude 'backend/data/publish/'
  --exclude 'backend/data/telegram.session*' --exclude 'backend/data/.login-*'
  --exclude 'backend/.env'
  --exclude '.cat-cafe/' --exclude '.claude/' --exclude '.codex/' --exclude '.gemini/' --exclude '.kimi/'
)

if [ "$DRY_RUN" = "1" ]; then
  rsync "${RSYNC_ARGS[@]}" --dry-run ./ "$REMOTE:$REMOTE_DIR/"
  echo "==> --dry-run 结束,未改动远端。"
  exit 0
fi

echo "==> 同步代码 + 地名库"
rsync "${RSYNC_ARGS[@]}" ./ "$REMOTE:$REMOTE_DIR/"

if [ "$WITH_CREDENTIALS" = "1" ]; then
  echo "==> 同步 Telegram 凭据(仅本次显式要求时)"
  ssh "$REMOTE" "mkdir -p $REMOTE_DIR/backend/data && chmod 700 $REMOTE_DIR/backend/data"
  # 容器内以 root 运行,Telethon 会把 session 重写为 root 属主;直接 rsync 覆盖会 permission denied,
  # 所以先删掉(宿主目录属主是部署用户,删 root 文件没问题),再拷新的。
  ssh "$REMOTE" "rm -f $REMOTE_DIR/backend/data/telegram.session $REMOTE_DIR/backend/data/telegram.session-journal"
  rsync -az -e ssh backend/.env "$REMOTE:$REMOTE_DIR/backend/"
  rsync -az -e ssh backend/data/telegram.session "$REMOTE:$REMOTE_DIR/backend/data/" 2>/dev/null || \
    echo "    (没有 telegram.session,跳过)"
  ssh "$REMOTE" "chmod 600 $REMOTE_DIR/backend/.env $REMOTE_DIR/backend/data/telegram.session 2>/dev/null || true"
fi

if [ "$SYNC_ONLY" = "1" ]; then
  echo "==> --sync-only 结束。"
  exit 0
fi

echo "==> 远端构建并启动"
ssh "$REMOTE" "cd $REMOTE_DIR && docker compose up -d --build"

echo "==> 容器状态"
ssh "$REMOTE" "cd $REMOTE_DIR && docker compose ps"

cat <<'EOF'

==> 部署完成。验证:
  ssh ubuntu-remote 'curl -s http://127.0.0.1:8000/api/v1/healthz'      # API 存活 + 地名库条目数
  ssh ubuntu-remote 'cd ~/telegram-alarm && docker compose logs api | grep -i telethon'   # 采集器是否起来
  ssh -N -L 5173:127.0.0.1:5173 ubuntu-remote                          # 本地隧道
  # 浏览器打开 http://127.0.0.1:5173

灌入演示样本(时间平移到当前时刻;合成数据):
  ssh ubuntu-remote 'cd ~/telegram-alarm && python3 backend/scripts/replay_fixtures.py --api http://127.0.0.1:8000'

启用真实 Telegram 采集(把本机 backend/.env 与 telegram.session 同步上去):
  scripts/deploy.sh --with-credentials
  注意:同一份 session 不要在本机和服务器上同时跑,否则可能互相踢下线。
EOF
