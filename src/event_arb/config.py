"""Application configuration.

Settings come from environment variables (prefix ``EVENT_ARB_``) and an optional
``.env`` file. Nested sections use ``__`` as the delimiter, e.g.
``EVENT_ARB_OPPORTUNITY__MIN_NET_PROFIT_USD``.

Safety invariant: :attr:`Settings.trading_enabled` defaults to ``False`` and, in this
build, cannot be made effective — Phase 7 is not implemented and the execution adapter
refuses unconditionally.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "AlertSettings",
    "DataSource",
    "KalshiEnvironment",
    "KalshiSettings",
    "OpportunitySettings",
    "PaperSettings",
    "PolymarketSettings",
    "Settings",
    "get_settings",
]


class DataSource(StrEnum):
    FIXTURE = "fixture"
    LIVE = "live"


class KalshiEnvironment(StrEnum):
    DEMO = "demo"
    PROD = "prod"


#: Verified 2026-08-21 against docs.kalshi.com/getting_started/api_environments.
KALSHI_REST_URLS: dict[KalshiEnvironment, str] = {
    KalshiEnvironment.PROD: "https://external-api.kalshi.com/trade-api/v2",
    KalshiEnvironment.DEMO: "https://external-api.demo.kalshi.co/trade-api/v2",
}
KALSHI_WS_URLS: dict[KalshiEnvironment, str] = {
    KalshiEnvironment.PROD: "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    KalshiEnvironment.DEMO: "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}


class KalshiSettings(BaseModel):
    environment: KalshiEnvironment = KalshiEnvironment.DEMO
    api_key_id: str = ""
    #: Path to an RSA private key file. The key material is never read into config.
    private_key_path: Path | None = None
    requests_per_second: float = 5.0
    #: Kalshi balance precision. Non-direct members settle to whole cents.
    direct_member: bool = False

    @property
    def rest_base_url(self) -> str:
        return KALSHI_REST_URLS[self.environment]

    @property
    def ws_url(self) -> str:
        return KALSHI_WS_URLS[self.environment]

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id) and self.private_key_path is not None


class PolymarketSettings(BaseModel):
    clob_base_url: str = "https://clob.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    requests_per_second: float = 5.0
    #: Haircut applied to USDC-denominated proceeds to price depeg/conversion risk.
    usdc_haircut_pct: Decimal = Decimal("0.10")


class OpportunitySettings(BaseModel):
    min_net_profit_usd: Decimal = Decimal("0.50")
    min_return_pct: Decimal = Decimal("0.5")
    max_book_age_ms: int = 2000
    slippage_buffer_pct: Decimal = Decimal("0.5")
    operational_cost_usd: Decimal = Decimal("0.10")
    max_quantity: Decimal = Decimal("100")
    alert_cooldown_seconds: int = 300
    #: Emit research candidates for unapproved pairs (never labeled arbitrage).
    include_research_candidates: bool = True

    @field_validator("max_book_age_ms")
    @classmethod
    def _positive_age(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_book_age_ms must be positive")
        return v


class PaperSettings(BaseModel):
    leg1_latency_ms: int = 250
    leg2_latency_ms: int = 400
    #: Fraction of the book at each level assumed to vanish before we arrive.
    adverse_fill_pct: Decimal = Decimal("0")
    starting_usd_balance: Decimal = Decimal("10000")
    starting_usdc_balance: Decimal = Decimal("10000")


class AlertSettings(BaseModel):
    console_enabled: bool = True
    webhook_url: str = ""
    webhook_timeout_seconds: float = 5.0
    #: Kalshi Developer Agreement §3.1 forbids sharing API-accessed data with third
    #: parties without prior written authorization. A webhook endpoint is a third party.
    #: Leave false unless you hold that authorization; alerts then omit venue market
    #: identifiers, prices, and sizes. See docs/compliance.md.
    allow_third_party_market_data: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EVENT_ARB_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["local", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    database_url: str = "sqlite+aiosqlite:///./data/event_arb.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    data_source: DataSource = DataSource.FIXTURE
    fixture_dir: Path = Path("tests/fixtures")

    kalshi: KalshiSettings = Field(default_factory=KalshiSettings)
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    opportunity: OpportunitySettings = Field(default_factory=OpportunitySettings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)

    # ---------------------------------------------------------------- live trading
    #: MUST remain False. Phase 7 is not implemented in this build.
    trading_enabled: bool = False
    #: Second factor required alongside ``trading_enabled``; still insufficient here.
    trading_confirmation_phrase: SecretStr = SecretStr("")

    @property
    def live_trading_possible(self) -> bool:
        """Always ``False`` in this build.

        Even with ``trading_enabled=true`` set, no execution adapter exists that can
        submit an order. This property documents and enforces that fact.
        """
        return False


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
