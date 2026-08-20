import { useEffect, useState } from 'react';
import { Calendar, Loader2 } from 'lucide-react';
import { updateGroup, groupPlanPreview } from '../../lib/publishing';

// PostingPlanEditor — a group's posting rhythm: "start at 06:00, one post
// every 6 hours, at most 3 a day". The AI agent page's Rhythm schedule mode
// places its clips on THIS plan, batch-wide and quota-aware, and scheduled
// posts are handed to the provider with their timestamp so publishing does
// not need this machine awake at the slot.
//
// The plan lives in group.settings.plan — validated server-side on write
// (normalize_plan), so a bad value never reaches the scheduler.
const INTERVALS = [2, 3, 4, 6, 8, 12];
const CATCH_UP = [
  { value: 'next_slot', label: 'Next free slot (recommended)' },
  { value: 'immediate', label: 'Publish immediately on wake' },
  { value: 'skip', label: 'Skip the missed slot' },
];

export default function PostingPlanEditor({ group, onChanged }) {
  const existing = group.settings?.plan || null;
  const [on, setOn] = useState(!!existing);
  const [startTime, setStartTime] = useState(existing?.start_time || '06:00');
  const [interval, setInterval_] = useState(String(existing?.interval_hours || 6));
  const [maxPerDay, setMaxPerDay] = useState(String(existing?.max_per_day || 3));
  const [timezone, setTimezone] = useState(
    existing?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [catchUp, setCatchUp] = useState(existing?.catch_up || 'next_slot');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState(null);

  // The preview is computed by the same server function the scheduler runs,
  // with this group's live bookings — what it shows is what will happen.
  useEffect(() => {
    if (!existing) { setPreview(null); return; }
    let cancelled = false;
    groupPlanPreview(group.id, 5)
      .then((p) => { if (!cancelled) setPreview(p); })
      .catch(() => { if (!cancelled) setPreview(null); });
    return () => { cancelled = true; };
  }, [group.id, existing, group.settings]);

  const save = async (nextOn) => {
    setBusy(true); setError('');
    try {
      const settings = { ...(group.settings || {}) };
      if (nextOn) {
        settings.plan = {
          mode: 'rhythm', start_time: startTime,
          interval_hours: parseFloat(interval), max_per_day: parseInt(maxPerDay, 10),
          timezone, catch_up: catchUp,
        };
      } else {
        delete settings.plan;
      }
      await updateGroup(group.id, { settings });
      setOn(nextOn);
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const dirty = on !== !!existing
    || (on && (startTime !== (existing?.start_time || '06:00')
      || interval !== String(existing?.interval_hours || 6)
      || maxPerDay !== String(existing?.max_per_day || 3)
      || timezone !== (existing?.timezone || '')
      || catchUp !== (existing?.catch_up || 'next_slot')));

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm text-ink">
          <Calendar size={13} className="text-brass" />
          <span className="font-medium">Posting rhythm</span>
        </div>
        <button
          className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
            on ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
          }`}
          disabled={busy}
          onClick={() => (on || existing ? save(!on) : setOn(true))}
        >
          {on ? '✓ on' : 'off'}
        </button>
      </div>

      {!on && !existing && (
        <p className="text-xs text-muted">
          Off — posts from this group's clips go out as soon as they are ready. Turn on to
          place them on a fixed rhythm (e.g. 06:00, then every 6 hours).
        </p>
      )}

      {(on || existing) && (
        <div className="space-y-2.5">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <label className="block">
              <span className="text-[11px] text-muted">Start time</span>
              <input type="time" value={startTime} onChange={(e) => setStartTime(e.target.value)}
                className="input-field py-1.5 text-sm mt-0.5" />
            </label>
            <label className="block">
              <span className="text-[11px] text-muted">Every</span>
              <select value={interval} onChange={(e) => setInterval_(e.target.value)}
                className="input-field py-1.5 text-sm mt-0.5">
                {INTERVALS.map((h) => <option key={h} value={h}>{h} hours</option>)}
              </select>
            </label>
            <label className="block">
              <span className="text-[11px] text-muted">Max / day</span>
              <input type="number" min="1" max="10" value={maxPerDay}
                onChange={(e) => setMaxPerDay(e.target.value)}
                className="input-field py-1.5 text-sm mt-0.5" />
            </label>
            <label className="block">
              <span className="text-[11px] text-muted">Timezone</span>
              <input value={timezone} onChange={(e) => setTimezone(e.target.value)}
                className="input-field py-1.5 text-sm mt-0.5" />
            </label>
          </div>
          <label className="block">
            <span className="text-[11px] text-muted">If a slot passes while the machine is off</span>
            <select value={catchUp} onChange={(e) => setCatchUp(e.target.value)}
              className="input-field py-1.5 text-sm mt-0.5">
              {CATCH_UP.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>

          {error && <p className="text-xs text-danger">{error}</p>}

          <div className="flex items-center gap-2">
            <button className="btn-accent px-3 py-1.5 text-xs" disabled={busy || !dirty}
              onClick={() => save(true)}>
              {busy ? <Loader2 size={12} className="animate-spin" /> : 'Save rhythm'}
            </button>
            {on && existing && (
              <button className="btn-ghost px-3 py-1.5 text-xs" disabled={busy}
                onClick={() => save(false)}>
                Turn off
              </button>
            )}
          </div>

          {preview?.plan && (
            <div className="pt-1">
              <p className="text-[11px] text-muted mb-1">
                Next slots {preview.daily_cap ? `(cap ${preview.daily_cap}/day, ${preview.booked_count} already booked)` : ''}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {preview.slots.map((s) => (
                  <span key={s} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-paper3 border border-rule text-muted">
                    {new Date(s).toLocaleString(undefined, {
                      weekday: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                    })}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
