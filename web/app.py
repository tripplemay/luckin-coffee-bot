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

from core import db

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
SESSIONS: dict[str, dict] = {}  # sid -> {client, csrf}


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
        user_key = db.consume_login_nonce(nonce)
        if user_key is not None:
            db.set_token(user_key, token, str(content.get("luckyMcpTokenDate") or ""))
            stored = True
            log.info("token stored for user_key %s via nonce", user_key)
    return JSONResponse({"ok": bool(token), "stored": stored})


@app.get("/health")
async def health():
    return {"ok": True}
