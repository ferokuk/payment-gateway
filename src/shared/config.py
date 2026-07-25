from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = Field(description="Database URL")
    app_port: int = Field(default=8000, description="Application port")
    app_env: str = Field(default="local", description="Application environment")
    is_debug: bool = Field(default=True, description="Whether debug mode is enabled")
    api_key: str = Field(description="Merchant API key (X-API-Key header)")
    fake_provider_mode: Literal["manual", "auto"] = Field(
        default="manual", description="Fake provider mode"
    )
    callback_secret: str = Field(description="Provider callback secret (X-Callback-Secret header)")
    self_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the service — the auto provider sends callbacks to it",
    )
    # gt=0: with zero delay a callback can outrun the Txn2 commit and get a 409
    # on the transition from CREATED — the payment would be stuck in PENDING forever.
    fake_callback_delay_seconds: float = Field(
        default=1.0, gt=0, description="Delay before each callback of the auto provider"
    )
    reconcile_interval_seconds: float = Field(
        default=60.0, gt=0, description="Pause between reconciliation passes"
    )
    reconcile_stuck_after_seconds: float = Field(
        default=900.0,
        gt=0,
        description="Age at which a refund still in created counts as stuck",
    )
    # 20 hours: safely inside the ~24 hours a PSP keeps an idempotency key.
    # Past that a repeat is no longer deduplicated and would refund twice.
    reconcile_give_up_after_seconds: float = Field(
        default=72000.0,
        gt=0,
        description="Age past which a stuck refund is only reported, never re-initiated",
    )
    reconcile_batch_size: int = Field(
        default=100, gt=0, description="Stuck refunds handled per reconciliation pass"
    )


settings = Settings()
