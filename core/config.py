"""集中配置：从 .env 读取，schema 校验。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    public_base_url: str = ""
    # ---- 多用户：owner（/admin + 告警）+ 每用户限额/限频 ----
    owner_tg_id: int = 0          # Telegram owner 数字 id（0=未配置，则 /admin 关闭）
    owner_wx_key: str = ""        # 微信 owner 的原始 user_key（from_user_id）
    bot_username: str = ""        # Telegram bot 用户名（不含 @），落地页深链用，如 tripplecoffeebot
    daily_msg_limit: int = 50     # 每用户每日"动用 LLM 的消息/语音"上限，防 API 预算被滥用
    history_max_msgs: int = 24    # 对话历史保留的最大消息条数（不含 system），控上下文成本
    agent_max_iters: int = 20     # agent function-calling 单步最大轮数（复杂多杯定制单需要更多）
    llm_model: str = "deepseek-v3"
    aigc_base_url: str = "https://aigc.guangai.ai/v1"
    aigc_api_key: str = ""
    luckin_env: str = "prod"  # prod | test03 | pre
    fernet_key: str = ""
    db_path: str = "coffee.db"
    daily_spend_limit: float = 100.0
    bridge_secret: str = ""  # 渠道服务 /message 的共享密钥（微信桥接用），留空则不校验
    amap_key: str = ""       # 高德 Web 服务 key，用于「地址→GCJ-02 坐标」地理编码
    wechat_push_url: str = ""  # 微信 bridge 入站推送端点基址（如 http://127.0.0.1:8300），用于登录/定位成功回推
    # ---- 语音转写（云 ASR；网关无 ASR 模态，必须外接）----
    asr_provider: str = ""    # "" 关闭 | dashscope(阿里) | tencent(腾讯) | iflytek(讯飞)
    asr_api_key: str = ""     # 单 key 厂商（阿里 DashScope）
    asr_app_id: str = ""      # 讯飞 APPID / 腾讯 SecretId
    asr_api_secret: str = ""  # 讯飞 APISecret / 腾讯 SecretKey
    # ---- 用户偏好 + 老样子复购 ----
    prefs_enabled: bool = True          # 总开关：偏好读写 + 注入提示词
    prefs_max_items: int = 20           # 偏好注入提示词的条目上限（控上下文成本）
    implicit_learning_enabled: bool = False  # 隐式学习：某商品点≥N 次→建议设为常买（默认关）


@lru_cache
def get_settings() -> Settings:
    return Settings()


def login_base_url() -> str:
    """登录页公网 URL：优先读 cloudflared 写入的 web/.public_url，回退 .env 的 PUBLIC_BASE_URL。"""
    for p in ("web/.public_url", "/opt/coffee-bot/web/.public_url"):
        try:
            u = open(p, encoding="utf-8").read().strip()
            if u:
                return u.rstrip("/")
        except OSError:
            continue
    return get_settings().public_base_url.rstrip("/")
