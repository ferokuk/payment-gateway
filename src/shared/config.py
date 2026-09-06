from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = Field(description="Database URL")
    app_port: int = Field(default=8000, description="Application port")
    app_env: str = Field(default="local", description="Application environment")
    is_debug: bool = Field(default=True, description="Whether debug mode is enabled")
    api_key: str | None = Field(
        default=None,
        repr=False,
        description="Deprecated compatibility setting; never used for authentication",
    )
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
        description="Age at which a CREATED/PENDING/ERROR refund counts as stuck",
    )
    # Must be shorter than the actual provider's guaranteed deduplication window.
    refund_initiation_max_age_seconds: float = Field(
        default=72000.0,
        gt=0,
        validation_alias=AliasChoices(
            "refund_initiation_max_age_seconds", "reconcile_give_up_after_seconds"
        ),
        description="Maximum refund age for initiation by the API or reconciler",
    )
    reconcile_batch_size: int = Field(
        default=100, gt=0, description="Stuck refunds handled per reconciliation pass"
    )
    reconcile_retry_after_seconds: float = Field(
        default=60.0, gt=0, description="Initial delay before checking an unresolved refund again"
    )
    reconcile_retry_max_seconds: float = Field(
        default=3600.0, gt=0, description="Maximum exponential backoff for refund status checks"
    )

    @model_validator(mode="after")
    def validate_reconciliation_backoff(self) -> Settings:
        if self.reconcile_retry_max_seconds < self.reconcile_retry_after_seconds:
            raise ValueError("reconcile_retry_max_seconds must be >= reconcile_retry_after_seconds")
        return self


settings = Settings()
