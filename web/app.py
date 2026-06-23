"""手机号登录页（免粘贴 token）。

用户在 bot 里发 /login → bot 生成一次性 nonce 链接 → 用户打开本页 → 填手机号+验证码
（开放平台实测无滑块；若个别账号要滑块，前端有极验兜底）→ 后端复刻
validcode → (sliderVerify) → loginAi → getToken 拿到 luckyMcpToken → 按 nonce 绑定的用户入库。

对外经 cloudflared/域名 HTTPS 暴露；与 bot 共用 coffee.db。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core import amap, db, push
from core.config import get_settings
from core.coupon import ConsumerClient
from core.geo import wgs84_to_gcj02

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("weblogin")

LK_ORIGIN = "https://open.lkcoffee.com"
HOME = LK_ORIGIN + "/"
CAPI = LK_ORIGIN + "/capi"
BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": LK_ORIGIN, "Referer": LK_ORIGIN + "/",
}

LOGIN_HTML = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")
COUPON_LOGIN_HTML = (Path(__file__).parent / "coupon_login.html").read_text(encoding="utf-8")
LOCATION_HTML = (Path(__file__).parent / "location.html").read_text(encoding="utf-8")
LANDING_HTML = (Path(__file__).parent / "landing.html").read_text(encoding="utf-8")
SESSIONS: dict[str, dict] = {}  # sid -> {client, csrf}
# 领券登录客户端：按**一次性 nonce**(登录票据)隔离，绝不用共享 sid —— 否则两个并发用户会串号/抢验证码
CONSUMER_CLIENTS: dict[str, ConsumerClient] = {}  # nonce -> 消费版 H5 客户端


async def _consumer_client(key: str) -> ConsumerClient:
    cl = CONSUMER_CLIENTS.get(key)
    if cl is None:
        if len(CONSUMER_CLIENTS) > 500:  # 防无界增长：淘汰最早的一个
            CONSUMER_CLIENTS.pop(next(iter(CONSUMER_CLIENTS)), None)
        cl = ConsumerClient()
        await cl.start()  # GET 首页拿 csrf + cookie
        CONSUMER_CLIENTS[key] = cl
    return cl


def _sid(req: Request) -> str:
    return req.cookies.get("lsid") or "default"


async def _session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if s is None:
        s = {"client": httpx.AsyncClient(headers=BASE_HEADERS, http2=True, timeout=20.0, follow_redirects=True),
             "csrf": None}
        SESSIONS[sid] = s
    return s


async def _ensure_csrf(s: dict) -> None:
    if s["csrf"]:
        return
    c: httpx.AsyncClient = s["client"]
    r = await c.get(HOME, headers={"Accept": "text/html,application/xhtml+xml"})
    csrf = c.cookies.get("csrfToken")
    if not csrf:
        m = re.search(r"window\._csrf\s*=\s*'([^']+)'", r.text)
        csrf = m.group(1) if m else ""
    s["csrf"] = csrf


async def _upstream(s: dict, path: str, params: dict) -> dict:
    await _ensure_csrf(s)
    c: httpx.AsyncClient = s["client"]
    r = await c.post(f"{CAPI}{path}?_csrf={s['csrf']}", json=params)
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_raw": r.text}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="luckin-phone-login", lifespan=_lifespan)


@app.get("/login", response_class=HTMLResponse)
async def login_page(req: Request):
    resp = HTMLResponse(LOGIN_HTML)
    if not req.cookies.get("lsid"):
        resp.set_cookie("lsid", uuid.uuid4().hex, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/validcode")
async def validcode(req: Request):
    b = await req.json()
    s = await _session(_sid(req))
    return JSONResponse(await _upstream(s, "/resource/m/sys/base/validcode", {
        "mobile": str(b["mobile"]).strip(), "callCode": str(b.get("callCode", "86")),
        "blackbox": b.get("blackbox", ""),
    }))


@app.post("/api/sliderVerify")
async def slider_verify(req: Request):
    b = await req.json()
    s = await _session(_sid(req))
    return JSONResponse(await _upstream(s, "/resource/m/sys/base/sliderVerify", {
        "sourceUrl": b.get("sourceUrl", "/resource/m/sys/base/validcode"), "sliderType": 0,
        "blackbox": b.get("blackbox", ""), "verifyParams": b["verifyParams"],
        "phone": str(b["mobile"]).strip(), "countryNo": str(b.get("countryNo", "86")),
    }))


@app.post("/api/loginAi")
async def login_ai(req: Request):
    b = await req.json()
    s = await _session(_sid(req))
    return JSONResponse(await _upstream(s, "/resource/m/user/loginAi", {
        "mobile": str(b["mobile"]).strip(), "validateCode": str(b["code"]).strip(),
        "countryNo": str(b.get("countryNo", "86")), "type": 1,
    }))


@app.post("/api/getToken")
async def get_token(req: Request):
    b = await req.json()
    s = await _session(_sid(req))
    data = await _upstream(s, "/resource/m/oauth/mcp/getToken", {"oauthApp": "LUCKIN_MCP_AI"})
    content = (data or {}).get("content") or {}
    token = content.get("luckyMcpToken")
    nonce = (b or {}).get("t", "")
    stored = False
    if token and nonce:
        rec = db.consume_login_nonce(nonce)
        if rec is not None:
            db.set_token(rec.user_key, token, str(content.get("luckyMcpTokenDate") or ""))
            stored = True
            log.info("token stored for user_key %s via nonce", rec.user_key)
            # 登录成功 → 回推到来源渠道（聊天窗口给反馈，不再只在网页显示 ✅）
            try:
                await push.push_to_channel(
                    rec.channel, rec.push_target,
                    "✅ 已登录瑞幸账号，可以开始点单啦～发个位置或直接说想喝什么。")
            except Exception as e:
                log.warning("login push failed: %s", e)
    return JSONResponse({"ok": bool(token), "stored": stored})


@app.get("/coupon-login", response_class=HTMLResponse)
async def coupon_login_page(req: Request):
    resp = HTMLResponse(COUPON_LOGIN_HTML)
    if not req.cookies.get("lsid"):
        resp.set_cookie("lsid", uuid.uuid4().hex, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/coupon/sendcode")
async def coupon_sendcode(req: Request):
    b = await req.json()
    token = str((b or {}).get("t") or "").strip()
    if not db.peek_login_nonce(token):  # 必须带 bot 下发的有效票据，挡随机号码 SMS 滥用
        return JSONResponse({"ok": False, "msg": "登录链接无效或已过期，请回机器人重发 /福利"}, status_code=400)
    cl = await _consumer_client(token)
    return JSONResponse(await cl.send_code(str(b["mobile"]).strip()))


@app.post("/api/coupon/login")
async def coupon_login(req: Request):
    b = await req.json()
    token = str((b or {}).get("t") or "").strip()
    cl = CONSUMER_CLIENTS.get(token)  # 必须是 sendcode 时建立的同一会话（同 nonce）
    if not cl:
        return JSONResponse({"ok": False, "msg": "请先获取验证码"}, status_code=400)
    resp = await cl.login(str(b["mobile"]).strip(), str(b["code"]).strip())
    ok = isinstance(resp, dict) and resp.get("status") == "SUCCESS" and resp.get("loginState") == 1
    stored = False
    if ok:
        rec = db.consume_login_nonce(token)
        if rec is not None:
            db.set_consumer_session(rec.user_key, cl.session.to_json())
            stored = True
            log.info("consumer session stored for user_key %s", rec.user_key)
            try:
                await push.push_to_channel(
                    rec.channel, rec.push_target,
                    "✅ 领券登录已绑定！发『/福利』就能领每周免费券（只领免费、不会扣钱）。")
            except Exception as e:
                log.warning("coupon login push failed: %s", e)
        CONSUMER_CLIENTS.pop(token, None)  # 登录成功即清掉内存会话（防泄漏/复用）；失败则保留供重试验证码
    return JSONResponse({"ok": ok, "stored": stored,
                         "msg": resp.get("msg", "") if isinstance(resp, dict) else ""})


@app.get("/set-location", response_class=HTMLResponse)
async def location_page(req: Request):
    return HTMLResponse(LOCATION_HTML)


@app.post("/api/location")
async def set_location(req: Request):
    b = await req.json()
    # 先校验坐标，再消费 nonce —— 坏请求不该烧掉一次性票据（可重试）
    try:
        lat, lng = float(b["lat"]), float(b["lng"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "msg": "坐标无效"}, status_code=400)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JSONResponse({"ok": False, "msg": "坐标超出范围"}, status_code=400)
    nonce = str((b or {}).get("t") or "").strip()
    rec = db.consume_login_nonce(nonce)
    if rec is None:
        return JSONResponse({"ok": False, "msg": "定位链接无效或已过期，请回机器人重发"}, status_code=400)
    # 浏览器定位是 WGS-84，瑞幸按 GCJ-02 检索门店 → 服务端统一转换（与 Telegram 原生定位同源逻辑）
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    label = await amap.regeo(gcj_lng, gcj_lat)
    db.set_location(rec.user_key, gcj_lng, gcj_lat, label)
    log.info("location set for user_key %s via nonce", rec.user_key)
    try:
        await push.push_to_channel(rec.channel, rec.push_target,
                                   f"📍 已定位：{label}，直接说想喝什么就行～")
    except Exception as e:
        log.warning("location push failed: %s", e)
    return JSONResponse({"ok": True, "label": label})


@app.get("/", response_class=HTMLResponse)
async def landing():
    """可分享入口页（Telegram-only：微信 ClawBot 是 owner 私人接口，别人进不来）。"""
    return HTMLResponse(LANDING_HTML.replace("{{BOT_USERNAME}}", get_settings().bot_username))


@app.get("/health")
async def health():
    return {"ok": True}
