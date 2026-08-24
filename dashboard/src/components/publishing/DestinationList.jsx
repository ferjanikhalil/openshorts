import { useState } from 'react';
import {
  Plus, Loader2, Trash2, RotateCcw, Power, Pencil, Check, X, ListTree,
} from 'lucide-react';
import {
  createDestination, updateDestination, deleteDestination,
  listProviderAccounts, platformLabel, healthStyle, PLATFORM_LABELS,
  groupSlots, slotLabel,
} from '../../lib/publishing';

// DestinationList — the connected social accounts inside one group.
//
// How an account gets in here depends on the provider, and the difference is
// declared rather than assumed. A provider with `supports_account_listing` can be
// asked what its key reaches (the "Fetch" button below), which both saves typing
// and proves the key works. A provider without it — Status 200, whose documented
// listing routes all answer 405 — leaves hand entry as the only option.
// `provider_account_ref` is opaque either way: nothing in the frontend parses it,
// it is just handed back to the provider.
//
// The ref is editable in place, and that is not a convenience. A wrong ref is
// rejected by the provider with an error that cannot be told apart from a real
// permission problem — Status 200 answers a nonexistent account and a genuine
// one with byte-identical `Account does not belong to API key owner` — so
// getting it right is iterative. The alternative, delete and recreate, cascades
// to the destination's attempts and destroys the record of what was posted.
//
// Health starts `unverified` and only a real publish can prove it (or, where the
// provider can list accounts, a read-only check), so this list shows that state
// plainly rather than pretending an untested account is fine.
//
// The `credential_slot` column is what makes a multi-account batch work: each row
// names WHICH provider account it posts through. Unset means the group default,
// and resolution is one-directional — a row naming a slot never falls back to
// the default, because falling back would post through the wrong account.
export default function DestinationList({ group, platforms, provider, onChanged }) {
  const [adding, setAdding] = useState(false);
  const [platform, setPlatform] = useState('youtube');
  const [ref, setRef] = useState('');
  const [label, setLabel] = useState('');
  const [slot, setSlot] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [editingId, setEditingId] = useState('');
  const [editRef, setEditRef] = useState('');
  const [editSlotId, setEditSlotId] = useState('');
  const [fetched, setFetched] = useState(null);
  const [fetchNote, setFetchNote] = useState('');

  const options = platforms?.length ? platforms : Object.keys(PLATFORM_LABELS);
  const dests = group.destinations || [];
  const multi = !!provider?.multiCredential;
  const slots = multi ? groupSlots(group) : [];
  const canList = !!provider?.supportsAccountListing;

  const add = async (e) => {
    e.preventDefault();
    if (!ref.trim()) return;
    setBusy('add'); setError('');
    try {
      await createDestination(group.id, {
        platform,
        provider_account_ref: ref.trim(),
        display_name: label.trim() || null,
        // null, not '' — the backend reads null as "the group default".
        credential_slot: multi && slot ? slot : null,
      });
      setRef(''); setLabel(''); setAdding(false); setFetched(null);
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const act = async (id, fn) => {
    setBusy(id); setError('');
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const saveRef = async (d) => {
    const next = editRef.trim();
    if (!next || next === d.provider_account_ref) { setEditingId(''); return; }
    await act(d.id, () => updateDestination(d.id, { provider_account_ref: next }));
    setEditingId('');
  };

  // Ask the provider which accounts this key reaches. Read-only: it publishes
  // nothing and spends no quota, so it is safe to click while debugging.
  const fetchAccounts = async () => {
    setBusy('fetch'); setError(''); setFetchNote('');
    try {
      const res = await listProviderAccounts(group.id, (multi && slot) || null);
      if (!res.accounts) {
        setFetched(null);
        setFetchNote(res.detail || 'This provider cannot list accounts.');
      } else {
        setFetched(res.accounts);
        setFetchNote(res.accounts.length
          ? `${res.accounts.length} account(s) connected to this key.`
          : 'This key reaches no connected accounts yet — connect one at the provider first.');
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const pick = (a) => {
    setRef(a.ref || '');
    if (a.platform && options.includes(a.platform)) setPlatform(a.platform);
    if (!label.trim() && a.username) setLabel(a.username);
  };

  return (
    <div className="space-y-2">
      {dests.length === 0 && !adding && (
        <p className="text-sm text-muted">
          No accounts yet. Add the connected accounts this group should post to.
        </p>
      )}

      {dests.map((d) => {
        const h = healthStyle(d.health);
        return (
          <div key={d.id}
            className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-input bg-paper px-3 py-2">
            <span className="eyebrow shrink-0">{platformLabel(d.platform)}</span>
            {editingId === d.id ? (
              <>
                <input
                  autoFocus
                  className="input-field grow py-1 font-mono text-xs"
                  value={editRef}
                  onChange={(e) => setEditRef(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); saveRef(d); }
                    if (e.key === 'Escape') setEditingId('');
                  }}
                />
                <button title="Save" className="btn-quiet px-2 py-1 text-xs"
                  disabled={!!busy} onClick={() => saveRef(d)}>
                  {busy === d.id ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                </button>
                <button title="Cancel" className="btn-quiet px-2 py-1 text-xs"
                  onClick={() => setEditingId('')}>
                  <X size={12} />
                </button>
              </>
            ) : (
              <>
                <span className={`text-sm ${d.enabled ? 'text-ink2' : 'text-muted line-through'}`}>
                  {d.display_name || d.provider_account_ref}
                </span>
                <span className={h.cls}>{h.label}</span>
              </>
            )}

            {/* Which provider account this row posts through. Shown only where a
                group can hold several — otherwise there is nothing to choose and
                the control would just be noise. */}
            {multi && (
              editSlotId === d.id ? (
                <select
                  autoFocus
                  className="input-field w-auto py-1 text-xs"
                  value={d.credential_slot || ''}
                  disabled={!!busy}
                  onChange={(e) => {
                    const next = e.target.value || null;
                    setEditSlotId('');
                    act(d.id, () => updateDestination(d.id, { credential_slot: next }));
                  }}
                  onBlur={() => setEditSlotId('')}
                >
                  <option value="">default account</option>
                  {slots.filter(Boolean).map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              ) : (
                <button
                  className="readout hover:text-ink transition-colors"
                  title="Which provider account this posts through"
                  disabled={!!busy}
                  onClick={() => setEditSlotId(d.id)}
                >
                  via {slotLabel(d.credential_slot)}
                </button>
              )
            )}

            {d.quota_remaining !== null && d.quota_remaining !== undefined && (
              <span className="readout">
                {d.quota_remaining}
                {d.quota_limit ? `/${d.quota_limit}` : ''} left today
              </span>
            )}
            {d.cooldown_until && new Date(d.cooldown_until) > new Date() && (
              <span className="badge-warn">cooling down</span>
            )}

            <div className="ml-auto flex items-center gap-1">
              <button
                title="Edit the provider account id"
                className="btn-quiet px-2 py-1 text-xs"
                disabled={!!busy || editingId === d.id}
                onClick={() => { setEditingId(d.id); setEditRef(d.provider_account_ref); }}
              >
                <Pencil size={12} />
              </button>
              <button
                title={d.enabled ? 'Disable' : 'Enable'}
                className="btn-quiet px-2 py-1 text-xs"
                disabled={!!busy}
                onClick={() => act(d.id, () => updateDestination(d.id, { enabled: !d.enabled }))}
              >
                {busy === d.id ? <Loader2 size={12} className="animate-spin" /> : <Power size={12} />}
              </button>
              {['blocked', 'disconnected', 'degraded'].includes(d.health) && (
                <button
                  title="Clear the health flag after fixing the connection at the platform"
                  className="btn-quiet px-2 py-1 text-xs"
                  disabled={!!busy}
                  onClick={() => act(d.id, () => updateDestination(d.id, { reset_health: true }))}
                >
                  <RotateCcw size={12} />
                </button>
              )}
              <button
                title="Remove — refused while it has posts in flight"
                className="btn-danger px-2 py-1 text-xs"
                disabled={!!busy}
                onClick={() => act(d.id, () => deleteDestination(d.id))}
              >
                <Trash2 size={12} />
              </button>
            </div>

            {d.health_detail && (
              <p className="w-full text-xs text-muted">{d.health_detail}</p>
            )}
          </div>
        );
      })}

      {error && <p className="text-xs text-danger">{error}</p>}

      {adding ? (
        <form onSubmit={add} className="space-y-2 rounded-input bg-paper p-3">
          {canList && (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className="btn-ghost py-1 text-xs"
                  disabled={!!busy} onClick={fetchAccounts}>
                  {busy === 'fetch'
                    ? <Loader2 size={12} className="animate-spin" />
                    : <ListTree size={12} />}
                  Fetch accounts
                </button>
                <span className="text-xs text-muted">
                  Read-only — asks {provider?.label || 'the provider'} what the
                  stored key{multi && slot ? ` for ${slot}` : ''} reaches. Nothing
                  is published.
                </span>
              </div>
              {fetchNote && <p className="text-xs text-muted">{fetchNote}</p>}
              {fetched?.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {fetched.map((a) => (
                    <button
                      key={a.ref}
                      type="button"
                      className={`btn-quiet px-2 py-1 text-xs ${
                        a.registered ? 'opacity-50' : ''}`}
                      title={a.registered
                        ? 'Already registered in this group'
                        : `Use ${a.ref}`}
                      disabled={a.registered}
                      onClick={() => pick(a)}
                    >
                      {platformLabel(a.platform)}
                      {a.username ? ` · ${a.username}` : ''}
                      {a.needs_reconnection ? ' · reconnect' : ''}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <select
              className="input-field w-auto grow-0 py-2"
              value={platform}
              onChange={(e) => setPlatform(e.target.value)}
            >
              {options.map((p) => (
                <option key={p} value={p}>{platformLabel(p)}</option>
              ))}
            </select>
            {multi && (
              <select
                className="input-field w-auto grow-0 py-2 text-sm"
                title="Which provider account this posts through"
                value={slot}
                onChange={(e) => { setSlot(e.target.value); setFetched(null); }}
              >
                <option value="">default account</option>
                {slots.filter(Boolean).map((s) => (
                  <option key={s} value={s}>via {s}</option>
                ))}
              </select>
            )}
            <input
              className="input-field grow py-2 font-mono text-sm"
              placeholder={provider?.accountRefHint || 'account id at the provider'}
              value={ref}
              onChange={(e) => setRef(e.target.value)}
            />
            <input
              className="input-field grow py-2"
              placeholder="label (optional)"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn-primary py-2 text-xs" disabled={!!busy || !ref.trim()}>
              {busy === 'add' ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
              Add account
            </button>
            <button type="button" className="btn-ghost py-2 text-xs"
              onClick={() => { setAdding(false); setError(''); setFetched(null); setFetchNote(''); }}>
              Cancel
            </button>
          </div>
          {/* Where to find the id, in the provider's own terms. Provider-specific
              prose, so a second provider does not inherit instructions written
              for the first — that is the mistake this replaced. */}
          <p className="text-xs text-muted">{provider?.accountRefHelp}</p>
          <p className="text-xs text-muted">
            A wrong id fails with the same error as a real permission problem, so
            expect to correct it — it stays editable afterwards. Health stays
            unverified until{canList ? ' it is checked or' : ''} the first
            successful post.
          </p>
          {multi && slots.filter(Boolean).length > 0 && (
            <p className="text-xs text-muted">
              A row on a named account uses that account&rsquo;s key only — it
              never falls back to the default. If that key is missing or revoked
              the post parks and says so, rather than publishing through the
              wrong account.
            </p>
          )}
        </form>
      ) : (
        <button className="btn-ghost py-2 text-xs" onClick={() => setAdding(true)}>
          <Plus size={12} /> Add account
        </button>
      )}
    </div>
  );
}
