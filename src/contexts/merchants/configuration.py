from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MerchantSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(
        validation_alias=AliasChoices("MERCHANT_DATABASE_URL", "DATABASE_URL")
    )
    encryption_key: SecretStr = Field(validation_alias="MERCHANT_ENCRYPTION_KEY", repr=False)
    service_secret: SecretStr = Field(
        min_length=32, validation_alias="MERCHANT_SERVICE_SECRET", repr=False
    )
    support_secret: SecretStr = Field(
        min_length=32, validation_alias="MERCHANT_SUPPORT_SECRET", repr=False
    )
    session_ttl_seconds: int = Field(
        default=3600, gt=0, le=86400, validation_alias="MERCHANT_SESSION_TTL_SECONDS"
    )
