import { useState, useEffect, useCallback } from 'react';
import {
  Loader2, RefreshCw, ExternalLink, RotateCw, AlertTriangle, Ban,
} from 'lucide-react';
import {
  listAttempts, retryAttempt, cancelRequest, listRequests,
  platformLabel, statusStyle, formatWhen,
} from '../../lib/publishing';

// PublishStatusBoard — per-account publication history.
//
// The unit here is the attempt, not the request: a 9-account publish where one
// TikTok failed is one row to look at, not a whole request to re-read. That is
// why `publish_group_id` and `platform` live on the attempt row.
//
// `unknown` gets its own treatment. The provider accepted the post but never
// confirmed it, so the post may well be live — retrying could double-post. The
// board asks for an explicit confirmation instead of quietly sending `force`.
export default function PublishStatusBoard({ jobId = null }) {
  const [attempts, setAttempts] = useState([]);
  const [requests, setRequests] = useState([]);
  const [onlyAttention, setOnlyAttention] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [forcing, setForcing] = useState(null);

  const load = useCallback(async () => {
    setError('');
    // Independent fetches: one failing must not blank the other. A requests 404
    // and a working attempts list is still a useful board — Promise.all would
    // have thrown both away.
    const [aResult, rResult] = await Promise.allSettled([
      listAttempts({ needs_attention: onlyAttention, limit: 200 }),
      listRequests({ job_id: jobId || undefined, limit: 50 }),
    ]);
    if (aResult.status === 'fulfilled') {
      setAttempts(aResult.value.attempts || []);
    } else {
      setAttempts([]);
      setError((prev) => prev || `attempts: ${aResult.reason.message}`);
    }
    if (rResult.status === 'fulfilled') {
      setRequests(rResult.value.requests || []);
    } else {
      setRequests([]);
      setError((prev) => prev || `requests: ${rResult.reason.message}`);
    }
    setLoading(false);
  }, [onlyAttention, jobId]);

  useEffect(() => { load(); }, [load]);

  // Poll while anything is still moving. Stops on its own once the board is
  // settled, so an idle tab makes no requests.
  useEffect(() => {
    const live = attempts.some((a) =>
      ['pending', 'in_flight', 'submitted', 'deferred', 'failed'].includes(a.status));
    if (!live) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [attempts, load]);

  const rows = jobId
    ? attempts.filter((a) => requests.some((r) => r.id === a.publish_request_id))
    : attempts;

  const retry = async (id, force) => {
    setBusy(id); setError('');
    try {
      await retryAttempt(id, force);
      setForcing(null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  const cancel = async (id) => {
    setBusy(id); setError('');
    try {
      await cancelRequest(id);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  const scheduled = requests.filter(
    (r) => r.scheduled_for && !(r.attempts || []).some((a) => a.status !== 'pending'),
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={onlyAttention}
            onChange={(e) => setOnlyAttention(e.target.checked)} />
          Only what needs attention
        </label>
        <button className="btn-quiet ml-auto px-3 py-1.5 text-xs" onClick={load}>
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>

      {error && <p className="text-xs text-danger">{error}</p>}

      {scheduled.length > 0 && (
        <div className="rounded-input bg-paper p-3">
          <p className="eyebrow mb-2">scheduled</p>
          {scheduled.map((r) => (
            <div key={r.id} className="flex flex-wrap items-center gap-2 py-1 text-sm">
              <span className="readout">clip {r.clip_index + 1}</span>
              <span className="text-ink2">{formatWhen(r.scheduled_for)}</span>
              <span className="text-muted text-xs">
                {(r.attempts || []).length} account(s)
              </span>
              <button
                className="btn-danger ml-auto px-2 py-1 text-xs"
                disabled={busy === r.id}
                onClick={() => cancel(r.id)}
              >
                {busy === r.id ? <Loader2 size={12} className="animate-spin" /> : <Ban size={12} />}
                Cancel
              </button>
            </div>
          ))}
          <p className="mt-1 text-xs text-muted">
            Cancelling stops what has not reached the provider. A post already
            submitted cannot be recalled.
          </p>
        </div>
      )}

      {loading && rows.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Loader2 size={14} className="animate-spin" /> Loading…
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted">
          {onlyAttention
            ? 'Nothing needs attention.'
            : 'No posts yet. Publish a clip and it shows up here, one row per account.'}
        </p>
      ) : (
        <div className="space-y-1.5">
          {rows.map((a) => {
            const st = statusStyle(a.status);
            return (
              <div key={a.id} className="rounded-input bg-paper px-3 py-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="eyebrow shrink-0">{platformLabel(a.platform)}</span>
                  <span className="text-sm text-ink2">{a.destination_label || '—'}</span>
                  <span className={st.cls}>{st.label}</span>
                  {a.attempt_number > 1 && (
                    <span className="readout">try {a.attempt_number}</span>
                  )}
                  {a.deferred_until && (
                    <span className="readout">next {formatWhen(a.deferred_until)}</span>
                  )}
                  <span className="readout ml-auto">{formatWhen(a.created_at)}</span>

                  {a.permalink && (
                    <a href={a.permalink} target="_blank" rel="noreferrer"
                      className="btn-quiet px-2 py-1 text-xs">
                      <ExternalLink size={12} /> View
                    </a>
                  )}
                  {['dead', 'failed', 'unknown', 'blocked'].includes(a.status) && (
                    <button
                      className="btn-ghost px-3 py-1 text-xs"
                      disabled={busy === a.id}
                      onClick={() => (a.status === 'unknown' ? setForcing(a) : retry(a.id, false))}
                    >
                      {busy === a.id ? <Loader2 size={12} className="animate-spin" /> : <RotateCw size={12} />}
                      Retry
                    </button>
                  )}
                </div>

                {a.error_message && (
                  <p className="mt-1 text-xs text-danger">
                    {a.error_code ? `${a.error_code}: ` : ''}{a.error_message}
                  </p>
                )}

                {forcing?.id === a.id && (
                  <div className="mt-2 rounded-input border border-warn/40 p-2.5 text-xs">
                    <p className="flex items-start gap-1.5 text-warn">
                      <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                      The provider accepted this post but never confirmed it. It may
                      already be live — retrying can publish it twice. Check the
                      account first.
                    </p>
                    <div className="mt-2 flex gap-2">
                      <button className="btn-danger py-1 text-xs"
                        disabled={busy === a.id}
                        onClick={() => retry(a.id, true)}>
                        Retry anyway
                      </button>
                      <button className="btn-ghost py-1 text-xs" onClick={() => setForcing(null)}>
                        Leave it
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
