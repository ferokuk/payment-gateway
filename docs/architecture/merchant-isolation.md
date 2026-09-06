# Merchant isolation — design spec

## Scope and decision

The gateway remains a modular monolith with one PostgreSQL database. Traffic and
tenant counts are not specified; this stage adds indexed tenant boundaries, not
partitioning or per-tenant infrastructure. Public payment/refund URLs, request
bodies, response bodies and `X-API-Key` remain compatible.

`merchants` owns merchant activation and API-key issuance, authentication,
expiration and revocation. `core_payment` owns financial state and stores an
immutable `merchant_id` reference on Payment and Refund. It does not query
merchant configuration or interpret credentials. An immutable authenticated
identity crosses the context boundary; identity is never accepted from a body,
query parameter, metadata or a merchant header.

Flow: X-API-Key → merchants authentication → request identity → scoped
repositories → existing payment/refund use cases → PostgreSQL.

## Alternatives and trade-offs

| Choice | Decision and reason |
| --- | --- |
| Merchant parameter on every repository method | Rejected: easy to omit on a recovery or diagnostic branch; spreads plumbing through monetary algorithms. |
| Mandatory constructor-bound merchant repositories | Chosen: all reads, inserts, status CAS, reserve/release, lists and idempotency access share one scope. No default/global scope. |
| Global repositories plus router ownership checks | Rejected: direct use-case calls and race/error paths would bypass authorization. |
| Separate schema/database per merchant | Deferred: disproportionate migration/operational costs for this monolith. |
| PostgreSQL RLS | Deferred: reliable enforcement needs non-owner/non-superuser application roles and a separate privileged worker connection. Current deployment uses an owner account. Explicit SQL predicates plus composite FKs enforce the supported application boundary; DB credentials remain privileged. |
| OAuth/JWT or self-describing tenant API keys | Rejected for this stage: opaque revocable API keys fit existing machine clients and permit immediate database-backed revocation. |

## Security and credentials

Merchants have UUID identity, name, active flag and creation time. Each API key
has its own public UUID identifier, merchant owner, digest, label, creation time,
optional expiry and irreversible revocation time. New tokens contain a public
identifier and 256 bits of cryptographic randomness. Store only SHA-256 of the
secret material and compare in constant time; random credentials do not need a
password KDF. Return the credential only upon issuance, exclude secrets/digests
from repr and logs. Database engines hide bound SQL parameters so query failures
do not expose credential digests in tracebacks. Multiple keys permit
issue → deploy → revoke rotation.

No public administration API is added. A local operator CLI creates merchants,
issues keys, imports the previous global key once for migration, revokes keys,
and activates/deactivates merchants. Operator/database access is a privileged
boundary. Missing, malformed, unknown, revoked, expired and inactive-merchant
credentials all produce the same 401. No auth cache; revocation applies to the
next authentication, not retroactively to already authenticated requests.

Callbacks and reconciliation use explicitly named system repositories and
separate DI wiring. They retain service authority over all merchants, including
inactive ones, so accepted financial operations can finish. Client routes cannot
select this authority. Provider authentication remains `X-Callback-Secret`;
merchant credentials cannot authorize callbacks. No merchant-supplied owner is
trusted by callback processing.

## Data and monetary invariants

All four payment/refund/idempotency tables have non-null merchant ownership.
Refund `(merchant_id, payment_id)` references Payment `(merchant_id, id)`;
idempotency records similarly reference their operation with the same owner.
Merchant FKs restrict deletion. Database constraints prevent linking tenants even
if application insertion is faulty. Ownership is not changed by repository updates.

Idempotency uniqueness becomes `(merchant_id, key)` independently in payment and
refund tables. Keys belong to merchants, not credentials: rotation preserves
replay. Two merchants may reuse a key with unrelated bodies. Request hashes and
stored response format stay unchanged to preserve existing snapshots. Refund
creation checks payment visibility before any replay/mismatch response, yielding
the same 404 for foreign and nonexistent payment IDs.

Every scoped query includes ownership, including fallback reads after zero-row
updates. Foreign objects look absent; rejected access must not reserve money,
insert idempotency records or call a provider. Existing SAVEPOINT boundaries,
two-phase initiation, provider deduplication by operation UUID, status CAS,
write-once refund snapshots, refund balance guards, reconciliation schedules and
atomic failed-refund release are preserved.

## Migration and rollout

Use a maintenance window: stop API and workers, back up, apply the migration,
import the old API key for the fixed legacy merchant using a hidden CLI prompt,
then start the new code and workers. All existing rows are backfilled to that
merchant, including pending operations and idempotency snapshots. No implicit
owner defaults remain in the final schema. Migration never stores the old key or
reads environment secrets. The old `API_KEY` setting is no longer an authentication
fallback. Existing clients keep working after explicit import; new installations
must provision a merchant/key. Mixed old/new binaries are unsupported.

Downgrade refuses non-legacy merchants/data/keys, inactive merchants and revoked
or expiring legacy credentials: old code cannot represent these security rules.
Export/restore or an explicit operator data migration is required then.
Transactional DDL/backfill acquires locks; large installations need a separately
planned online migration. Tenant/key deletion and idempotency expiry are excluded.

## Validation

Run formatting, lint, strict typing, existing unit/API tests and PostgreSQL tests.
Add two-merchant API and repository tests for every read/write entry point,
replay/recovery, identical concurrent idempotency keys, key rotation/revocation/
expiry/inactive merchants, header/body spoofing, composite FK rejection, service
callbacks/reconciliation, and populated legacy migration with guarded downgrade.
Only real PostgreSQL tests establish race/constraint behavior; report skipped or
unavailable verification explicitly.

Remaining boundaries: trusted application/operator DB access; a shared provider
callback secret without signature/replay protection; TLS termination and secret
redaction in deployment proxies; rate limiting and key-management audit export
are future operational work. Imported credentials preserve their original entropy;
rotate a weak legacy secret promptly to a generated key.

Verified on 2026-09-06 with Python 3.14.6 and a disposable PostgreSQL 17:

- `ruff check .` and `ruff format --check .`: passed (121 files formatted).
- `mypy --strict src tests notebooks/gateway_client.py`: passed (113 source files).
- `pytest -q -p no:cacheprovider` with `TEST_DATABASE_URL`: **417 passed**, no skips.
- Includes 11 old/new migration tests with `alembic.check`, 9 cross-tenant isolation
  scenarios, 5 credential database scenarios and the existing concurrent monetary
  invariant tests. This includes successful downgrade with an imported legacy key
  and SQL-error credential redaction. Offline upgrade/downgrade SQL compilation
  also passed.
- Application DI/CLI import and `git diff --check`: passed. The notebook helper
  was type-checked; the interactive notebook was not executed end-to-end.

No migration or provisioning was performed against application data.

References: object-level authorization must cover every operation accepting an
object identifier ([OWASP API1:2023](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/)).
RLS requires attention to privileged roles and table ownership
([PostgreSQL 17 row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html)).
Token generation and constant-time comparison use the standard
[`secrets` library](https://docs.python.org/3.14/library/secrets.html).
