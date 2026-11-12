#!/bin/bash
# 定位 Playwright 镜像内的 Chromium 并以 CDP 服务模式常驻
set -e

CHROME=$(ls -d /ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
if [ -z "$CHROME" ]; then
  CHROME=$(ls -d /ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | head -1)
fi
if [ -z "$CHROME" ]; then
  echo "未找到 Chromium 可执行文件" >&2
  exit 1
fi

echo "使用浏览器: $CHROME"
exec "$CHROME" \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  about:blank
