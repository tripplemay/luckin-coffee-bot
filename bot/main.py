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
from datetime import datetime

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
from core import db
from core.config import get_settings, login_base_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

MCP = LuckinMCPClient()
AGENT = OrderingAgent(MCP)


def _require_token(user_id: int):
    return db.get_token(user_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings()
    kb = ui.login_keyboard(s.public_base_url)
    text = (
        "☕ 欢迎使用瑞幸点单助手！\n\n"
        "1) 先登录瑞幸账号"
        + ("（点下方按钮）" if kb else "：把你的 Token 发给我 `/login <token>`")
        + "\n2) 点「📍 发送我的位置」分享定位\n"
        "3) 直接说想喝什么，比如「来杯热的生椰拿铁」\n\n"
        "下单前我会显示价格让你确认，不会乱扣款 👍"
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
    base = login_base_url()  # 无参数 → 手机号登录链接
    if not base:
        await update.message.reply_text("登录页未配置。用法：/login <你的瑞幸Token>")
        return
    nonce = secrets.token_urlsafe(12)
    db.create_login_nonce(nonce, update.effective_user.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔑 手机号登录（免粘贴）", url=f"{base}/login?t={nonce}")]])
    await update.message.reply_text("点下方按钮用手机号登录（填手机号 + 短信验证码）。链接 15 分钟内有效。",
                                    reply_markup=kb)


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
    context.user_data["location"] = (loc.longitude, loc.latitude)
    context.user_data["messages"] = AGENT.new_conversation((loc.longitude, loc.latitude))
    await update.message.reply_text("📍 位置已记录，想喝点什么？", reply_markup=ReplyKeyboardRemove())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rec = _require_token(update.effective_user.id)
    if not rec:
        await update.message.reply_text("请先登录：/login <你的瑞幸Token>（或用 /start 的登录按钮）。")
        return
    messages = context.user_data.get("messages") or AGENT.new_conversation(context.user_data.get("location"))
    messages.append({"role": "user", "content": update.message.text})
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        result = await AGENT.step(messages, rec.token)
    except Exception as e:
        log.exception("agent step failed")
        await update.message.reply_text(f"出错了：{e}")
        return
    context.user_data["messages"] = result.messages

    if result.kind == "text":
        await update.message.reply_text(result.text or "（没听懂，换个说法试试？）")
        return

    # createOrder 拦截 → 价格确认护栏
    text, price = flows.format_preview(result.preview)
    reason = flows.spend_guard(update.effective_user.id, price)
    if reason:
        res2 = await AGENT.resume_after_confirm(result.messages, result.pending_call, rec.token,
                                                approved=False, exec_result={"rejected": reason})
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
    pending = context.user_data.get("pending")
    messages = context.user_data.get("messages")
    rec = _require_token(q.from_user.id)
    if not pending or not rec or messages is None:
        await q.edit_message_text("会话已过期，请重新点单。")
        return

    if q.data == "order:cancel":
        res = await AGENT.resume_after_confirm(messages, pending["call"], rec.token, approved=False)
        context.user_data["messages"] = res.messages
        context.user_data.pop("pending", None)
        await q.edit_message_text("已取消本次下单。")
        if res.text:
            await q.message.reply_text(res.text)
        return

    # 确认 → 执行 createOrder（我们自己执行以拿到二维码并记账）
    await q.edit_message_text("⏳ 正在为你下单…")
    create_result = await AGENT.execute_pending(rec.token, pending["call"])
    text, qr, order_id, need_pay, pay_page = flows.format_order_created(create_result)

    # 价格一致性兜底：实际下单价高于确认价（如优惠未生效）→ 显著告警
    confirmed = pending.get("price")
    actual = flows.created_price(create_result)
    if confirmed is not None and actual is not None and actual > confirmed + 0.01:
        await q.message.reply_text(
            f"⚠️ 注意：实际下单金额 ¥{actual:.2f} 高于确认价 ¥{confirmed:.2f}（优惠可能未生效）。"
            f"\n若还没支付，可在瑞幸 App 取消该订单。")
    record_price = actual if actual is not None else confirmed
    if order_id:
        db.record_order(q.from_user.id, order_id, pending.get("summary"))
        # 已被券/余额全额支付(needPay=false)立即记账；需支付的单留给轮询在确认到账后记
        if not need_pay and record_price:
            db.record_spend(q.from_user.id, datetime.now().strftime("%Y-%m-%d"), record_price, order_id)
    await q.message.reply_text(text)
    if need_pay and qr:
        page_kb = (InlineKeyboardMarkup([[InlineKeyboardButton("📱 同屏无法扫码？点此打开支付页", url=pay_page)]])
                   if pay_page else None)
        await context.bot.send_photo(q.message.chat_id, ui.make_qr_png(qr),
                                     caption="微信扫码直接支付", reply_markup=page_kb)

    res = await AGENT.resume_after_confirm(messages, pending["call"], rec.token,
                                           approved=True, exec_result=create_result)
    context.user_data["messages"] = res.messages
    context.user_data.pop("pending", None)
    if res.text:
        await q.message.reply_text(res.text)

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
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(on_cancel_select, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(on_cancel_do, pattern=r"^cxldo:"))
    app.add_handler(CallbackQueryHandler(on_cancel_abort, pattern=r"^cxlno$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    build_app().run_polling()


if __name__ == "__main__":
    main()
