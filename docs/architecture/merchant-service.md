# Merchant Service

## Requirements and service boundary

The Merchant component in `assets/payment-gateway.svg` owns registration,
account authentication, merchant profiles, API keys, provider credentials,
webhook destinations, retry settings and support-assisted account closure.
This is a backend service; a browser UI is outside this implementation.
Accounts register and log in with email and password. INN and OGRN are not
collected or stored.

`src.contexts.merchants.main:create_app` runs as its own FastAPI process on port
8001. The payment API runs on port 8000 and validates `X-API-Key` through
`POST /internal/authenticate`, using a separate service credential. It never
reads merchant credentials or interprets account bearer tokens. Merchant
unavailability fails closed with 503; invalid payment credentials produce 401.
Callbacks continue to require `X-Callback-Secret`.

The current deployment uses the existing PostgreSQL database and Alembic chain.
Merchant owns its tables; payment and refund rows retain their merchant foreign
keys. This preserves existing financial data and explicit legacy-key imports.
Independent physical databases would require moving these tables and replacing
the cross-service merchant foreign keys; this change does not perform that data
transfer. HTTP authentication is the application boundary between the processes.

## Public API

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/merchants` | Register email, password, name, provider and provider secret; issue API key |
| POST | `/auth/token` | Exchange email/password for an expiring bearer session |
| GET | `/profile` | Read own account, current API key, provider and notification settings |
| PATCH | `/profile` | Change name, webhook, retry policy or provider with its new secret |
| PATCH | `/profile/secrets` | Rotate API key, provider secret or password |
| DELETE | `/profile?merchant_id=UUID` | Support-only soft deletion |

Profile reads and updates use the authenticated bearer identity only. Additional
merchant identifiers in bodies are rejected. Registration email is normalized
case-insensitively and constrained unique in PostgreSQL. Passwords are hashed
with Argon2id outside the event loop. Access tokens have 256 random bits and only
their SHA-256 digests are stored. Password rotation revokes existing sessions.
Requests authenticated before revocation may finish; the next authentication
checks the current database state.

`DELETE /profile` requires `X-Support-Secret`, not a merchant bearer token or an
API key. It records deletion time, disables the merchant and revokes all sessions
and API keys. Financial history and provider credentials remain available to
trusted workers so already accepted operations can settle. Operator activation
cannot restore a deleted account.

## Keys and backward compatibility

Payment API keys contain a public UUID and 256 random secret bits. The key
digest is used for authentication. The current account API key and provider
secrets are also stored encrypted using Fernet because the account must be able
to view its API key and trusted provider adapters need the original provider
secret. The encryption key is configured separately from the database and must
be retained across restarts and backed up with deployment secrets.

Rotating an API key preserves the old credential for exactly 24 hours. Rotating
a provider secret similarly preserves its previous version and provider name
for 24 hours. Rapid successive rotations do not shorten earlier deadlines.
Account row locks serialize rotations and updates; a new key is returned only
after the transaction commits. Immediate operator revocation and account closure
override the compatibility window.

`GET /internal/merchants/{merchant_id}/configuration`, protected by
`X-Service-Secret`, returns notification settings and current/unexpired previous
provider credentials. Public profile responses never reveal provider secrets or
password hashes. Credentials are omitted from repr, validation errors and SQL
parameters in error logs. Responses use `Cache-Control: no-store`.

## Notification and payment boundaries

Webhook settings contain an HTTP(S) destination and a retry policy consisting of
`max_attempts` and `window_seconds`. `null` removes a webhook. Changing a provider
requires its new secret in the same request so unrelated credentials are not
silently reused for another provider.

Merchant stores and exposes these settings. The architecture assigns webhook
delivery to Notification and payment execution to provider adapters; those
components are not implemented by merely saving a profile. Existing fake payment
providers keep their current behavior. No delivery worker, analytics subsystem
or real provider integration is claimed here.

Payments, refunds and both idempotency namespaces remain scoped by immutable
merchant identity. Composite foreign keys prevent cross-merchant operation
links. Replay, refunds, status CAS, balance reservations and reconciliation
preserve their existing monetary invariants. Rotating a credential retains the
merchant's idempotency history. Foreign operation IDs still look absent.

## Migration and rollout

The ownership migration backfills existing financial data to the fixed legacy
merchant. The profile migration adds account/session/provider credential tables
and the soft-deletion marker; existing API keys remain valid and are not
automatically converted into password-based accounts. Legacy credentials are
imported explicitly through the maintenance CLI; `API_KEY` is never an implicit
authentication fallback.

Stop API and workers, back up the database, configure service/support/encryption
secrets, run migrations, then start Merchant before the payment API and workers.
Mixed binaries and migrating while accepting requests are unsupported.

Downgrade of the profile migration refuses account, session, provider credential
or soft-deletion data, because the older schema cannot represent it. The earlier
ownership downgrade retains its checks for non-legacy tenants and credentials.
No migration or provisioning against application data is part of validation.

## Validation

Checks cover registration/login, duplicate email races, profile ownership,
credential redaction/encryption, key and provider rotation deadlines, support
deletion, password/session expiry, the HTTP Merchant-to-payment boundary and
populated/guarded PostgreSQL migrations. Real PostgreSQL establishes constraint,
transaction and concurrency behavior; unit doubles alone do not establish it.

Verified on 2026-09-06 with Python 3.14.6 and disposable PostgreSQL 17:

- Full `pytest -q -p no:cacheprovider`: **509 passed**, no skips.
- Ruff lint and formatting checks; strict mypy for `src`, `tests` and the notebook
  helper; migration metadata checks; Compose configuration validation.
- Includes concurrent registration/rotation, exact compatibility deadlines,
  real Merchant-to-payment HTTP calls and populated migration rollback guards.
- Two standalone HTTP processes passed registration, email/password login,
  profile retrieval, a payment authorized through Merchant, and provider callbacks
  against the disposable database.

Application data was not migrated or provisioned. The interactive notebook was
not executed end-to-end.
