# 微信版咖啡 bot（wx-link 桥接）

把咖啡 bot 接到**个人微信**：Node 用 [`wx-link`](https://yhsrzbg.github.io/wx-link-doc/) 收发微信消息，
转发给 Python 渠道服务（`service/app.py`，复用现有下单大脑）。

```
微信用户 ──► wx-link (腾讯 iLink/ClawBot) ──► bridge.mjs ──HTTP──► Python service/app.py
                                                                  └─ OrderingAgent + MCP + 下单护栏
```

## ⚠️ 风险（务必先读）
- wx-link 走的是腾讯自家 `ilinkai.weixin.qq.com`（数据到微信自己服务器），但它是**非官方协议**、obfuscated、刚发布的单人包 → **务必用专用/小号微信登录**，别用主号；可能因微信更新失效或触发风控。
- `wechat/.wxsession.json`（含微信 botToken）和 `coffee.db` 都不入库。

## 运行
**1) 起 Python 渠道服务**（在仓库根，已装好 venv）：
```bash
source .venv/bin/activate
uvicorn service.app:app --host 127.0.0.1 --port 8100
```

**2) 起微信桥接**（另开终端）：
```bash
cd wechat
npm install
SERVICE_URL=http://127.0.0.1:8100 npm start
```
首次会在终端打印二维码 —— 用**专用微信**扫码登录（可能要求输入手机微信显示的配对数字）。登录态存 `.wxsession.json`，下次免扫。

**3) 在微信里跟这个号对话**：
```
/login <你的瑞幸Token>     # open.lkcoffee.com 登录后复制
/loc 116.392,39.982        # 设置位置（经度,纬度）
来杯热的生椰拿铁            # 自然语言点单
确认                        # 看到预览后回复『确认』下单 → 回支付二维码（长按识别支付）
查订单 / /orders            # 查状态和取餐码
/cancel                     # 取消（两步确认）
```

## 与 Telegram 版的差异
- 无原生「位置按钮」「确认按钮」→ 位置用 `/loc`，确认用回复『确认』。
- 无后台状态轮询 → 支付后用户主动发『查订单』查取餐码。
- 与 Telegram 版共用同一 `coffee.db`，用户 key 命名空间不同（微信用 wx 的 userId），互不干扰。
