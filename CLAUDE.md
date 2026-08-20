# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenShorts is an AI-powered vertical video generator that transforms long YouTube videos or local uploads into viral-ready short clips (9:16 format) for TikTok, Instagram Reels, and YouTube Shorts. Uses Google Gemini 2.0 Flash for viral moment detection and title generation.

## Development Commands

### Local Development (Docker)
```bash
docker compose up --build   # Build and run full stack
```
- Backend: http://localhost:8000 (FastAPI/Uvicorn)
- Frontend: http://localhost:5175 (Vite proxies API calls to backend)

### Frontend Only (Dashboard)
```bash
cd dashboard
npm install
npm run dev       # Dev server with HMR (port 5173)
npm run build     # Production build
npm run lint      # ESLint (strict, --max-warnings 0)
```

### Backend Only
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Architecture

### Core Processing Pipeline
1. **Ingest** - YouTube download (yt-dlp) or local upload
2. **Transcription** - faster-whisper with word-level timestamps
3. **Scene Detection** - PySceneDetect for segment boundaries
4. **AI Analysis** - Gemini identifies 3-15 viral moments (15-60 sec each)
5. **FFmpeg Extraction** - Precise clip cutting
6. **AI Cropping** - Vertical reframing with subject tracking
7. **Effects/Subtitles** - Optional AI-generated FFmpeg filters
8. **Hook Overlay** - Text overlays with styled fonts
9. **Voice Dubbing** - Optional ElevenLabs AI translation (30+ languages)
10. **S3 Backup** - Silent background upload
11. **Social Distribution** - Upload-Post API (async upload)

### Key Files
| File | Purpose |
|------|---------|
| `main.py` | Core video processing: transcription, scene detection, clip extraction, vertical reframing |
| `app.py` | FastAPI server with async job queue and REST endpoints |
| `editor.py` | Gemini AI integration for dynamic video effects (FFmpeg filter generation) |
| `hooks.py` | Hook text overlay generation with font rendering |
| `s3_uploader.py` | AWS S3 upload with caching |
| `subtitles.py` | SRT generation, FFmpeg subtitle burning, and dubbed video transcription |
| `translate.py` | ElevenLabs dubbing API for AI voice translation |
| `dashboard/src/App.jsx` | Main React component with state management |
| `dashboard/src/components/TranslateModal.jsx` | Voice dubbing UI with language selection |

### Dual-Mode Video Reframing
- **TRACK Mode** (single subject): MediaPipe face detection + YOLOv8 fallback with "Heavy Tripod" stabilization
- **GENERAL Mode** (groups/landscapes): Blurred background layout preserving full width

### Key Classes
- `SmoothedCameraman` - Stabilized camera movement with safe zone logic (prevents jitter)
- `SpeakerTracker` - Prevents rapid speaker switching, handles temporary occlusions

### API Endpoints
| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/process` | Submit video for processing |
| GET | `/api/status/{job_id}` | Poll job status and logs |
| POST | `/api/edit` | Apply AI video effects |
| POST | `/api/subtitle` | Generate and apply subtitles (auto-transcribes dubbed videos) |
| POST | `/api/hook` | Add text hook overlays |
| POST | `/api/translate` | AI voice dubbing via ElevenLabs |
| GET | `/api/translate/languages` | List supported dubbing languages |
| POST | `/api/social/post` | Post to social media (async upload) |

### Concurrency Model
Async job queue with semaphore-based concurrency control. Configure via `MAX_CONCURRENT_JOBS` env var (default: 5). Finished jobs (clips + source) auto-cleanup after `JOB_RETENTION_SECONDS` (default: 24 hours); this also bounds how long a project stays re-openable from the header's "Recent" menu in self-host mode.

## Automated Publishing (`publishing/`)

Optional subsystem that posts finished clips to YouTube Shorts, Instagram Reels and TikTok. Dormant unless `PUBLISHING_ENABLED` is set — independent of `BILLING_ENABLED` (works in self-host and cloud), but **requires Postgres** regardless of mode, because duplicate-post prevention, retry state and the audit trail have to survive a redeploy. Full detail lives in [`publishing/README.md`](publishing/README.md); this is the map.

- **Provider abstraction.** `providers/base.py` defines a 5-method `Provider` protocol + a `Capabilities` dataclass. There is no `if provider == "status200"` anywhere outside `providers/` — adding a provider is a new file plus a registry entry (mirrors `batch.OPERATIONS`). The first (only) adapter is **Status 200** (`providers/status200.py`); `providers/fake.py` mirrors its capabilities exactly and backs `PUBLISHING_DRY_RUN=1`, so the full pipeline runs with no credential and no real post.
- **Unit of publication is the destination, not the batch.** A `publish_group` ("Batch" in the UI) is a reusable bundle of `publish_destination` rows sharing one provider credential; a `publish_request` may span groups. This is what lets single-account, hand-picked multi-account, and whole-batch publishing share one code path (`service.expand_destinations`) instead of three.
- **Credentials are never exposed.** Entered through the admin UI only, sealed with AES-256-GCM before touching the database (`crypto.py`), and the API returns only a `fingerprint` + `last4` — never the plaintext. No provider key belongs in `.env`, compose files, frontend code, or logs.
- **`unknown` is terminal and never auto-retried.** A submit timeout is ambiguous (the post may already be live); blindly retrying it risks double-publishing to a real audience. A human resolves it.
- **The duplicate-post guard is a DB constraint**, not application logic: a partial unique index on `publish_attempts (publish_request_id, publish_destination_id)` for live/won states. `state.LIVE_STATES` must stay in lockstep with it.
- **No provider holds a schedule, so the slot is held here.** Status 200 accepts `post.scheduledFor` and posts immediately anyway (measured over three real posts; TikTok's and Instagram's APIs expose no scheduled publish either), so `supports_remote_schedule=False` and every slot is held by a local clock. The consequence: **something must be running when a slot arrives.** Two extra entrypoints move that clock off a laptop that sleeps — `publishing/runner.py` (always-on process) and `publishing/tick.py` (one pass then exit, for scheduled/serverless hosts). Both share one Postgres with the app and neither imports `app` or `main`; `tests/test_publishing_runner.py` and `tests/test_publishing_tick.py` enforce that in a subprocess, because a stray import would make the slim image unbuildable and nothing else would notice. It works because a submit reads no local bytes: the provider fetches the clip from the staging bucket by presigned URL. Deployment runbook: [`deploy/publisher/README.md`](deploy/publisher/README.md) (a free Hugging Face Space runs `runner.py`, a Cloudflare Worker cron keeps it from idling, Supabase holds the shared queue — all card-free; a box you control is Appendix A). Modal was the original plan and its working deployment is kept in `deploy/publisher/modal_app.py`, demoted to Appendix B because signup requires a credit card.
- **Two processes on one queue is a designed-for state, not an accident.** `FOR UPDATE SKIP LOCKED` claims and the DB duplicate guard make it safe with no broker, but the claim is committed in one transaction and the work happens in another — so `dispatcher.dispatch_attempt` re-checks that the claim is still ours before submitting, and boot recovery only re-queues claims quiet for `PUBLISHING_ORPHAN_CLAIM_MIN_AGE` (default 900s). Without the ownership check, one clip becomes two real posts; the unique index does not cover it, because that is one *row* submitted twice, not two rows.
- **Migrations.** The repo boots via `create_all`; `alembic/versions/20260809_publishing_baseline.py` is written defensively (checks the live catalogue before creating anything) so it's safe whether or not the tables already exist.
- **Tests** (`tests/test_publishing_*.py`) run in CI with no Postgres, no credentials, no network — pure-logic suites plus a provider contract suite against `httpx.MockTransport`.

## Environment Variables

**Server-side (.env):**
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_S3_BUCKET` - For S3 backup
- `MAX_CONCURRENT_JOBS` - Concurrent processing limit (default: 5)
- `VITE_API_URL` - Production API URL override

**Client-side (localStorage, encrypted):**
- `GEMINI_API_KEY` - Google Gemini API key (required)
- `ELEVENLABS_API_KEY` - ElevenLabs API key for voice dubbing (optional)
- `UPLOAD_POST_API_KEY` - Upload-Post API key for social posting (optional)

> API keys are stored encrypted in the browser and sent via headers only when needed. Never stored server-side.

**Publishing (`PUBLISHING_ENABLED`, server-side only):** `DATABASE_URL`, `PUBLISHING_MASTER_KEY` (32 bytes base64, wraps stored credentials), `PUBLISHING_MASTER_KEY_OLD` (rotation window), `PUBLISHING_ADMIN_TOKEN` / `PUBLISHING_ADMIN_EMAILS` (without one the admin router stays unmounted and nothing can publish), `PUBLISHING_PUBLIC_BASE_URL` (origin **the provider** fetches clips from — `localhost` cannot work), `PUBLISHING_S3_*` (staging bucket — the fast media origin, and what lets a submit read no local file), `PUBLISHING_ROLE` (`full` | `publisher`; read only by health, to tell "no clip files by design" apart from "the resolver failed to register"), `PUBLISHING_DRY_RUN`, plus queue tuning. See `.env.example`.

> In a split deployment both hosts need the **same** `PUBLISHING_MASTER_KEY` and the same bucket. A mismatched key surfaces at the first submit as an auth failure that looks exactly like a revoked provider key.

> Provider API keys are NOT env vars. They are entered through the admin UI, encrypted server-side, and never returned to the frontend.

## Tech Stack
- **Backend:** Python 3.11, FastAPI, google-genai, faster-whisper, ultralytics (YOLOv8), mediapipe, opencv-python, yt-dlp, FFmpeg, httpx
- **Frontend:** React 18, Vite 4, Tailwind CSS 3.4
- **External APIs:** Google Gemini, ElevenLabs Dubbing, Upload-Post
- **Infrastructure:** Docker + Docker Compose, AWS S3
