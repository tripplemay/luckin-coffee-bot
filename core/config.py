"""集中配置：从 .env 读取，schema 校验。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    public_base_url: str = ""
    llm_model: str = "deepseek-v3"
    aigc_base_url: str = "https://aigc.guangai.ai/v1"
    aigc_api_key: str = ""
    luckin_env: str = "prod"  # prod | test03 | pre
    fernet_key: str = ""
    db_path: str = "coffee.db"
    daily_spend_limit: float = 100.0
    bridge_secret: str = ""  # 渠道服务 /message 的共享密钥（微信桥接用），留空则不校验


@lru_cache
def get_settings() -> Settings:
    return Settings()
