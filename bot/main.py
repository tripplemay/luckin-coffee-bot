"""Telegram 瑞幸点单机器人入口（长轮询）。

登录支持两种（取决于 P0 结论）：
  - Mini App：/start 给出 web_app 登录按钮（需配置 PUBLIC_BASE_URL）。
  - 粘贴 token 兜底：/login <token>。
两者都把 token 加密存进 SQLite，点单逻辑一致。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import flows, ui
from bot.agent import OrderingAgent
from bot.mcp_client import LuckinMCPClient
from core import admin, asr, db, push
from core import prefs as prefs_mod
from core.config import get_settings, login_base_url
from core.geo import wgs84_to_gcj02

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

MCP = LuckinMCPClient()
AGENT = OrderingAgent(MCP)


def _require_token(user_id: int):
    return db.get_token(user_id)


def _login_kb(user_id: int, chat_id: int):
    """生成带一次性 nonce 的手机号登录按钮，并把回推目标(本 TG 聊天)绑到该 nonce。

    未配置登录页则返回 None（调用方退回粘贴 token 提示）。
    """
    base = login_base_url()
    if not base:
        return None
    nonce = secrets.token_urlsafe(12)
    db.create_login_nonce(nonce, user_id, channel="tg", push_target=str(chat_id))
    return ui.login_keyboard(base, nonce)


def _coupon_login_kb(user_id: int, chat_id: int):
    """领券登录（消费版 H5）按钮 + 绑定 nonce。未配置登录页返回 None。"""
    base = login_base_url()
    if not base:
        return None
    nonce = secrets.token_urlsafe(12)
    db.create_login_nonce(nonce, user_id, channel="tg", push_target=str(chat_id))
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🎁 绑定领券登录", url=f"{base}/coupon-login?t={nonce}")]])


def _location_link_kb(user_id: int, chat_id: int):
    """网页一键定位按钮（GPS）+ 绑定 nonce，定位结果会回推到本对话。未配置登录页返回 None。"""
    base = login_base_url()
    if not base:
        return None
    nonce = secrets.token_urlsafe(12)
    db.create_login_nonce(nonce, user_id, channel="tg", push_target=str(chat_id))
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📍 一键获取当前位置", url=f"{base}/set-location?t={nonce}")]])


async def cmd_here(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """发位置：手机原生分享（最佳）或网页 GPS 定位（桌面也行）。"""
    await update.message.reply_text(
        "把位置发我：手机点下方「📍 发送我的位置」最快，或用网页一键定位。",
        reply_markup=ui.location_keyboard())
    kb = _location_link_kb(update.effective_user.id, update.effective_chat.id)
    if kb:
        await update.message.reply_text("网页定位（桌面/没有定位按钮时）👇", reply_markup=kb)


async def cmd_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """领取每周免费福利券。未绑定领券登录则先给绑定链接（带风险提示）。只领免费券，绝不扣钱。"""
    from core import coupon
    user_id = update.effective_user.id
    res = await coupon.run_claim_for_user(user_id, coupon.today_cst(), int(time.time()))
    if res.get("need_login"):
        kb = _coupon_login_kb(user_id, update.effective_chat.id)
        if not kb:
            await update.message.reply_text("领券登录页未配置，暂时用不了。")
            return
        await update.message.reply_text(
            "领免费券需先单独绑定瑞幸「领券登录」（与点单登录不同源）。\n"
            "⚠️ 第三方代领属灰色地带，仅个人低频、只领免费券、绝不扣钱。\n点下方按钮绑定（手机号+短信）：",
            reply_markup=kb)
        return
    await update.message.reply_text(coupon.format_claim_result(res))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if db.touch_user(uid, "tg", update.effective_user.full_name):  # 首触建档 + 告警 owner
        context.application.create_task(push.notify_owner(
            f"🆕 新用户(TG)：{update.effective_user.full_name or uid}（{uid}）"))
    kb = _login_kb(uid, update.effective_chat.id)
    text = (
        "☕ 欢迎使用瑞幸点单助手！\n\n"
        "1) 先登录瑞幸账号"
        + ("（点下方按钮，手机号+短信，免粘贴）" if kb else "：把你的 Token 发给我 `/login <token>`")
        + "\n2) 点「📍 发送我的位置」分享定位\n"
        "3) 直接说想喝什么，比如「来杯热的生椰拿铁」\n\n"
        "下单前我会显示价格让你确认，不会乱扣款 👍\n"
        "小技巧：说「以后都要热的」我会记住偏好（/prefs 查看）；下次发「老样子」一键复购上次那单。"
    )
    await update.message.reply_text(text, reply_markup=kb)
    await update.message.reply_text("分享位置 👇", reply_markup=ui.location_keyboard())


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip():  # 粘贴 token 兜底
        db.set_token(update.effective_user.id, parts[1].strip())
        await update.message.reply_text("✅ 登录成功，已安全保存。分享位置后就能点单啦～",
                                        reply_markup=ui.location_keyboard())
        return
    kb = _login_kb(update.effective_user.id, update.effective_chat.id)  # 无参数 → 手机号登录链接
    if not kb:
        await update.message.reply_text("登录页未配置。用法：/login <你的瑞幸Token>")
        return
    await update.message.reply_text("点下方按钮用手机号登录（填手机号 + 短信验证码）。链接 15 分钟内有效。",
                                    reply_markup=kb)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"你的 Telegram id：`{update.effective_user.id}`",
                                    parse_mode="Markdown")


async def cmd_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查看/设置/清除点单偏好（无需登录）。设置/清除在待确认订单时禁止。"""
    uid = update.effective_user.id
    if db.touch_user(uid, "tg", update.effective_user.full_name):  # 命令绕过 on_text 的建档/告警，补上
        context.application.create_task(push.notify_owner(
            f"🆕 新用户(TG)：{update.effective_user.full_name or uid}（{uid}）"))
    text = (update.message.text or "").strip()
    intent = prefs_mod.parse_prefs_command(text)
    if intent["action"] in ("set", "clear_all", "clear_field") and context.user_data.get("pending"):
        await update.message.reply_text("有一笔待确认的订单，请先点『确认/取消』，再改偏好。")
        return
    reply = prefs_mod.apply_prefs_command(uid, text)
    kb = ui.prefs_keyboard() if (intent["action"] == "view" and db.get_prefs(uid)) else None
    await update.message.reply_text(reply, reply_markup=kb)


async def on_prefs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.data == "prefs:clearall":
        if context.user_data.get("pending"):
            await q.edit_message_text("有一笔待确认的订单，请先点『确认/取消』，再改偏好。")
            return
        db.clear_prefs(q.from_user.id)
        await q.edit_message_text("已清空全部偏好。")


async def cmd_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """老样子：复购最近一笔订单（仍走价格确认）。"""
    rec = _require_token(update.effective_user.id)
    if not rec:
        await update.message.reply_text("请先登录：/login <你的瑞幸Token>。")
        return
    payload = db.get_last_order_payload(update.effective_user.id)
    if not payload:
        await update.message.reply_text("你还没有可复购的订单，先点一杯吧～")
        return
    await _present_reorder(update, context, rec, payload)


async def _present_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE, rec, payload: dict) -> None:
    """确定性复购：新预览 → spend_guard → 确认按钮。预览失败回退 LLM 重搜（仍经确认门）。"""
    uid = update.effective_user.id
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    result = await AGENT.build_reorder(rec.token, payload)
    text, price = (flows.format_preview(result.preview) if result else (None, None))
    if result is None or price is None:  # 预览失败/无可用价格 → 回退 LLM，绝不出 ¥0 确认
        summary = payload.get("summary") or "上次那杯"
        await _handle_text(update, context, f"再来一份：{summary}")
        return
    reason = flows.spend_guard(uid, price)
    if reason:
        await update.message.reply_text("⛔ " + reason)
        return
    context.user_data["pending"] = {"call": result.pending_call, "price": price,
                                    "summary": flows.preview_summary(result.preview), "reorder": True}
    await update.message.reply_text("🔁 老样子复购（上次门店）\n" + text + "\n\n确认下单吗？",
                                    reply_markup=ui.confirm_order_keyboard(price))


async def cmd_usual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """常买：列出近期可复购订单，点按钮选一个复购。"""
    rec = _require_token(update.effective_user.id)
    if not rec:
        await update.message.reply_text("请先登录：/login <你的瑞幸Token>。")
        return
    items = db.list_recent_payloads(update.effective_user.id, limit=10)
    uniq, seen = [], set()
    for it in items:
        s = it.get("summary") or "订单"
        if s in seen:
            continue
        seen.add(s)
        uniq.append(it)
        if len(uniq) >= 5:
            break
    if not uniq:
        await update.message.reply_text("你还没有可复购的订单，先点一杯吧～")
        return
    buttons = [[InlineKeyboardButton(f"🔁 {it.get('summary') or '订单'}", callback_data=f"reorder:{it['order_id']}")]
               for it in uniq]
    await update.message.reply_text("选一个复购（上次门店、价格以预览为准）：",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def on_reorder_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    rec = _require_token(q.from_user.id)
    if not rec:
        await q.edit_message_text("请先登录后再操作。")
        return
    order_id = q.data.split(":", 1)[1]
    payload = next((p for p in db.list_recent_payloads(q.from_user.id, limit=20)
                    if p["order_id"] == order_id), None)
    if not payload:
        await q.edit_message_text("该订单已不可复购。")
        return
    await q.edit_message_text("⏳ 正在准备复购…")
    result = await AGENT.build_reorder(rec.token, payload)
    text, price = (flows.format_preview(result.preview) if result else (None, None))
    if result is None or price is None:  # 预览失败/无价 → 让用户用文字重点（回退）
        await q.message.reply_text("这杯的商品/门店可能变了，直接说『再来一份" +
                                   (payload.get("summary") or "") + "』我帮你重点～")
        return
    reason = flows.spend_guard(q.from_user.id, price)
    if reason:
        await q.message.reply_text("⛔ " + reason)
        return
    context.user_data["pending"] = {"call": result.pending_call, "price": price,
                                    "summary": flows.preview_summary(result.preview), "reorder": True}
    await q.message.reply_text("🔁 老样子复购（上次门店）\n" + text + "\n\n确认下单吗？",
                               reply_markup=ui.confirm_order_keyboard(price))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin.is_owner_tg(update.effective_user.id):
        return  # 非 owner：静默忽略
    parts = (update.message.text or "").split(maxsplit=1)
    await update.message.reply_text(admin.admin_command(parts[1] if len(parts) > 1 else ""),
                                    parse_mode="Markdown")


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.delete_token(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text("已退出登录，Token 已删除。")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rec = _require_token(update.effective_user.id)
    if not rec:
        await update.message.reply_text("请先登录：/login <你的瑞幸Token>。")
        return
    orders = db.list_orders(update.effective_user.id, limit=5)
    if not orders:
        await update.message.reply_text("还没有订单记录。点一杯试试？")
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    lines = ["🧾 最近订单（仅显示通过本助手下的单）："]
    failures = 0
    for o in orders:
        try:
            detail = await asyncio.wait_for(
                MCP.call_tool(rec.token, "queryOrderDetailInfo", {"orderId": o["order_id"]}), timeout=15)
            brief = flows.order_brief(detail)
        except Exception as e:
            log.warning("queryOrderDetailInfo %s failed: %s", o["order_id"], e)
            brief = "查询失败"
            failures += 1
        lines.append(f"• {o.get('summary') or '订单'} (#{o['order_id'][-6:]}) — {brief}")
    if failures == len(orders):
        lines.append("\n⚠️ 全部查询失败，登录可能已过期，请重新 /login。")
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rec = _require_token(update.effective_user.id)
    if not rec:
        await update.message.reply_text("请先登录：/login <你的瑞幸Token>。")
        return
    orders = db.list_orders(update.effective_user.id, limit=5)
    if not orders:
        await update.message.reply_text("没有可取消的订单。")
        return
    buttons = [[InlineKeyboardButton(f"取消 {o.get('summary') or '订单'} (#{o['order_id'][-6:]})",
                                     callback_data=f"cancel:{o['order_id']}")] for o in orders]
    await update.message.reply_text(
        "选择要取消的订单（仅显示通过本助手下的单；点击后会再确认一次）：",
        reply_markup=InlineKeyboardMarkup(buttons))


async def on_cancel_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """第一步：选中某单 → 展示详情 + 二次确认按钮（取消真实订单的护栏）。"""
    q = update.callback_query
    await q.answer()
    rec = _require_token(q.from_user.id)
    if not rec:
        await q.edit_message_text("请先登录后再操作。")
        return
    order_id = q.data.split(":", 1)[1]
    try:
        detail = await asyncio.wait_for(
            MCP.call_tool(rec.token, "queryOrderDetailInfo", {"orderId": order_id}), timeout=15)
        brief = flows.order_brief(detail)
    except Exception as e:
        log.warning("cancel-select query %s failed: %s", order_id, e)
        brief = "（状态查询失败）"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ 确认取消", callback_data=f"cxldo:{order_id}"),
        InlineKeyboardButton("返回", callback_data="cxlno"),
    ]])
    await q.edit_message_text(f"确认取消这笔订单？\n#{order_id[-6:]} — {brief}\n（已支付/制作中可能无法取消）",
                              reply_markup=kb)


async def on_cancel_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """第二步：用户确认后才真正调用 cancelOrder。"""
    q = update.callback_query
    await q.answer()
    rec = _require_token(q.from_user.id)
    if not rec:
        await q.edit_message_text("请先登录后再操作。")
        return
    order_id = q.data.split(":", 1)[1]
    try:
        result = await MCP.call_tool(rec.token, "cancelOrder", {"orderId": order_id})
    except Exception as e:
        log.warning("cancelOrder %s failed: %s", order_id, e)
        await q.edit_message_text("取消失败，请稍后重试。")
        return
    if flows.cancel_succeeded(result):
        db.mark_order_cancelled(q.from_user.id, order_id)
        await q.edit_message_text("✅ 已取消订单。")
    else:
        await q.edit_message_text("取消失败：" + flows.cancel_message(result))


async def on_cancel_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("已返回，未取消。")


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    # Telegram 原生定位是 WGS-84，瑞幸/高德按 GCJ-02 检索门店 → 必须转换，否则偏 100~500m
    coords = wgs84_to_gcj02(loc.longitude, loc.latitude)
    context.user_data["location"] = coords
    prefs_data = db.get_prefs(update.effective_user.id) if get_settings().prefs_enabled else None
    context.user_data["messages"] = AGENT.new_conversation(coords, prefs_data)
    db.set_location(update.effective_user.id, coords[0], coords[1], "我的位置")  # 落库，重启不丢
    await update.message.reply_text("📍 位置已记录，想喝点什么？", reply_markup=ReplyKeyboardRemove())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if db.touch_user(uid, "tg", update.effective_user.full_name):
        context.application.create_task(push.notify_owner(
            f"🆕 新用户(TG)：{update.effective_user.full_name or uid}（{uid}）"))
        await cmd_start(update, context)  # 新用户首触(非 /start) → 引导，不当点单处理
        return
    if not admin.is_owner_tg(uid):  # owner 不限频/不封禁（你管着预算，自己别被卡）
        reason = db.gate_message(uid, db.today_cst())  # 封禁 / 每日次数上限（护 API 预算）
        if reason:
            await update.message.reply_text(reason)
            return
    await _handle_text(update, context, (update.message.text or "").strip())


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """语音消息 → 转写 → 回显 → 当作文字走同一套点单逻辑（确认护栏不变）。"""
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    uid = update.effective_user.id
    if db.touch_user(uid, "tg", update.effective_user.full_name):
        context.application.create_task(push.notify_owner(
            f"🆕 新用户(TG)：{update.effective_user.full_name or uid}（{uid}）"))
        await cmd_start(update, context)  # 新用户首触(非 /start) → 引导，不当点单处理
        return
    if not admin.is_owner_tg(uid):  # owner 不限频
        reason = db.gate_message(uid, db.today_cst())  # 闸在转写前，过限不花 ASR
        if reason:
            await update.message.reply_text(reason)
            return
    if not asr.asr_enabled():
        await update.message.reply_text("语音功能还没开启（需配置云 ASR），先打字告诉我吧～")
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        f = await context.bot.get_file(voice.file_id)
        audio = bytes(await f.download_as_bytearray())
        text = await asr.transcribe(audio)
    except Exception as e:
        log.warning("voice transcribe failed: %s", e)
        await update.message.reply_text("没听清，要不打字告诉我？")
        return
    if not text:
        await update.message.reply_text("没听清，要不打字告诉我？")
        return
    await update.message.reply_text(f"🎧 听到：{text}")
    await _handle_text(update, context, text)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text0: str) -> None:
    if text0 in ("福利", "领券", "免费券", "领福利"):
        await cmd_coupon(update, context)
        return
    if text0 in ("定位", "位置", "改位置", "重新定位"):
        await cmd_here(update, context)
        return
    if text0 in ("我的偏好", "偏好"):  # 查看偏好免登录（与 /prefs 同源）
        await cmd_prefs(update, context)
        return
    rec = _require_token(update.effective_user.id)
    if not rec:
        kb = _login_kb(update.effective_user.id, update.effective_chat.id)
        await update.message.reply_text(
            "请先登录瑞幸账号👇（也可 `/login <Token>` 粘贴登录）。" if kb
            else "请先登录：/login <你的瑞幸Token>。",
            reply_markup=kb)
        return
    # 待确认订单是模态的：先点确认/取消按钮，别让新文字覆盖 pending（与微信侧对齐，防旧按钮触发新单）
    if context.user_data.get("pending"):
        await update.message.reply_text("有一笔待确认的订单，请先点上方『✅ 确认 / ❌ 取消』按钮，再继续～")
        return
    if text0 in ("老样子", "再来一杯", "再来一份"):  # 复购需登录，故放在 token 门之后
        await cmd_reorder(update, context)
        return
    if text0 in ("常买", "我的常买"):
        await cmd_usual(update, context)
        return
    uid = update.effective_user.id
    # 内存没有位置时，回落到上次落库的位置（重启/换会话也不用重设）
    loc = context.user_data.get("location")
    if not loc:
        saved = db.get_location(uid)
        if saved:
            loc = (saved["lng"], saved["lat"])
            context.user_data["location"] = loc
        # 无位置不再强弹定位：消息可能自带地点(交给 agent geocode)；agent 需要时会按提示词请用户 /here
    prefs_data = db.get_prefs(uid) if get_settings().prefs_enabled else None
    messages = context.user_data.get("messages")
    if messages is None:
        messages = AGENT.new_conversation(loc, prefs_data)
    else:
        AGENT.refresh_system(messages, loc, prefs_data)  # 就地刷新偏好/位置，保留对话尾
    context.user_data["messages"] = messages
    messages.append({"role": "user", "content": text0})
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        result = await AGENT.step(messages, rec.token, user_key=uid)
    except Exception as e:
        log.exception("agent step failed")
        context.user_data.pop("messages", None)  # 自愈：清掉可能损坏的会话状态
        context.user_data.pop("pending", None)
        context.application.create_task(push.notify_owner(
            f"🐞 TG 点单出错（user {update.effective_user.id}）：{str(e)[:300]}"))
        await update.message.reply_text("出错了，已重置当前对话，请再说一次～")
        return
    context.user_data["messages"] = result.messages

    if result.kind == "text":
        await update.message.reply_text(result.text or "（没听懂，换个说法试试？）")
        return

    # createOrder 拦截 → 价格确认护栏
    text, price = flows.format_preview(result.preview)
    if price is None:  # 拿不到价格 → 不出 ¥0 确认按钮（防绕过 spend_guard）
        await update.message.reply_text("没拿到这单的价格，麻烦再说一次或换个说法～")
        return
    reason = flows.spend_guard(update.effective_user.id, price)
    if reason:
        res2 = await AGENT.resume_after_confirm(result.messages, result.pending_call, rec.token,
                                                approved=False, exec_result={"rejected": reason}, user_key=uid)
        context.user_data["messages"] = res2.messages
        await update.message.reply_text("⛔ " + reason)
        if res2.text:
            await update.message.reply_text(res2.text)
        return
    context.user_data["pending"] = {"call": result.pending_call, "price": price,
                                    "summary": flows.preview_summary(result.preview)}
    await update.message.reply_text(text + "\n\n确认下单吗？",
                                    reply_markup=ui.confirm_order_keyboard(price or 0.0))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    pending = context.user_data.get("pending")
    messages = context.user_data.get("messages")
    rec = _require_token(uid)
    is_reorder = bool(pending and pending.get("reorder"))
    # 复购无对话锚点，不需要 messages；普通单需要 messages 才能续聊
    if not pending or not rec or (messages is None and not is_reorder):
        await q.edit_message_text("会话已过期，请重新点单。")
        return

    if q.data == "order:cancel":
        context.user_data.pop("pending", None)
        if is_reorder:  # 复购取消：无续聊（否则追加孤儿 tool 消息→400）
            await q.edit_message_text("已取消本次下单。")
            return
        res = await AGENT.resume_after_confirm(messages, pending["call"], rec.token, approved=False, user_key=uid)
        context.user_data["messages"] = res.messages
        await q.edit_message_text("已取消本次下单。")
        if res.text:
            await q.message.reply_text(res.text)
        return

    # 确认 → 执行 createOrder（我们自己执行以拿到二维码并记账）
    await q.edit_message_text("⏳ 正在为你下单…")
    create_result = await AGENT.execute_pending(rec.token, pending["call"], user_key=uid)
    text, qr, order_id, need_pay, pay_page = flows.format_order_created(create_result)

    # 价格一致性兜底：实际下单价高于确认价（如优惠未生效）
    confirmed = pending.get("price")
    actual = flows.created_price(create_result)
    over_limit = None
    if confirmed is not None and actual is not None and actual > confirmed + 0.01:
        await q.message.reply_text(
            f"⚠️ 注意：实际下单金额 ¥{actual:.2f} 高于确认价 ¥{confirmed:.2f}（优惠可能未生效）。")
        over_limit = flows.spend_guard(uid, actual)  # 用实付价重核单日上限
    record_price = actual if actual is not None else confirmed
    _pl = flows.reorder_payload_from_call(pending["call"]) if order_id else None

    if over_limit and order_id:  # 超额 → 尝试自动取消（与微信侧对齐），赶在扫码前
        cancelled = False
        try:
            cxl = await MCP.call_tool(rec.token, "cancelOrder", {"orderId": order_id})
            cancelled = flows.cancel_succeeded(cxl)
        except Exception as e:
            log.warning("auto-cancel over-limit order %s failed: %s", order_id, e)
        # 无论取消是否成功都落库，确保订单可在 /orders /cancel 看到、可追溯（修评审 MEDIUM）
        db.record_order(uid, order_id, pending.get("summary"), product_list=_pl["product_list"],
                        dept_id=_pl["dept_id"], lng=_pl["lng"], lat=_pl["lat"])
        context.user_data.pop("pending", None)
        context.user_data.pop("messages", None)  # 重置会话，避免"已取消"与续聊矛盾
        if cancelled:
            db.mark_order_cancelled(uid, order_id)
            await q.message.reply_text(f"⛔ 实付超出单日上限（{over_limit}），已自动取消该订单，未扣款。")
        else:  # 取消未确认成功：计入当日额度，提示手动取消，不谎称"未扣款"
            if record_price:
                db.record_spend(uid, db.today_cst(), record_price, order_id)
            await q.message.reply_text(
                f"⛔ 实付超出单日上限（{over_limit}），但自动取消未成功。"
                f"请在瑞幸 App 手动取消该订单（#{order_id[-6:]}），未支付请勿付款。")
        return

    if order_id:
        db.record_order(uid, order_id, pending.get("summary"), product_list=_pl["product_list"],
                        dept_id=_pl["dept_id"], lng=_pl["lng"], lat=_pl["lat"])
        # 已被券/余额全额支付(needPay=false)立即记账；需支付的单留给轮询在确认到账后记
        if not need_pay and record_price:
            db.record_spend(uid, db.today_cst(), record_price, order_id)

    context.user_data.pop("pending", None)
    await q.message.reply_text(text)
    if need_pay and qr:
        page_kb = (InlineKeyboardMarkup([[InlineKeyboardButton("📱 同屏无法扫码？点此打开支付页", url=pay_page)]])
                   if pay_page else None)
        await context.bot.send_photo(q.message.chat_id, ui.make_qr_png(qr),
                                     caption="微信扫码直接支付", reply_markup=page_kb)

    if not is_reorder:  # 复购无对话锚点，跳过续聊
        res = await AGENT.resume_after_confirm(messages, pending["call"], rec.token,
                                               approved=True, exec_result=create_result, user_key=uid)
        context.user_data["messages"] = res.messages
        if res.text:
            await q.message.reply_text(res.text)

    sug = prefs_mod.suggest_usual(uid)  # 隐式学习建议（默认关）
    if sug:
        await q.message.reply_text(sug)

    if order_id:
        spend_kwargs = {}
        if need_pay and record_price:
            spend_kwargs = {"spend_user_id": q.from_user.id, "spend_amount": record_price}
        context.application.create_task(
            flows.poll_order_until_ready(context.bot, q.message.chat_id, MCP, rec.token, order_id, **spend_kwargs)
        )


async def _post_init(app: Application) -> None:
    db.init_db()
    log.info("DB ready; bot started.")


def build_app() -> Application:
    s = get_settings()
    if not s.bot_token:
        raise SystemExit("BOT_TOKEN 未配置（.env）")
    app = ApplicationBuilder().token(s.bot_token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("coupon", cmd_coupon))
    app.add_handler(CommandHandler("here", cmd_here))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("prefs", cmd_prefs))
    app.add_handler(CommandHandler("reorder", cmd_reorder))
    app.add_handler(CommandHandler("usual", cmd_usual))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(on_prefs_cb, pattern=r"^prefs:"))
    app.add_handler(CallbackQueryHandler(on_reorder_select, pattern=r"^reorder:"))
    app.add_handler(CallbackQueryHandler(on_cancel_select, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(on_cancel_do, pattern=r"^cxldo:"))
    app.add_handler(CallbackQueryHandler(on_cancel_abort, pattern=r"^cxlno$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app


def main() -> None:
    build_app().run_polling()


if __name__ == "__main__":
    main()
