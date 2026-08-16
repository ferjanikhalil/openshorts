import { useState } from 'react';
import { KeyRound, Loader2, ShieldCheck, AlertTriangle, RefreshCw, Trash2, Webhook, Copy, ClipboardCheck } from 'lucide-react';
import { setCredential, verifyCredential, revokeCredential } from '../../lib/publishing';

// CredentialForm — the one place in the frontend that touches a provider API key.
//
// The key lives in local component state for exactly as long as the operator is
// typing it, and is cleared the moment the request succeeds. It is never written
// to localStorage, never put in a URL, never logged, and never read back: the
// backend has no route that returns it. Everything shown after saving comes from
// the masked view (fingerprint + last4).
//
// The same form stores the webhook signing secret (kind="webhook_secret"). The
// two do not verify the same way: an API key has a probe endpoint, a webhook
// secret is just HMAC bytes, so the verify checkbox is hidden for it and the
// store call passes { verify: false }.
export default function CredentialForm({ group, onSaved }) {
  const [value, setValue] = useState('');
  const [verify, setVerify] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [copied, setCopied] = useState(false);

  const cred = group.credential;
  const hasHook = !!group.webhook_secret;
  const hookUrl = group.webhook_url || '';

  const save = async (e) => {
    e.preventDefault();
    const key = value.trim();
    if (key.length < 8) {
      setError('That key looks too short to be valid.');
      return;
    }
    setBusy('save'); setError(''); setNotice('');
    try {
      const saved = await setCredential(group.id, key, { verify });
      // Cleared before anything else so the plaintext does not survive a
      // re-render, even if the callback below throws.
      setValue('');
      // The endpoint answers {credential, replaced, verified} — the masked view
      // is nested. Reading it flat here is what raised "cannot read properties
      // of undefined" AFTER the key had already been stored successfully.
      const info = saved?.credential;
      setNotice(info
        ? `Stored and sealed · ${info.masked} · fingerprint ${(info.fingerprint || '').slice(0, 8)}`
        : 'Stored and sealed.');
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const saveHook = async (e) => {
    e.preventDefault();
    const secret = value.trim();
    if (secret.length < 8) {
      setError('That webhook secret looks too short to be valid.');
      return;
    }
    setBusy('save'); setError(''); setNotice('');
    try {
      // No probe endpoint exists for a signing secret — it is verified only when
      // the first real callback arrives with a matching signature. verify=false
      // skips the API-key probe path entirely.
      const saved = await setCredential(group.id, secret, { kind: 'webhook_secret', verify: false });
      setValue('');
      const info = saved?.credential;
      setNotice(info
        ? `Webhook secret sealed · ${info.masked} · fingerprint ${(info.fingerprint || '').slice(0, 8)}`
        : 'Webhook secret sealed.');
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const copyUrl = async () => {
    if (!hookUrl) return;
    try {
      await navigator.clipboard.writeText(hookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError('Could not copy — copy the URL from the field instead.');
    }
  };

  const recheck = async () => {
    setBusy('verify'); setError(''); setNotice('');
    try {
      const res = await verifyCredential(group.id);
      // ok is tri-state: true, false, or null when the provider offers no way to
      // check a key without publishing. Treating null as a rejection would tell
      // the operator their good key was refused.
      if (res.ok === null || res.ok === undefined) {
        setNotice(res.detail || 'This provider cannot verify a key without publishing.');
      } else if (res.ok) {
        setNotice('The provider accepted this key.');
      } else {
        setError(res.detail || 'The provider rejected this key.');
      }
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const revoke = async () => {
    setBusy('revoke'); setError(''); setNotice('');
    try {
      await revokeCredential(group.id);
      setNotice('Key revoked. Publishing for this group is paused until a new one is entered.');
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const revokeHook = async () => {
    setBusy('revoke'); setError(''); setNotice('');
    try {
      await revokeCredential(group.id, 'webhook_secret');
      setNotice('Webhook secret revoked. Provider callbacks will stop verifying until a new one is entered.');
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="space-y-3">
      {cred ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className={cred.invalid ? 'badge-danger' : 'badge-ok'}>
            {cred.invalid ? <AlertTriangle size={11} /> : <ShieldCheck size={11} />}
            {cred.invalid ? 'rejected' : 'active'}
          </span>
          <code className="font-mono text-xs text-ink2">{cred.masked}</code>
          <span className="text-muted text-xs">
            fingerprint {(cred.fingerprint || '').slice(0, 8)}
            {cred.last_used_at ? ` · last used ${new Date(cred.last_used_at).toLocaleDateString()}` : ' · never used'}
          </span>
          {cred.needs_rotation && (
            <span className="badge-warn">re-enter to re-seal under the current master key</span>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted">
          No key stored. This group cannot publish until one is added.
        </p>
      )}

      {cred?.invalid && cred.invalid_reason && (
        <p className="text-xs text-danger">{cred.invalid_reason}</p>
      )}

      <form onSubmit={save} className="space-y-2">
        <label className="block">
          <span className="eyebrow">{cred ? 'replace key' : 'status 200 api key'}</span>
          <div className="mt-1.5 flex gap-2">
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              className="input-field grow font-mono text-sm"
              placeholder="rl_…"
              value={value}
              onChange={(e) => { setValue(e.target.value); setError(''); }}
            />
            <button type="submit" className="btn-primary shrink-0" disabled={!!busy || !value.trim()}>
              {busy === 'save' ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
              {cred ? 'Replace' : 'Save'}
            </button>
          </div>
        </label>

        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
          Check the key against the provider before storing it
        </label>
      </form>

      <p className="text-xs text-muted">
        Sealed with AES-256-GCM before it touches the database. It is never sent back
        to this page — only the masked form above.
      </p>

      {(error || notice) && (
        <p className={`text-xs ${error ? 'text-danger' : 'text-ok'}`}>{error || notice}</p>
      )}

      {cred && (
        <div className="flex gap-2 pt-1">
          <button onClick={recheck} className="btn-ghost text-xs" disabled={!!busy}>
            {busy === 'verify' ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Re-check
          </button>
          <button onClick={revoke} className="btn-danger text-xs" disabled={!!busy}>
            {busy === 'revoke' ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
            Revoke
          </button>
        </div>
      )}

      {/* The webhook signing secret. Without it every provider callback 401s
          silently and every submitted post ages into "needs check", which is
          exactly the live bug this section exists to make visible. */}
      <div className="mt-4 space-y-2 border-t border-rule pt-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Webhook size={12} className="text-muted" />
          <span className="eyebrow">webhook secret</span>
          {hasHook && (
            <span className="badge-ok">
              <ShieldCheck size={11} />
              sealed
            </span>
          )}
        </div>

        {!hasHook && (
          <p className="text-xs text-warn leading-relaxed">
            Completion signal is off. Without a webhook secret, every provider callback
            fails verification and every post ages into <em>needs-check</em> instead of
            being confirmed. Paste the secret Status 200 generated for the callback below.
          </p>
        )}

        {hasHook && (
          <span className="text-muted text-xs">
            fingerprint {(group.webhook_secret.fingerprint || '').slice(0, 8)}
          </span>
        )}

        {hookUrl && (
          <label className="block">
            <span className="eyebrow">callback url</span>
            <div className="mt-1.5 flex gap-2">
              <input
                readOnly
                spellCheck={false}
                className="input-field grow font-mono text-xs"
                value={hookUrl}
                onFocus={(e) => e.target.select()}
              />
              <button type="button" onClick={copyUrl} className="btn-quiet shrink-0 text-xs" disabled={!!busy}>
                {copied ? <ClipboardCheck size={12} /> : <Copy size={12} />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </label>
        )}

        <form onSubmit={saveHook} className="space-y-2">
          <label className="block">
            <span className="eyebrow">{hasHook ? 'replace webhook secret' : 'webhook secret'}</span>
            <div className="mt-1.5 flex gap-2">
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                className="input-field grow font-mono text-sm"
                placeholder="whsec_…"
                value={value}
                onChange={(e) => { setValue(e.target.value); setError(''); }}
              />
              <button type="submit" className="btn-primary shrink-0" disabled={!!busy || !value.trim()}>
                {busy === 'save' ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
                {hasHook ? 'Replace' : 'Save'}
              </button>
            </div>
          </label>
        </form>

        {hasHook && (
          <button onClick={revokeHook} className="btn-danger text-xs" disabled={!!busy}>
            {busy === 'revoke' ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
            Revoke webhook secret
          </button>
        )}
      </div>
    </div>
  );
}
