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
internet. Three strategies, picked automatically (first that applies):

- **cloud mode** — a presigned R2 GET. The bytes are already in R2, so nothing is
  copied.
- **object store** — any S3-compatible bucket (`PUBLISHING_S3_*`). A background
  loop copies the clip there ahead of its slot; the provider gets a presigned
  GET. Preferred everywhere else, and effectively required on a home connection.
- **signed token** — `GET /api/publishing/media/{token}`, where the token is an
  HMAC-signed payload pinning job id, clip index, filename and expiry. The
  signing secret is derived from the master key, so rotating the key invalidates
  outstanding URLs. Serves the clip from *this* machine.

`PUBLISHING_PUBLIC_BASE_URL` must be an origin the **provider** can reach — not
the browser's origin. A `localhost` or plain-HTTP value produces a boot warning
because it cannot work. It stays required even with an object store: it is also
where the provider's webhook lands, and an unconfirmed post ends up `unknown`.

### Why the signed-token route is not enough

The provider does not copy the bytes when media is registered — registration is a
cheap `Range` probe. It keeps the URL and downloads the **whole clip inside the
`POST /posts` request**, at post time. Two consequences, both learned the hard
way:

1. **Your upload bandwidth is a hard requirement.** Measured 2026-08-18 on a home
   line: 21.7 MB took 244 s to leave the machine, the provider's gateway gave up
   first and returned a bodyless `504`. That classifies as `unknown`, which is
   never auto-retried, so every post needed a human. Landing a 20 MB clip inside
   a 60 s gateway window needs ~370 KB/s sustained. Nothing in the queue, the
   tunnel or the provider config can substitute for that.
2. **The URL must outlive the request that made it.** For a scheduled post,
   registration happens hours or days before the download.
   `PUBLISHING_PROVIDER_MEDIA_URL_TTL` defaults to 7 days for that reason —
   matching the provider's own ref lifetime, and SigV4's ceiling.

### Staging in an object store

Four env vars (`publishing/objectstore.py`), any S3 API — Supabase Storage,
Backblaze B2, Cloudflare R2, MinIO, AWS S3. `PUBLISHING_S3_*` wins; `R2_*` then
`AWS_*` are used as fallbacks so a deploy that already has object storage needs
nothing new. Use a **private** bucket: access is by presigned URL only.

Recipe for a card-free setup (Supabase):

1. New project → **Storage** → new bucket, private, e.g. `openshorts-publishing`.
2. **Storage → Settings → S3 connection**: enable it, copy the endpoint, note the
   region.
3. **Generate S3 access keys** → set `PUBLISHING_S3_ACCESS_KEY_ID` /
   `PUBLISHING_S3_SECRET_ACCESS_KEY`.
4. Restart. `GET /api/publishing/admin/health` reports
   `media_strategy: objectstore_presigned` and the bucket under `media_store`.

Mind the free plan's **50 MB per-object** ceiling — a typical clip is ~20 MB, but
a long or high-bitrate one can exceed it, and staging then fails permanently
rather than slowly. That surfaces as a `media.stage_failed` event while the
attempt stays parked, so check **Publishing → Events** before suspecting the
queue. (1 GB of storage and 5 GB of monthly egress are the other caps; egress is
one clip download per destination.)

Then the shape of a publish changes: the **transfer loop** (`worker.transfer_once`)
copies one clip at a time into the bucket, ahead of its slot, on our clock — the
slow part, entirely off the provider's request window. Object keys are
content-addressed (`publishing/{job}/{clip}/{fingerprint}.mp4`), so re-styling a
clip writes a different object and a live URL can never serve stale bytes.

While a clip is still uploading, dispatch **parks** the attempt (`media_pending`,
transient, consumes no try) with a visible reason instead of failing it. A store
that is configured but unreachable also parks rather than degrading to the slow
route: parking is recoverable, and a submit that times out is ambiguous forever.

Objects are swept `PUBLISHING_STORE_RETENTION_HOURS` (default 48) after nothing
needs them. Two things pin an object: a queued attempt, and a cached provider
media ref — deleting under a live ref would turn a healthy scheduled post into
"could not download the file". Budget ~25 MB per clip for about nine days; 1 GB
covers roughly 3 posts a day. Set the value to `0` to defer to a bucket lifecycle
rule instead.

### The consequence: the clock can leave the box

Once the provider fetches bytes from the store, **a submit reads no local file**,
and the process that holds a slot no longer has to be the process that made the
clip. That is what makes a schedule survive a closed laptop: run a second
instance of this subsystem — and nothing else — on a host that never sleeps.

`publishing/runner.py` is that entrypoint. It imports neither `app` nor `main`,
so torch, mediapipe, ultralytics, faster-whisper and ffmpeg are absent from its
image; it needs Postgres, `PUBLISHING_MASTER_KEY`, the `PUBLISHING_S3_*` values
and outbound HTTPS. Both hosts share one database and poll the same queue with no
broker and no new machinery: `service.claim_due_attempts` claims with
`FOR UPDATE SKIP LOCKED`, and the duplicate-post guard below is a DB constraint,
so it holds across processes by construction.

`publishing/tick.py` is the same subsystem as a **cron job** — one pass, then
exit — because free *scheduled* compute is far easier to come by than a free
always-on VM. It reconciles first (so a slot that came due goes out in this pass,
not the next one), drains the queue one claim at a time, and stops on a wall-clock
budget before the host's timeout can kill it mid-claim. The cost is granularity: a
slot is honoured by the first tick after it, so the tick interval is the
worst-case lateness.

That second process is what turned one assumption into a bug. Boot recovery used
to re-queue **every** `in_flight` row, which is correct for a single long-lived
process and wrong the moment two processes share the queue: a tick boots every ten
minutes and lands mid-batch of its predecessor. Two layers now prevent the
duplicate post that follows — `dispatcher.dispatch_attempt` re-checks that the
claim is still ours before it submits anything (the boundary; the unique index
does not cover this, because it is one *row* submitted twice, not two rows), and
recovery only takes claims quiet for `PUBLISHING_ORPHAN_CLAIM_MIN_AGE`
(thrash avoidance). Both are tested in `tests/test_publishing_tick.py`.

Two details make the resolver-less host correct rather than merely quiet:

- `dispatcher._staged_info` falls back to the object store when no `publish_media`
  row exists yet. Object keys are content-addressed, so a HEAD is enough to
  presign and register the ref locally. Without that the app host would sit on
  the critical path of every slot, which is the thing being removed.
- Health takes `PUBLISHING_ROLE=publisher` to mean a missing clip resolver is the
  design. It is an operator declaration, not a guess, so a genuinely broken app
  host still fails the check.

The transfer loop and store sweeper are left running there rather than switched
off: `worker.transfer_once` finds no local bytes and skips each candidate without
recording a failure, and the sweeper reads only the database and object ages. One
code path, and the same image also serves as a plain second replica.

The operational rule this creates: **bytes must reach the bucket while the app
host is awake.** A slot with nothing staged parks and retries — it is never lost —
but it does not go out until the clip is uploaded.

Step-by-step deployment on a free, card-free always-on host (Hugging Face Space +
Cloudflare Worker cron + Supabase):
[`deploy/publisher/README.md`](../deploy/publisher/README.md).

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
`supports_account_listing=False` — each from a probe, and the first of those
probes is now suspect: it tested REST-shaped paths (`GET /v1/posts/{id}`) and got
405s, but this API dispatches on an `action` parameter, and a live submit response
tells us to "poll **action** `check_job_status`". Treat `supports_status_lookup`
as unmeasured rather than settled — see `status200.fetch_status`. Given the flags
as they stand:

- there is **no polling loop**; webhooks are the only completion signal and the
  stale sweeper is the safety net;
- scheduling is **held here, not by the provider**. `supports_remote_schedule`
  exists so a provider can take a slot off our hands, and Status 200 documents
  exactly that (`post.scheduledFor`, docs v1.4.0) — but it declares `False`,
  because the field is accepted and discarded. Measured over three real posts: a
  `+00:00` timestamp published immediately, so did a correctly-formatted
  `2026-08-18T17:00:00.000Z` one, and a probe of both API hosts (v2
  `/api/v2/posts` and the v1 edge function every scheduling example in the docs
  targets) found one handler answering `Missing 'post' object` to everything —
  no `action` dispatch, no second endpoint where scheduling could live. A held
  slot has a documented shape we have never seen: `202` with `scheduled_at` and
  `scheduled_post_id`; we only ever get `200` `processing`. Use
  `probe_endpoints` to re-measure — it publishes nothing.
  **This flag decides whether a plan spaces out.** At `True` the promote pass
  releases a future attempt for immediate submit and lets the provider hold the
  clock; if the provider then ignores the field, an entire spread-out plan fires
  at once. At `False` each attempt stays parked on its own slot and *this
  process* submits when the slot arrives — correct spacing, but the container has
  to be running at every slot, which only an always-on host removes.
  `PUBLISHING_REMOTE_SCHEDULE` (`auto`|`on`|`off`) overrides; `auto` currently
  behaves as `off` since nothing declares support.
  Two fallbacks guard the day a provider claims support again, because a schedule
  that quietly doesn't happen is worse than one that's refused: a 4xx naming the
  field flips the process to the local clock
  (`attempt.remote_schedule_fallback`), and a response to a future slot whose
  status says the upload is *already moving* does the same
  (`attempt.remote_schedule_ignored`) — after one post, not a day's worth. That
  second check tests against `GOING_OUT_NOW`, not `published` alone: the live
  answer in the 2026-08-18 incident was `processing`, so a narrower check would
  have slept through the exact event it exists to catch. An echoed `scheduledFor`
  overrides it, being positive proof the slot was taken;
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
| `objectstore.py` | S3-compatible staging bucket: the fast media origin. |
| `providers/` | `base.py` contract, `status200.py`, `fake.py`, registry. |
| `db.py` | Async engine/session for the publishing tables. |
| `clips.py` | Reads finished clips out of a job — the seam to the video pipeline. |
| `service.py` | Destination expansion, request/attempt creation, pre-flight. |
| `dispatcher.py` | Claims an attempt, uploads media, submits, records the outcome. |
| `worker.py` | The loops: dispatch, reconcile, transfer, stale sweep, webhook drain. |
| `planner.py` | Assignments: which clip is earmarked for which group. |
| `autopublish.py` | Hook from a finished job into the planner. |
| `api.py` | Public routes. `admin_api.py` — admin routes. `webhooks.py` — ingestion. |
| `views.py` / `schemas.py` | Response shaping and request validation. |
| `runner.py` | Publisher-only entrypoint: this subsystem, no video pipeline. |
| `tick.py` | One pass then exit — the same subsystem as a cron job. |

