"""渠道无关的点单服务（HTTP）。

复用 Python 下单大脑（OrderingAgent + MCP + flows + db），把交互抽象成
`POST /message {user_key, text} -> {actions:[...]}`，供任意渠道（微信 wx-link 桥接、
也可接其它 IM）调用。下单确认护栏在此用**文本**实现（回复『确认』/『取消』）。

Action 形态：
  {"type":"text","text": "..."}
  {"type":"image","b64": "<png base64>","caption": "..."}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Optional

import httpx
import qrcode
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from bot import flows
from bot.agent import OrderingAgent
from bot.mcp_client import LuckinMCPClient
from core import db
from core.config import get_settings, login_base_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("service")

WELCOME = (
    "☕ 瑞幸点单助手\n"
    "1) 登录：/login <你的瑞幸Token>（在 open.lkcoffee.com 登录后复制 Token）\n"
    "2) 设位置：/loc 你的地址（如 /loc 成都天府五街999号），或 /loc 经度,纬度\n"
    "3) 直接说想喝什么，例如「来杯热的生椰拿铁」\n"
    "位置会被记住，下次不用重设。下单前会让你回复『确认』，不会乱扣款。其他：/orders 查订单、/cancel 取消"
)


_CONFIRM_WORDS = {"确认", "确认下单", "确定", "1", "y", "Y", "yes"}
_CANCEL_WORDS = {"取消", "不要了", "2", "n", "N", "no"}


def _uid(user_key: str) -> int:
    """渠道字符串用户 id → 稳定整数，复用 INTEGER 主键 db（不改 schema）。
    加 'wx:' 前缀并置高位 bit62 → 与 Telegram 的小整数 id 物理隔离，杜绝跨用户串号。"""
    h = int(hashlib.sha1(("wx:" + user_key).encode()).hexdigest()[:15], 16)
    return h | (1 << 62)


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _qr_action(payload: str, caption: str) -> dict:
    img = qrcode.make(payload)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return {"type": "image", "b64": base64.b64encode(buf.getvalue()).decode(), "caption": caption}


@dataclass
class UserState:
    messages: Optional[list] = None
    location: Optional[tuple] = None
    pending_order: Optional[dict] = None
    pending_price: Optional[float] = None
    pending_summary: str = "订单"
    cancel_map: dict = field(default_factory=dict)   # 序号 -> order_id
    pending_cancel: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChannelCore:
    def __init__(self) -> None:
        self._mcp = LuckinMCPClient()
        self._agent = OrderingAgent(self._mcp)
        self._states: dict[str, UserState] = {}
        self._seen: "OrderedDict[str, bool]" = OrderedDict()  # 消息幂等去重（有界）

    def _state(self, key: str) -> UserState:
        st = self._states.get(key)
        if st is None:
            if len(self._states) > 2000:  # 淘汰无挂起态的空闲会话，避免无限增长
                stale = [k for k, v in self._states.items()
                         if v.pending_order is None and v.pending_cancel is None and not v.lock.locked()]
                for k in stale[:500]:
                    self._states.pop(k, None)
            st = UserState()
            self._states[key] = st
        return st

    async def handle(self, user_key: str, text: str, msg_id: Optional[str] = None) -> list[dict]:
        if msg_id:  # 幂等：同一条微信消息重投不重复处理（防重复下单）
            mk = f"{user_key}:{msg_id}"
            if mk in self._seen:
                return []
            self._seen[mk] = True
            if len(self._seen) > 5000:
                self._seen.popitem(last=False)
        st = self._state(user_key)
        async with st.lock:  # 串行化同一用户的并发消息
            try:
                return await self._dispatch(user_key, st, text.strip())
            except Exception:  # 兜底：不向用户泄露内部细节
                log.exception("handle failed for %s", user_key)
                return [_text("出错了，请稍后重试。")]

    async def _dispatch(self, key: str, st: UserState, text: str) -> list[dict]:
        if not text:
            return [_text(WELCOME)]

        if text.startswith("/login"):
            parts = text.split(maxsplit=1)
            if len(parts) >= 2 and parts[1].strip():  # 粘贴 token 兜底
                db.set_token(_uid(key), parts[1].strip())
                st.pending_order = st.pending_price = st.pending_cancel = None
                return [_text("✅ 登录成功，已安全保存。\n发『/loc 你的地址』设位置，再说想喝什么～")]
            base = login_base_url()  # 无参数 → 发手机号登录链接（免粘贴）
            if not base:
                return [_text("登录页未配置。可先用 /login <你的瑞幸Token> 粘贴登录。")]
            nonce = secrets.token_urlsafe(12)
            db.create_login_nonce(nonce, _uid(key))
            return [_text(f"点链接用手机号登录（填手机号+短信验证码，免粘贴 Token）：\n{base}/login?t={nonce}\n链接 15 分钟内有效。")]

        if text in ("/start", "/help", "help", "你好", "在吗"):
            return [_text(WELCOME)]

        rec = db.get_token(_uid(key))
        if not rec:
            return [_text("请先登录：/login <你的瑞幸Token>（open.lkcoffee.com 登录后复制 Token）。")]

        # ── 模态护栏：有待确认订单时只认『确认』/『取消』，其余一律提醒（防绕过/状态串味）──
        if st.pending_order is not None:
            if text in _CONFIRM_WORDS:
                return await self._do_order(key, st, rec.token)
            if text in _CANCEL_WORDS:
                call = st.pending_order
                st.pending_order = st.pending_price = None
                res = await self._agent.resume_after_confirm(st.messages, call, rec.token, approved=False)
                st.messages = res.messages
                return [_text("已取消本次下单。")] + ([_text(res.text)] if res.text else [])
            return [_text("有一笔待确认的订单。回复『确认』下单，或『取消』放弃。")]

        # 两步取消的第二步；其余消息则放弃这个取消挂起
        if st.pending_cancel:
            if text == "确认取消":
                return await self._cancel_do(key, st, rec.token)
            st.pending_cancel = None

        if text.startswith("/loc"):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if not arg:
                return [_text("用法：/loc 你的地址（如 /loc 成都天府五街999号），或 /loc 经度,纬度")]
            coords = _parse_coords(arg)
            label = f"{coords[0]},{coords[1]}" if coords else None
            if not coords:
                if _looks_like_coords(arg):
                    return [_text("坐标超出范围（经度∈[-180,180]、纬度∈[-90,90]）。也可直接发地址，如 /loc 成都天府五街")]
                geo = await self._geocode(arg)
                if geo is None:
                    if not get_settings().amap_key:
                        return [_text("还没配置地址解析（缺 AMAP_KEY）。先用经纬度：/loc 116.392,39.982")]
                    return [_text(f"没找到「{arg}」，换个更具体的写法试试（带城市/区/路名）。")]
                coords, label = (geo[0], geo[1]), geo[2]
            st.location = coords
            st.messages = self._agent.new_conversation(coords)
            db.set_location(_uid(key), coords[0], coords[1], label)
            return [_text(f"📍 已定位：{label}（{coords[0]}, {coords[1]}），想喝点什么？")]

        if text in ("/orders", "查订单", "我的订单"):
            return await self._orders(key, rec.token)

        if text == "/cancel":
            return await self._cancel_list(key, st, rec.token)
        if text.startswith("/cancel "):
            return self._cancel_select(st, text)

        # 自然语言点单
        if not st.location:
            saved = db.get_location(_uid(key))  # 记住的位置，免得每次重设
            if saved:
                st.location = (saved["lng"], saved["lat"])
                st.messages = self._agent.new_conversation(st.location)
            else:
                return [_text("先发『/loc 你的地址』设置位置吧（如 /loc 成都天府五街999号），我才能帮你找附近门店。")]
        if st.messages is None:
            st.messages = self._agent.new_conversation(st.location)
        st.messages.append({"role": "user", "content": text})
        result = await self._agent.step(st.messages, rec.token)
        st.messages = result.messages
        if result.kind == "text":
            return [_text(result.text or "（没听懂，换个说法试试？）")]

        # createOrder 拦截 → 价格确认
        preview_text, price = flows.format_preview(result.preview)
        reason = flows.spend_guard(_uid(key), price)
        if reason:
            res2 = await self._agent.resume_after_confirm(result.messages, result.pending_call, rec.token,
                                                          approved=False, exec_result={"rejected": reason})
            st.messages = res2.messages
            return [_text("⛔ " + reason)] + ([_text(res2.text)] if res2.text else [])
        st.pending_order = result.pending_call
        st.pending_price = price
        st.pending_summary = flows.preview_summary(result.preview)
        return [_text(preview_text + "\n\n回复『确认』下单，或『取消』放弃。")]

    async def _do_order(self, key: str, st: UserState, token: str) -> list[dict]:
        call = st.pending_order
        confirmed = st.pending_price
        summary = st.pending_summary
        # 真实下单（花钱）；成功返回后**立即**清空挂起态，杜绝后续任何异常导致二次下单
        create_result = await self._agent.execute_pending(token, call)
        st.pending_order = None
        st.pending_price = None

        text, qr, order_id, need_pay, pay_page = flows.format_order_created(create_result)
        actual = flows.created_price(create_result)
        higher = actual is not None and confirmed is not None and actual > confirmed + 0.01

        # 实付价高于确认价：用实付价重核单日上限，超限则自动取消刚下的单（保护用户，赶在扫码前）
        if higher:
            over = flows.spend_guard(_uid(key), actual)
            if over and order_id:
                try:
                    await self._mcp.call_tool(token, "cancelOrder", {"orderId": order_id})
                except Exception as e:
                    log.warning("auto-cancel over-limit order %s failed: %s", order_id, e)
                await self._safe_resume(st, call, token, create_result)
                return [_text(f"⚠️ 实付 ¥{actual:.2f} 超出单日上限（{over}），已尝试自动取消该订单，请在瑞幸 App 核对，未扣款勿支付。")]

        record_price = actual if actual is not None else confirmed
        acts: list[dict] = []
        if higher:
            acts.append(_text(f"⚠️ 实付 ¥{actual:.2f} 高于确认价 ¥{confirmed:.2f}（优惠可能未生效），未支付可在瑞幸取消。"))
        elif actual is None:
            log.warning("createOrder %s 无 discountPrice，按确认价记账", order_id)
        if order_id:
            db.record_order(_uid(key), order_id, summary)
            if record_price:  # 微信版无轮询，下单即记账（偏保守，安全）
                db.record_spend(_uid(key), datetime.now().strftime("%Y-%m-%d"), record_price, order_id)

        closing = await self._safe_resume(st, call, token, create_result)
        acts.append(_text(text))
        if need_pay and qr:
            acts.append(_qr_action(qr, "微信扫码支付（长按识别二维码）"))
            if pay_page:
                acts.append(_text("同屏无法扫码？打开支付页：\n" + pay_page))
        acts.append(_text("支付后回复『查订单』查看状态和取餐码。"))
        if closing:
            acts.append(_text(closing))
        return acts

    async def _geocode(self, address: str) -> Optional[tuple]:
        """高德地理编码：地址 → (lng, lat, formatted)，GCJ-02（与瑞幸一致）。未配 key/失败返回 None。"""
        gkey = get_settings().amap_key
        if not gkey:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://restapi.amap.com/v3/geocode/geo",
                                params={"address": address, "key": gkey})
                data = r.json()
            if data.get("status") == "1" and data.get("geocodes"):
                g = data["geocodes"][0]
                lng_s, lat_s = g["location"].split(",")
                return (float(lng_s), float(lat_s), g.get("formatted_address") or address)
        except Exception as e:
            log.warning("geocode failed for %r: %s", address, e)
        return None

    async def _safe_resume(self, st: UserState, call: dict, token: str, create_result) -> str:
        """续聊拿收尾文本；失败只影响提示，绝不影响已下的单。"""
        try:
            res = await self._agent.resume_after_confirm(st.messages, call, token, approved=True, exec_result=create_result)
            st.messages = res.messages
            return res.text or ""
        except Exception as e:
            log.warning("resume after order failed: %s", e)
            return ""

    async def _orders(self, key: str, token: str) -> list[dict]:
        orders = db.list_orders(_uid(key), limit=5)
        if not orders:
            return [_text("还没有订单记录。点一杯试试？")]
        lines = ["🧾 最近订单（仅显示通过本助手下的单）："]
        fails = 0
        for o in orders:
            try:
                detail = await asyncio.wait_for(
                    self._mcp.call_tool(token, "queryOrderDetailInfo", {"orderId": o["order_id"]}), timeout=15)
                brief = flows.order_brief(detail)
            except Exception as e:
                log.warning("orders query %s failed: %s", o["order_id"], e)
                brief = "查询失败"
                fails += 1
            lines.append(f"• {o.get('summary') or '订单'} (#{o['order_id'][-6:]}) — {brief}")
        if fails == len(orders):
            lines.append("\n⚠️ 全部查询失败，登录可能已过期，请重新 /login。")
        return [_text("\n".join(lines))]

    async def _cancel_list(self, key: str, st: UserState, token: str) -> list[dict]:
        orders = db.list_orders(_uid(key), limit=5)
        if not orders:
            return [_text("没有可取消的订单。")]
        st.cancel_map = {str(i + 1): o["order_id"] for i, o in enumerate(orders)}
        lines = ["选择要取消的订单，回复『/cancel 序号』（如 /cancel 1）："]
        for i, o in enumerate(orders):
            lines.append(f"{i + 1}. {o.get('summary') or '订单'} (#{o['order_id'][-6:]})")
        return [_text("\n".join(lines))]

    def _cancel_select(self, st: UserState, text: str) -> list[dict]:
        idx = text.split(maxsplit=1)[1].strip()
        order_id = st.cancel_map.get(idx)
        if not order_id:
            return [_text("序号无效，先发 /cancel 看列表。")]
        st.pending_cancel = order_id
        return [_text(f"确认取消第 {idx} 单（#{order_id[-6:]}）？回复『确认取消』执行。\n（已支付/制作中可能无法取消）")]

    async def _cancel_do(self, key: str, st: UserState, token: str) -> list[dict]:
        order_id = st.pending_cancel
        st.pending_cancel = None
        try:
            result = await self._mcp.call_tool(token, "cancelOrder", {"orderId": order_id})
        except Exception as e:
            log.warning("cancelOrder %s failed: %s", order_id, e)
            return [_text("取消失败，请稍后重试。")]
        if flows.cancel_succeeded(result):
            db.mark_order_cancelled(_uid(key), order_id)
            return [_text("✅ 已取消订单。")]
        return [_text("取消失败：" + flows.cancel_message(result))]


def _looks_like_coords(arg: str) -> bool:
    """形如「数字,数字」（无字母/汉字）→ 用户意图是坐标而非地址。"""
    return re.fullmatch(r"\s*-?\d+\.?\d*\s*[,，\s]\s*-?\d+\.?\d*\s*", arg) is not None


def _parse_coords(arg: str) -> Optional[tuple]:
    """只解析参数段里的前两个数字为 (经度, 纬度) 并校验范围，避免抓错文本/越界定位。"""
    nums = re.findall(r"-?\d+\.?\d*", arg)
    if len(nums) < 2:
        return None
    try:
        lng, lat = float(nums[0]), float(nums[1])
    except ValueError:
        return None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None
    return (lng, lat)


class MessageReq(BaseModel):
    user_key: str
    text: str = ""
    msg_id: Optional[str] = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init_db()  # 独立部署时也保证建表
    yield


app = FastAPI(title="coffee-channel-service", lifespan=_lifespan)
CORE = ChannelCore()


@app.post("/message")
async def message(req: MessageReq, x_bridge_secret: str = Header(default="")):
    secret = get_settings().bridge_secret
    if secret and x_bridge_secret != secret:  # 配了 BRIDGE_SECRET 才校验（防本机其它进程冒充用户）
        raise HTTPException(status_code=401, detail="unauthorized")
    actions = await CORE.handle(req.user_key.strip(), req.text, req.msg_id)
    return {"actions": actions}


@app.get("/health")
async def health():
    return {"ok": True}
