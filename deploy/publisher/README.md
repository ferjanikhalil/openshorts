# Always-on publisher — HF Space + Cloudflare cron (card-free, $0)

A schedule needs a clock that is awake at the slot. Nothing in the chain provides
one: Status 200 accepts `scheduledFor` and discards it (measured across three
real posts — see [`providers/status200.py`](../../publishing/providers/status200.py)),
TikTok's Content Posting API has no scheduling, and Instagram's Graph API has no
scheduled publish. So the clock is ours, and on a single-machine deploy it is the
laptop's: a slot fires only if the laptop happens to be awake at it.

This directory moves the clock off the laptop, and leaves the video pipeline —
GPU, models, clip files — at home.

```
   laptop (Docker, as today)              Hugging Face Space (always awake)
   ┌───────────────────────────┐          ┌────────────────────────────────┐
   │ ingest → clips → styling  │          │ publishing/runner.py           │
   │ plans the batch           │          │  · holds every slot            │
   │ stages clip bytes ────────┼─Supabase─┤  · submits within ~5s of it    │
   │ dashboard / admin UI      │  storage │  · receives provider webhooks  │
   └────────────┬──────────────┘          └───────────────┬────────────────┘
                │                                         │ GET /health
                └───── one Supabase Postgres ─────────────┤ every 10 min
                                                          │
                                          Cloudflare Worker cron (the pulse)
```

The split works because **a submit needs no bytes.** The provider downloads the
clip from Supabase storage using a presigned URL, so at slot time the publisher
needs Postgres, the master key and outbound HTTPS. Nothing local. That is why
[`publishing/runner.py`](../../publishing/runner.py) imports neither `app` nor
`main` — no torch, no mediapipe, no ffmpeg in its image — and why it fits in a
free container instead of needing a VM.

Sharing the queue needs no new machinery. Claims are taken with
`FOR UPDATE SKIP LOCKED`, the duplicate-post guard is a partial unique index in
the database, and `dispatcher.dispatch_attempt` re-checks that a claim is still
ours before it submits anything. Both hosts can poll the same table with no
broker and no double-post.

## Why two hosts and not one

Neither free platform can do this alone, and each one's flaw is the other's
strength:

|  | runs our Python | never sleeps | card-free |
|---|---|---|---|
| Hugging Face Space | ✅ `runner.py` unmodified | ❌ sleeps when idle | ✅ |
| Cloudflare Workers | ❌ V8/Pyodide: no asyncpg, no C extensions, 10 ms CPU | ✅ | ✅ |
| **both** | ✅ | ✅ — the cron keeps the Space awake | ✅ |

Cloudflare cannot host the publisher and it is not being asked to. Claiming a
Postgres row, unsealing an AES-256-GCM credential and submitting with a 120 s
read timeout is not a 10 ms-CPU workload, and porting it to JavaScript would mean
a **second implementation of the duplicate-post guard** — the one piece of this
system where a divergence publishes twice to a real audience. So Cloudflare gets
the job it is actually good at: `fetch()` one URL every ten minutes, which costs
milliseconds of CPU because waiting on the network is not CPU time.

**One consequence worth knowing up front:** because the Space runs the always-on
`runner.py` rather than a cron, there is **no scheduling granularity penalty**.
Its dispatch loop polls every `PUBLISHING_DISPATCH_INTERVAL` (default 5) seconds,
so a slot at 18:03 fires at 18:03. The ten-minute cron is a *pulse*, not the
clock — it only has to be frequent enough to prevent an idle timeout.

## Division of labour

| | laptop | HF Space | CF Worker |
|---|---|---|---|
| Generate + style clips | ✅ | — | — |
| Upload clip bytes to Supabase storage | ✅ | — | — |
| Plan a batch (create the attempts) | ✅ | — | — |
| **Hold the slot and submit** | if awake | ✅ **always** | — |
| Register the provider media ref | if awake | ✅ (presigns the staged object) | — |
| Receive webhooks / reconcile | — | ✅ | — |
| Keep the publisher from idling | — | — | ✅ |
| Tell you it died | — | — | ✅ Telegram |
| Dashboard + admin UI | ✅ | see [the exposure decision](#the-one-decision-to-make-deliberately) | — |

**The one thing the laptop must finish before it sleeps: uploading the bytes.**
The publisher can presign and register media itself, but it cannot invent bytes
that were never uploaded. If a slot arrives with nothing in the bucket, the
attempt *parks* (never fails) and retries every 5 minutes until the laptop comes
back. In practice: after planning a batch, leave the laptop on until you see
`✅ Publishing: staged …` in its logs for each clip.

## What stays free

| Piece | Free allowance | The catch |
|---|---|---|
| HF Space (CPU basic) | $0, no card | sleeps on inactivity — which is what the Worker is for. HF can also rebuild or restart it; boot recovery handles that. |
| Cloudflare Workers | $0, no card; 100k req/day, 5 crons | uses 1 cron and ~4,300 requests/month |
| Cloudflare KV (optional) | 1k writes/day | only written when the up/down state changes, so ~0/day |
| Supabase Postgres | 500 MB, no card | pauses after **7 days with no activity** — the publisher's own polling is activity, so it never triggers here |
| Supabase storage | existing bucket | 50 MB per object on the free plan; a clip over that fails as `media.stage_failed` |

The honest risk is not cost, it is **tenancy**: a free Space is HF's to restart,
and a scheduler is not what Spaces are marketed for. The mitigation is that
nothing here is Space-specific — `runner.py` and `tick.py` are host-agnostic, so
a move is a secret and a redeploy ([Appendix A](#appendix-a--a-box-you-control),
[Appendix B](#appendix-b--other-card-free-clocks)).

---

## Step 0 — Push the code first

The Space builds by downloading the source from **GitHub**, not from your laptop.
Right now the working tree has uncommitted publishing changes, so a build today
would deploy code older than this runbook.

```powershell
git status --short          # expect to see modified publishing/ files
git add -A; git commit -m "publishing: off-box scheduling clock"; git push
```

This is also the step that makes the rest cheap: because the repo is public, the
Space's `Dockerfile` can fetch the source itself, so the Space holds no second
copy of `publishing/` to drift out of sync.

> The repo being public also means **no secret may ever enter it** — not in the
> Dockerfile, not in a compose file, not in a build arg. Every credential below
> goes into an HF secret or a Wrangler secret. `.env` is gitignored
> (`.gitignore:28`); keep it that way.

## Step 1 — Postgres that is awake when the laptop is not

Publishing already requires Postgres; the change is *whose*. Both halves need the
queue at every moment, and the laptop is asleep for most of them.



Supabase (the project already holding your clip bucket) → **Project Settings →
Database → Connection string**. Pick **Session pooler** and copy it verbatim —
do not hand-assemble the host, it differs per project and region.

Two reasons it must be the *session* pooler and not either alternative:

- **Not the direct connection.** Supabase's direct database host is IPv6-only
  unless you buy the IPv4 add-on. A container with no IPv6 route fails with
  `Network is unreachable`, at the slot, having reported healthy up to then.
- **Not the transaction pooler** (the other option, on port 6543). asyncpg
  prepares statements, which transaction pooling breaks unless you disable the
  statement cache — and more fundamentally, `FOR UPDATE SKIP LOCKED` holds a lock
  for the length of a transaction on one backend, which is exactly what a
  transaction pooler is free to take away. The claim mechanism depends on it.

Convert it to a SQLAlchemy async URL by swapping the scheme:

```
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres
```

> If the password contains `@ : / ?` or `#`, percent-encode it. An unencoded `@`
> does not error — it silently splits the URL in the wrong place and you get a
> host that does not exist.

### Bring the existing data across

Your groups, provider credential, destinations and history live in the laptop's
local Postgres. Dump and restore, using a container so nothing needs installing:

```powershell
docker exec openshorts-db pg_dump -U openshorts -d openshorts `
  --no-owner --no-privileges -f /tmp/openshorts.sql
docker cp openshorts-db:/tmp/openshorts.sql .\openshorts.sql
```

Write to a file *inside* the container and copy it out, as above — PowerShell's
`>` adds a BOM that `psql` chokes on.

```powershell
docker run --rm -v ${PWD}:/dump postgres:16-alpine `
  psql "postgresql://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres" `
  -v ON_ERROR_STOP=1 -f /dump/openshorts.sql
```

Note the plain `postgresql://` scheme here: `+asyncpg` is a SQLAlchemy dialect,
meaningless to `psql`. Then prove it landed:

```powershell
docker run --rm postgres:16-alpine `
  psql "postgresql://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres" `
  -c "select count(*) from publish_destinations; select count(*) from publish_attempts;"
```

Those two counts are the proof. Keep the laptop's `db` volume as a cold backup —
do not delete it.

## Step 2 — The Space

**huggingface.co → New Space.** Name it `openshorts-publisher`, SDK **Docker**
(blank template), visibility **public**.

> Public is not optional here: a private Space requires an HF token on every
> request, so the Status 200 webhook could not reach it. Nothing sensitive is
> exposed by that — the admin router stays unmounted (below), and the only routes
> are health and the signature-gated webhook.

Copy exactly two files from [`space/`](space/) into the Space repo's root:

| From | To | Why |
|---|---|---|
| [`space/Dockerfile`](space/Dockerfile) | `Dockerfile` | fetches `publishing/` + `cloud/` from GitHub, installs the publisher's deps, runs uvicorn on 7860 |
| [`space/README.md`](space/README.md) | `README.md` | the YAML frontmatter is load-bearing — `sdk: docker` and `app_port: 7860` |

Then **Settings → Variables and secrets**, and add every one of these as a
**secret** (not a variable):

```
PUBLISHING_ENABLED=1
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres
PUBLISHING_MASTER_KEY=<copied from the laptop's .env, byte for byte>
PUBLISHING_S3_ENDPOINT=<copied from the laptop's .env>
PUBLISHING_S3_BUCKET=<copied from the laptop's .env>
PUBLISHING_S3_REGION=<copied from the laptop's .env>
PUBLISHING_S3_ACCESS_KEY_ID=<copied from the laptop's .env>
PUBLISHING_S3_SECRET_ACCESS_KEY=<copied from the laptop's .env>
PUBLISHING_PUBLIC_BASE_URL=https://<hf-username>-openshorts-publisher.hf.space
```

`PUBLISHING_MASTER_KEY` is the line that matters most. The provider API key lives
in the shared database sealed with AES-256-GCM, and this key is what unwraps it.
A different key here does **not** fail at boot — it fails at the first submit, as
an authentication error against the provider, which reads exactly like a revoked
API key and sends you to the wrong dashboard. Copy it; do not generate one.

Copy the five `PUBLISHING_S3_*` values from the laptop rather than re-deriving
them from the Supabase dashboard. The endpoint hostname in particular has two
valid forms depending on when the project was created, and the one already in your
`.env` is the one known to work. Different buckets — or a subtly different
endpoint — and every post parks forever, waiting for media that was staged
somewhere else.

**First boot, safely:** add `PUBLISHING_DRY_RUN=1`. The whole queue, retry and
webhook path then exercises against the in-repo fake provider, with no credential
and nothing reaching a real audience. Remove it when the shape looks right.

Expected in the Space's **Logs** tab once it builds:

```
📡 Publishing mode ENABLED (DB ready, dispatch + reconcile + transfer active,
   media: objectstore_presigned).
```

Then confirm from your machine:

```powershell
curl.exe "https://<hf-username>-openshorts-publisher.hf.space/api/publishing/health"
```

Expect `"role": "publisher"`, `"clip_resolver_registered": false`, and
`"media_strategy": "objectstore_presigned"`. The first two are healthy *here* —
the declared role tells `api.health` that a missing clip resolver is by design.
The third is the one to actually check: anything else means the `PUBLISHING_S3_*`
values did not come across, and the publisher would try to serve clip bytes it
does not have.

### The one decision to make deliberately

`PUBLISHING_ADMIN_TOKEN` is **not** in the list above, and that is a choice.

The app mounts the admin router only if an admin identity is configured
([`publishing/admin_auth.py`](../../publishing/admin_auth.py)). Leave the token
out and this public URL serves health and the webhook and nothing else — the
credential endpoints are not reachable from the internet at all. You lose nothing
operationally, because you manage credentials from the laptop's dashboard.

The visible cost: `health` reports **`"ok": false`** forever, with one warning
saying no admin identity is configured. That is accurate and harmless — and it is
why the heartbeat judges liveness on `role`, never on `ok`. A monitor keyed on
`ok` would alert every ten minutes for the rest of time.

Add the token (32+ random bytes: `openssl rand -hex 32`) only if you want to
administer publishing while the laptop is off — and then it is the single thing
standing between the internet and your sealed provider key.

## Step 3 — The heartbeat

Without this, the Space sleeps and you are back where you started.

```powershell
cd deploy/publisher/heartbeat
npm test                       # 21 tests, no dependencies — Node's own runner
npx wrangler login             # browser; no card
```

Set `HEARTBEAT_URL` in [`wrangler.toml`](heartbeat/wrangler.toml) to the Space's
health URL, then optionally wire up the two extras:

```powershell
npx wrangler secret put TELEGRAM_BOT_TOKEN      # same bot as cloud/alerts.py
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler kv namespace create HEARTBEAT_STATE
# paste the printed id into wrangler.toml and uncomment the kv_namespaces block
npx wrangler deploy
```

Both extras are optional and independent. Without Telegram the Worker still keeps
the Space awake, silently. Without KV it still alerts, but at most once an hour
instead of exactly once per outage. With both: one message when the publisher
dies, one when it comes back, and nothing in between.

Check it immediately, without waiting for a tick — the Worker's own URL runs the
probe on demand and deliberately sends no alert:

```powershell
curl.exe "https://openshorts-publisher-heartbeat.<your-subdomain>.workers.dev"
```

`{"live":true,"degraded":false,"kind":"live",...}` means the pulse works. Live
cron logs: `npx wrangler tail`.

## Step 4 — Point the laptop at the shared database and the new URL

In the laptop's `.env`:

```
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres
PUBLISHING_PUBLIC_BASE_URL=https://<hf-username>-openshorts-publisher.hf.space
```

Then restart the backend — **`restart`, not `up -d`**. Source is bind-mounted and
uvicorn runs without `--reload`, so `up -d` prints `Running` and keeps serving the
old code and the old config.

Finally, paste the callback into the Status 200 dashboard:

```
https://<hf-username>-openshorts-publisher.hf.space/api/publishing/webhook/status200
```

That closes the `unknown` hole for good. A submit that gets no confirmation ages
into `unknown`, which is terminal and never auto-retried — because a submit
timeout may mean the post is already live. With a callback that lands, posts get
confirmed instead. **ngrok is no longer needed for anything.**

Proof you are on the shared database: the dashboard still lists your existing
batches and history. An empty Publishing tab means the laptop is still talking to
its own local Postgres — check the host in `DATABASE_URL`.

## Step 5 — Verify the thing you actually wanted

1. Plan a batch with slots 30–60 minutes out.
2. Watch the laptop until each clip logs `✅ Publishing: staged … KB/s`.
3. **Close the laptop.**
4. At the slots, check the platforms. The Space's Logs tab shows each submit; the
   dashboard (once the laptop is back) shows the attempts as submitted/succeeded
   with their provider refs.

Step 3 is the test. Everything else in this document exists to make it pass.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Attempt reason: *waiting for this clip's media to be staged…* | bytes never reached the bucket | keep the laptop awake until `✅ Publishing: staged` appears; the post is parked, not lost |
| Every submit fails authentication right after the move | `PUBLISHING_MASTER_KEY` differs between hosts | copy it across byte-for-byte; do not generate a new one |
| Attempt reason mentions *cannot be decrypted* | same cause, detected earlier | as above, or re-enter the key in the admin UI to re-seal it |
| Space fails with `Network is unreachable` | the direct (IPv6-only) Supabase host | use the **session pooler** host from Step 1 |
| Space fails with a prepared-statement or `DuplicatePreparedStatement` error | the **transaction** pooler (port 6543) | switch to the session pooler (5432) |
| Space boots but the code looks old | the build fetched the GitHub branch, not your laptop | commit and push, then **Settings → Factory rebuild** |
| Space build fails at the download step | wrong `REPO`/`REF` in the Dockerfile, or the repo is not public | check `ARG REPO`; the URL must be fetchable with no auth |
| `health` shows `ok: false` | almost always the deliberate missing admin token | read `warnings`; if that is the only one, this is correct — see [the exposure decision](#the-one-decision-to-make-deliberately) |
| `media_strategy` is not `objectstore_presigned` | `PUBLISHING_S3_*` incomplete on the Space | re-add the five secrets; the publisher has no clip bytes to fall back on |
| Heartbeat alerts once after every rebuild | a waking Space answers 503 for a minute or two | expected: an alert needs two consecutive failures, so this means it stayed down for 20 min |
| Telegram alerts repeat hourly | no KV namespace bound, so the Worker cannot remember it told you | bind KV (Step 3), or fix the outage |
| Dashboard's Publishing tab is empty after Step 4 | laptop still on its local Postgres | fix the host in `DATABASE_URL`, then restart the backend |
| A whole plan fires at once instead of spacing out | `PUBLISHING_REMOTE_SCHEDULE=on` somewhere | unset it; no provider holds a schedule, so `on` is diagnostics-only |
| Supabase project shows as paused | 7 days with no activity at all | resume it in the dashboard; a running publisher prevents it |

## Still needs a human

- **The `unknown` attempts.** ~18 rows from before this deployment. `unknown`
  means a submit timed out ambiguously — the post may already be live. Check each
  against YouTube Studio / TikTok / the Status 200 history **before** retrying;
  a blind retry double-publishes to a real audience.
- **Rotate the Supabase keys** that were pasted into a chat transcript. Rotate in
  Supabase, then update `PUBLISHING_S3_*` in the laptop's `.env` *and* in the
  Space's secrets.

---

## Appendix A — a box you control

The files beside this README — [`Dockerfile`](Dockerfile),
[`docker-compose.yml`](docker-compose.yml), [`.env.example`](.env.example) — are a
verified always-on deployment of the same subsystem: `publishing/runner.py` under
uvicorn plus its own Postgres, on any VM or spare machine that stays on. Use it if
the Space proves unreliable, if you would rather own the database, or if you want
no free-tier tenancy at all.

The shape: `cp .env.example .env`, fill it in (same master key, same bucket),
`docker compose up -d --build`, and reach it at
`http://127.0.0.1:8000/api/publishing/health`. Postgres is published on
`DB_BIND_ADDR` only, so pick a private address (Tailscale gives both machines one
for free) and let the laptop connect over that.

Two traps worth carrying over, both cost real time:

> **`ports:` is not filtered by ufw.** Docker writes DNAT rules that are traversed
> before the INPUT chain, so a published port is reachable from the internet even
> while `ufw status` reports it denied. The bind address is the control that
> actually holds. Never publish Postgres to `0.0.0.0`.

> **The `db` healthcheck must probe over TCP** (`pg_isready -h 127.0.0.1`). During
> `initdb` the entrypoint runs a temporary server with `listen_addresses=''`, so a
> socket probe passes while connections are still refused — and the publisher
> starts, fails, and flaps. The comment in `docker-compose.yml` explains it.

A device you already own — a Raspberry Pi, a NAS, an old laptop — is this
appendix verbatim, and is the most durable option in this document.

## Appendix B — other card-free clocks

Both of these run [`publishing/tick.py`](../../publishing/tick.py) (one pass then
exit) instead of `runner.py`, and both accept the granularity cost that the Space
avoids: a slot is honoured by the first tick after it.

- **GitHub Actions cron.** `schedule:` on a workflow running
  `python -m publishing.tick` with the env as repository secrets. Genuinely free
  and unlimited here **because this repo is public** (a private repo would spend
  ~4,300 min/month against a 2,000-minute allowance at a 10-minute cron). Two
  drawbacks: scheduled workflows are best-effort and can run ten-plus minutes
  late under load, and there is no HTTPS endpoint, so webhooks stay unlanded and
  confirmation falls back to `worker.reconcile_once` polling. A Cloudflare Worker
  can fix the punctuality half by calling `workflow_dispatch` instead of relying
  on GitHub's schedule queue.
- **Modal scheduled functions.** [`modal_app.py`](modal_app.py) is a complete,
  working deployment of the same tick — it was the original plan and is kept for
  anyone who can use it. **It requires a credit card at signup**, which is why it
  is in an appendix rather than the runbook.

## Footnote: the one platform that can hold its own schedule

YouTube's Data API does real server-side scheduling — `videos.insert` with
`privacyStatus: private` plus `publishAt`, free, roughly 6 uploads/day inside the
default 10,000-unit quota. It would remove this host from the loop for YouTube
alone, but not for TikTok or Instagram, and it means a second integration path
alongside Status 200. Worth knowing; not worth splitting the pipeline for while
one always-on process covers all three uniformly.
