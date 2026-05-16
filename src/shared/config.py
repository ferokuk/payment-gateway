from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = Field(description="Database URL")
    app_port: int = Field(default=8000, description="Порт приложения")
    app_env: str = Field(default="local", description="Окружение приложения")
    is_debug: bool = Field(default=True, description="Включен ли дебаг")
    api_key: str = Field(description="API-ключ мерчанта (заголовок X-API-Key)")


settings = Settings()
