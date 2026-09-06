"""Local operator interface. Database credentials grant administration authority."""

import argparse
import asyncio
import getpass
import warnings
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from src.contexts.merchants.infrastructure.database.repositories import SQLAlchemyMerchantRepository
from src.shared.config import settings
from src.shared.database.engine import create_engine, create_sessionmaker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage merchants and opaque API credentials")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create an active merchant")
    create.add_argument("--name", required=True)
    issue = commands.add_parser("issue-key", help="Issue a key; stdout contains the secret once")
    issue.add_argument("merchant_id", type=UUID)
    issue.add_argument("--label", required=True)
    issue.add_argument("--expires-at", type=datetime.fromisoformat)
    legacy = commands.add_parser(
        "import-legacy-key", help="Import the old key using a hidden prompt"
    )
    legacy.add_argument("--label", default="Legacy migration")
    legacy.add_argument("--expires-at", type=datetime.fromisoformat)
    revoke = commands.add_parser("revoke-key", help="Irreversibly revoke a key")
    revoke.add_argument("key_id", type=UUID)
    for command in ("activate", "deactivate"):
        activation = commands.add_parser(command)
        activation.add_argument("merchant_id", type=UUID)
    return parser


async def run(args: argparse.Namespace) -> str:
    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            repository = SQLAlchemyMerchantRepository(session)
            if args.command == "create":
                result = str((await repository.create(args.name)).id)
            elif args.command == "issue-key":
                issued = await repository.issue_key(args.merchant_id, args.label, args.expires_at)
                result = issued.token
            elif args.command == "import-legacy-key":
                # getpass normally falls back to an echoed stdin prompt. Fail closed.
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    token = getpass.getpass("Previous API key: ")
                key = await repository.import_legacy_key(token, args.label, args.expires_at)
                result = str(key.id)
            elif args.command == "revoke-key":
                await repository.revoke_key(args.key_id)
                result = "API key revoked"
            else:
                await repository.set_active(args.merchant_id, active=args.command == "activate")
                result = f"Merchant {'activated' if args.command == 'activate' else 'deactivated'}"
            await session.commit()
            return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    except SQLAlchemyError:
        parser.exit(1, "Database operation failed; no credential was returned.\n")
    except getpass.GetPassWarning, EOFError:
        parser.exit(1, "A terminal supporting hidden input is required for legacy import.\n")
    else:
        print(result)


if __name__ == "__main__":
    main()
