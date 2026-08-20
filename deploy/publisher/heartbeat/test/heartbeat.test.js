/**
 * Tests for the heartbeat's alerting rules.
 *
 *     npm test
 *
 * No dependencies and no test framework: Node's built-in runner, because adding
 * a toolchain to a 200-line worker would cost more than it protects.
 *
 * What is worth pinning here is not the fetch — it is the judgement. Two rules
 * decide whether a human hears about an outage, and both are easy to get subtly
 * wrong in a way that only shows up months later, in the exact situation where
 * you need the alert:
 *
 *   1. `ok: false` is the publisher's PERMANENT, CORRECT state, because the
 *      admin router is deliberately unmounted. A monitor keyed on `ok` fires
 *      every ten minutes forever, gets muted, and then the real outage is
 *      silent. `test_ok_false_is_not_an_outage` is the guard.
 *   2. A waking Space is indistinguishable from a dead one for a tick or two, so
 *      an unreachable publisher gets a grace tick while a misconfigured one does
 *      not. Getting that backwards means either crying wolf after every deploy
 *      or never reporting a broken media strategy at all.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  probe, decide, message, EXPECTED_WARNING, FAILURES_BEFORE_ALERT,
} from '../src/worker.js';

const URL_ENV = { HEARTBEAT_URL: 'https://x-publisher.hf.space/api/publishing/health' };

/** A health payload in the shape the publisher actually returns. */
function health(over = {}) {
  return {
    enabled: true,
    role: 'publisher',
    media_strategy: 'objectstore_presigned',
    clip_resolver_registered: false,
    // The deployment runs with no admin identity on purpose, so this warning is
    // always present and `ok` is always false. Both are correct.
    warnings: [`${EXPECTED_WARNING} — the admin API is not mounted.`],
    ok: false,
    ...over,
  };
}

/** Swap global fetch for one canned response, restoring it afterwards. */
async function withFetch(impl, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = impl;
  try {
    return await fn();
  } finally {
    globalThis.fetch = original;
  }
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

// --- probe: what counts as alive ------------------------------------------

test('ok:false with only the expected warning is a healthy publisher', async () => {
  const result = await withFetch(async () => jsonResponse(health()),
    () => probe(URL_ENV));
  assert.equal(result.live, true, 'a deliberately admin-less publisher is live');
  assert.equal(result.degraded, false);
  assert.equal(result.kind, 'live');
  assert.deepEqual(result.unexpectedWarnings, [],
    'the admin-identity warning must not be reported as a problem');
});

test('an unexpected warning is surfaced but does not make it not-live', async () => {
  const body = health({ warnings: [`${EXPECTED_WARNING} …`, 'PUBLISHING_PUBLIC_BASE_URL is unset'] });
  const result = await withFetch(async () => jsonResponse(body), () => probe(URL_ENV));
  assert.equal(result.live, true);
  assert.deepEqual(result.unexpectedWarnings, ['PUBLISHING_PUBLIC_BASE_URL is unset']);
  assert.match(result.detail, /PUBLISHING_PUBLIC_BASE_URL/);
});

test('a publisher with no presigning object store is degraded, not dead', async () => {
  const body = health({ media_strategy: 'signed_token' });
  const result = await withFetch(async () => jsonResponse(body), () => probe(URL_ENV));
  assert.equal(result.live, true);
  assert.equal(result.degraded, true, 'it is up, but it has no clip bytes to serve');
  assert.match(result.detail, /objectstore_presigned/);
});

test('the probe busts caches, because a cached 200 would let the Space sleep', async () => {
  let seen = '';
  await withFetch(async (url) => { seen = String(url); return jsonResponse(health()); },
    () => probe(URL_ENV));
  assert.match(seen, /[?&]hb=\d+/, 'the request must be unique per tick');
});

test('HTTP 503 from a waking Space is not-live but not a wrong-role error', async () => {
  const result = await withFetch(async () => new Response('starting', { status: 503 }),
    () => probe(URL_ENV));
  assert.equal(result.live, false);
  assert.equal(result.kind, 'http_503');
});

test('200 with a non-JSON holding page is not-live', async () => {
  const result = await withFetch(async () => new Response('<html>building</html>',
    { status: 200, headers: { 'content-type': 'text/html' } }), () => probe(URL_ENV));
  assert.equal(result.live, false);
  assert.equal(result.kind, 'not_json');
});

test('reaching something that is not our publisher is not-live', async () => {
  const result = await withFetch(async () => jsonResponse({ role: 'full', ok: true }),
    () => probe(URL_ENV));
  assert.equal(result.live, false);
  assert.equal(result.kind, 'wrong_role');
});

test('a network failure is reported as unreachable, not thrown', async () => {
  const result = await withFetch(async () => { throw new Error('boom'); },
    () => probe(URL_ENV));
  assert.equal(result.live, false);
  assert.equal(result.kind, 'unreachable');
});

test('a missing HEARTBEAT_URL is caught before any request', async () => {
  let called = false;
  const result = await withFetch(async () => { called = true; return jsonResponse(health()); },
    () => probe({}));
  assert.equal(called, false);
  assert.equal(result.kind, 'misconfigured');
});

// --- decide: when a human hears about it ----------------------------------

const LIVE = { live: true, degraded: false, kind: 'live' };
const DEAD = { live: false, degraded: false, kind: 'unreachable' };
const DEGRADED = { live: true, degraded: true, kind: 'degraded' };
const HAS_STATE = true;
const T = '2026-08-20T12:20:00.000Z';   // minute 20: not the first tick of an hour

test('a healthy tick after a healthy tick alerts nothing and writes nothing', () => {
  const prev = { fails: 0, alerted: false, degraded: false };
  const { alert, changed } = decide(LIVE, prev, T, HAS_STATE);
  assert.equal(alert, null);
  assert.equal(changed, false, 'a steady-state deployment must cost 0 KV writes');
});

test('the first failure stays quiet, because a waking Space looks the same', () => {
  const { alert, next } = decide(DEAD, null, T, HAS_STATE);
  assert.equal(alert, null);
  assert.equal(next.fails, 1);
  assert.equal(next.alerted, false);
});

test('the second consecutive failure alerts', () => {
  const { alert, next } = decide(DEAD, { fails: 1, alerted: false, degraded: false }, T, HAS_STATE);
  assert.equal(alert, 'problem');
  assert.equal(next.fails, FAILURES_BEFORE_ALERT);
  assert.equal(next.alerted, true);
});

test('a continuing outage does not alert again', () => {
  for (const fails of [2, 3, 50]) {
    const { alert } = decide(DEAD, { fails, alerted: true, degraded: false }, T, HAS_STATE);
    assert.equal(alert, null, `fails=${fails} must stay quiet once alerted`);
  }
});

test('recovery after an alert says so, and resets', () => {
  const { alert, next } = decide(LIVE, { fails: 4, alerted: true, degraded: false }, T, HAS_STATE);
  assert.equal(alert, 'recovered');
  assert.deepEqual(next, { fails: 0, alerted: false, degraded: false });
});

test('recovery from a blip you were never told about stays quiet', () => {
  const { alert } = decide(LIVE, { fails: 1, alerted: false, degraded: false }, T, HAS_STATE);
  assert.equal(alert, null, 'no alert was sent, so there is nothing to recover from');
});

test('degraded alerts on the FIRST sighting — it will never clear on its own', () => {
  const { alert, next } = decide(DEGRADED, null, T, HAS_STATE);
  assert.equal(alert, 'problem', 'a config fault gets no grace period');
  assert.equal(next.alerted, true);
  assert.equal(next.degraded, true);
});

test('degraded does not re-alert every ten minutes', () => {
  const { alert } = decide(DEGRADED, { fails: 0, alerted: true, degraded: true }, T, HAS_STATE);
  assert.equal(alert, null);
});

// --- decide: the no-KV fallback -------------------------------------------

test('without KV, a problem alerts only on the first tick of the hour', () => {
  const early = decide(DEAD, null, '2026-08-20T12:00:00.000Z', false);
  const late = decide(DEAD, null, '2026-08-20T12:30:00.000Z', false);
  assert.equal(early.alert, 'problem');
  assert.equal(late.alert, null, 'bounds a multi-day outage to 24 messages, not 144');
});

test('without KV, healthy ticks never alert and never ask for a write', () => {
  const { alert, next } = decide(LIVE, null, '2026-08-20T12:00:00.000Z', false);
  assert.equal(alert, null);
  assert.equal(next, null, 'there is no namespace to write to');
});

// --- message ---------------------------------------------------------------

test('the outage message names the consequence, not just the symptom', () => {
  const text = message('problem',
    { kind: 'unreachable', detail: 'no response in 25000 ms', ms: 25000 }, URL_ENV);
  assert.match(text, /not going out/, 'the operator needs to know what it costs');
  assert.match(text, /unreachable/);
  assert.doesNotMatch(text, /\/api\/publishing\/health/,
    'the alert links the host, not the probe path');
});

test('the recovery message is short and says it is back', () => {
  const text = message('recovered', { kind: 'live', detail: 'up', ms: 412 }, URL_ENV);
  assert.match(text, /back up/);
});
