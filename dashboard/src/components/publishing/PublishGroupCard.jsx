import { useState } from 'react';
import {
  ChevronDown, ChevronRight, Loader2, Power, Trash2, Check, X, Pencil,
} from 'lucide-react';
import {
  updateGroup, deleteGroup, groupSlots, slotLabel, credentialForSlot,
} from '../../lib/publishing';
import CredentialForm from './CredentialForm';
import DestinationList from './DestinationList';
import PostingPlanEditor from './PostingPlanEditor';

// PublishGroupCard — one group: its keys, its accounts, its state.
//
// A group is a reusable bundle of accounts plus the credential that reaches
// them. It is NOT the unit of publication: a publish can pick accounts across
// groups, and every attempt row names its own destination. This card is the
// place the operator configures the bundle, nothing more.
//
// "The credential" is singular only for a provider that connects every social
// account under one key. Where a provider caps that (Zernio's free tier: two
// accounts) a group holds several keys, one per credential slot, and the badges
// below have to answer "can this batch publish?" across all of them — a group
// whose default key is fine but whose second account has none is exactly as
// stuck, for that platform, as a group with no key at all.
export default function PublishGroupCard({ group, platforms, provider, onChanged }) {
  // Every slot a destination or a key refers to, and which of them lack a usable
  // key. Computed before state so the card can open itself on a problem.
  // A brand-new group refers to no slot at all, so it is treated as having the
  // one every group has — the default — which is what keeps "no key" showing on
  // a group that has nothing in it yet.
  const declaredSlots = groupSlots(group);
  const slots = declaredSlots.length ? declaredSlots : [''];
  const missingKeys = slots.filter((s) => !credentialForSlot(group, s));
  const rejectedKeys = slots.filter((s) => credentialForSlot(group, s)?.invalid);
  const hasAnyKey = missingKeys.length < slots.length;

  // Open by default when something needs doing — including a key the provider
  // rejected, which is just as publish-stopping as a missing one, and including
  // one bad key among several, which is otherwise invisible while collapsed.
  const [open, setOpen] = useState(
    !group.destinations?.length
    || missingKeys.length > 0 || rejectedKeys.length > 0);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(group.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const s = group.summary || {};
  const dests = group.destinations || [];

  const run = async (tag, fn) => {
    setBusy(tag); setError('');
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy('');
    }
  };

  const rename = async (e) => {
    e.preventDefault();
    const next = name.trim();
    if (!next || next === group.name) { setRenaming(false); return; }
    await run('rename', () => updateGroup(group.id, { name: next }));
    setRenaming(false);
  };

  return (
    <div className="card p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-3">
        <button
          className="text-muted hover:text-ink transition-colors"
          aria-label={open ? 'collapse' : 'expand'}
          onClick={() => setOpen(!open)}
        >
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>

        {renaming ? (
          <form onSubmit={rename} className="flex items-center gap-1.5">
            <input
              autoFocus
              className="input-field py-1.5 text-sm"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <button type="submit" className="btn-quiet px-2 py-1" disabled={busy === 'rename'}>
              {busy === 'rename' ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
            </button>
            <button type="button" className="btn-quiet px-2 py-1"
              onClick={() => { setRenaming(false); setName(group.name); }}>
              <X size={12} />
            </button>
          </form>
        ) : (
          <button
            className="group/name flex items-center gap-2 text-left"
            onClick={() => setRenaming(true)}
          >
            <h3 className="font-display lowercase text-xl text-ink leading-none">{group.name}</h3>
            <Pencil size={11} className="text-muted opacity-0 group-hover/name:opacity-100 transition-opacity" />
          </button>
        )}

        <span className="readout">{provider?.label || group.provider}</span>
        {!group.enabled && <span className="badge-quiet">paused</span>}
        {/* Key health, per provider account. A single-key group reads exactly as
            before ("no key" / "key rejected"); a multi-account batch names the
            account, because "no key" on a batch that is publishing to two of
            three platforms is a sentence the operator cannot act on. */}
        {missingKeys.map((s) => (
          <span key={`m${s}`} className="badge-warn">
            {slots.length > 1 ? `no key · ${slotLabel(s)}` : 'no key'}
          </span>
        ))}
        {rejectedKeys.map((s) => (
          <span key={`r${s}`} className="badge-danger">
            {slots.length > 1 ? `key rejected · ${slotLabel(s)}` : 'key rejected'}
          </span>
        ))}
        {!group.webhook_secret && hasAnyKey
          && (provider ? provider.supportsWebhooks : true) && (
          <span className="badge-warn" title="No webhook secret stored — provider
            callbacks cannot verify, so every post ages into needs-check">
            no webhook secret
          </span>
        )}
        {s.needs_attention > 0 && (
          <span className="badge-danger">{s.needs_attention} need attention</span>
        )}

        <div className="ml-auto flex items-center gap-1">
          <span className="readout mr-1">
            {s.enabled_destinations ?? dests.filter((d) => d.enabled).length}/{dests.length} accounts
          </span>
          <button
            title={group.enabled ? 'Pause this group' : 'Resume this group'}
            className="btn-quiet px-2 py-1 text-xs"
            disabled={!!busy}
            onClick={() => run('toggle', () => updateGroup(group.id, { enabled: !group.enabled }))}
          >
            {busy === 'toggle' ? <Loader2 size={12} className="animate-spin" /> : <Power size={12} />}
          </button>
          <button
            title="Delete this group and its publication history"
            className="btn-danger px-2 py-1 text-xs"
            disabled={!!busy}
            onClick={() => setConfirmDelete(true)}
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      {error && <p className="mt-2 text-xs text-danger">{error}</p>}

      {confirmDelete && (
        <div className="mt-3 rounded-input border border-danger/40 bg-paper p-3 text-sm">
          <p className="text-ink2">
            Deleting <strong>{group.name}</strong> also deletes its accounts and the
            record of every post they made. Pausing keeps the history and stops
            future posts, which is usually what is wanted.
          </p>
          <div className="mt-2 flex gap-2">
            <button className="btn-danger py-1.5 text-xs" disabled={!!busy}
              onClick={() => run('delete', () => deleteGroup(group.id))}>
              {busy === 'delete' ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
              Delete anyway
            </button>
            <button className="btn-ghost py-1.5 text-xs" onClick={() => setConfirmDelete(false)}>
              Keep it
            </button>
          </div>
        </div>
      )}

      {open && (
        <div className="mt-4 space-y-5 border-t border-rule pt-4">
          <section>
            <p className="eyebrow mb-2">posting plan</p>
            <PostingPlanEditor group={group} onChanged={onChanged} />
          </section>
          <section>
            <p className="eyebrow mb-2">
              {slots.length > 1 ? 'credentials' : 'credential'}
            </p>
            <CredentialForm group={group} provider={provider} onSaved={onChanged} />
          </section>
          <section>
            <p className="eyebrow mb-2">accounts</p>
            <DestinationList group={group} platforms={platforms}
              provider={provider} onChanged={onChanged} />
          </section>
        </div>
      )}
    </div>
  );
}
