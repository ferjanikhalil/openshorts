import { useState, useEffect, useCallback } from 'react';
import {
  Send, Plus, Loader2, AlertTriangle, Info, ShieldCheck, FlaskConical,
} from 'lucide-react';
import {
  publishingHealth, adminHealth, listGroups, createGroup,
  setAdminToken, clearAdminToken,
} from '../../lib/publishing';
import SegmentedControl from '../ui/SegmentedControl';
import PublishGroupCard from './PublishGroupCard';
import PublishStatusBoard from './PublishStatusBoard';

// PublishingTab — the admin surface for automated posting.
//
// Gated on GET /api/publishing/health: publishing is an optional subsystem, and a
// deployment that did not enable it should not be shown a broken feature. The
// same health call is what surfaces misconfiguration (no public media origin, no
// admin identity) as text an operator can act on rather than a failed request.
//
// No API key is ever read back here. Keys go in through CredentialForm and come
// back only as fingerprint + last4.
const VIEWS = [
  { value: 'settings', label: 'Accounts' },
  { value: 'activity', label: 'Activity' },
];

export default function PublishingTab() {
  const [health, setHealth] = useState(null);
  const [admin, setAdmin] = useState(null);
  const [groups, setGroups] = useState([]);
  const [view, setView] = useState('settings');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tokenInput, setTokenInput] = useState('');
  const [signingIn, setSigningIn] = useState(false);

  const reload = useCallback(async () => {
    setError('');
    try {
      const r = await listGroups();
      setGroups(r.groups || []);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  // Try the admin API with whatever identity is currently available (a stored
  // token, or the JWT apiFetch attaches in cloud mode). Returns false rather
  // than throwing: "not an admin yet" is a normal state, not an error.
  const tryAdmin = useCallback(async () => {
    try {
      const a = await adminHealth();
      setAdmin(a);
      await reload();
      return true;
    } catch {
      setAdmin(null);
      return false;
    }
  }, [reload]);

  const signIn = async (e) => {
    e.preventDefault();
    const tok = tokenInput.trim();
    if (!tok) return;
    setSigningIn(true); setError('');
    setAdminToken(tok);
    const ok = await tryAdmin();
    if (ok) {
      setTokenInput('');
    } else {
      // Do not keep a token that the server rejected.
      clearAdminToken();
      setError('That admin token was rejected.');
    }
    setSigningIn(false);
  };

  const signOut = async () => {
    clearAdminToken();
    setAdmin(null);
    setGroups([]);
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const h = await publishingHealth();
      if (cancelled) return;
      setHealth(h);
      // The admin router is only mounted when an admin identity can be
      // enforced, and in self-host mode the operator has to present a token
      // first. Neither case is an error — both render a sign-in prompt.
      if (h.enabled && h.admin_api_mounted !== false) await tryAdmin();
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [tryAdmin]);

  const create = async (e) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setBusy(true); setError('');
    try {
      await createGroup({ name });
      setNewName(''); setCreating(false);
      await reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-16 text-sm text-muted">
        <Loader2 size={14} className="animate-spin" /> Checking publishing…
      </div>
    );
  }

  if (!health?.enabled) {
    return (
      <div className="card mx-auto max-w-xl p-6 text-center">
        <Send size={20} className="mx-auto text-muted" />
        <h2 className="font-display lowercase text-2xl text-ink mt-3">publishing is off</h2>
        <p className="mt-2 text-sm text-muted">
          Set <code className="font-mono text-xs text-ink2">PUBLISHING_ENABLED=true</code> and
          restart the server to post clips to social accounts automatically.
        </p>
      </div>
    );
  }

  const warnings = [...(health.warnings || []), ...(admin?.warnings || [])];
  const platforms = admin?.platforms || [];

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <header className="flex flex-wrap items-end gap-3">
        <div>
          <p className="eyebrow">automated posting</p>
          <h1 className="font-display lowercase text-3xl text-ink leading-none mt-1">publishing</h1>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {health.dry_run && (
            <span className="badge-brass"><FlaskConical size={11} /> dry run</span>
          )}
          {admin?.admin && (
            <span className="badge-ok"><ShieldCheck size={11} /> {admin.admin.kind}</span>
          )}
          {admin?.admin?.kind === 'token' && (
            // Only the token identity can be dropped from here; an email
            // identity is the app session and logs out with it.
            <button type="button" onClick={signOut} className="link text-xs">
              lock
            </button>
          )}
          <span className="readout">{health.default_provider}</span>
        </div>
      </header>

      {health.dry_run ? (
        <p className="rounded-input bg-paper px-3 py-2 text-xs text-brass">
          Dry run is on: everything runs end to end but no post reaches a real
          account. Set PUBLISHING_DRY_RUN=false and <strong>recreate</strong> the
          container — <code>docker compose … up -d</code>, not{' '}
          <code>docker restart</code>, which reuses the old environment and
          silently leaves this on.
        </p>
      ) : (
        <p className="rounded-input bg-paper px-3 py-2 text-xs text-ink2">
          Live: publishing posts to real accounts. Posts are public and
          permanent, and quota spent here is spent for the day.
        </p>
      )}

      {!admin && (
        health?.admin_auth?.token ? (
          // Self-host: the operator holds PUBLISHING_ADMIN_TOKEN. Remembered by
          // the client and sent only on /admin routes, until "lock" forgets it.
          <form onSubmit={signIn} className="card space-y-2 p-4">
            <p className="eyebrow">admin sign-in</p>
            <p className="text-xs text-muted">
              Publishing configuration holds provider API keys, so it is behind
              an admin token. Paste the value of{' '}
              <code className="font-mono text-ink2">PUBLISHING_ADMIN_TOKEN</code>{' '}
              from your server environment. This browser remembers it until you
              click <span className="text-ink2">lock</span>.
            </p>
            <div className="flex flex-wrap gap-2">
              <input
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="publishing admin token"
                autoComplete="off"
                className="input min-w-[16rem] flex-1 font-mono text-xs"
              />
              <button type="submit" className="btn-primary" disabled={signingIn || !tokenInput.trim()}>
                {signingIn ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}
                unlock
              </button>
            </div>
          </form>
        ) : (
          <p className="flex items-start gap-2 rounded-input bg-paper px-3 py-2 text-xs text-warn">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            {health?.admin_auth?.email
              ? 'Sign in with an account listed in PUBLISHING_ADMIN_EMAILS to configure publishing.'
              : 'No admin identity is configured, so the admin API is not mounted. Set PUBLISHING_ADMIN_TOKEN (self-host) or PUBLISHING_ADMIN_EMAILS (cloud) and restart.'}
          </p>
        )
      )}

      {warnings.length > 0 && (
        <div className="rounded-input border border-warn/40 p-3">
          <p className="eyebrow mb-1.5">needs configuration</p>
          {warnings.map((w, i) => (
            <p key={i} className="flex items-start gap-1.5 text-xs text-warn">
              <Info size={11} className="mt-0.5 shrink-0" /> {w}
            </p>
          ))}
        </div>
      )}

      {!health.clip_resolver_registered && (
        <p className="flex items-start gap-2 rounded-input bg-paper px-3 py-2 text-xs text-danger">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          The clip resolver is not registered, so no clip can be published. This is
          a server wiring problem, not a settings one.
        </p>
      )}

      <SegmentedControl options={VIEWS} value={view} onChange={setView} size="sm" columns={2} />

      {error && <p className="text-xs text-danger">{error}</p>}

      {view === 'settings' ? (
        <div className="space-y-3">
          {groups.length === 0 && !creating && (
            <div className="card p-6 text-center">
              <p className="text-sm text-muted">
                A group bundles the accounts that share one Status 200 API key.
                Create one per key you hold.
              </p>
            </div>
          )}

          {groups.map((g) => (
            <PublishGroupCard key={g.id} group={g} platforms={platforms} onChanged={reload} />
          ))}

          {creating ? (
            <form onSubmit={create} className="card flex flex-wrap items-center gap-2 p-4">
              <input
                autoFocus
                className="input-field grow py-2"
                placeholder="group name — e.g. Brand A"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <button type="submit" className="btn-primary py-2 text-xs" disabled={busy || !newName.trim()}>
                {busy ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
                Create
              </button>
              <button type="button" className="btn-ghost py-2 text-xs"
                onClick={() => { setCreating(false); setNewName(''); }}>
                Cancel
              </button>
            </form>
          ) : (
            <button className="btn-ghost w-full" onClick={() => setCreating(true)}>
              <Plus size={14} /> New group
            </button>
          )}

          <p className="text-xs text-muted">
            Each group holds its own key, sealed with AES-256-GCM before it reaches
            the database. Keys are never returned to this page and never written to
            a log, a URL or the repository.
          </p>
        </div>
      ) : (
        <PublishStatusBoard />
      )}
    </div>
  );
}
