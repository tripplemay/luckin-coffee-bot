"""Telegram 键盘/按钮/二维码 构造。"""
from __future__ import annotations

from io import BytesIO
from typing import Optional

import qrcode
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def login_keyboard(base_url: str, nonce: str) -> Optional[InlineKeyboardMarkup]:
    """手机号登录按钮：指向带一次性 nonce 的登录页（手机号+短信，免粘贴 Token）。

    nonce 是绑定凭证——没有它登录页无法把 token 回写到本用户，所以这里必须带上。
    base_url 为空（未配置登录页）则返回 None。
    """
    if not base_url:
        return None
    url = base_url.rstrip("/") + f"/login?t={nonce}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔑 手机号登录（免粘贴）", url=url)]])


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 发送我的位置", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def confirm_order_keyboard(price: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ 确认支付 ¥{price:.2f}", callback_data="order:confirm"),
        InlineKeyboardButton("❌ 取消", callback_data="order:cancel"),
    ]])


def prefs_keyboard() -> InlineKeyboardMarkup:
    """偏好查看时附带的快捷操作（清空全部）。逐项设置/清除走文本 /prefs set|clear。"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑 清空全部偏好", callback_data="prefs:clearall")]])


def make_qr_png(data: str) -> BytesIO:
    img = qrcode.make(data)
    buf = BytesIO()
    buf.name = "pay_qr.png"
    img.save(buf, format="PNG")
    buf.seek(0)  # 必须 rewind，否则 Telegram 收到 0 字节
    return buf
