import { useState } from 'react';
import { Plus, Loader2, Trash2, RotateCcw, Power, Pencil, Check, X } from 'lucide-react';
import {
  createDestination, updateDestination, deleteDestination,
  platformLabel, healthStyle, PLATFORM_LABELS,
} from '../../lib/publishing';

// DestinationList — the connected social accounts inside one group.
//
// Accounts are typed in by hand because Status 200 exposes no account-listing
// endpoint (every documented listing route answers 405). `provider_account_ref`
// is opaque here: nothing in the frontend parses it, it is just handed back to
// the provider.
//
// The ref is editable in place, and that is not a convenience. A wrong ref is
// rejected by the provider with an error that cannot be told apart from a real
// permission problem — Status 200 answers a nonexistent account and a genuine
// one with byte-identical `Account does not belong to API key owner` — so
// getting it right is iterative. The alternative, delete and recreate, cascades
// to the destination's attempts and destroys the record of what was posted.
//
// Health starts `unverified` and only a real publish can prove it, so this list
// shows that state plainly rather than pretending an untested account is fine.
export default function DestinationList({ group, platforms, onChanged }) {
  const [adding, setAdding] = useState(false);
  const [platform, setPlatform] = useState('youtube');
  const [ref, setRef] = useState('');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [editingId, setEditingId] = useState('');
  const [editRef, setEditRef] = useState('');

  const options = platforms?.length ? platforms : Object.keys(PLATFORM_LABELS);
  const dests = group.destinations || [];

  const add = async (e) => {
    e.preventDefault();
    if (!ref.trim()) return;
    setBusy('add'); setError('');
    try {
      await createDestination(group.id, {
        platform,
        provider_account_ref: ref.trim(),
        display_name: label.trim() || null,
      });
      setRef(''); setLabel(''); setAdding(false);
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
            <input
              className="input-field grow py-2 font-mono text-sm"
              placeholder="profile uuid at the provider"
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
              onClick={() => { setAdding(false); setError(''); }}>
              Cancel
            </button>
          </div>
          <p className="text-xs text-muted">
            For Status 200 this is the <strong>profile UUID</strong>, not the
            @handle — the same value for every platform under one profile. Their
            docs and the &ldquo;copy API ID&rdquo; button both give the handle,
            which is rejected. Find it in the dashboard at Connections with
            DevTools open (Network → the <code>social_media_profiles</code>{' '}
            request → Preview → <code>id</code>).
          </p>
          <p className="text-xs text-muted">
            A wrong id fails with the same error as a real permission problem, so
            expect to correct it — it stays editable afterwards. Health stays
            unverified until the first successful post.
          </p>
        </form>
      ) : (
        <button className="btn-ghost py-2 text-xs" onClick={() => setAdding(true)}>
          <Plus size={12} /> Add account
        </button>
      )}
    </div>
  );
}
