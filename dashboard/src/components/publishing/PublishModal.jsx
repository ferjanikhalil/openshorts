import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Loader2, Send, AlertTriangle, Check, Clock, RefreshCw,
} from 'lucide-react';
import Modal from '../ui/Modal';
import {
  listDestinations, previewPublish, publishClip, publishJob, platformLabel,
} from '../../lib/publishing';

// PublishModal — the one surface that starts a publication.
//
// Three modes, one request. The operator ticks accounts; ticking every account
// of a group, or one account, or a hand-picked set spanning several groups, all
// produce the same POST with a different selection. The backend records a `mode`
// label for the history view and branches on nothing.
//
// Preview runs the dispatcher's own pre-flight checks and writes nothing, so the
// "9 accounts, 2 blocked" answer arrives before a quota slot is spent rather
// than after.

function GroupSection({ group, selected, onToggle, onToggleAll }) {
  const ids = group.destinations.map((d) => d.id);
  const allOn = ids.every((id) => selected.has(id));
  return (
    <div className="rounded-input bg-paper p-3">
      <div className="flex items-center gap-2">
        <button
          className="btn-quiet px-2 py-1 text-xs"
          onClick={() => onToggleAll(ids, !allOn)}
        >
          {allOn ? 'None' : 'All'}
        </button>
        <span className="font-display lowercase text-lg text-ink leading-none">{group.name}</span>
        {!group.enabled && <span className="badge-quiet">paused</span>}
      </div>
      <div className="mt-2 space-y-1">
        {group.destinations.map((d) => {
          const on = selected.has(d.id);
          const usable = d.enabled && group.enabled;
          return (
            <label key={d.id}
              className={`flex items-center gap-2 text-sm ${usable ? 'text-ink2' : 'text-muted'}`}>
              <input
                type="checkbox"
                checked={on}
                disabled={!usable}
                onChange={() => onToggle(d.id)}
              />
              <span className="eyebrow">{platformLabel(d.platform)}</span>
              <span>{d.display_name || d.provider_account_ref}</span>
              {!d.enabled && <span className="badge-quiet">disabled</span>}
              {['blocked', 'disconnected'].includes(d.health) && (
                <span className="badge-danger">{d.health}</span>
              )}
            </label>
          );
        })}
      </div>
    </div>
  );
}

export default function PublishModal({
  isOpen, onClose, jobId, clipIndex = null, clipCount = 0, clipLabel, onPublished,
}) {
  // Whole-job mode when no single clip was named: the caller opened this from a
  // finished batch rather than from one result card.
  const jobMode = clipIndex === null || clipIndex === undefined;

  const [dests, setDests] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [spacing, setSpacing] = useState(15);
  const [immediate, setImmediate] = useState(false);
  const [maxClips, setMaxClips] = useState('');
  const [caption, setCaption] = useState('');

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    setLoading(true); setError(''); setResult(null);
    listDestinations()
      .then((r) => { if (!cancelled) setDests(r.destinations || []); })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [isOpen]);

  const groups = useMemo(() => {
    const by = new Map();
    for (const d of dests) {
      const key = d.publish_group_id;
      if (!by.has(key)) {
        by.set(key, { id: key, name: d.group_name, enabled: d.group_enabled, destinations: [] });
      }
      by.get(key).destinations.push(d);
    }
    return [...by.values()];
  }, [dests]);

  const ids = useMemo(() => [...selected], [selected]);

  // Re-preview whenever the selection changes. Cheap by design — it writes
  // nothing — and it is the only way to learn a destination is out of quota
  // before an attempt discovers it.
  const refreshPreview = useCallback(async () => {
    if (!ids.length) { setPreview(null); return; }
    setPreviewing(true);
    try {
      setPreview(await previewPublish({
        destination_ids: ids,
        job_id: jobId,
        // Whole-job preview checks clip 0: the per-clip differences that matter
        // here (size, duration) are the same shape across a job's clips.
        clip_index: jobMode ? 0 : clipIndex,
      }));
      setError('');
    } catch (e) {
      setError(e.message);
      setPreview(null);
    } finally {
      setPreviewing(false);
    }
  }, [ids, jobId, clipIndex, jobMode]);

  useEffect(() => {
    const t = setTimeout(refreshPreview, 150);
    return () => clearTimeout(t);
  }, [refreshPreview]);

  const toggle = (id) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const toggleAll = (list, on) => setSelected((prev) => {
    const next = new Set(prev);
    for (const id of list) { if (on) next.add(id); else next.delete(id); }
    return next;
  });

  const send = async () => {
    setSending(true); setError('');
    try {
      const body = {
        job_id: jobId,
        destination_ids: ids,
        caption: caption.trim() || undefined,
      };
      const res = jobMode
        ? await publishJob({
          ...body,
          clip_count: clipCount,
          spacing_seconds: Math.max(60, Math.round(spacing * 60)),
          immediate,
          max_clips: maxClips ? Number(maxClips) : undefined,
        })
        : await publishClip({ ...body, clip_index: clipIndex });
      setResult(res);
      onPublished?.(res);
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  };

  const readyCount = preview?.ready_count ?? 0;
  const blocked = (preview?.destinations || []).filter((c) => !c.ready);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size="lg"
      eyebrow="publish"
      title={jobMode ? `all clips · ${clipCount} available` : (clipLabel || `clip ${clipIndex + 1}`)}
      footer={
        result ? (
          <button className="btn-primary w-full" onClick={onClose}>Done</button>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <button
              className="btn-primary grow"
              disabled={sending || !ids.length || readyCount === 0}
              onClick={send}
            >
              {sending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              {jobMode
                ? `Schedule ${readyCount} account${readyCount === 1 ? '' : 's'} × clips`
                : `Publish to ${readyCount} account${readyCount === 1 ? '' : 's'}`}
            </button>
            <button className="btn-ghost" onClick={refreshPreview} disabled={previewing}>
              {previewing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              Re-check
            </button>
          </div>
        )
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 size={14} className="animate-spin" /> Loading accounts…
        </div>
      ) : result ? (
        <div className="space-y-3">
          {jobMode ? (
            (result.created?.length > 0) ? (
              <p className="flex items-center gap-2 text-sm text-ok">
                <Check size={14} />
                {result.created.length} post{result.created.length === 1 ? '' : 's'} queued.
              </p>
            ) : (result.existing?.length > 0) ? (
              <p className="flex items-center gap-2 text-sm text-warn">
                <AlertTriangle size={14} />
                No new post was queued — this selection already has publishing request(s).
              </p>
            ) : (
              <p className="flex items-center gap-2 text-sm text-muted">
                <AlertTriangle size={14} />
                Nothing was queued.
              </p>
            )
          ) : (
            <p className="flex items-center gap-2 text-sm text-ok">
              <Check size={14} />
              Queued to {result.attempts?.length || 0} account(s).
              {result.duplicate && ' This selection was already published — showing the original request.'}
            </p>
          )}
          {(jobMode && result.existing?.length > 0) && (
            <div className="rounded-input bg-paper p-3 text-xs text-muted">
              <p className="eyebrow mb-1">existing requests</p>
              {result.existing.map((e, i) => (
                <p key={i}>
                  clip {(e.clip_index ?? 0) + 1} — status: {e.status || 'pending'}
                </p>
              ))}
            </div>
          )}
          {(result.skipped?.length > 0) && (
            <div className="rounded-input bg-paper p-3 text-xs text-muted">
              <p className="eyebrow mb-1">skipped</p>
              {result.skipped.map((s, i) => (
                <p key={i}>clip {(s.clip_index ?? 0) + 1} — {s.reason}</p>
              ))}
            </div>
          )}
          <p className="text-xs text-muted">
            {(jobMode && (result.created?.length || 0) === 0)
              ? 'Open Publishing → Activity to inspect or retry the existing request.'
              : 'Track them in Publishing → Activity. Nothing else is needed here.'}
          </p>
        </div>
      ) : dests.length === 0 ? (
        <p className="text-sm text-muted">
          No accounts are configured yet. Add a group, its API key and its accounts
          in Publishing → Settings first.
        </p>
      ) : (
        <div className="space-y-4">
          <div className="space-y-2">
            {groups.map((g) => (
              <GroupSection
                key={g.id}
                group={g}
                selected={selected}
                onToggle={toggle}
                onToggleAll={toggleAll}
              />
            ))}
          </div>

          {jobMode && (
            <div className="rounded-input bg-paper p-3 space-y-2">
              <p className="eyebrow">schedule</p>
              <div className="flex flex-wrap items-center gap-3 text-sm text-ink2">
                <label className="flex items-center gap-2">
                  <input
                    type="number" min="1" max="1440"
                    className="input-field w-20 py-1.5"
                    value={spacing}
                    onChange={(e) => setSpacing(e.target.value)}
                  />
                  minutes apart
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="number" min="1" placeholder={String(clipCount || '')}
                    className="input-field w-20 py-1.5"
                    value={maxClips}
                    onChange={(e) => setMaxClips(e.target.value)}
                  />
                  clips at most
                </label>
                <label className="flex items-center gap-2 text-xs text-muted">
                  <input type="checkbox" checked={immediate}
                    onChange={(e) => setImmediate(e.target.checked)} />
                  Start now, ignore the daytime window
                </label>
              </div>
              <p className="text-xs text-muted">
                Spacing exists because firing a job&apos;s clips at one account at once
                earns a rate-limit on everything after the first.
              </p>
            </div>
          )}

          <label className="block">
            <span className="eyebrow">caption override (optional)</span>
            <textarea
              rows={2}
              className="input-field mt-1.5"
              placeholder="Leave empty to use each clip's generated title and description"
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
            />
          </label>

          {preview && (
            <div className="rounded-input bg-paper p-3 text-sm">
              <p className="flex items-center gap-2">
                {readyCount > 0
                  ? <><Check size={13} className="text-ok" /><span className="text-ink2">{readyCount} of {preview.count} account(s) ready</span></>
                  : <><AlertTriangle size={13} className="text-danger" /><span className="text-danger">No selected account can publish right now</span></>}
              </p>
              {!preview.media_ready && (
                <p className="mt-1.5 text-xs text-warn">
                  No public media origin is configured, so the provider cannot fetch
                  the video. Set PUBLISHING_PUBLIC_BASE_URL or R2 credentials.
                </p>
              )}
              {blocked.map((c) => (
                <p key={c.destination.id} className="mt-1.5 text-xs text-danger">
                  {platformLabel(c.destination.platform)}{' '}
                  {c.destination.display_name || c.destination.provider_account_ref}: {c.blockers.join('; ')}
                </p>
              ))}
              {(preview.destinations || []).flatMap((c) =>
                c.warnings.map((w, i) => (
                  <p key={`${c.destination.id}-${i}`} className="mt-1 flex items-start gap-1.5 text-xs text-muted">
                    <Clock size={11} className="mt-0.5 shrink-0" />
                    {platformLabel(c.destination.platform)}: {w}
                  </p>
                )))}
            </div>
          )}

          {error && <p className="text-xs text-danger">{error}</p>}
        </div>
      )}
    </Modal>
  );
}
