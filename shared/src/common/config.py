from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://signals:signals@127.0.0.1:55432/signals"
    api_token: SecretStr = SecretStr("")
    signal_api_url: str = "http://127.0.0.1:8010"
    codex_bridge_url: str = "http://127.0.0.1:8766"
    codex_bin: str = "codex"
    ai_model: str = "gpt-5.6-terra"
    ai_reasoning_effort: str = "medium"
    ai_timeout_seconds: int = 240
    runtime_dir: Path = Path("runtime")
    tradingview_profile_dir: Path = Path("runtime/tradingview-profile")
    telegram_api_id: int = 0
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_session_path: str = "runtime/telegram/user"
    analysis_bot_token: SecretStr = SecretStr("")
    trading_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: int = 0
    telegram_user_id: int = 0
    binance_demo_api_key: SecretStr = SecretStr("")
    binance_demo_api_secret: SecretStr = SecretStr("")
    trading_enabled: bool = False
    coinglass_api_key: SecretStr = SecretStr("")
    cryptoquant_api_key: SecretStr = SecretStr("")
    nansen_api_key: SecretStr = SecretStr("")
    coinmarketcal_api_key: SecretStr = SecretStr("")
    lunarcrush_api_key: SecretStr = SecretStr("")
    optional_request_interval_seconds: float = 2.1
    nansen_token_map: dict[str, str] = {}


settings = Settings()
