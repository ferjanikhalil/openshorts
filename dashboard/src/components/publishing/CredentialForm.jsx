import { useState } from 'react';
import {
  KeyRound, Loader2, ShieldCheck, AlertTriangle, RefreshCw, Trash2, Webhook,
  Copy, ClipboardCheck, Plus, X,
} from 'lucide-react';
import {
  setCredential, verifyCredential, revokeCredential,
  groupSlots, slotLabel, credentialForSlot,
} from '../../lib/publishing';

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
//
// A group may hold MORE THAN ONE api_key, one per credential slot, because a
// provider can cap how many social accounts a single account connects (Zernio's
// free tier: two) while a batch needs three. Each slot is a separate provider
// account with its own key, its own health and its own revoke — so this form
// renders one row per slot rather than one key per group. `provider` supplies
// every provider-specific word here; nothing below names a provider.
export default function CredentialForm({ group, provider, onSaved }) {
  const [value, setValue] = useState('');
  const [verify, setVerify] = useState(true);
  // Which slot the input above is for. '' is the group default — the only shape
  // a single-account provider ever has, and the default for everyone else too.
  const [slot, setSlot] = useState('');
  const [addingSlot, setAddingSlot] = useState(false);
  const [newSlot, setNewSlot] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [copied, setCopied] = useState(false);

  const hasHook = !!group.webhook_secret;
  const hookUrl = group.webhook_url || '';
  const multi = !!provider?.multiCredential;
  // One row per provider account. A single-account provider has exactly one, the
  // group default — the empty slot. For a multi-account provider the default is
  // always shown too (it is what an unslotted destination uses) alongside every
  // named slot, plus whichever slot the input is currently pointed at.
  const shownSlots = multi
    ? [...new Set(['', ...groupSlots(group), slot])]
    : [''];

  // A slot is only meaningful for a provider that admits several accounts per
  // group; for anything else the label is dropped and the key is the default.
  const slotArg = multi && slot ? slot : null;

  const save = async (e) => {
    e.preventDefault();
    const key = value.trim();
    if (key.length < 8) {
      setError('That key looks too short to be valid.');
      return;
    }
    setBusy('save'); setError(''); setNotice('');
    try {
      const saved = await setCredential(group.id, key, { verify, slot: slotArg });
      // Cleared before anything else so the plaintext does not survive a
      // re-render, even if the callback below throws.
      setValue('');
      // The endpoint answers {credential, replaced, verified} — the masked view
      // is nested. Reading it flat here is what raised "cannot read properties
      // of undefined" AFTER the key had already been stored successfully.
      const info = saved?.credential;
      const where = slotArg ? ` for ${slotArg}` : '';
      setNotice(info
        ? `Stored and sealed${where} · ${info.masked} · fingerprint ${(info.fingerprint || '').slice(0, 8)}`
        : `Stored and sealed${where}.`);
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
      //
      // Sent for the SAME slot as the key above: each provider account signs its
      // callbacks with its own secret, and webhook verification tries every
      // stored secret for the provider until one matches, so a per-account
      // secret is what lets a two-account batch confirm both accounts' posts.
      const saved = await setCredential(group.id, secret, {
        kind: 'webhook_secret', verify: false, slot: slotArg,
      });
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

  const recheck = async (which = '') => {
    setBusy(`verify:${which}`); setError(''); setNotice('');
    try {
      const res = await verifyCredential(group.id, which || null);
      const where = which ? ` for ${which}` : '';
      // ok is tri-state: true, false, or null when the provider offers no way to
      // check a key without publishing. Treating null as a rejection would tell
      // the operator their good key was refused.
      if (res.ok === null || res.ok === undefined) {
        setNotice(res.detail || `${provider?.label || 'This provider'} cannot verify a key without publishing.`);
      } else if (res.ok) {
        setNotice(`The provider accepted this key${where}.`);
      } else {
        setError(res.detail || `The provider rejected this key${where}.`);
      }
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const revoke = async (which = '') => {
    setBusy(`revoke:${which}`); setError(''); setNotice('');
    try {
      // Narrow by design: revoking one account's key in a multi-account batch
      // must not touch the others. `groupSlots` keeps the slot visible
      // afterwards, because a destination still pointing at it is now parked.
      await revokeCredential(group.id, 'api_key', { slot: which || null });
      setNotice(which
        ? `Key for ${which} revoked. Destinations on that account are paused until a new one is entered.`
        : 'Key revoked. Publishing for this group is paused until a new one is entered.');
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
      await revokeCredential(group.id, 'webhook_secret', { allSlots: true });
      setNotice('Webhook secret revoked. Provider callbacks will stop verifying until a new one is entered.');
      onSaved?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const addSlot = (e) => {
    e.preventDefault();
    const next = newSlot.trim();
    if (!next) return;
    // Nothing is stored yet — naming a slot only points the input at it. The row
    // appears once its key is saved.
    setSlot(next);
    setNewSlot(''); setAddingSlot(false); setError(''); setNotice('');
  };

  const keyRow = (which) => {
    const c = credentialForSlot(group, which);
    const active = (slot || '') === (which || '');
    return (
      <div key={which || '_default'}
        className={`flex flex-wrap items-center gap-2 rounded-input px-3 py-2 text-sm ${
          active ? 'bg-paper ring-1 ring-rule' : 'bg-paper'}`}>
        {multi && (
          <button type="button"
            className={`eyebrow shrink-0 ${active ? 'text-ink' : 'text-muted'}`}
            title="Enter or replace the key for this account"
            onClick={() => { setSlot(which); setError(''); setNotice(''); }}
          >
            {slotLabel(which)}
          </button>
        )}
        {c ? (
          <>
            <span className={c.invalid ? 'badge-danger' : 'badge-ok'}>
              {c.invalid ? <AlertTriangle size={11} /> : <ShieldCheck size={11} />}
              {c.invalid ? 'rejected' : 'active'}
            </span>
            <code className="font-mono text-xs text-ink2">{c.masked}</code>
            <span className="text-muted text-xs">
              fingerprint {(c.fingerprint || '').slice(0, 8)}
              {c.last_used_at
                ? ` · last used ${new Date(c.last_used_at).toLocaleDateString()}`
                : ' · never used'}
            </span>
            {c.needs_rotation && (
              <span className="badge-warn">re-enter to re-seal under the current master key</span>
            )}
            <div className="ml-auto flex gap-1">
              <button onClick={() => recheck(which)} className="btn-quiet px-2 py-1 text-xs"
                title="Re-check this key against the provider" disabled={!!busy}>
                {busy === `verify:${which}`
                  ? <Loader2 size={12} className="animate-spin" />
                  : <RefreshCw size={12} />}
              </button>
              <button onClick={() => revoke(which)} className="btn-danger px-2 py-1 text-xs"
                title={which ? `Revoke only ${which}` : 'Revoke this key'} disabled={!!busy}>
                {busy === `revoke:${which}`
                  ? <Loader2 size={12} className="animate-spin" />
                  : <Trash2 size={12} />}
              </button>
            </div>
          </>
        ) : (
          <span className="text-xs text-warn">
            {/* The state that silently stops one platform in a multi-account
                batch: a destination points here and there is no key to reach
                it, so its posts park instead of publishing. */}
            {multi
              ? 'no key stored — destinations on this account cannot publish'
              : 'No key stored. This group cannot publish until one is added.'}
          </span>
        )}
        {c?.invalid && c.invalid_reason && (
          <p className="w-full text-xs text-danger">{c.invalid_reason}</p>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3">
      {shownSlots.length > 0 && (
        <div className="space-y-1.5">{shownSlots.map(keyRow)}</div>
      )}

      {multi && (
        <div className="flex flex-wrap items-center gap-2">
          {addingSlot ? (
            <form onSubmit={addSlot} className="flex items-center gap-1.5">
              <input
                autoFocus
                className="input-field py-1 font-mono text-xs"
                placeholder="account label — e.g. zernio-b"
                value={newSlot}
                onChange={(e) => setNewSlot(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') setAddingSlot(false); }}
              />
              <button type="submit" className="btn-quiet px-2 py-1 text-xs"
                disabled={!newSlot.trim()}>
                <Plus size={12} />
              </button>
              <button type="button" className="btn-quiet px-2 py-1 text-xs"
                onClick={() => { setAddingSlot(false); setNewSlot(''); }}>
                <X size={12} />
              </button>
            </form>
          ) : (
            <button type="button" className="btn-ghost py-1 text-xs"
              onClick={() => setAddingSlot(true)}>
              <Plus size={12} /> Another {provider?.label || 'provider'} account
            </button>
          )}
          <span className="text-xs text-muted">
            {provider?.label || 'This provider'} caps how many social accounts one
            account can connect, so a batch that needs more holds several keys.
            Each account gets a label, and every destination names the account it
            posts through.
          </span>
        </div>
      )}

      <form onSubmit={save} className="space-y-2">
        <label className="block">
          <span className="eyebrow">
            {credentialForSlot(group, slot) ? 'replace key' : 'api key'}
            {multi && slot ? ` · ${slot}` : ''}
            {multi && !slot ? ' · default account' : ''}
          </span>
          <div className="mt-1.5 flex gap-2">
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              className="input-field grow font-mono text-sm"
              // The declared prefix, not a hardcoded one. It is a placeholder
              // only — nothing validates against it, so a provider changing its
              // prefix cannot make this form refuse a working key.
              placeholder={provider?.keyPrefix ? `${provider.keyPrefix}…` : 'provider api key'}
              value={value}
              onChange={(e) => { setValue(e.target.value); setError(''); }}
            />
            <button type="submit" className="btn-primary shrink-0" disabled={!!busy || !value.trim()}>
              {busy === 'save' ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
              {credentialForSlot(group, slot) ? 'Replace' : 'Save'}
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

        {provider && !provider.supportsWebhooks ? (
          <p className="text-xs text-muted leading-relaxed">
            {provider.label} sends no callbacks, so there is no secret to store.
            {provider.supportsStatusLookup
              ? ' Posts are confirmed by polling the provider instead.'
              : ' Posts are confirmed on submit.'}
          </p>
        ) : !hasHook && (
          <p className="text-xs text-warn leading-relaxed">
            Completion signal is off. Without a webhook secret, every provider callback
            fails verification and every post ages into <em>needs-check</em> instead of
            being confirmed. Paste the secret {provider?.label || 'the provider'}{' '}
            generated for the callback below
            {multi ? ', once per account — each signs with its own.' : '.'}
          </p>
        )}

        {hasHook && (
          <span className="text-muted text-xs">
            fingerprint {(group.webhook_secret.fingerprint || '').slice(0, 8)}
            {(group.webhook_secrets?.length || 0) > 1
              ? ` · ${group.webhook_secrets.length} secrets stored, one per account`
              : ''}
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

        {(!provider || provider.supportsWebhooks) && (
          <form onSubmit={saveHook} className="space-y-2">
            <label className="block">
              <span className="eyebrow">
                {hasHook ? 'replace webhook secret' : 'webhook secret'}
                {multi && slot ? ` · ${slot}` : ''}
              </span>
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
        )}

        {hasHook && (
          <button onClick={revokeHook} className="btn-danger text-xs" disabled={!!busy}>
            {busy === 'revoke' ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
            Revoke webhook secret{(group.webhook_secrets?.length || 0) > 1 ? 's' : ''}
          </button>
        )}
      </div>
    </div>
  );
}
