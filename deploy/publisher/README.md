# Always-on publisher — a free container host + Cloudflare cron (card-free, $0)

> **Host history, so the vendor names below read correctly.** This ran on a
> Hugging Face Space until 2026-08-20, when `/new-space` began gating every
> compute SDK behind PRO — *"Gradio and Docker Spaces require a paid plan. Static
> Spaces stay free for everyone."* Static runs no compute, so HF is out. Steps 0,
> 1, 4 and 5 never depended on the host and are unchanged; **Step 2 is now written
> for any container host that builds from a git repo**, with per-vendor settings.
> [Appendix C](#appendix-c--choosing-the-host) is the host comparison and the
> fallback if none of them can be signed up for.
>
> Existing free Docker Spaces are not evidence the gate does not exist — they
> predate it, and querying the HF API for them is how you talk yourself back into
> a paywall. A creation form is the only authority on what a creation form allows.

A schedule needs a clock that is awake at the slot. Nothing in the chain provides
one: Status 200 accepts `scheduledFor` and discards it (measured across three
real posts — see [`providers/status200.py`](../../publishing/providers/status200.py)),
TikTok's Content Posting API has no scheduling, and Instagram's Graph API has no
scheduled publish. So the clock is ours, and on a single-machine deploy it is the
laptop's: a slot fires only if the laptop happens to be awake at it.

This directory moves the clock off the laptop, and leaves the video pipeline —
GPU, models, clip files — at home.

```
   laptop (Docker, as today)              free container host (always awake)
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
| free container host | ✅ `runner.py` unmodified | ❌ sleeps when idle | ✅ |
| Cloudflare Workers | ❌ V8/Pyodide: no asyncpg, no C extensions, 10 ms CPU | ✅ | ✅ |
| **both** | ✅ | ✅ — the cron keeps the publisher awake | ✅ |

Cloudflare cannot host the publisher and it is not being asked to. Claiming a
Postgres row, unsealing an AES-256-GCM credential and submitting with a 120 s
read timeout is not a 10 ms-CPU workload, and porting it to JavaScript would mean
a **second implementation of the duplicate-post guard** — the one piece of this
system where a divergence publishes twice to a real audience. So Cloudflare gets
the job it is actually good at: `fetch()` one URL every ten minutes, which costs
milliseconds of CPU because waiting on the network is not CPU time.

**One consequence worth knowing up front:** because this host runs the always-on
`runner.py` rather than a cron, there is **no scheduling granularity penalty**.
Its dispatch loop polls every `PUBLISHING_DISPATCH_INTERVAL` (default 5) seconds,
so a slot at 18:03 fires at 18:03. The ten-minute cron is a *pulse*, not the
clock — it only has to be frequent enough to prevent an idle timeout.

## Division of labour

| | laptop | publisher host | CF Worker |
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
| Free container instance | $0, no card — *confirm that on the signup form, which is the only authority* | sleeps on inactivity, which is what the Worker is for. The vendor may also restart or evict it; boot recovery handles that. |
| Cloudflare Workers | $0, no card; 100k req/day, 5 crons | uses 1 cron and ~4,300 requests/month |
| Cloudflare KV (optional) | 1k writes/day | only written when the up/down state changes, so ~0/day |
| Supabase Postgres | 500 MB, no card | pauses after **7 days with no activity** — the publisher's own polling is activity, so it never triggers here |
| Supabase storage | existing bucket | 50 MB per object on the free plan; a clip over that fails as `media.stage_failed` |

The honest risk is not cost, it is **tenancy**: a free instance is the vendor's
to restart, evict, or put behind a paywall — and one of them has already done the
last of those, mid-runbook. The mitigation is that nothing here is vendor-specific:
`runner.py` and `tick.py` take no host's SDK, and [`Dockerfile`](Dockerfile) is an
ordinary Dockerfile. Moving is a new service, the same nine secrets, and one line
in `wrangler.toml` ([Appendix A](#appendix-a--a-box-you-control),
[Appendix B](#appendix-b--other-card-free-clocks)).

---

## Step 0 — Push the code first

The host builds from **GitHub**, not from your laptop: you connect it to the
repository and it clones the branch. Whatever is unpushed is not in the build.

```powershell
git status --short
git add -A; git commit -m "publishing: off-box scheduling clock"; git push
```

**Mind which remote.** This working tree has two, and only one of them has this
subsystem:

| remote | repo | state |
|---|---|---|
| `khalil` | `ferjanikhalil/openshorts` | what the branch tracks, and what `git push` with no argument updates — **has `publishing/`** |
| `origin` | `mutonby/openshorts` | a divergent line with **no `publishing/` at all** |

Both are public, and that is what makes the mistake quiet: connect a host to the
wrong one and the clone succeeds, then the build dies saying
`deploy/publisher/Dockerfile: no such file` — which reads like a typo in a path
rather than the wrong repository. Verify the branch really serves the build's
inputs before you create the service:

```powershell
foreach ($p in 'deploy/publisher/Dockerfile','publishing/runner.py','deploy/publisher/requirements.txt','requirements-publishing.txt','cloud/database.py') {
  $u = "https://api.github.com/repos/ferjanikhalil/openshorts/contents/$p`?ref=main"
  try { $s = (Invoke-WebRequest $u -Headers @{'User-Agent'='openshorts'} -ErrorAction Stop).StatusCode }
  catch { $s = $_.Exception.Response.StatusCode.value__ }
  "{0,-38} HTTP {1}" -f $p, $s
}
```

Five `HTTP 200`s and the build has everything it needs. Any `404` means either the
push did not land, or you are looking at the wrong repository.

This is also the step that makes the rest cheap: the host holds no second copy of
`publishing/` to drift out of sync, so deploying is `git push` plus a redeploy —
and most hosts redeploy on push by themselves.

> The repo being public also means **no secret may ever enter it** — not in the
> Dockerfile, not in a compose file, not in a build arg. Every credential below
> goes into the host's secret store or a Wrangler secret. `.env` is gitignored
> (`.gitignore:28`), and `*.sql` now is too, so a database dump cannot be
> committed by a careless `git add -A`. Keep it that way.

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
> host that does not exist. A **space** must become `%20` and an apostrophe is
> safest as `%27`; both are legal in a Supabase-set password and neither is
> obvious. Verify the encoded URL before trusting it — `psql` percent-decodes the
> same way SQLAlchemy does, so it is a valid test:
>
> ```powershell
> docker run --rm postgres:16-alpine `
>   psql "postgresql://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres" `
>   -t -c "select 'url OK'"
> ```

### Bring the existing data across

Your groups, provider credential, destinations and history live in the laptop's
local Postgres. If the stack is stopped, start just the database — there is no
reason to boot the app for a dump:

```powershell
docker compose -f docker-compose.yml -f docker-compose.publishing.yml up -d db
docker inspect --format '{{.State.Health.Status}}' openshorts-db   # wait for: healthy
```

Then dump and restore, using a container so nothing needs installing:

```powershell
$dump = "$HOME\Desktop\openshorts-db-dump"      # OUTSIDE the repo — see below
New-Item -ItemType Directory -Force $dump | Out-Null
docker exec openshorts-db pg_dump -U openshorts -d openshorts `
  --no-owner --no-privileges -f /tmp/openshorts.sql
docker cp openshorts-db:/tmp/openshorts.sql "$dump\openshorts.sql"
```

Two things about that snippet are deliberate. Write the file *inside* the
container and copy it out, rather than redirecting `pg_dump`'s stdout — PowerShell's
`>` adds a BOM that `psql` chokes on. And keep the dump **outside the working
tree**: it contains the `publish_credentials` rows and the whole audit trail,
`*.sql` is a plausible thing to forget in a `git add -A`, and this repo is public.

```powershell
docker run --rm -v ${dump}:/dump postgres:16-alpine `
  psql "postgresql://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres" `
  -v ON_ERROR_STOP=1 -q -f /dump/openshorts.sql
```

Note the plain `postgresql://` scheme here: `+asyncpg` is a SQLAlchemy dialect,
meaningless to `psql`. `ON_ERROR_STOP=1` matters because the target's `public`
schema should be empty — if it is not, you want the restore to abort on the first
`CREATE TABLE` collision rather than half-apply. Check before you start:

```powershell
docker run --rm postgres:16-alpine `
  psql "postgresql://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres" `
  -t -c "select coalesce(string_agg(tablename,', '),'(empty)') from pg_tables where schemaname='public';"
```

Then prove it landed. Row counts, and — more importantly — the guard:

```powershell
docker run --rm postgres:16-alpine `
  psql "postgresql://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres" `
  -t -c "select count(*) from publish_destinations; select count(*) from publish_attempts; `
         select indexdef from pg_indexes where indexname='uq_attempt_live_per_destination';"
```

The counts must match the source. The index must exist — it is the partial unique
index that makes double-posting impossible, and it is the one object in this
migration whose absence would not show up until it had already let a clip post
twice. A dump carries it, but confirm rather than assume.

Those two counts are the proof. Keep the laptop's `db` volume as a cold backup —
do not delete it.

## Step 2 — Deploy the publisher container

Any host that builds a Dockerfile out of a git repo and hands back an HTTPS URL
will do; [Appendix C](#appendix-c--choosing-the-host) is the comparison and the
fallback if none of them can be signed up for. Everything in this step is the same
whichever one you pick.

### What every host needs to know

| Setting | Value | Why this value |
|---|---|---|
| Repository / branch | `ferjanikhalil/openshorts`, `main` | Step 0 — the other remote has no `publishing/` at all |
| Dockerfile path | `deploy/publisher/Dockerfile` | **not** [`space/Dockerfile`](space/Dockerfile), which downloads a source tarball because a Hugging Face Space has no repo to build from. A host that clones needs none of that. |
| Build context | the **repository root**, `.` | [`requirements.txt`](requirements.txt) beside the Dockerfile includes `-r ../../requirements-publishing.txt`, so both halves of the split pin identical DB versions — and pip only resolves that include from the root |
| Instance size | the free one | 512 MB is ample: no torch, no ffmpeg, no models, no clip bytes. The image is ~220 MB. |
| Port | leave it to the host | the image binds `$PORT` when one is injected and falls back to 8000, so there is nothing to configure |
| Health check path | `/api/publishing/health` | but read the warning below before you enter it anywhere |
| Persistent disk | none | the publisher holds no local state: the queue is in Postgres, the clips are in the bucket. A disk would only be a thing to get out of sync. |
| Region | nearest the Supabase project (eu-west-1 → Frankfurt / Europe) | every claim, dispatch and webhook drain is a round trip to that database |

> **Do not point a host's health check at `ok`.** The endpoint answers HTTP 200
> unconditionally, so a plain "is it 200" check passes — but the `ok` field is
> `false` here permanently and *by design* (see [the exposure
> decision](#the-one-decision-to-make-deliberately)). A host that restarts a
> service on a false `ok` gives you a restart loop instead of a red dot. The
> image's own `HEALTHCHECK` and the Step 3 Worker both judge liveness on
> `role == "publisher"` for exactly this reason.

### Per-vendor translation

Field labels drift between redesigns; match them by meaning, not by string.

| | Render | Koyeb |
|---|---|---|
| Create | New → **Web Service** → language/runtime **Docker** | Create Service → **Web Service** → builder **Dockerfile** |
| Where the path goes | *Dockerfile Path* = `./deploy/publisher/Dockerfile`, *Docker Build Context Directory* = `.` | *Dockerfile location* = `deploy/publisher/Dockerfile`, *work directory* left empty |
| Free tier | Free instance, 512 MB / 0.1 CPU | one Free instance **per organization**, Web Service only, 512 MB / 0.1 vCPU / 2 GB SSD |
| Regions | several | Frankfurt or Washington DC only |
| Idle | spins down after ~15 min without traffic | scales to zero after **1 h** without traffic |
| URL | `https://<name>.onrender.com` | `https://<name>-<org>.koyeb.app` |

Koyeb's column was read from their own docs; Render's was not (their docs blocked
an automated fetch), so treat it as a starting point and confirm on the form —
including the two disqualifiers in [Appendix C](#appendix-c--choosing-the-host).
One number to check on any host with an hours cap: a month is 730 hours, so an
always-on service consumes essentially the whole allowance and a second free
service would not fit beside it.

**Idle spin-down is not a disqualifier.** Step 3's heartbeat exists precisely to
prevent it, and if a cold start does happen it costs one late post, not a lost one,
because the queue is durable and slots are honoured at the next dispatch tick.

### The secrets

Add all nine as **secrets** / environment variables on the service:

```
PUBLISHING_ENABLED=1
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>:5432/postgres
PUBLISHING_MASTER_KEY=<copied from the laptop's .env, byte for byte>
PUBLISHING_S3_ENDPOINT=<copied from the laptop's .env>
PUBLISHING_S3_BUCKET=<copied from the laptop's .env>
PUBLISHING_S3_REGION=<copied from the laptop's .env>
PUBLISHING_S3_ACCESS_KEY_ID=<copied from the laptop's .env>
PUBLISHING_S3_SECRET_ACCESS_KEY=<copied from the laptop's .env>
PUBLISHING_PUBLIC_BASE_URL=https://<the URL the host just gave you>
```

`PUBLISHING_ROLE` is deliberately **not** in that list: the image sets
`PUBLISHING_ROLE=publisher` in its own `ENV`, because this image is only ever the
clock half of a split deployment, so "there are no clip files here" is a property
of what it is rather than a choice an operator makes. It matters that it is not
forgettable — the default is `full`, and a publisher reporting `full` is judged
dead by the Step 3 heartbeat, which then alerts every ten minutes forever about a
service that is working perfectly. If you ever deploy this subsystem *without* this
Dockerfile, set it yourself.

`PUBLISHING_PUBLIC_BASE_URL` is the one value you cannot fill in before the service
exists. Most hosts derive the hostname from the service name, so you can usually
predict it; if not, deploy once, copy the URL, set the secret, and redeploy. Until
it is right, presigned media URLs point somewhere the provider cannot fetch.

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

### Confirm it

Expected in the host's log stream once the build finishes:

```
📡 Publishing mode ENABLED (DB ready, dispatch + reconcile + transfer active,
   media: objectstore_presigned).
```

Then from your machine:

```powershell
curl.exe "https://<publisher-host>/api/publishing/health"
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

Without this, a free instance sleeps on idle and you are back where you started.

```powershell
cd deploy/publisher/heartbeat
npm test                       # 21 tests, no dependencies — Node's own runner
npx wrangler login             # browser; no card
```

Set `HEARTBEAT_URL` in [`wrangler.toml`](heartbeat/wrangler.toml) to the health
URL from Step 2 — `https://<publisher-host>/api/publishing/health`. It is a
`[vars]` entry, not code, so moving hosts later is this one line. Then optionally
wire up the two extras:

```powershell
npx wrangler secret put TELEGRAM_BOT_TOKEN      # same bot as cloud/alerts.py
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler kv namespace create HEARTBEAT_STATE
# paste the printed id into wrangler.toml and uncomment the kv_namespaces block
npx wrangler deploy
```

Both extras are optional and independent. Without Telegram the Worker still keeps
the publisher awake, silently. Without KV it still alerts, but at most once an hour
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
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<encoded-password>@<pooler-host>:5432/postgres
PUBLISHING_PUBLIC_BASE_URL=https://<publisher-host>
```

`DATABASE_URL` is only read from `.env` because `docker-compose.publishing.yml`
spells the override out as `${DATABASE_URL:-…<local db>…}`. Compose's
`environment:` block wins over `.env`, so a service that hardcodes the value
ignores your entry completely — it does not warn, it just keeps using the local
database. If you ever add another service that needs the shared queue, give it the
same `${DATABASE_URL:-…}` form.

Then apply it with **`up -d`, not `restart`**:

```powershell
docker compose -f docker-compose.yml -f docker-compose.publishing.yml up -d backend
```

The distinction is worth getting right, because each command is the wrong one for
the other kind of change:

- **Config changed** (this step): `up -d`. Compose compares the resolved config to
  the running container, sees the new `DATABASE_URL`, and prints `Recreate`.
  `restart` reuses the existing container definition and would silently keep the
  old value.
- **Only source code changed:** `restart`. The source is bind-mounted and uvicorn
  runs without `--reload`, so `up -d` finds no config drift, prints `Running`, and
  leaves the old code being served.

Finally, paste the callback into the Status 200 dashboard:

```
https://<publisher-host>/api/publishing/webhook/status200
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
4. At the slots, check the platforms. The host's log stream shows each submit; the
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
| Signup wants a card, or the free plan says "personal / non-commercial" | the tier is disqualified, not the runbook | pick another candidate; [Appendix C](#appendix-c--choosing-the-host) lists them and the two checks |
| Hugging Face: `/new-space` demands PRO | since 2026-08-20 every compute SDK (Docker *and* Gradio) is PRO-gated, and Static runs no compute | not fixable — which is why Step 2 is vendor-neutral |
| The publisher fails with `Network is unreachable` | the direct (IPv6-only) Supabase host | use the **session pooler** host from Step 1 |
| The publisher fails with a prepared-statement or `DuplicatePreparedStatement` error | the **transaction** pooler (port 6543) | switch to the session pooler (5432) |
| It boots but the code looks old | the host built the pushed branch, not your working tree | commit and push, then redeploy; if it still looks old, clear the build cache |
| Build fails: `deploy/publisher/Dockerfile: no such file` | the host is cloning the wrong repository — see Step 0's remote table | point it at `ferjanikhalil/openshorts`; a clone succeeding proves nothing about the contents |
| Build fails in `pip install`, cannot find `requirements-publishing.txt` | build context set to `deploy/publisher/` instead of the repo root | set the context to `.` — the requirements file includes `-r ../../requirements-publishing.txt` |
| The host calls the service unhealthy while it is publishing fine | a health check keyed on `ok`, which is false by design | check for HTTP 200, or for `role` — never `ok` |
| `health` shows `ok: false` | almost always the deliberate missing admin token | read `warnings`; if that is the only one, this is correct — see [the exposure decision](#the-one-decision-to-make-deliberately) |
| `media_strategy` is not `objectstore_presigned` | `PUBLISHING_S3_*` incomplete on the host | re-add the five secrets; the publisher has no clip bytes to fall back on |
| Heartbeat alerts once after every deploy | a waking free instance answers 503 for a minute or two | expected: an alert needs two consecutive failures, so this means it stayed down for 20 min |
| Telegram alerts repeat hourly | no KV namespace bound, so the Worker cannot remember it told you | bind KV (Step 3), or fix the outage |
| Dashboard's Publishing tab is empty after Step 4 | laptop still on its local Postgres | check the host in `DATABASE_URL`, then `up -d` (not `restart`) |
| `DATABASE_URL` in `.env` appears to have no effect | a compose `environment:` entry hardcodes it, which beats `.env` | use the `${DATABASE_URL:-…}` form, as `docker-compose.publishing.yml` now does |
| Restore aborts on `CREATE TABLE … already exists` | the target's `public` schema is not empty | inspect it first; do not re-run a half-applied restore blind |
| A whole plan fires at once instead of spacing out | `PUBLISHING_REMOTE_SCHEDULE=on` somewhere | unset it; no provider holds a schedule, so `on` is diagnostics-only |
| Supabase project shows as paused | 7 days with no activity at all | resume it in the dashboard; a running publisher prevents it |

## Still needs a human

- **The `unknown` attempts.** ~18 rows from before this deployment. `unknown`
  means a submit timed out ambiguously — the post may already be live. Check each
  against YouTube Studio / TikTok / the Status 200 history **before** retrying;
  a blind retry double-publishes to a real audience.
- **Rotate the Supabase keys** that were pasted into a chat transcript. Rotate in
  Supabase, then update `PUBLISHING_S3_*` in the laptop's `.env` *and* in the
  publisher host's secrets.

---

## Appendix A — a box you control

The files beside this README — [`Dockerfile`](Dockerfile),
[`docker-compose.yml`](docker-compose.yml), [`.env.example`](.env.example) — are a
verified always-on deployment of the same subsystem: `publishing/runner.py` under
uvicorn plus its own Postgres, on any VM or spare machine that stays on. Use it if
a free instance proves unreliable, if you would rather own the database, or if you
want no free-tier tenancy at all.

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
exit) instead of `runner.py`, and both accept the granularity cost that an
always-on host avoids: a slot is honoured by the first tick after it.

- **GitHub Actions cron.** `schedule:` on a workflow running
  `python -m publishing.tick` with the env as repository secrets. Genuinely free
  and unlimited here **because this repo is public** (a private repo would spend
  ~4,300 min/month against a 2,000-minute allowance at a 10-minute cron). It can
  *submit* posts, and that half works.

  **What it cannot do is confirm them** — and this is worse than a missing
  nicety. A workflow run has no inbound HTTPS endpoint, so the provider's webhook
  has nowhere to land, and there is nothing to fall back on: Status 200 exposes
  no status lookup ([`providers/status200.py`](../../publishing/providers/status200.py)
  sets `supports_status_lookup=False` and `fetch_status` returns `None`), so
  there is no polling loop anywhere in this system. Every submit then ages out
  through `service.sweep_stale_submitted` into **`unknown`** once
  `PUBLISHING_SUBMIT_TIMEOUT` has passed — terminal, never auto-retried, and
  cleared only by a human opening the account and looking. **The fix is a mailbox
  that never sleeps** —
  [Appendix C, Option 2](#appendix-c--choosing-the-host) — not a
  different cron.

  Smaller, separate drawback: scheduled workflows are best-effort and can run
  ten-plus minutes late under load. A Cloudflare Worker calling
  `workflow_dispatch` fixes that half, but not the one above.
- **Modal scheduled functions.** [`modal_app.py`](modal_app.py) is a complete,
  working deployment of the same tick — it was the original plan and is kept for
  anyone who can use it. **It requires a credit card at signup**, which is why it
  is in an appendix rather than the runbook.

## Appendix C — choosing the host

Steps 0, 1, 4 and 5 do not depend on the host. Steps 2 and 3 are written for
whichever option you take here — and the reason there is a choice at all is that
Hugging Face closed its door on 2026-08-20 (see the banner at the top).

**Any replacement has to do two separate jobs.** Confusing them is what makes
"just use GitHub Actions" sound sufficient:

| Job | Why | Can a cron-only host do it? |
|---|---|---|
| **Hold the clock** — be running when a slot arrives, and submit | no provider holds a schedule | **yes** — this is exactly [`publishing/tick.py`](../../publishing/tick.py) |
| **Hold an HTTPS URL** — 24/7, to receive the provider's callback | Status 200 exposes no status lookup, so a webhook is the *only* completion signal | **no** — and this is the one that bites |

The second row is not a nicety. `providers/status200.py` sets
`supports_status_lookup=False` and `fetch_status` returns `None`, so there is no
polling loop anywhere in this system —
[`publishing/webhooks.py`](../../publishing/webhooks.py) opens by saying so. With
no endpoint, every submit ages out through `service.sweep_stale_submitted` into
**`unknown`**: terminal, never auto-retried, resolvable only by a human opening
three platform dashboards. You already have ~18 of those rows and they are the
worst state in the system to manufacture more of.

### Option 1 — another free always-on container (chosen; Step 2 is written for it)

If it builds a Dockerfile from a git repo and gives a public HTTPS URL,
**nothing in this repo changes**: [`Dockerfile`](Dockerfile) already builds from a
repo root, already binds whatever `$PORT` the host injects, and already declares
`PUBLISHING_ROLE=publisher`. Step 2's nine secrets and Step 3's heartbeat are the
same with one hostname substituted. Candidates: Render, Koyeb, Northflank, Back4App
Containers, Clever Cloud.

[`space/`](space/) is Hugging-Face-only and now unused — its Dockerfile downloads
a source tarball because a Space has no repository to build from. Kept in case the
gate ever lifts; ignore it otherwise.

Two things decide it, and both are one glance at the signup form — faster and more
authoritative than any documentation, which is the lesson of this appendix:

1. **Does it demand a card?** That is the constraint that has already eliminated
   Oracle, Modal and Fly.
2. **Does its free tier forbid commercial use?** OpenShorts takes Stripe payments,
   so a "hobby / personal projects only" clause disqualifies the tier regardless of
   price. This rules out Vercel's Hobby plan specifically.

A free tier that sleeps on idle is fine, not disqualifying — that is what the
Step 3 heartbeat already exists for. A cold start of a minute costs one late post,
not a lost one, because the queue is durable.

### Option 2 — GitHub Actions for the clock, a Cloudflare Worker for the mailbox

Certain to be card-free, because both are accounts you already have, and free on a
**public** repo (unlimited Actions minutes; a private repo would burn ~4,300
min/month against a 2,000-minute allowance at a 10-minute cron). It needs code
this repo does not have yet — roughly 150 lines plus tests — and it works like
this:

```
Cloudflare Worker            GitHub Actions (every 5–10 min)
┌──────────────────────┐     ┌──────────────────────────────┐
│ POST /webhook/:prov  │     │ python -m publishing.tick    │
│  · size-cap, ack 200 │◄────┤  · GET  the parked bodies    │
│  · park body + sig   │     │  · verify with the EXISTING  │
│    in Workers KV     ├────►│    signing.py + crypto.py    │
└──────────────────────┘     │  · DELETE what it applied    │
   never sleeps, holds       │  · then reconcile + dispatch │
   the URL 24/7              └──────────────────────────────┘
```

The Worker parks the raw body and its `X-Webhook-Signature` header and returns 200
immediately. It does **no** crypto and **no** payload parsing — signature
verification and `parse_webhook` stay in Python, called through the same code path
the FastAPI route uses today, which is the whole point: no second implementation of
anything security-critical, and no provider knowledge outside `providers/`. It
happens to be strictly *more* reliable than a direct endpoint at receiving, since
a host that is asleep at delivery time no longer loses the callback (Status 200
retries only at 1m/5m/30m).

Work required: extract the verify-and-persist body of `webhooks.receive_webhook`
into a function both entrypoints call, add a KV drain to `worker.reconcile_once`
(so `runner.py` and `tick.py` both get it), and write the Worker. Two traps to
handle while doing it:

- **`PUBLISHING_SUBMIT_TIMEOUT` must comfortably exceed the tick interval.** A
  confirmation now lands one tick late, and the default 1800s against a 10-minute
  cron that GitHub runs late leaves a thin margin before the sweeper calls a
  confirmed post `unknown`. Raise it.
- **`PUBLISHING_MASTER_KEY` moves into a public repo's Actions secrets.** Fork PRs
  cannot read them and logs mask them, but anyone who can push to `main` can print
  them. That is a genuinely larger blast radius than a Space's secrets, and it is
  the key that unseals every stored provider credential. Weigh it deliberately;
  the [Appendix A](#appendix-a--a-box-you-control) box does not have this problem.

Also worth knowing: point the Worker's `scheduled` handler at
`workflow_dispatch` rather than relying on GitHub's own `schedule:`, which is
best-effort and can run ten-plus minutes late. That makes the Worker the clock and
Actions merely the runtime — and it is the same Worker Step 3 already deploys, with
a different target.

### Ranking

Option 1 if any candidate passes both checks: zero new code, and the runbook holds
with one hostname substituted. Option 2 if none do — it is certain, and it is the
only path here that depends on nothing but accounts you already have. Appendix A
if you have or can borrow a machine that stays on, which remains the most durable
answer in this document. **GitHub Actions alone, with no mailbox, is not an
option** — that is the `unknown`-manufacturing configuration described above.

## Footnote: the one platform that can hold its own schedule

YouTube's Data API does real server-side scheduling — `videos.insert` with
`privacyStatus: private` plus `publishAt`, free, roughly 6 uploads/day inside the
default 10,000-unit quota. It would remove this host from the loop for YouTube
alone, but not for TikTok or Instagram, and it means a second integration path
alongside Status 200. Worth knowing; not worth splitting the pipeline for while
one always-on process covers all three uniformly.
