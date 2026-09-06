import getpass
import warnings
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from src.contexts.merchants import __main__ as cli
from src.contexts.merchants.domain.api_key import issue_api_key
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository


def command_mocks(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, MagicMock]:
    session = AsyncMock(spec=AsyncSession)
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    monkeypatch.setattr(cli, "create_engine", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        cli, "create_sessionmaker", MagicMock(return_value=MagicMock(return_value=session_context))
    )
    repository = MagicMock(spec=SQLAlchemyMerchantRepository)
    monkeypatch.setattr(cli, "SQLAlchemyMerchantRepository", MagicMock(return_value=repository))
    return session, repository


@pytest.mark.anyio
async def test_issuance_returns_secret_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, repository = command_mocks(monkeypatch)
    issued = issue_api_key(uuid4(), "key", datetime.now(UTC))
    repository.issue_key.return_value = issued
    args = cli.build_parser().parse_args(
        ["issue-key", str(issued.key.merchant_id), "--label", "key"]
    )
    result = await cli.run(args)
    session.commit.assert_awaited_once()
    assert result == issued.token
    assert capsys.readouterr().out == ""


def test_failed_commit_does_not_print_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, repository = command_mocks(monkeypatch)
    issued = issue_api_key(uuid4(), "key", datetime.now(UTC))
    repository.issue_key.return_value = issued
    session.commit.side_effect = SQLAlchemyError("commit failed")
    monkeypatch.setattr(
        "sys.argv", ["merchants", "issue-key", str(issued.key.merchant_id), "--label", "key"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert issued.token not in output.err
    assert "Database operation failed" in output.err


def test_successful_issuance_prints_secret_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, repository = command_mocks(monkeypatch)
    issued = issue_api_key(uuid4(), "key", datetime.now(UTC))
    repository.issue_key.return_value = issued
    monkeypatch.setattr(
        "sys.argv", ["merchants", "issue-key", str(issued.key.merchant_id), "--label", "key"]
    )
    cli.main()
    session.commit.assert_awaited_once()
    output = capsys.readouterr()
    assert output.out == issued.token + "\n"
    assert output.err == ""


def test_legacy_import_refuses_getpass_echo_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, repository = command_mocks(monkeypatch)

    def fallback(prompt: str) -> str:
        warnings.warn("Cannot hide input", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Must not read a credential after hidden input fails")

    monkeypatch.setattr(getpass, "getpass", fallback)
    monkeypatch.setattr("sys.argv", ["merchants", "import-legacy-key"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    repository.import_legacy_key.assert_not_awaited()
    session.commit.assert_not_awaited()
    output = capsys.readouterr()
    assert output.out == ""
    assert "hidden input is required" in output.err
