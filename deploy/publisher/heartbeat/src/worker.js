/**
 * OpenShorts publisher heartbeat — Cloudflare Worker cron.
 *
 * Why this exists
 * ---------------
 * The publisher runs on a free container host (Render, as of 2026-08-21) which
 * spins down on inactivity. Sleeping is the exact failure the whole split
 * deployment exists to remove: a slot that arrives while the publisher is asleep
 * is a post that does not go out — measured, not theoretical, on 2026-08-21, when
 * a rehearsal slot was claimed by the laptop because the publisher was down.
 * This worker's only job is to make the host never idle. One `fetch` every ten
 * minutes is enough, and it costs milliseconds of CPU — which is the entire
 * reason the clock lives on Cloudflare and the work does not. Workers cap CPU at
 * 10 ms per invocation on the free plan; waiting on the network is not CPU, so a
 * probe fits with room to spare, while claiming a Postgres row and unsealing an
 * AES-GCM credential would not fit at all.
 *
 * Its second job is telling you when the publisher is dead. Without that, an
 * outage is silent until you notice posts missing — which is how this whole
 * problem started.
 *
 * What "healthy" means here, and why it is NOT `ok`
 * ------------------------------------------------
 * `health.ok` is `not warnings and …`, so it reports configuration tidiness, not
 * liveness — and which warnings are expected depends on how the host happens to
 * be configured. On a publisher with no admin identity `ok` is permanently
 * `false` by design, and a monitor keyed to it would fire every ten minutes
 * forever and be muted within a day; on this deployment an admin identity IS set,
 * so `ok` reads `true` and would mask a real fault the moment that changed.
 * Either way it is the wrong signal. Liveness here is instead: HTTP 200, a
 * parseable body, and `role == "publisher"` (which also proves we reached our own
 * app and not a captive portal or a holding page).
 */

// The one warning that is expected on this deployment. Matched by prefix so the
// message can be reworded without turning into a false alarm.
const EXPECTED_WARNING = "No publishing admin identity configured";

// Matches cloud/alerts.py, because the Telegram chat is shared with other
// products and this is what tags which one an alert came from.
const TELEGRAM_PREFIX = "OPENSHORTS ✂️ - ";

const DEFAULT_TIMEOUT_MS = 60000;

// A host that is waking or rebuilding answers 503 for a minute or two, and the
// probe itself is often what woke it. Alerting on one bad tick would cry wolf
// after every deploy, so a real alert needs this many consecutive failures.
// Requires the KV binding to count; see the fallback in `decide`.
const FAILURES_BEFORE_ALERT = 2;

/** Probe the publisher. Returns liveness plus enough detail for an alert. */
async function probe(env) {
  const base = (env.HEARTBEAT_URL || '').trim();
  if (!base) {
    return { live: false, degraded: false, kind: 'misconfigured',
             detail: 'HEARTBEAT_URL is not set on the worker', ms: 0 };
  }

  // Cache-busting query rather than a cache header: the request MUST reach the
  // origin, because reaching it is the entire point. A response served from
  // Cloudflare's cache would let the host fall asleep while this worker
  // cheerfully reported success — the one failure mode that would make the
  // heartbeat worse than useless, since it would also suppress the alert.
  const url = base + (base.includes('?') ? '&' : '?') + 'hb=' + Date.now();
  const timeout = Number(env.TIMEOUT_MS || DEFAULT_TIMEOUT_MS);
  const started = Date.now();

  let res;
  try {
    res = await fetch(url, {
      signal: AbortSignal.timeout(timeout),
      headers: { 'user-agent': 'openshorts-heartbeat/1' },
    });
  } catch (err) {
    const ms = Date.now() - started;
    // A timeout on a sleeping host is normal — the wake takes longer than the
    // probe waits — and is reported as not-live so the retry counter advances.
    const detail = err && err.name === 'TimeoutError'
      ? `no response in ${timeout} ms`
      : String(err);
    return { live: false, degraded: false, kind: 'unreachable', detail, ms };
  }

  const ms = Date.now() - started;
  if (res.status !== 200) {
    return { live: false, degraded: false, kind: `http_${res.status}`,
             detail: `health returned HTTP ${res.status}`, ms };
  }

  let body;
  try {
    body = await res.json();
  } catch {
    // 200 with a non-JSON body is the platform's holding page, not our app.
    return { live: false, degraded: false, kind: 'not_json',
             detail: 'health returned 200 but not JSON (host still starting?)', ms };
  }

  if (body.role !== 'publisher') {
    return { live: false, degraded: false, kind: 'wrong_role',
             detail: `role is ${JSON.stringify(body.role)}, expected "publisher"`, ms };
  }

  // Live. Now the one config drift that silently breaks a clip-less publisher:
  // without a presigning object store it falls back to serving clip bytes from
  // itself, and it has none. Posts would park forever with nothing obviously
  // broken, so this is worth an alert even though the process is up.
  const strategy = body.media_strategy;
  const degraded = strategy !== 'objectstore_presigned';
  const extra = (body.warnings || []).filter(w => !String(w).startsWith(EXPECTED_WARNING));

  return {
    live: true,
    degraded,
    kind: degraded ? 'degraded' : 'live',
    detail: degraded
      ? `media_strategy is ${JSON.stringify(strategy)}, expected "objectstore_presigned" `
        + '— the publisher has no clip files to serve, so posts will park'
      : (extra.length ? `up, with warnings: ${extra.join(' | ')}` : 'up'),
    unexpectedWarnings: extra,
    ms,
  };
}

/** Best-effort Telegram push. Never throws: an alert must not break the beat. */
async function notify(env, text) {
  const token = env.TELEGRAM_BOT_TOKEN;
  const chat = env.TELEGRAM_CHAT_ID;
  if (!token || !chat) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: chat,
        text: TELEGRAM_PREFIX + text,
        disable_web_page_preview: true,
      }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Decide whether this tick should alert, and what the next stored state is.
 *
 * With the KV binding: count consecutive failures, alert once when they cross
 * the threshold, and alert once again on recovery. Without it there is nowhere
 * to remember "already told you", so fall back to at most one alert per hour —
 * which bounds a multi-day outage to 24 messages instead of 144, at the cost of
 * up to an hour's delay before the first one.
 */
function decide(result, previous, scheduledTime, hasState) {
  const bad = !result.live || result.degraded;

  if (!hasState) {
    const firstTickOfHour = new Date(scheduledTime).getUTCMinutes() < 10;
    return { alert: bad && firstTickOfHour ? 'problem' : null, next: null };
  }

  const prev = previous || { fails: 0, alerted: false, degraded: false };

  if (result.live && !result.degraded) {
    const alert = prev.alerted ? 'recovered' : null;
    const next = { fails: 0, alerted: false, degraded: false };
    return { alert, next, changed: prev.fails !== 0 || prev.alerted || prev.degraded };
  }

  // Degraded-but-live is a config fault, not a transient wake: it alerts on the
  // first sighting, because it will never clear on its own. An unreachable
  // publisher gets one grace tick, since a waking host looks identical.
  const fails = result.live ? prev.fails : prev.fails + 1;
  const shouldAlert = !prev.alerted
    && (result.degraded || fails >= FAILURES_BEFORE_ALERT);
  const next = { fails, alerted: prev.alerted || shouldAlert, degraded: !!result.degraded };
  return { alert: shouldAlert ? 'problem' : null, next, changed: true };
}

function message(kind, result, env) {
  const where = (env.HEARTBEAT_URL || '').replace(/\/api\/publishing\/health.*$/, '');
  if (kind === 'recovered') {
    return `publisher is back up (${result.ms} ms)\n${where}`;
  }
  return [
    'publisher heartbeat FAILED',
    `${result.kind}: ${result.detail}`,
    'Scheduled posts are not going out while this is down.',
    where,
  ].join('\n');
}

async function beat(env, scheduledTime) {
  const result = await probe(env);
  const hasState = !!env.STATE;

  let previous = null;
  if (hasState) {
    try {
      previous = await env.STATE.get('last', 'json');
    } catch {
      previous = null;   // treat an unreadable namespace as a fresh start
    }
  }

  const { alert, next, changed } = decide(result, previous, scheduledTime, hasState);

  if (alert) {
    const sent = await notify(env, message(alert, result, env));
    if (!sent) console.log('heartbeat: alert not delivered (TELEGRAM_* unset?)');
  }
  // Written only when something actually changed, so a healthy deployment costs
  // 0 writes/day against the free tier's 1,000.
  if (hasState && next && changed !== false) {
    try {
      await env.STATE.put('last', JSON.stringify(next));
    } catch (err) {
      console.log(`heartbeat: could not persist state: ${err}`);
    }
  }

  console.log(`heartbeat ${result.kind} in ${result.ms} ms — ${result.detail}`);
  return result;
}

export default {
  async scheduled(event, env, _ctx) {
    await beat(env, event.scheduledTime);
  },

  // Manual check, for confirming the worker works without waiting for a tick.
  // Deliberately does NOT alert or touch stored state: this URL is public on
  // workers.dev, and a probe endpoint that could push Telegram messages would be
  // an open spam relay.
  async fetch(_request, env, _ctx) {
    const result = await probe(env);
    return Response.json({
      live: result.live,
      degraded: result.degraded,
      kind: result.kind,
      detail: result.detail,
      ms: result.ms,
      note: 'manual probe — no alert sent, no state written',
    }, { status: result.live && !result.degraded ? 200 : 503 });
  },
};

// Cloudflare only ever uses the default export above. These are the seam for
// test/heartbeat.test.js, which is the only place the alerting rules get
// exercised before an outage does it for real.
export { probe, decide, message, EXPECTED_WARNING, FAILURES_BEFORE_ALERT };
