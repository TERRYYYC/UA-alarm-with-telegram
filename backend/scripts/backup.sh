#!/bin/bash
# 备份脚本:Postgres dump(生产)或 sqlite 备份(开发)+ Telegram session 文件。
#
# 用法:
#   ./scripts/backup.sh [备份目录]     # 默认 ../data/backups
#
# 恢复(Postgres,示例):
#   gunzip -c backups/smon-YYYYmmdd-HHMMSS.sql.gz | docker compose exec -T db psql -U smon -d smon
# 恢复(sqlite):
#   停止服务后,把 backups/smon-*.db 覆盖回 data/app.db
#
# 注意:session 文件与凭据同等级敏感,生产环境应放到权限受控的存储/加密卷;
# 本脚本只负责收集,权限设置 chmod 600。
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${1:-data/backups}"
KEEP="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

DB_URL="${DATABASE_URL:-sqlite:///./data/app.db}"

if [[ "$DB_URL" == postgresql* ]]; then
  # 生产:走 docker compose 里的 db 容器 pg_dump
  if docker compose version >/dev/null 2>&1; then
    docker compose exec -T db pg_dump -U "${POSTGRES_USER:-smon}" "${POSTGRES_DB:-smon}" \
      | gzip > "$BACKUP_DIR/smon-$STAMP.sql.gz"
  else
    echo "本机无 docker,跳过 Postgres dump(需在部署环境运行)" >&2
  fi
else
  # 开发:sqlite 在线备份
  DB_FILE="${DB_URL#sqlite:///}"
  if [[ -f "$DB_FILE" ]]; then
    sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/smon-$STAMP.db'"
    echo "sqlite 备份: $BACKUP_DIR/smon-$STAMP.db"
  else
    echo "未找到 sqlite 文件: $DB_FILE(跳过)" >&2
  fi
fi

# Telegram session 与配置(如存在)
if [[ -f data/telegram.session ]]; then
  mkdir -p "$BACKUP_DIR/sessions-$STAMP"
  cp data/telegram.session "$BACKUP_DIR/sessions-$STAMP/"
  chmod 600 "$BACKUP_DIR/sessions-$STAMP/telegram.session"
  echo "session 已备份"
fi

# 清理保留期之外的备份
find "$BACKUP_DIR" -name 'smon-*' -mtime "+$KEEP" -delete 2>/dev/null || true
echo "备份完成(保留 $KEEP 天): $BACKUP_DIR"
