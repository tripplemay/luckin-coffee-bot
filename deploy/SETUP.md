# 部署到自有 VPS（多服务 + CI/CD）

生产上有 4 个常驻 systemd 服务，全部本机长服务，无需公网入口（仅登录页需经 nginx+域名对外）：

| 服务 | 作用 | 监听 |
|---|---|---|
| `coffee-bot` | Telegram 机器人（长轮询） | — |
| `coffee-service` | 渠道无关下单服务（FastAPI），微信桥接调它 | 127.0.0.1:8100 |
| `coffee-wechat` | 微信桥接（Node, wx-link/腾讯 iLink） | — |
| `coffee-web` | 手机号登录页（FastAPI），经 nginx+域名对外 | 127.0.0.1:8200 |

需要 Python 3.10+、Node 18+。

## 一、一次性配置

```bash
# 1. 拉代码
sudo mkdir -p /opt/coffee-bot && sudo chown "$USER" /opt/coffee-bot
git clone https://github.com/<账号>/luckin-coffee-bot /opt/coffee-bot
cd /opt/coffee-bot

# 2. Python venv + 依赖
python3 -m venv .venv && .venv/bin/pip install -e .

# 3. 配置 .env（绝不入库）
cp .env.example .env
#   填 BOT_TOKEN、AIGC_API_KEY、AMAP_KEY；生成 FERNET_KEY：
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   登录页域名：PUBLIC_BASE_URL=https://你的域名（见第三节）；微信桥接可选 BRIDGE_SECRET

# 4. 微信桥接依赖
cd wechat && npm install && cd ..

# 5. 安装 systemd 服务（按需改 User / 路径）
sudo cp deploy/coffee-bot.service deploy/coffee-service.service \
        deploy/coffee-wechat.service deploy/coffee-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coffee-bot coffee-service coffee-web
#   coffee-wechat 需先一次性扫码登录，见下
```

让部署用户能免密重启服务（供 CI 自动部署）：
```bash
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart coffee-bot coffee-service coffee-wechat coffee-web, /bin/systemctl try-restart coffee-bot coffee-service coffee-wechat coffee-web" | sudo tee /etc/sudoers.d/coffee-bot
```

## 二、微信渠道一次性登录
微信桥接首次需用**专用/小号**扫码登录（之后会话存盘、无人值守）：
```bash
bash /root/coffee-wechat-login.sh   # 或: cd wechat && SERVICE_URL=http://127.0.0.1:8100 node bridge.mjs
# 扫码 → 看到「开始轮询微信消息」→ Ctrl+C → systemctl enable --now coffee-wechat
```

## 三、手机号登录页（域名 + HTTPS）
让 `/login` 给出固定登录链接。需要一个解析到本机的子域名（如 `coffee.example.com → <VPS_IP>`），用 nginx 反代 `coffee-web` + Let's Encrypt：
```bash
# nginx 反代 vhost
sudo tee /etc/nginx/sites-available/coffee.example.com >/dev/null <<'NG'
server {
    listen 80;
    server_name coffee.example.com;
    location / {
        proxy_pass http://127.0.0.1:8200;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NG
sudo ln -sf /etc/nginx/sites-available/coffee.example.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d coffee.example.com --redirect   # 自动签证书+开 HTTPS+自动续期
```
然后把 `.env` 的 `PUBLIC_BASE_URL=https://coffee.example.com`，重启 `coffee-bot coffee-service`。
之后 bot 里发 `/login`（不带 token）就给出 `https://coffee.example.com/login?t=<nonce>`，用户填手机号+短信即可登录，免粘贴 Token。

> 备选（无域名）：`deploy/coffee-tunnel.service` + `deploy/tunnel.sh` 用 cloudflared 临时隧道把 URL 写进 `web/.public_url`（`login_base_url()` 会优先读它）。URL 重启会变，仅适合临时/测试。

## 四、GitHub Actions 自动部署
仓库 → Settings → Secrets → Actions 配 `VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY`/`VPS_PATH`(`/opt/coffee-bot`)/`VPS_PORT`。
push 到 `main` 后 `deploy.yml` 自动 SSH 部署并重启 `coffee-bot` 及 try-restart `coffee-service`/`coffee-wechat`/`coffee-web`。

## 五、运维
```bash
journalctl -u coffee-bot -f          # 或 coffee-service / coffee-wechat / coffee-web
systemctl restart coffee-service     # 手动重启
bash deploy/deploy.sh                # 手动拉最新并重启
```
注意：`coffee.db`（加密 token / 订单 / 位置）存本机，不入库；换机/重装需重新登录。
