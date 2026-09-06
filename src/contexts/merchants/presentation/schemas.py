from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class MerchantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @field_validator("name", "provider_name", mode="before", check_fields=False)
    @classmethod
    def strip_labels(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RegisterMerchantRequest(MerchantRequest):
    email: EmailStr = Field(max_length=320)
    name: str = Field(min_length=1, max_length=255)
    provider_name: str = Field(min_length=1, max_length=100)
    provider_secret_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    password: SecretStr = Field(min_length=12, max_length=128, repr=False)


class LoginRequest(MerchantRequest):
    email: EmailStr = Field(max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128, repr=False)


class PatchProfileRequest(MerchantRequest):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    webhook_url: HttpUrl | None = None
    retry_max_attempts: int | None = Field(default=None, ge=1, le=100)
    retry_window_seconds: int | None = Field(default=None, ge=1, le=86400)
    provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    provider_secret_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )

    @model_validator(mode="after")
    def validate_changes(self) -> PatchProfileRequest:
        if not self.model_fields_set:
            raise ValueError("At least one profile change is required")
        for field in self.model_fields_set - {"webhook_url"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PatchSecretsRequest(MerchantRequest):
    rotate_api_key: bool = False
    provider_secret_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )
    provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128, repr=False)

    @model_validator(mode="after")
    def validate_changes(self) -> PatchSecretsRequest:
        if not (self.rotate_api_key or self.provider_secret_key is not None or self.password):
            raise ValueError("At least one secret change is required")
        if self.provider_name is not None and self.provider_secret_key is None:
            raise ValueError("Changing provider requires provider_secret_key")
        return self


class AuthenticateAPIKeyRequest(MerchantRequest):
    api_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)


class RetryPolicyResponse(BaseModel):
    max_attempts: int
    window_seconds: int


class PreviousAPIKeyResponse(BaseModel):
    api_key_id: UUID
    expires_at: datetime


class ProfileResponse(BaseModel):
    merchant_id: UUID
    email: str
    name: str
    provider_name: str | None
    webhook_url: str | None
    retry_policy: RetryPolicyResponse
    api_key: str | None = Field(repr=False)
    api_key_id: UUID | None
    previous_api_keys: list[PreviousAPIKeyResponse]
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: Literal["bearer"] = Field(default="bearer")
    expires_in: int


class MerchantIdentityResponse(BaseModel):
    merchant_id: UUID
    api_key_id: UUID


class ProviderCredentialsResponse(BaseModel):
    provider_name: str
    secret_key: str = Field(repr=False)
    valid_until: datetime | None


class MerchantConfigurationResponse(BaseModel):
    merchant_id: UUID
    is_active: bool
    provider_name: str | None
    credentials: list[ProviderCredentialsResponse]
    webhook_url: str | None
    retry_policy: RetryPolicyResponse
