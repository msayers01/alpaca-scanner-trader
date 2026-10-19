from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path.cwd() / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    vantage_mode: Literal["sim", "paper", "live"] = "sim"

    alpaca_paper_key: str = ""
    alpaca_paper_secret: str = ""
    alpaca_live_key: str = ""
    alpaca_live_secret: str = ""
    allow_live_trading: bool = False

    alpaca_stock_feed: Literal["iex", "sip"] = "iex"
    alpaca_options_feed: Literal["indicative", "opra"] = "indicative"

    llm_enabled: bool = True
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3.6:35b-a3b"
    llm_timeout_s: float = 45.0
    llm_max_concurrency: int = 2

    finnhub_api_key: str = ""

    # intel sources (all optional; keys live in .env only and are never sent to the UI)
    intel_enabled: bool = True
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = ""
    x_bearer_token: str = ""

    host: str = "127.0.0.1"
    port: int = 8765
    ws_flush_ms: int = 50
    risk_free_rate: float = 0.04

    universe: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX", "JPM", "XLF", "XLE", "SMH", "COIN", "PLTR", "UBER"]
    )
    data_dir: Path = REPO_ROOT / "data"

    @field_validator("universe", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v

    @property
    def paper_keys(self) -> bool:
        return bool(self.alpaca_paper_key and self.alpaca_paper_secret)

    @property
    def live_keys(self) -> bool:
        return bool(self.alpaca_live_key and self.alpaca_live_secret)

    @property
    def live_allowed(self) -> bool:
        return self.allow_live_trading and self.live_keys

    @property
    def intel_env(self) -> dict[str, str]:
        """Credentials handed to the intel collectors (backend only)."""
        return {"REDDIT_CLIENT_ID": self.reddit_client_id, "REDDIT_CLIENT_SECRET": self.reddit_client_secret,
                "REDDIT_USER_AGENT": self.reddit_user_agent, "X_BEARER_TOKEN": self.x_bearer_token}

    def keys_for(self, mode: str) -> tuple[str, str]:
        if mode == "paper":
            return self.alpaca_paper_key, self.alpaca_paper_secret
        if mode == "live":
            return self.alpaca_live_key, self.alpaca_live_secret
        return "", ""


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
