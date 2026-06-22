#!/usr/bin/env bash
# 起 cloudflared 隧道指向登录页(127.0.0.1:8200)，并把当前公网 URL 写入 web/.public_url，
# 供 bot 生成手机号登录链接。URL 变化时(重启)自动更新文件。
set -uo pipefail
URLFILE=/opt/coffee-bot/web/.public_url
/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8200 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  if [ -n "${url:-}" ]; then printf '%s' "$url" > "$URLFILE"; echo "[tunnel] published $url"; fi
done
