// Publishing API client.
//
// Two rules this file exists to keep:
//   1. No function here ever sends or receives a provider API key except
//      setCredential, which sends one and reads back only the masked view. The
//      plaintext is passed straight from the input to fetch and is never stored
//      in component state, localStorage, or a URL.
//   2. Error text comes from the backend. A 409 on "delete group" says WHY
//      (a post is in flight), and swallowing that into "Request failed" would
//      leave the operator with no next step.
import { apiFetch, apiJson } from './api';

const ADMIN_TOKEN_HEADER = 'X-Publishing-Admin-Token';
const ADMIN_TOKEN_KEY = 'openshorts.publishing.adminToken';

// The self-host admin identity. Held in localStorage so it survives closing the
// browser: a self-host install is one operator on their own machine, and making
// them re-paste a 48-character token every session buys no security — it only
// pushes them towards keeping it somewhere more exposed. Forgetting it is an
// explicit act: the "lock" button in PublishingTab calls clearAdminToken.
// It is NOT a provider key — it authenticates configuration, and the keys it
// guards stay sealed server-side and never come back to this page.
// In cloud mode this stays empty and the JWT that apiFetch already attaches is
// the identity instead.
export function getAdminToken() {
  try { return localStorage.getItem(ADMIN_TOKEN_KEY) || ''; } catch { return ''; }
}

export function setAdminToken(token) {
  try {
    if (token) localStorage.setItem(ADMIN_TOKEN_KEY, token);
    else localStorage.removeItem(ADMIN_TOKEN_KEY);
  } catch { /* private-mode browsers: the header just goes unset */ }
}

export const clearAdminToken = () => setAdminToken('');

// FastAPI puts the message in `detail`; surface it instead of the status code.
async function jsonOrThrow(path, options = {}) {
  // Admin routes only. Never widen this: the token authenticates configuration,
  // so it has no business on a public publish call.
  if (path.startsWith('/api/publishing/admin')) {
    const tok = getAdminToken();
    if (tok) {
      const headers = new Headers(options.headers || {});
      headers.set(ADMIN_TOKEN_HEADER, tok);
      options = { ...options, headers };
    }
  }
  const res = await apiFetch(path, options);
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      const detail = body?.detail ?? body?.error;
      if (typeof detail === 'string') message = detail;
      else if (detail) message = JSON.stringify(detail);
    } catch { /* non-JSON error body: keep the status line */ }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

const post = (path, body) => jsonOrThrow(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const put = (path, body) => jsonOrThrow(path, {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const patch = (path, body) => jsonOrThrow(path, {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const del = (path) => jsonOrThrow(path, { method: 'DELETE' });

const ADMIN = '/api/publishing/admin';
const PUB = '/api/publishing';

// --- Availability -----------------------------------------------------------
// Publishing is an optional subsystem: the routes 404 when PUBLISHING_ENABLED is
// off. Every caller checks this first so the UI hides the feature instead of
// showing an error for a deployment that never asked for it.
export async function publishingHealth() {
  try {
    return await apiJson(`${PUB}/health`);
  } catch {
    return { enabled: false, ok: false, warnings: [] };
  }
}

export const adminHealth = () => jsonOrThrow(`${ADMIN}/health`);

// --- Groups -----------------------------------------------------------------
export const listGroups = () => jsonOrThrow(`${ADMIN}/groups`);
export const getGroup = (id) => jsonOrThrow(`${ADMIN}/groups/${id}`);
export const createGroup = (body) => post(`${ADMIN}/groups`, body);
export const updateGroup = (id, body) => patch(`${ADMIN}/groups/${id}`, body);
export const deleteGroup = (id) => del(`${ADMIN}/groups/${id}?confirm=true`);

// --- Credentials ------------------------------------------------------------
/**
 * Store a provider API key for one group.
 *
 * `apiKey` is write-only by design: the response carries a fingerprint and last4
 * and no route anywhere returns the plaintext again. Callers must not keep the
 * value they passed in — clear the input on success.
 */
export const setCredential = (
  groupId, apiKey,
  { kind = 'api_key', verify = true, slot = null } = {},
) =>
  put(`${ADMIN}/groups/${groupId}/credential?verify=${verify ? 'true' : 'false'}`,
    // `credential_slot` names WHICH provider account this key is. Sent as null
    // (not omitted, not '') for the group default, which is the only shape a
    // single-account provider ever has.
    { api_key: apiKey, kind, credential_slot: slot || null });

export const listCredentials = (groupId) =>
  jsonOrThrow(`${ADMIN}/groups/${groupId}/credentials`);
// The accounts one stored key can reach. `accounts: null` means the provider
// offers no listing — not an error, and the caller falls back to hand entry.
export const listProviderAccounts = (groupId, slot = null) =>
  jsonOrThrow(`${ADMIN}/groups/${groupId}/accounts${slot ? `?slot=${encodeURIComponent(slot)}` : ''}`);
export const verifyCredential = (groupId, slot = null) =>
  post(`${ADMIN}/groups/${groupId}/credential/verify${slot ? `?slot=${encodeURIComponent(slot)}` : ''}`);
// Narrowest scope by default: no slot revokes the group DEFAULT key only, never
// every account in a multi-account batch. `allSlots` is the deliberate wide one.
export const revokeCredential = (groupId, kind = 'api_key',
  { slot = null, allSlots = false } = {}) => {
  const q = new URLSearchParams({ kind });
  if (slot) q.set('slot', slot);
  if (allSlots) q.set('all_slots', 'true');
  return del(`${ADMIN}/groups/${groupId}/credential?${q}`);
};

// --- Destinations -----------------------------------------------------------
export const createDestination = (groupId, body) =>
  post(`${ADMIN}/groups/${groupId}/destinations`, body);
export const updateDestination = (id, body) =>
  patch(`${ADMIN}/destinations/${id}`, body);
export const deleteDestination = (id) => del(`${ADMIN}/destinations/${id}`);
export const listDestinations = () => jsonOrThrow(`${PUB}/destinations`);

// --- Publishing -------------------------------------------------------------
export const previewPublish = (body) => post(`${PUB}/preview`, body);
export const publishClip = (body) => post(`${PUB}/publish`, body);
export const publishJob = (body) => post(`${PUB}/publish-job`, body);

// --- History ----------------------------------------------------------------
export const listRequests = (params = {}) => {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== '' && v !== false),
  ).toString();
  return jsonOrThrow(`${PUB}/requests${q ? `?${q}` : ''}`);
};
export const getRequest = (id) => jsonOrThrow(`${PUB}/requests/${id}`);
export const cancelRequest = (id) => post(`${PUB}/requests/${id}/cancel`);
export const retryAttempt = (id, force = false) =>
  post(`${PUB}/attempts/${id}/retry`, { force });
export const listAttempts = (params = {}) => {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== '' && v !== false),
  ).toString();
  return jsonOrThrow(`${PUB}/attempts${q ? `?${q}` : ''}`);
};

// --- Scheduling -------------------------------------------------------------
export const createAssignments = (groupId, body) =>
  post(`${ADMIN}/groups/${groupId}/assignments`, body);
export const listAssignments = (params = {}) => {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== ''),
  ).toString();
  return jsonOrThrow(`${ADMIN}/assignments${q ? `?${q}` : ''}`);
};
export const deleteAssignment = (id) => del(`${ADMIN}/assignments/${id}`);
export const groupCapacity = (groupId) =>
  jsonOrThrow(`${ADMIN}/groups/${groupId}/capacity`);
export const groupPlanPreview = (groupId, count = 6) =>
  jsonOrThrow(`${ADMIN}/groups/${groupId}/plan/preview?count=${count}`);
export const runScheduler = () => post(`${ADMIN}/schedule/run`);

// --- Audit ------------------------------------------------------------------
export const listEvents = (params = {}) => {
  const q = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== ''),
  ).toString();
  return jsonOrThrow(`${ADMIN}/events${q ? `?${q}` : ''}`);
};
export const dryRunLog = () => jsonOrThrow(`${ADMIN}/dry-run`);
export const dryRunReset = () => post(`${ADMIN}/dry-run/reset`);

// --- Display helpers --------------------------------------------------------
// Provider-specific prose. The FACTS about a provider (display name, key prefix,
// whether several accounts fit in one batch) are declared in the backend's
// Capabilities and arrive via adminHealth().providers — nothing here invents
// them. What lives here is only the copy that tells an operator where to find an
// account id, which is documentation, not capability, and which needs markup the
// backend has no business carrying.
//
// A provider missing from this table still works: `providerInfo` falls back to
// the declared label and generic wording. That fallback is the point — the next
// adapter must not need a frontend change to be usable.
export const PROVIDER_GUIDES = {
  status200: {
    accountRefLabel: 'profile UUID',
    accountRefHint: 'profile uuid at the provider',
    // Learned the hard way: their docs and their own "copy API ID" button both
    // hand over the @handle, which the API rejects.
    accountRefHelp: 'This is the profile UUID, not the @handle — the same value '
      + 'for every platform under one profile. Their docs and the "copy API ID" '
      + 'button both give the handle, which is rejected. Find it in the '
      + 'dashboard at Connections with DevTools open (Network → the '
      + 'social_media_profiles request → Preview → id).',
  },
  zernio: {
    accountRefLabel: 'account id',
    accountRefHint: 'account id at the provider',
    accountRefHelp: 'This is the connected account’s id (a 24-character hex '
      + 'string), one per social account rather than one per profile. Zernio can '
      + 'list them: use Fetch accounts above instead of typing it, which also '
      + 'proves the key reaches the account.',
  },
};

const GENERIC_GUIDE = {
  accountRefLabel: 'account id',
  accountRefHint: 'account id at the provider',
  accountRefHelp: 'The identifier the provider uses for this connected account. '
    + 'It is passed through untouched, so it has to be exactly what they expect.',
};

/**
 * Everything the UI needs to render forms for one provider.
 *
 * `providers` is adminHealth().providers. An unknown or absent name still returns
 * a usable object — a form that cannot label itself must still let the operator
 * paste a key.
 */
export function providerInfo(providers, name) {
  const declared = (providers || []).find((p) => p.name === name) || {};
  return {
    name: name || declared.name || 'provider',
    label: declared.label || name || 'provider',
    keyPrefix: declared.key_prefix || '',
    multiCredential: !!declared.multi_credential,
    supportsAccountListing: !!declared.supports_account_listing,
    supportsRemoteSchedule: !!declared.supports_remote_schedule,
    supportsStatusLookup: !!declared.supports_status_lookup,
    supportsWebhooks: !!declared.supports_webhooks,
    platforms: declared.platforms || [],
    ...(PROVIDER_GUIDES[name] || GENERIC_GUIDE),
  };
}

/**
 * The credential slots present in one group, default first.
 *
 * Derived from the group's own credentials and destinations rather than a fixed
 * list: a slot exists because the operator named it. Destinations are included so
 * a slot a destination points at still shows up after its key was revoked —
 * which is exactly the state that stops that destination publishing and so the
 * one that must be visible.
 */
export function groupSlots(group) {
  const slots = new Set();
  for (const c of group?.credentials || []) {
    if (c?.kind !== 'webhook_secret') slots.add(c.credential_slot || '');
  }
  for (const d of group?.destinations || []) slots.add(d.credential_slot || '');
  const named = [...slots].filter(Boolean).sort();
  return slots.has('') ? ['', ...named] : named;
}

export const slotLabel = (slot) => slot || 'default account';

/** The api_key credential for one slot ('' = the group default), or undefined. */
export const credentialForSlot = (group, slot) =>
  (group?.credentials || []).find(
    (c) => c.kind !== 'webhook_secret' && (c.credential_slot || '') === (slot || ''),
  );

export const PLATFORM_LABELS = {
  youtube: 'YouTube',
  instagram: 'Instagram',
  tiktok: 'TikTok',
  facebook: 'Facebook',
  linkedin: 'LinkedIn',
  x: 'X',
  threads: 'Threads',
  pinterest: 'Pinterest',
};

export const platformLabel = (p) => PLATFORM_LABELS[p] || (p || '').toUpperCase();

// Attempt status → the two things the operator needs: a colour and whether it is
// their turn to act. `unknown` is amber, not red: the post may well be live.
// Classes are the app's four badge tokens, so publishing looks like the rest of
// the dashboard instead of introducing a fifth palette.
export const STATUS_STYLES = {
  succeeded: { label: 'Published', cls: 'badge-ok' },
  submitted: { label: 'Submitted', cls: 'badge-brass' },
  in_flight: { label: 'Sending', cls: 'badge-brass' },
  pending: { label: 'Queued', cls: 'badge-quiet' },
  deferred: { label: 'Scheduled', cls: 'badge-quiet' },
  failed: { label: 'Retrying', cls: 'badge-warn' },
  dead: { label: 'Failed', cls: 'badge-danger' },
  blocked: { label: 'Blocked', cls: 'badge-danger' },
  unknown: { label: 'Needs check', cls: 'badge-warn' },
  skipped: { label: 'Skipped', cls: 'badge-quiet' },
  cancelled: { label: 'Cancelled', cls: 'badge-quiet' },
  // Request-level statuses share the map; a request is `partial` when some of
  // its accounts published and some did not.
  partial: { label: 'Partial', cls: 'badge-warn' },
  queued: { label: 'Queued', cls: 'badge-quiet' },
};

export const statusStyle = (s) => STATUS_STYLES[s] || { label: s || '—', cls: 'badge-quiet' };

// Destination health is a separate vocabulary from attempt status: it describes
// the account, not one post.
export const HEALTH_STYLES = {
  ok: { label: 'Healthy', cls: 'badge-ok' },
  unverified: { label: 'Unverified', cls: 'badge-quiet' },
  degraded: { label: 'Degraded', cls: 'badge-warn' },
  blocked: { label: 'Blocked', cls: 'badge-danger' },
  disconnected: { label: 'Disconnected', cls: 'badge-danger' },
};

export const healthStyle = (h) => HEALTH_STYLES[h] || { label: h || '—', cls: 'badge-quiet' };

export function formatWhen(value) {
  if (!value) return 'now';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}
