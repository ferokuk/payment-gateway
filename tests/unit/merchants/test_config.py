import pytest
from src.contexts.merchants.domain.merchant_config import MerchantConfig


@pytest.mark.parametrize("attempts", [0, 101, True])
def test_retry_attempts_reject_invalid_policy(attempts: int) -> None:
    with pytest.raises(ValueError, match="Retry maximum attempts"):
        MerchantConfig(provider_name="bank", retry_max_attempts=attempts)


@pytest.mark.parametrize("window", [0, 86401, True])
def test_retry_window_rejects_invalid_policy(window: int) -> None:
    with pytest.raises(ValueError, match="Retry window"):
        MerchantConfig(provider_name="bank", retry_window_seconds=window)


@pytest.mark.parametrize("attempts,window", [(1, 1), (100, 86400)])
def test_retry_policy_accepts_limits(attempts: int, window: int) -> None:
    config = MerchantConfig(
        provider_name="bank", retry_max_attempts=attempts, retry_window_seconds=window
    )
    assert config.retry_max_attempts == attempts
    assert config.retry_window_seconds == window


@pytest.mark.parametrize("provider", ["", " \t", "a" * 101])
def test_provider_name_is_required_and_bounded(provider: str) -> None:
    with pytest.raises(ValueError, match="Provider name"):
        MerchantConfig(provider_name=provider)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://merchant.example/callback",
        "/callback",
        "https:///callback",
        "https://password@merchant.example/callback",
        "https://user:password@merchant.example/callback",
        "https://merchant.example/a b",
        "https://merchant.example/" + "a" * 2048,
    ],
)
def test_webhook_rejects_invalid_or_credential_containing_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Webhook URL"):
        MerchantConfig(provider_name="bank", webhook_url=url)


@pytest.mark.parametrize(
    "url", [None, "https://merchant.example/callback", "http://localhost:8080/callback"]
)
def test_webhook_can_be_disabled_or_set_to_http_endpoint(url: str | None) -> None:
    assert MerchantConfig(provider_name="bank", webhook_url=url).webhook_url == url
