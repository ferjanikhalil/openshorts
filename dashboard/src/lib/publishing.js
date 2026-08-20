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
export const setCredential = (groupId, apiKey, { kind = 'api_key', verify = true } = {}) =>
  put(`${ADMIN}/groups/${groupId}/credential?verify=${verify ? 'true' : 'false'}`,
    { api_key: apiKey, kind });

export const listCredentials = (groupId) =>
  jsonOrThrow(`${ADMIN}/groups/${groupId}/credentials`);
export const verifyCredential = (groupId) =>
  post(`${ADMIN}/groups/${groupId}/credential/verify`);
export const revokeCredential = (groupId, kind = 'api_key') =>
  del(`${ADMIN}/groups/${groupId}/credential?kind=${kind}`);

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
