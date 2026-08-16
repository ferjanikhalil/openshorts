# Automated publishing

Takes finished clips from the normal OpenShorts pipeline and posts them to
YouTube Shorts, Instagram Reels and TikTok through a publishing provider. Off by
default; the whole package is dormant unless `PUBLISHING_ENABLED` is set.

The first (and so far only) provider adapter is **Status 200**. Nothing outside
`providers/` knows its name.

## Vocabulary

| Term | Means |
|------|-------|
| **publish group** | A reusable bundle of destinations sharing **one** provider credential. The UI calls it a *Batch*. |
| **publish destination** | ONE connected social account (platform + provider account ref). **This is the atomic publish target.** |
| **publish request** | One user-visible publish operation for one clip. May span groups. |
| **publish attempt** | One row per destination per try. **This is the publication record.** |
| **assignment** | A clip earmarked for a group before anyone asked to publish it — the planning layer. |

The destination, not the group, is the unit of publication. A group only ever
*expands* into destinations, which is what lets one code path serve all three
publishing modes:

- **single account** — one `destination_id`
- **hand-picked multi-account** — several `destination_ids`, freely spanning groups
  (`Clip 4 → YouTube 1 + TikTok 2 + Instagram 3`)
- **whole group** — one `group_id`, expanded at request time

`mode` is recorded for the UI and never branches behaviour. Adding a fourth way
to choose destinations means touching `service.expand_destinations` and nothing
else.

## Enabling it

Publishing requires **Postgres**. Duplicate-post prevention, the retry clock and
the audit trail are durability requirements, and a guard that forgets on
redeploy is not a guard. It is otherwise independent of `BILLING_ENABLED` — it
works in self-host and cloud mode alike.

```bash
# self-host + publishing
docker compose -f docker-compose.yml -f docker-compose.publishing.yml up --build

# cloud + publishing
docker compose -f docker-compose.yml -f docker-compose.cloud.yml \
               -f docker-compose.publishing.yml up --build
```

Required host env (see `.env.example` for the full list and the generator
commands):

| Variable | Why |
|----------|-----|
| `POSTGRES_PASSWORD` | The `db` service refuses to start without it. |
| `PUBLISHING_MASTER_KEY` | 32 bytes, base64. Wraps every stored provider credential. **Losing it means re-entering every key.** |
| `PUBLISHING_ADMIN_TOKEN` *or* `PUBLISHING_ADMIN_EMAILS` | Who may configure credentials. Without one of these the admin router stays unmounted. |
| `PUBLISHING_PUBLIC_BASE_URL` | The origin **the provider** fetches clips from. A `localhost` value cannot work. |

`docker-compose.publishing.yml` defaults `PUBLISHING_DRY_RUN` to `true`, so a
first `up` cannot post to a real account by accident.

## Dry run

With `PUBLISHING_DRY_RUN=1` every provider lookup resolves to the in-repo fake
adapter (`providers/fake.py`), so the entire pipeline — enqueue, media
registration, dispatch, retries, quota deferral, webhooks, derived status, admin
UI — runs end to end with no credential and no real post.

The fake is not a happy-path stub. Markers in the caption or the account ref
force each failure mode deterministically:

| Marker | Behaviour |
|--------|-----------|
| `fail-auth` | 401-equivalent; credential marked invalid |
| `fail-blocked` | 403-equivalent; destination marked blocked |
| `fail-transient` | retryable network error |
| `fail-oversize` | permanent media-too-large |
| `fail-unknown` | ambiguous timeout (must never auto-retry) |
| `quota` | 202-equivalent deferral |
| `slow` | submitted, awaiting a webhook |
| anything else | published immediately |

Its declared capabilities mirror Status 200 exactly, so dry run exercises the
same orchestration branches production takes. `GET /api/publishing/admin/dry-run`
shows what it recorded.

## The admin flow

The admin router mounts only when `PUBLISHING_ADMIN_TOKEN` (≥24 chars) or
`PUBLISHING_ADMIN_EMAILS` is configured. With neither, publishing goes **inert,
not open** — there is no "no admin configured means everyone is admin" path.
Self-host callers authenticate with the `X-Publishing-Admin-Token` header; cloud
callers with their normal JWT, if the email is on the list.

In the dashboard: **Publishing** tab.

1. **Create a group** (`POST /api/publishing/admin/groups`) — name + provider.
2. **Set its credential** (`PUT /api/publishing/admin/groups/{id}/credential`).
   The key is sealed before it touches the database and probed against the
   provider by default (`?verify=true`). Each group has its **own** credential;
   one key never controls all groups.
3. **Add destinations** (`POST .../groups/{id}/destinations`) — one row per
   connected account: platform + the provider's account ref.
4. **Publish** (`POST /api/publishing/publish`) with any mix of
   `destination_ids` and `group_ids`.

`POST /api/publishing/preview` runs the identical expansion and the same
pre-flight checks the dispatcher will run, but writes nothing: the operator sees
which accounts would receive the clip and why any were dropped, before a quota
slot is spent.

### Endpoints

Public (`/api/publishing`): `health`, `destinations`, `preview`, `publish`,
`publish-job`, `requests`, `requests/{id}`, `requests/{id}/cancel`,
`attempts`, `attempts/{id}/retry`, `media/{token}`, `webhook/{provider}`.

Admin (`/api/publishing/admin`): `health`, `groups` CRUD,
`groups/{id}/credential` (`PUT`/`DELETE`/`verify`), `groups/{id}/credentials`
(masked history), `destinations` CRUD, `groups/{id}/assignments`, `assignments`,
`groups/{id}/capacity`, `schedule/run`, `events`, `dry-run`, `dry-run/reset`.

## Security model

- **No provider key is in this repository, in `.env.example`, in compose, in
  frontend code, or in any log.** Keys are entered through the admin UI only.
- **Sealed at rest.** AES-256-GCM, fresh 96-bit nonce per encryption, AAD bound
  to the credential's purpose so a webhook-secret blob cannot be swapped into an
  `api_key` row and decrypt cleanly.
- **Never returned to the frontend.** The API returns a `fingerprint` and
  `last4` — enough to answer "is this the key I pasted?" and nothing more.
  `crypto.SecretStr` makes accidental exposure through a repr, log line or
  traceback impossible without a greppable `.reveal()` call.
- **Scrubbed from logs.** `rl_`-shaped tokens are masked by `crypto.scrub`, which
  `app.py`'s log scrubber also uses.
- **Rotation is by-insert.** Every blob records the `key_version` that sealed it;
  set `PUBLISHING_MASTER_KEY_OLD` to the previous key during the window and no
  stop-the-world re-encrypt is needed. The superseded credential row is revoked,
  never deleted, so a historical attempt can still name the key that signed it.
- **Webhooks.** Signature is `sha256=<hex>` HMAC over the raw body in
  `X-Webhook-Signature`, verified per group (each group has its own secret, and
  the secret that verifies also identifies the group). The signed preimage
  contains **no timestamp and no nonce**, so the signature never expires on its
  own — a UNIQUE `(provider, provider_event_id)` makes a replay a no-op and a
  `created_at` skew window bounds how old a first-time event may be. Both are
  required.

## Media reachability

Status 200 ingests media **by URL**, so a clip must be fetchable from the public
internet. Two strategies, picked automatically:

- **cloud mode** — a presigned R2 GET.
- **self-host** — `GET /api/publishing/media/{token}`, where the token is an
  HMAC-signed payload pinning job id, clip index, filename and expiry. The
  signing secret is derived from the master key, so rotating the key invalidates
  outstanding URLs.

`PUBLISHING_PUBLIC_BASE_URL` must be an origin the **provider** can reach — not
the browser's origin. A `localhost` or plain-HTTP value produces a boot warning
because it cannot work.

## Correctness: never post twice, never lose a post

**The duplicate-post guard is a database constraint,** not application logic:
a partial unique index on `publish_attempts (publish_request_id,
publish_destination_id)` `WHERE status IN ('pending','in_flight','submitted',
'succeeded')`. At most one live-or-won attempt per destination per request, and
because it is partial, a failed row does not block the retry that replaces it.
`state.LIVE_STATES` must stay in lockstep with that index —
`tests/test_publishing_state.py` asserts it.

Above that: a UNIQUE `idempotency_key` on `publish_requests` means a
double-clicked publish button cannot create a second fan-out. When the caller
supplies no key, `state.derive_idempotency_key` builds one over the **sorted**
destination set, so the same clip to a different set of accounts is correctly a
different request.

**`unknown` is terminal and never auto-retried.** It means the post was handed to
the provider and the outcome was never learned — a submit timeout, most often. A
blind retry there double-publishes to a real audience, so a human resolves it
(`NEEDS_ATTENTION`). This is why a *submit* timeout classifies as `E_UNKNOWN`
while a *media upload* timeout classifies as retryable `E_TIMEOUT`: no post
exists yet in the second case.

**Request status is derived, never written.** `state.derive_request_status` is a
pure function of the attempt rows. `partial` is the state that matters: one clip
to three accounts where TikTok fails is not a failed request and not a
successful one, and collapsing it either way loses the only fact the operator
needs.

**Retries** use exponential backoff with ±20% jitter derived by hashing the
attempt id — reproducible in tests, identical across workers, and enough to
de-synchronize the herd when a provider outage fails every post at once.

**Queue claiming** is `FOR UPDATE SKIP LOCKED` against Postgres. No Celery, no
Redis, no broker. A stale sweeper moves attempts that were handed over but never
confirmed (`PUBLISHING_SUBMIT_TIMEOUT`, default 30 min) to `unknown`, and
`recover_stale_on_boot` reclaims anything a killed worker left `in_flight`.

## Provider abstraction

`providers/base.py` is the whole contract: five methods and a `Capabilities`
dataclass. There is no `if provider == "status200"` in the dispatcher, the API,
the workers or the UI — adding a provider is a new file in `providers/` plus a
registry entry, mirroring `batch.OPERATIONS`.

`Capabilities` is what keeps the abstraction honest, because providers differ in
ways that change orchestration rather than just wire format. Status 200 declares
`supports_status_lookup=False`, `supports_cancel_scheduled=False` and
`supports_account_listing=False` — each verified by probe, not assumed — so:

- there is **no polling loop**; webhooks are the only completion signal and the
  stale sweeper is the safety net;
- scheduling is held **locally** and the submit happens at the appointed time.
  `scheduled_for` is deliberately never forwarded, because an uncancellable
  remote post is an uncontrollable one;
- destinations are entered by an admin and proven by their first real publish
  (a 403 flips the destination to `blocked`).

Adapters raise `errors.ProviderError` with a classified code — never a bare
`httpx` exception — so the retry decision never depends on which provider
produced the failure. The taxonomy is `PERMANENT` / `TRANSIENT` / `CAPACITY` /
`E_UNKNOWN`, plus `DESTINATION_FATAL` and `CREDENTIAL_FATAL` for failures that
mean the destination or the credential is broken rather than the post — so the
day's remaining posts don't each rediscover it.

### Quota

Status 200's cap is **per platform per account** (free tier 5/day) and is only
reported on a response — there is no quota query endpoint. So dispatch is
quota-aware by necessity, not as an optimization. A **202** with
`queued_for_next_day` is not an error: the daily cap is reached and the provider
parked the post, which authoritatively sets remaining to 0 until the reset. A
**429** is the spacing cooldown, *not* the daily cap; conflating the two would
park a post until midnight over a few seconds of throttling.

Daily volume is never hard-coded. `schedule.clip_selection` has no default cap
by design: the operator's volume is configuration, and a constant here would
silently become the system's ceiling.

## Tests

```bash
pytest tests/test_publishing_state.py tests/test_publishing_schedule.py \
       tests/test_publishing_crypto.py tests/test_publishing_provider.py -v
```

All four run in CI with no Postgres, no credentials and no network: the state
machine, backoff, idempotency, error classification, webhook signatures and media
tokens are pure functions; the provider suite runs against
`httpx.MockTransport` fixtures and the fake adapter. `schedule` takes `now` as a
parameter so the clock can be pinned.

## Migrations

The boot path is `create_all` (`cloud.database.init_engine`); Alembic exists for
controlled changes on top of it. `alembic/versions/20260809_publishing_baseline.py`
is therefore written defensively — it inspects the live catalogue and creates only
what is missing, so it is correct on a fresh database, on one that has already
booted, and on one half-way between.

```bash
DATABASE_URL=postgresql+asyncpg://... alembic upgrade head
```

## Module map

| File | Role |
|------|------|
| `config.py` | Env-backed settings, read lazily; `validate_required()` fails fast. |
| `models.py` | The nine tables and every constraint that enforces correctness. |
| `state.py` | State machine, backoff, fingerprints, idempotency. Pure. |
| `schedule.py` | Spacing, posting window, capacity allocation. Pure. |
| `platforms.py` | Platform media/caption ceilings. Pure. |
| `signing.py` | Webhook HMAC + signed media tokens. Pure. |
| `errors.py` | The provider-neutral error taxonomy. |
| `crypto.py` | AES-256-GCM sealing, `SecretStr`, scrubbing, rotation. |
| `admin_auth.py` | Who may configure publishing. Fails closed. |
| `media.py` | Public URL strategy + reachability warnings. |
| `providers/` | `base.py` contract, `status200.py`, `fake.py`, registry. |
| `db.py` | Async engine/session for the publishing tables. |
| `clips.py` | Reads finished clips out of a job — the seam to the video pipeline. |
| `service.py` | Destination expansion, request/attempt creation, pre-flight. |
| `dispatcher.py` | Claims an attempt, uploads media, submits, records the outcome. |
| `worker.py` | The loops: dispatch, reconcile, stale sweep, webhook drain. |
| `planner.py` | Assignments: which clip is earmarked for which group. |
| `autopublish.py` | Hook from a finished job into the planner. |
| `api.py` | Public routes. `admin_api.py` — admin routes. `webhooks.py` — ingestion. |
| `views.py` / `schemas.py` | Response shaping and request validation. |

