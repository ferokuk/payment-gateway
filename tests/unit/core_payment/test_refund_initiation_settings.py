import pytest
from pydantic import ValidationError
from src.shared.config import Settings


@pytest.mark.parametrize(
    "name",
    [
        "REFUND_INITIATION_MAX_AGE_SECONDS",
        "RECONCILE_GIVE_UP_AFTER_SECONDS",
    ],
)
def test_common_initiation_window_accepts_new_and_legacy_env_names(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.delenv("REFUND_INITIATION_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("RECONCILE_GIVE_UP_AFTER_SECONDS", raising=False)
    monkeypatch.setenv(name, "3600")
    assert Settings(_env_file=None).refund_initiation_max_age_seconds == 3600


def test_new_initiation_window_env_name_has_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFUND_INITIATION_MAX_AGE_SECONDS", "3600")
    monkeypatch.setenv("RECONCILE_GIVE_UP_AFTER_SECONDS", "72000")
    assert Settings(_env_file=None).refund_initiation_max_age_seconds == 3600


@pytest.mark.parametrize("seconds", [0, -1])
def test_initiation_window_must_be_positive(seconds: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, refund_initiation_max_age_seconds=seconds)
