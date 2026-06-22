#!/usr/bin/env bash
# 在 VPS 上手动部署/更新（CI 的 deploy.yml 做的也是这几步）。
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch --all --quiet
git reset --hard origin/main
.venv/bin/pip install -q -e .
sudo systemctl restart coffee-bot
sudo systemctl try-restart coffee-service coffee-wechat coffee-web || true
echo "deployed $(git rev-parse --short HEAD)"
