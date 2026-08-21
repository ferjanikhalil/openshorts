---
title: OpenShorts Publisher
emoji: 📡
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
short_description: Always-on publishing clock for OpenShorts
---

# OpenShorts publisher

> **Unused as of 2026-08-20.** Hugging Face now requires PRO for every Space SDK
> that runs compute, so this cannot be deployed on a free account. Kept in case
> that changes. The live deployment uses `deploy/publisher/Dockerfile` on a free
> container host instead — see the runbook linked at the bottom.

Not a demo — there is no UI here. This Space is a scheduling clock. It holds
publish slots for [OpenShorts](https://github.com/ferjanikhalil/openshorts) and
submits each clip to its social destination when the slot arrives, because no
provider in the chain will hold a schedule on our behalf: Status 200 accepts a
`scheduledFor` field and posts immediately anyway, and TikTok's and Instagram's
APIs expose no scheduled publish at all.

The video pipeline — GPU, models, clip files — stays on the machine that made the
clips. This Space needs none of it, because a submit needs no bytes: the provider
downloads the clip from object storage using a presigned URL registered hours
earlier. So at slot time this process needs Postgres, one master key and outbound
HTTPS, and nothing else.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /api/publishing/health` | Config sanity. Also what the heartbeat polls. |
| `POST /api/publishing/webhook/status200` | Provider callback. Paste this URL into the Status 200 dashboard. |

`"ok": false` with a single warning about **no admin identity configured** is the
expected, correct state here. The admin router is deliberately left unmounted so
that this public URL cannot reach the credential endpoints. See "the exposure
decision" in the runbook.

## Required secrets

Set these under **Settings → Variables and secrets**. All of them are secrets,
not variables — several are credentials and the rest reveal infrastructure.

| Secret | Notes |
|---|---|
| `PUBLISHING_ENABLED` | `1`. Without it the app refuses to boot rather than run with every loop dormant while reporting itself up. |
| `DATABASE_URL` | The shared Postgres, `postgresql+asyncpg://…`. Supabase's **session** pooler, port 5432 — not the direct host, not the transaction pooler. |
| `PUBLISHING_MASTER_KEY` | Byte-identical to the app host's. This unwraps the stored provider credential; a different value fails at the first submit, as an auth error that looks exactly like a revoked provider key. |
| `PUBLISHING_S3_ENDPOINT` `PUBLISHING_S3_BUCKET` `PUBLISHING_S3_REGION` `PUBLISHING_S3_ACCESS_KEY_ID` `PUBLISHING_S3_SECRET_ACCESS_KEY` | The same staging bucket the app host uploads into. Different buckets and every post parks forever waiting for media staged somewhere else. |
| `PUBLISHING_PUBLIC_BASE_URL` | This Space's own `https://…hf.space` URL. |

Deliberately **not** set: `PUBLISHING_ADMIN_TOKEN`. Add it only if you accept the
credential endpoints being reachable from the public internet.

Add `PUBLISHING_DRY_RUN=1` for a first boot: the whole queue, retry and webhook
path then runs against an in-repo fake provider, with no credential and nothing
reaching a real audience.

## Deploying a change

The image fetches the source from GitHub at build time, so this Space repo holds
only the `Dockerfile` and this README. To pick up a code change: push to the
branch, then **Settings → Factory rebuild**. Full runbook:
[`deploy/publisher/README.md`](https://github.com/ferjanikhalil/openshorts/blob/main/deploy/publisher/README.md).
