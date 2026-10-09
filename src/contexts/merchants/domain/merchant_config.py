from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MerchantConfig:
    provider_name: str
    webhook_url: str | None = None
    retry_max_attempts: int = 3
    retry_window_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.provider_name.strip() or len(self.provider_name) > 100:
            raise ValueError("Provider name must contain between 1 and 100 characters")
        if type(self.retry_max_attempts) is not int or not 1 <= self.retry_max_attempts <= 100:
            raise ValueError("Retry maximum attempts must be between 1 and 100")
        if (
            type(self.retry_window_seconds) is not int
            or not 1 <= self.retry_window_seconds <= 86400
        ):
            raise ValueError("Retry window must be between 1 and 86400 seconds")
        if self.webhook_url is not None:
            if len(self.webhook_url) > 2048:
                raise ValueError("Webhook URL must contain no more than 2048 characters")
            parsed = urlsplit(self.webhook_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(character.isspace() for character in self.webhook_url)
            ):
                raise ValueError("Webhook URL must be an HTTP or HTTPS URL without credentials")
