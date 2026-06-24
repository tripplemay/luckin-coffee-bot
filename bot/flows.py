"""下单流程辅助：解析瑞幸响应、消费护栏、订单状态轮询。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from telegram import Bot

from bot.mcp_client import LuckinMCPClient
from core import db
from core.luckin import ORDER_STATUS

log = logging.getLogger("flows")

# 视为终态的订单状态（停止轮询）
_TERMINAL_STATUS = {60, 80, 100}  # 等待取餐 / 已完成 / 已取消
# 视为「已支付」的状态（用于落实消费记账，排除 10待付款 / 100已取消）
_PAID_STATUS = {20, 30, 60, 80}  # 下单成功 / 制作中 / 等待取餐 / 已完成


def unwrap(resp: Any) -> Any:
    """MCP 工具返回多为 {code,msg,data,success} 信封；取出 data。否则原样返回。"""
    if isinstance(resp, dict) and "data" in resp and ("success" in resp or "code" in resp):
        return resp.get("data")
    return resp


def _price_of(preview_data: dict) -> Optional[float]:
    for key in ("discountPrice", "orderPayAmount", "totalInitialPrice"):
        v = preview_data.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def format_preview(resp: Any) -> tuple[str, Optional[float]]:
    data = unwrap(resp)
    if not isinstance(data, dict):
        return (f"预览失败：{resp}", None)
    price = _price_of(data)
    lines = ["🧾 订单预览"]
    shop = data.get("shopInfo") or {}
    if shop.get("deptName"):
        lines.append(f"门店：{shop['deptName']}")
    for it in data.get("productInfoList") or []:
        name = it.get("name", "商品")
        amount = it.get("amount", 1)
        extra = it.get("additionDesc") or ""
        ep = it.get("estimatePrice")
        seg = f"• {name} ×{amount}"
        if extra:
            seg += f"（{extra}）"
        if isinstance(ep, (int, float)):
            seg += f"  ¥{ep}"
        lines.append(seg)
    priv = data.get("privilegeMoney")
    if isinstance(priv, (int, float)) and priv > 0:
        lines.append(f"优惠：-¥{priv}")
    if price is not None:
        lines.append(f"合计应付：¥{price:.2f}")
    return ("\n".join(lines), price)


def spend_guard(tg_user_id: int, price: Optional[float]) -> Optional[str]:
    """返回 None 表示放行；否则返回拒绝原因。消费上限按用户取（可被 /admin 单独设）。"""
    if price is None:
        return None
    limit = db.effective_spend_limit(tg_user_id)  # 每用户覆盖值，未设则全局默认
    day = db.today_cst()  # 与消息限频同一"天"边界（固定+08:00），避免 UTC 服务器上提前重置
    already = db.spend_today(tg_user_id, day)
    if already + price > limit:
        return f"超出单日消费上限（已花 ¥{already:.2f}，本单 ¥{price:.2f}，上限 ¥{limit:.0f}）。"
    return None


def format_order_created(resp: Any) -> tuple[str, Optional[str], Optional[str], bool, Optional[str]]:
    """返回 (文本, 二维码内容, orderId, 是否需扫码支付, 中转支付页URL)。

    二维码优先用 `payOrderUrl`（weixin://wxpay/bizpayurl 原生支付码）→ 微信一扫直接付款，
    省去「先扫到瑞幸中转网页」那一跳；`payOrderQrCodeUrl`（https 中转页）作为同屏点击兜底。
    若 needPay=false（券/余额全额覆盖），免扫码：不返回二维码。
    """
    data = unwrap(resp)
    if not isinstance(data, dict):
        return (f"下单失败：{resp}", None, None, False, None)
    order_id = data.get("orderIdStr") or (str(data["orderId"]) if data.get("orderId") else None)
    need_pay = bool(data.get("needPay"))
    pay_deeplink = data.get("payOrderUrl")       # weixin://...  微信原生支付码（直接付）
    pay_page = data.get("payOrderQrCodeUrl")     # https 瑞幸中转页（同屏点击兜底）
    qr = (pay_deeplink or pay_page) if need_pay else None
    price = data.get("discountPrice")
    text = "✅ 已创建订单"
    if isinstance(price, (int, float)):
        text += f"，应付 ¥{price}"
    if need_pay:
        text += "\n请用微信扫下方二维码直接支付 👇"
    else:
        text += "\n🎉 已用券/余额完成支付，无需扫码，正在为你制作 ☕"
    return (text, qr, order_id, need_pay, pay_page if need_pay else None)


def created_price(resp: Any) -> Optional[float]:
    """createOrder 实际应付价（discountPrice）。"""
    data = unwrap(resp)
    if isinstance(data, dict):
        v = data.get("discountPrice")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def format_order_status(resp: Any) -> str:
    data = unwrap(resp)
    if not isinstance(data, dict):
        return f"查询失败：{resp}"
    status = data.get("orderStatus")
    name = data.get("orderStatusName") or ORDER_STATUS.get(status, str(status))
    lines = [f"📦 订单状态：{name}"]
    take = (data.get("takeMealCodeInfo") or {}).get("code")
    if take and take != "生成中":
        lines.append(f"取餐码：{take}")
    return "\n".join(lines)


def reorder_payload_from_call(call: Any) -> dict:
    """从 createOrder tool_call 抽取可复购 payload（productList JSON / deptId / 坐标），下单后落库。

    只取重放所需字段，**不含 couponCodeList**（券须复购时按新预览重配，旧券会过期）。
    """
    import json
    try:
        args = json.loads(call["function"].get("arguments", "{}"))
    except (KeyError, TypeError, ValueError):
        args = {}
    pl = args.get("productList")
    return {
        "product_list": json.dumps(pl, ensure_ascii=False) if pl else None,
        "dept_id": str(args["deptId"]) if args.get("deptId") is not None else None,
        "lng": args.get("longitude"), "lat": args.get("latitude"),
    }


def preview_summary(resp: Any) -> str:
    """从 previewOrder 提取一句话商品摘要，存进订单历史。"""
    data = unwrap(resp)
    if isinstance(data, dict):
        items = data.get("productInfoList") or []
        parts = [f"{it.get('name', '商品')}×{it.get('amount', 1)}" for it in items[:3]]
        if parts:
            return "、".join(parts)
    return "订单"


def cancel_succeeded(resp: Any) -> bool:
    """cancelOrder 是否成功。容忍 data 为 True / 1 / "true"，并兜底看 success/code。"""
    data = unwrap(resp)
    if isinstance(data, bool):
        return data
    if isinstance(data, int):
        return data == 1
    if isinstance(data, str):
        return data.strip().lower() == "true"
    if isinstance(resp, dict):
        return resp.get("success") is True and resp.get("code", 0) == 0
    return False


def cancel_message(resp: Any) -> str:
    """取消失败时透传后端 msg，而非写死猜测。"""
    if isinstance(resp, dict) and resp.get("msg"):
        return str(resp["msg"])
    return "可能已支付或已完成"


def order_brief(resp: Any) -> str:
    """queryOrderDetailInfo → 一行状态摘要（用于 /orders 列表）。"""
    data = unwrap(resp)
    if not isinstance(data, dict):
        return "查询失败"
    name = data.get("orderStatusName") or ORDER_STATUS.get(data.get("orderStatus"), "未知状态")
    take = (data.get("takeMealCodeInfo") or {}).get("code")
    if take and take != "生成中":
        return f"{name} · 取餐码 {take}"
    return name


async def poll_order_until_ready(bot: Bot, chat_id: int, mcp: LuckinMCPClient, token: str,
                                 order_id: str, interval: int = 20, max_minutes: int = 30,
                                 spend_user_id: Optional[int] = None,
                                 spend_amount: Optional[float] = None) -> None:
    """后台轮询订单状态，状态变化时推送；到终态或超时停止。

    若传入 spend_user_id/spend_amount（needPay=true 的待支付单），则在订单首次进入
    「已支付」状态时才把消费计入台账——避免未支付订单污染单日消费护栏。
    """
    last_status = None
    recorded = False
    deadline = max_minutes * 60
    waited = 0
    while waited < deadline:
        await asyncio.sleep(interval)
        waited += interval
        try:
            resp = await mcp.call_tool(token, "queryOrderDetailInfo", {"orderId": order_id})
        except Exception as e:
            log.warning("poll order %s failed: %s", order_id, e)
            continue
        data = unwrap(resp)
        if not isinstance(data, dict):
            continue
        status = data.get("orderStatus")
        if status != last_status:
            last_status = status
            try:
                await bot.send_message(chat_id, format_order_status(resp))
            except Exception as e:
                log.warning("push status failed: %s", e)
        if not recorded and spend_user_id is not None and spend_amount and status in _PAID_STATUS:
            db.record_spend(spend_user_id, db.today_cst(), spend_amount, order_id)
            recorded = True
        if status in _TERMINAL_STATUS:
            return
