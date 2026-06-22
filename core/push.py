"""主动回推：把一条消息从后端（web 登录页 / 定位页）推回用户所在渠道。

用途：手机号登录成功、网页定位成功等"后端完成、聊天窗口却无反馈"的场景，
由发起回推到来源渠道（见 login_nonce.channel/push_target）。

- Telegram：直接调 Bot API sendMessage（chat_id = push_target）。自包含。
- 微信：POST 到 bridge 暴露的入站 /push 端点（阶段1 增加）；未配置则优雅跳过。
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from core.config import get_settings

log = logging.getLogger("push")


async def _push_telegram(chat_id: str, text: str) -> bool:
    token = get_settings().bot_token
    if not token:
        log.warning("push tg skipped: BOT_TOKEN 未配置")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json={"chat_id": chat_id, "text": text})
        if r.status_code == 200 and r.json().get("ok"):
            return True
        log.warning("push tg failed: HTTP %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("push tg error: %s", e)
    return False


async def _push_wechat(user_key: str, text: str) -> bool:
    s = get_settings()
    if not s.wechat_push_url:
        log.info("push wx skipped: WECHAT_PUSH_URL 未配置（bridge 入站端点）")
        return False
    headers = {"content-type": "application/json"}
    if s.bridge_secret:
        headers["x-bridge-secret"] = s.bridge_secret
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(s.wechat_push_url.rstrip("/") + "/push",
                             headers=headers, json={"user_key": user_key, "text": text})
        if r.status_code == 200:
            return True
        log.warning("push wx failed: HTTP %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("push wx error: %s", e)
    return False


async def notify_owner(text: str) -> None:
    """把告警推给 owner（配了哪个渠道就推哪个，可同时 TG+微信）。失败只记日志、不抛。"""
    s = get_settings()
    if s.owner_tg_id:
        await _push_telegram(str(s.owner_tg_id), text)
    if s.owner_wx_key:
        await _push_wechat(s.owner_wx_key, text)


async def push_to_channel(channel: Optional[str], push_target: Optional[str], text: str) -> bool:
    """按 nonce 记录的渠道把 text 推给用户。无渠道信息（老链接）则跳过。"""
    if not channel or not push_target:
        return False
    if channel == "tg":
        return await _push_telegram(push_target, text)
    if channel == "wx":
        return await _push_wechat(push_target, text)
    log.warning("push: 未知渠道 %r", channel)
    return False
