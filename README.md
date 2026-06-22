# ☕ 瑞幸咖啡点单机器人 · Telegram + 微信

![ci](https://github.com/tripplemay/luckin-coffee-bot/actions/workflows/ci.yml/badge.svg)

用自然语言在 **Telegram** 或 **微信** 里点瑞幸咖啡。同一套 LLM agent 大脑编排瑞幸开放平台的
MCP 工具，完成「找店 → 选品 → 预览 → 确认 → 下单 → 支付 → 取餐」全流程。

- **交互**：纯 LLM Agent（aigc-gateway，OpenAI 兼容，默认 `deepseek-v3`）
- **两个渠道，共用下单大脑**：
  - **Telegram** —— 原生按钮、位置共享、inline 确认
  - **微信** —— 腾讯官方 ClawBot / iLink 个人号机器人（经 [`wx-link`](https://www.npmjs.com/package/wx-link)），文本指令交互
- **安全护栏**：`createOrder` 花真钱，**必须用户『确认』后才执行** + 单日消费上限 + token 加密存储（Fernet）

## 架构
```
        ┌─────────────────── 渠道无关下单大脑 ───────────────────┐
        │  OrderingAgent (LLM function-calling)  +  MCP 客户端    │
        │  + flows(预览/护栏/支付二维码/状态)  +  core(config/db) │
        └──────────▲──────────────────────────────────▲─────────┘
   Telegram 渠道   │                       微信渠道     │ HTTP /message
  bot/ (python-telegram-bot)          service/ (FastAPI) ◄── wechat/ (Node, wx-link/腾讯 iLink)
  原生按钮·位置·inline 确认           文本：/loc 地址 · 『确认』 · 查订单
```
- `core/` —— 配置、瑞幸端点、SQLite + Fernet 加密 token / 订单 / 位置存储
- `bot/` —— Telegram 渠道（agent + mcp_client + flows + ui + main）
- `service/` —— 渠道无关 HTTP 服务（`POST /message`），微信桥接调它
- `wechat/` —— Node 桥接，用 wx-link 收发微信消息
- `spike/` —— P0 去风险（验证瑞幸登录滑块，已转为粘贴 Token 方案）

## 登录（两渠道相同）
在 https://open.lkcoffee.com 用手机号登录，复制 Token，发给机器人 `/login <你的Token>`（约一个月有效）。

---

## Telegram 渠道
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # 填 BOT_TOKEN / AIGC_API_KEY / 生成 FERNET_KEY
python -m bot.main          # 长轮询启动
```
在 Telegram：`/start` → `/login <token>` → 点「📍 发送我的位置」→「来杯热的生椰拿铁」→「✅ 确认支付」→ 扫码 → 收取餐码。
其它：`/orders` 订单历史、`/cancel` 取消（两步确认）。

## 微信渠道
经腾讯官方 **ClawBot / iLink**（个人号机器人）。需要 Node 18+。


```bash
# 1) 渠道服务（Python）
uvicorn service.app:app --host 127.0.0.1 --port 8100
# 2) 微信桥接（Node）—— 首次扫码登录
cd wechat && npm install && SERVICE_URL=http://127.0.0.1:8100 node bridge.mjs   # 用小号扫码（可能要输配对码）
```
登录后，**用微信 → 我 → 设置 → 插件 → 微信ClawBot** 打开对话，发：
```
/login <你的瑞幸Token>
/loc 成都天府五街999号      # 发「地址」自动定位（高德地理编码→GCJ-02），也支持 /loc 经度,纬度；位置会被记住
来杯热的生椰拿铁
确认                        # 长按返回的二维码 →「识别图中二维码」微信支付
查订单 / /cancel
```
和 Telegram 版共用同一数据库，用户命名空间隔离，互不干扰。详见 [`wechat/README.md`](wechat/README.md)。

---

## 部署（自有 VPS + CI/CD）
长轮询 / 长服务都无需公网域名，只要 24/7 常驻进程。`.github/workflows/` 提供：
- **ci**：每次 push/PR 跑 pytest。
- **deploy**：push 到 `main` 自动 SSH 部署到 VPS，重启 `coffee-bot` / `coffee-service` / `coffee-wechat`（未配 `VPS_*` secrets 时自动跳过）。

一次性配置见 [`deploy/SETUP.md`](deploy/SETUP.md)。生产上三个 systemd 服务：`coffee-bot`（Telegram）、`coffee-service`（渠道服务）、`coffee-wechat`（微信桥接）。

## 测试
```bash
pytest    # 含「确认前绝不下单」护栏、券透传、订单/取消、位置持久化等
```

## 配置（.env）
| 键 | 说明 |
|---|---|
| `BOT_TOKEN` | Telegram BotFather `/newbot` 获取 |
| `AIGC_API_KEY` | aigc-gateway `pk_` key（LLM） |
| `LLM_MODEL` | 默认 `deepseek-v3`，可切 `qwen3.5-plus`/`kimi-k2.5`/`claude-opus-4.7` |
| `FERNET_KEY` | token 加密密钥 |
| `AMAP_KEY` | 高德「Web服务」key（微信渠道地址→坐标地理编码） |
| `BRIDGE_SECRET` | 微信桥接 ↔ 渠道服务共享密钥（可选） |
| `LUCKIN_ENV` | `prod`/`test03`/`pre` |
| `DAILY_SPEND_LIMIT` | 单日消费上限（元） |
| `PUBLIC_BASE_URL` | （Telegram Mini App 用，可选） |
