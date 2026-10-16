#!/bin/bash
# 渲染 map-mockup.html 为 PNG(暗色地图原型截图)
# 用法: ./screenshot.sh [输出文件.png]
# 说明: 使用 ?export=1 关闭脉冲动画;headless Chrome 在本机截图后可能不退出,
#       故截图完成后强制回收进程。生产环境的截图 Bot 将用 Playwright 替代此脚本。
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-mockup-map.png}"
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
HTML_ABS="$(pwd)/map-mockup.html"
PROF="$(pwd)/.chrome-profile"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  echo "未找到 Google Chrome: $CHROME" >&2
  exit 1
fi

mkdir -p "$PROF"
"$CHROME" \
  --headless=new --no-first-run --no-default-browser-check --hide-scrollbars \
  --no-sandbox --use-angle=swiftshader --user-data-dir="$PROF" \
  --force-device-scale-factor=2 --window-size=1600,1000 --timeout=20000 \
  --screenshot="$OUT_ABS" "file://$HTML_ABS?export=1" >/dev/null 2>&1 &
CPID=$!

sleep 42
kill "$CPID" 2>/dev/null || true
pkill -f "Google Chrome.*headless" 2>/dev/null || true
rm -rf "$PROF"

echo "已生成: $OUT_ABS"
