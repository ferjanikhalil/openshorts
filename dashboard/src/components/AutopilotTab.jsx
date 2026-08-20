import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Bot, Upload, Link2, Plus, X, Play, Square, Loader2, Check,
  AlertTriangle, ChevronDown, Download, Settings2, Share2, Calendar, Clock,
  Instagram, Youtube, Video,
} from 'lucide-react';
import { apiFetch, apiJson, uploadAutopilotSource } from '../lib/api';
import { publishingHealth, listGroups, listDestinations, groupPlanPreview, PLATFORM_LABELS } from '../lib/publishing';
import HookModal from './HookModal';
import SubtitleModal from './SubtitleModal';
import TranslateModal from './TranslateModal';
import BrandingSettings from './BrandingSettings';
import BrandingOverrideModal from './BrandingOverrideModal';
import Modal from './ui/Modal';
import { loadBrandingDefaults, saveBrandingDefaults } from '../lib/branding';
import ResultCard from './ResultCard';
import BatchPipeline from './BatchPipeline';

// AutopilotTab — the unattended multi-video cockpit. Self-contained (own state +
// own localStorage slot), mirroring the SaaShortsTab/HistoryTab precedent so it
// never repurposes the singleton dashboard job state. It fans N videos through
// POST /api/autopilot, polls GET /api/autopilot/{id} for a per-video stage board,
// and lands in a review grid that reuses ResultCard + BatchPipeline unchanged.

const STORAGE_KEY = 'openshorts_autopilot';

// Field name on RenderOptions differs from the op/modal type for subtitles.
const MODULE_KEYS = {
  subtitle: 'subtitles', hook: 'hook', translate: 'translate',
  auto_edit: 'auto_edit', branding: 'branding',
};

const PLATFORM_OPTIONS = [
  { value: 'tiktok', label: 'tiktok', icon: <Video size={16} /> },
  { value: 'instagram', label: 'instagram', icon: <Instagram size={16} /> },
  { value: 'youtube', label: 'youtube', icon: <Youtube size={16} /> },
];

// Map a modal's camelCase output to the snake_case RenderOptions sub-model shape.
// Mirrors BatchPipeline.toBatchConfig so recipe and manual runs are identical.
function toRecipeConfig(type, opts) {
  if (!opts) return {};
  if (type === 'subtitle') {
    return {
      position: opts.position,
      font_size: opts.fontSize,
      font_name: opts.fontName,
      font_color: opts.fontColor,
      border_color: opts.borderColor,
      border_width: opts.borderWidth,
      bg_color: opts.bgColor,
      bg_opacity: opts.bgOpacity,
      style: opts.style || 'classic',
      highlight_color: opts.highlightColor || '#FFD700',
      effect: opts.effect || 'none',
      base_opacity: opts.baseOpacity ?? 1.0,
      uppercase: opts.uppercase || false,
    };
  }
  if (type === 'hook') {
    const cfg = {
      position: opts.position,
      size: opts.size,
      style: opts.style || 'classic',
      duration_seconds: opts.remotion?.displayDurationSec ?? null,
    };
    if (opts.text && opts.text.trim()) cfg.text = opts.text.trim();
    return cfg;
  }
  if (type === 'translate') {
    return { target_language: opts.targetLanguage };
  }
  return {};
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* ignore */ }
  return null;
}
function saveState(state) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { /* ignore */ }
}
function clearState() {
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
}

const STAGE_META = {
  queued: { label: 'Queued', tone: 'text-muted' },
  processing: { label: 'Finding clips…', tone: 'text-brass' },
  clips_ready: { label: 'Clips ready', tone: 'text-brass' },
  editing: { label: 'Styling…', tone: 'text-brass' },
  done: { label: 'Done', tone: 'text-ok' },
  failed: { label: 'Failed', tone: 'text-danger' },
};

// Per-video override rows. Style modules can inherit / disable / customise.
// auto_edit is content-free so it only toggles. Branding customises too: the
// geometry always merges over the batch logo, and a video may additionally
// upload its OWN logo image (see BrandingOverrideModal).
const OVERRIDE_MODULES = [
  { key: 'subtitle', label: 'Subtitles', states: ['inherit', 'off', 'custom'] },
  { key: 'hook', label: 'Viral Hook', states: ['inherit', 'off', 'custom'] },
  { key: 'translate', label: 'Translate', states: ['inherit', 'off', 'custom'] },
  { key: 'auto_edit', label: 'Auto Edit', states: ['inherit', 'on', 'off'] },
  { key: 'branding', label: 'Branding', states: ['inherit', 'off', 'custom'] },
];
const STATE_LABEL = { inherit: 'Inherit', off: 'Off', on: 'On', custom: 'Custom…' };

// Framing + length. These are job-creation params (CLI flags to main.py), not
// RenderOptions, so they sit outside the recipe cascade and get their own
// batch -> video fallback. Mirrors MediaInput.jsx's single-video wording so the
// two surfaces teach the same thing.
const REFRAME_MODES = [
  { value: 'auto', label: 'Auto', hint: 'Detect per scene' },
  { value: 'track', label: 'Follow face', hint: 'Podcasts · talking head' },
  { value: 'general', label: 'Full frame', hint: 'Gaming · screen share' },
];
const CLIP_DURATION_MODES = [
  { value: 'auto', label: 'Auto', hint: 'Best viral length · 15–60s' },
  { value: 'short', label: 'Shortest', hint: 'As tight as possible · 11–30s' },
];
const MODE_LABEL = Object.fromEntries(
  [...REFRAME_MODES, ...CLIP_DURATION_MODES].map(o => [o.value, o.label]),
);

// The clip-selection prompt asks Gemini for "the 3–15 MOST VIRAL moments"
// (main.py:46), so this is the honest per-video yield range used to estimate
// paid per-clip calls before a run. Keep in sync if that prompt changes.
const CLIPS_PER_VIDEO_MIN = 3;
const CLIPS_PER_VIDEO_MAX = 15;

// "2h 05m" / "7m 30s" / "45s" — compact enough for a status bar.
function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
}

// Remaining-time estimate from observed throughput: average the elapsed time
// across finished videos and extend it to the unfinished ones. Deliberately
// returns null until at least one video has finished — an ETA extrapolated from
// zero completions is noise, and this feature's whole promise is "come back
// later", so a wrong number is worse than none.
function estimateRemaining(progress) {
  if (!progress?.created_at || progress.status !== 'running') return null;
  const finished = (progress.done || 0) + (progress.failed || 0);
  const total = progress.total || 0;
  if (finished === 0 || finished >= total) return null;
  const elapsed = Date.now() / 1000 - progress.created_at;
  if (elapsed <= 0) return null;
  return (elapsed / finished) * (total - finished);
}

// Card-style selector used for the batch-level defaults.
function ModeSelector({ options, value, onChange, cols }) {
  return (
    <div className={`grid gap-2 ${cols === 2 ? 'grid-cols-2' : 'grid-cols-3'}`}>
      {options.map((o) => {
        const active = value === o.value;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors
              ${active
                ? 'border-[color:var(--color-accent)] text-ink'
                : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'}`}
          >
            <span className="block font-mono text-sm leading-none">{o.label}</span>
            <span className="block text-[10px] leading-tight text-center text-muted">{o.hint}</span>
          </button>
        );
      })}
    </div>
  );
}

// Compact inherit-or-pick row used inside a per-video customize panel.
function InheritRow({ label, options, value, batchValue, onChange }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs text-ink2">{label}</span>
      <div className="flex gap-1 flex-wrap justify-end">
        <button
          onClick={() => onChange(null)}
          className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
            value == null
              ? 'bg-brass/15 border-brass text-brass'
              : 'bg-paper2 border-rule text-muted hover:text-ink'
          }`}
          title={`Inherit the batch setting (${MODE_LABEL[batchValue] || batchValue})`}
        >
          Inherit
        </button>
        {options.map((o) => (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
              value === o.value
                ? 'bg-brass/15 border-brass text-brass'
                : 'bg-paper2 border-rule text-muted hover:text-ink'
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

let _sid = 0;
const nextId = () => `s${++_sid}_${Math.round(performance.now())}`;

export default function AutopilotTab({ geminiApiKey, llmConfig, elevenLabsKey, uploadPostKey, uploadUserId, isManaged }) {
  const [phase, setPhase] = useState('setup'); // 'setup' | 'board'
  const [sources, setSources] = useState([]);
  const [urlInput, setUrlInput] = useState('');
  const [ack, setAck] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  // Batch recipe (batch-level defaults). Branding lives in the shared
  // openshorts_branding slot via <BrandingSettings/> and is read at build time.
  const [autoEditOn, setAutoEditOn] = useState(false);
  const [subtitleCfg, setSubtitleCfg] = useState(null);
  const [hookCfg, setHookCfg] = useState(null);
  const [translateCfg, setTranslateCfg] = useState(null);
  const [modal, setModal] = useState(null); // { module, sourceId | null }
  // Mirrors the shared openshorts_branding slot purely so the Branding chip can
  // show its on/off state. buildRecipe/recipeSteps still read the slot directly
  // at submit time — this is display state, not a second source of truth.
  const [brandingCfg, setBrandingCfg] = useState(() => loadBrandingDefaults());

  // Framing/length batch defaults. Not part of the recipe (see REFRAME_MODES) —
  // each source may override them, falling back to these.
  const [reframeMode, setReframeMode] = useState('auto');
  const [clipDurationMode, setClipDurationMode] = useState('auto');

  const [batchId, setBatchId] = useState(null);
  const [progress, setProgress] = useState(null);
  const fileInputRef = useRef(null);
  const pollRef = useRef(null);

  // ---- Auto-publish config -----------------------------------------------
  // Auto-post is optional and only shown when the publishing subsystem is
  // enabled (PUBLISHING_ENABLED). The toggle defaults to OFF so existing
  // batches are byte-for-byte unchanged.
  const [autoPostOn, setAutoPostOn] = useState(false);
  const [autoPostGroups, setAutoPostGroups] = useState([]); // selected group IDs
  const [autoPostPlatforms, setAutoPostPlatforms] = useState([]); // platform filter
  const [autoPostClipMode, setAutoPostClipMode] = useState('all'); // 'all' | 'first_n'
  const [autoPostMaxClips, setAutoPostMaxClips] = useState(5);
  const [autoPostSchedule, setAutoPostSchedule] = useState('immediate'); // 'immediate' | 'spread' | 'rhythm'

  // Rhythm preview: the next slots each selected group would actually book,
  // computed server-side by the same pure function the scheduler runs — so
  // what the operator sees here is what will happen.
  const [rhythmPreviews, setRhythmPreviews] = useState({}); // group id -> preview|null

  // Publishing availability: fetched on mount, determines whether the
  // auto-post section is shown at all.
  const [publishingAvailable, setPublishingAvailable] = useState(false);
  const [groups, setGroups] = useState([]); // available groups from API

  const stopPoll = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const startPoll = useCallback((bid) => {
    stopPoll();
    const tick = async () => {
      try {
        const data = await apiJson(`/api/autopilot/${bid}`);
        setProgress(data);
        if (data.status !== 'running') stopPoll();
      } catch (e) {
        // Registry lost (server restart) — drop the stale board rather than spin.
        if (String(e.message || '').startsWith('404')) {
          clearState(); stopPoll();
          setBatchId(null); setProgress(null); setPhase('setup');
        }
      }
    };
    tick();
    pollRef.current = setInterval(tick, 1500);
  }, [stopPoll]);

  // Rehydrate an in-flight batch on mount / reload.
  useEffect(() => {
    const saved = loadState();
    if (saved?.batchId) {
      setBatchId(saved.batchId);
      setPhase('board');
      startPoll(saved.batchId);
    }
    return stopPoll;
  }, [startPoll, stopPoll]);

  // Check publishing availability and fetch groups on mount.
  // Only shown when PUBLISHING_ENABLED is true server-side.
  useEffect(() => {
    // Rhythm schedule preview (fires when the rhythm option is selected).
    if (autoPostSchedule !== 'rhythm') { setRhythmPreviews({}); return; }
    let cancelled = false;
    const withPlan = groups.filter(g => autoPostGroups.includes(g.id) && g.plan);
    if (!withPlan.length) { setRhythmPreviews({}); return; }
    Promise.all(withPlan.map(g =>
      groupPlanPreview(g.id, 3).then(p => [g.id, p]).catch(() => [g.id, null])
    )).then((entries) => {
      if (!cancelled) setRhythmPreviews(Object.fromEntries(entries));
    });
    return () => { cancelled = true; };
  }, [autoPostSchedule, autoPostGroups, groups]);

  useEffect(() => {
    let cancelled = false;
    publishingHealth().then((health) => {
      if (!cancelled && health?.enabled) {
        setPublishingAvailable(true);
        // Try listGroups (admin endpoint) first — it gives us proper group names.
        // If the admin token isn't set, fall back to listDestinations (public)
        // and extract group_ids from the destination list.
        listGroups().then((grps) => {
          if (!cancelled && grps?.groups) {
            setGroups(grps.groups.map(g => ({
              id: g.id,
              name: g.name || g.id,
              // Posting rhythm + credential presence feed the agent page's
              // preflight and the rhythm schedule preview.
              plan: g.settings?.plan || null,
              // A rejected key is stored but unusable, so it must not count as
              // having one — otherwise preflight passes and every clip parks.
              hasCredential: !!g.credential && !g.credential.invalid,
              credentialRejected: !!g.credential?.invalid,
              enabled: g.enabled !== false,
            })));
          }
        }).catch((adminErr) => {
          if (cancelled) return;
          // Admin endpoint failed (likely no token). Fall back to public
          // destinations endpoint.
          console.warn('listGroups failed (admin token may be missing), falling back to listDestinations:', adminErr.message);
          listDestinations().then((dests) => {
            if (!cancelled && dests?.destinations) {
              const groupIds = [...new Set(dests.destinations.map(d => d.publish_group_id).filter(Boolean))];
              setGroups(groupIds.map(id => ({ id, name: id })));
            }
          }).catch((destErr) => {
            console.error('Failed to fetch publishing destinations:', destErr);
            // Keep publishingAvailable true so user sees the section,
            // but groups will be empty and they'll see the setup message.
          });
        });
      }
    }).catch(() => { /* publishing not available */ });
    return () => { cancelled = true; };
  }, []);

  // ---- source list -------------------------------------------------------
  const updateSource = (id, patch) =>
    setSources(prev => prev.map(s => (s.id === id ? { ...s, ...patch } : s)));

  const addUrl = () => {
    const u = urlInput.trim();
    if (!u) return;
    setSources(prev => [...prev, {
      id: nextId(), kind: 'url', url: u, label: u, status: 'ready', override: {},
    }]);
    setUrlInput('');
  };

  const addFiles = (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    setSources(prev => [...prev, ...files.map(f => ({
      id: nextId(), kind: 'file', file: f, label: f.name,
      status: 'pending', progress: 0, override: {},
    }))]);
  };

  const removeSource = (id) => setSources(prev => prev.filter(s => s.id !== id));

  // ---- per-video override ------------------------------------------------
  const setSourceOverride = (sourceId, moduleKey, value) => {
    setSources(prev => prev.map(s => {
      if (s.id !== sourceId) return s;
      const override = { ...(s.override || {}) };
      if (value === undefined) delete override[moduleKey];
      else override[moduleKey] = value;
      return { ...s, override };
    }));
  };

  const overrideStateOf = (source, moduleType) => {
    const v = source.override?.[MODULE_KEYS[moduleType]];
    if (v === undefined) return 'inherit';
    if (moduleType === 'branding') return v?.logo?.enabled === false ? 'off' : 'custom';
    if (v.enabled === false) return 'off';
    return moduleType === 'auto_edit' ? 'on' : 'custom';
  };

  const setOverrideState = (sourceId, moduleType, state) => {
    const key = MODULE_KEYS[moduleType];
    if (state === 'inherit') setSourceOverride(sourceId, key, undefined);
    else if (state === 'off') setSourceOverride(sourceId, key, moduleType === 'branding' ? { logo: { enabled: false } } : { enabled: false });
    else if (state === 'on') setSourceOverride(sourceId, key, { enabled: true });
    else if (state === 'custom') setModal({ module: moduleType, sourceId });
  };

  // An override can enable a module the batch recipe leaves off — branding on a
  // single video being the motivating case. The "at least one step" run guard has
  // to count those too, or such a batch is wrongly blocked.
  const hasVideoSteps = sources.some((s) =>
    OVERRIDE_MODULES.some((m) => ['on', 'custom'].includes(overrideStateOf(s, m.key))));

  // ---- recipe modals -----------------------------------------------------
  const applyModule = (opts) => {
    if (!modal) return;
    const { module, sourceId } = modal;
    if (module === 'branding') {
      // Branding is per-video only (the batch logo lives in the shared
      // BrandingSettings slot) and its shape is {logo:{...}}, not the flat
      // {enabled,...} the style modals emit — so it can't share the path below.
      if (sourceId) setSourceOverride(sourceId, 'branding', opts);
      setModal(null);
      return;
    }
    const cfg = toRecipeConfig(module, opts);
    if (!sourceId) {
      if (module === 'subtitle') setSubtitleCfg(cfg);
      else if (module === 'hook') setHookCfg(cfg);
      else if (module === 'translate') setTranslateCfg(cfg);
    } else {
      setSourceOverride(sourceId, MODULE_KEYS[module], { enabled: true, ...cfg });
    }
    setModal(null);
  };

  // The batch branding editor writes straight to the shared slot, so closing it
  // just means re-reading that slot to refresh the chip.
  const closeBrandingSettings = () => {
    setModal(null);
    setBrandingCfg(loadBrandingDefaults());
  };

  // Chip's × — switch branding off for the batch without discarding the logo,
  // so turning it back on doesn't mean re-uploading.
  const clearBranding = () => {
    const cur = loadBrandingDefaults() || {};
    const next = { ...cur, enabled: false };
    saveBrandingDefaults(next);
    setBrandingCfg(next);
  };

  const buildRecipe = () => {
    const recipe = {};
    if (autoEditOn) recipe.auto_edit = { enabled: true };
    if (subtitleCfg) recipe.subtitles = { enabled: true, ...subtitleCfg };
    if (hookCfg) recipe.hook = { enabled: true, ...hookCfg };
    if (translateCfg?.target_language) recipe.translate = { enabled: true, ...translateCfg };
    const b = loadBrandingDefaults();
    if (b?.enabled && b?.logoDataUrl) {
      recipe.branding = {
        logo: {
          enabled: true,
          position: b.position || 'bottom_right',
          size_pct: b.size_pct ?? 15,
          opacity: b.opacity ?? 1,
          margin_px: b.margin_px ?? 20,
          logo_image_data: b.logoDataUrl,
        },
      };
    }
    return recipe;
  };

  const recipeSteps = () => {
    const steps = [];
    if (autoEditOn) steps.push('auto edit');
    if (subtitleCfg) steps.push('subtitles');
    if (hookCfg) steps.push('hook');
    const b = loadBrandingDefaults();
    if (b?.enabled && b?.logoDataUrl) steps.push('branding');
    if (translateCfg?.target_language) steps.push(`translate → ${translateCfg.target_language}`);
    return steps;
  };

  // Build the publish plan from the auto-post UI state. Returns null when
  // auto-post is off or not enough config is set — the backend treats null
  // as "style only, no publishing", matching every existing batch.
  const buildPublishPlan = () => {
    if (!autoPostOn) return null;
    if (!autoPostGroups.length && !autoPostPlatforms.length) return null;
    const plan = {};
    if (autoPostGroups.length) plan.group_ids = autoPostGroups;
    if (autoPostPlatforms.length) plan.platforms = autoPostPlatforms;
    if (autoPostClipMode === 'first_n') plan.max_clips = autoPostMaxClips;
    if (autoPostSchedule === 'spread') plan.schedule = 'spread';
    // 'rhythm': each selected group places these clips on its own posting
    // plan (start time, interval, daily cap) — batch-wide, quota-aware, and
    // handed to the provider's own scheduler when it supports that.
    if (autoPostSchedule === 'rhythm') plan.schedule = 'rhythm';
    return plan;
  };

  // Preflight for the Review & Run card: the cheap config problems that turn
  // into 3 a.m. failures, surfaced at submit time instead.
  const publishPreflight = () => {
    if (!autoPostOn) return [];
    const out = [];
    for (const g of groups.filter(x => autoPostGroups.includes(x.id))) {
      if (!g.enabled) out.push(`Group “${g.name}” is disabled — its clips will not publish.`);
      if (g.credentialRejected)
        out.push(`Group “${g.name}” has an API key the provider rejected — re-check or replace it in the Publishing tab. Clips will queue but not post.`);
      else if (!g.hasCredential) out.push(`Group “${g.name}” has no API key — add one in the Publishing tab.`);
      if (autoPostSchedule === 'rhythm' && !g.plan)
        out.push(`Group “${g.name}” has no posting plan — its clips publish as soon as ready. Set a rhythm in the Publishing tab.`);
    }
    return out;
  };

  // ---- run ---------------------------------------------------------------
  const handleRun = async () => {
    setError(null);
    if (!sources.length) { setError('Add at least one video (URL or file).'); return; }
    if (!ack) { setError('Please confirm you own the content or have rights to process it.'); return; }
    const recipe = buildRecipe();
    if (!Object.keys(recipe).length && !hasVideoSteps) {
      setError('Enable at least one styling step (subtitles, hook, auto edit, branding or translate).');
      return;
    }
    setSubmitting(true);
    try {
      // Upload any pending files first (assemble-only, returns a server ref).
      const local = sources.map(s => ({ ...s }));
      for (const s of local) {
        if (s.kind === 'file' && !s.fileRef) {
          updateSource(s.id, { status: 'uploading', progress: 0 });
          try {
            const res = await uploadAutopilotSource(s.file, (p) => updateSource(s.id, { progress: p.percent }));
            s.fileRef = res.file_ref;
            updateSource(s.id, { fileRef: res.file_ref, status: 'ready', progress: 100 });
          } catch (e) {
            updateSource(s.id, { status: 'error', error: e.message });
            throw new Error(`Upload failed for ${s.label}: ${e.message}`);
          }
        }
      }

      const payloadSources = local.map(s => ({
        url: s.kind === 'url' ? s.url : undefined,
        file_ref: s.kind === 'file' ? s.fileRef : undefined,
        label: s.label,
        override: (s.override && Object.keys(s.override).length) ? s.override : undefined,
        // undefined => omitted from JSON => null server-side => inherit the batch value.
        reframe_mode: s.reframeMode ?? undefined,
        clip_duration_mode: s.clipDurationMode ?? undefined,
        publish: s.publish ?? undefined,
      }));

      const headers = { 'Content-Type': 'application/json' };
      // Clip selection must run on the SAME provider the single-video tab uses
      // (App.jsx handleProcess). Without these, resolve_llm_env falls through to
      // its gemini branch and every child job silently runs Gemini instead —
      // which is how a whole batch failed while single-video worked fine.
      if (llmConfig?.provider === 'openai_compat') {
        headers['X-LLM-Provider'] = 'openai_compat';
        headers['X-LLM-Base-URL'] = llmConfig.baseUrl || '';
        headers['X-LLM-Key'] = llmConfig.apiKey || '';
        headers['X-LLM-Model'] = llmConfig.model || '';
      }
      // Unlike the single-video path, keep sending the Gemini key even on
      // openai_compat: Auto Edit (editor.py) is Gemini-only and runs unattended
      // hours later from the key captured on the batch record, with no request
      // in scope to ask for it.
      if (geminiApiKey) headers['X-Gemini-Key'] = geminiApiKey;
      if (elevenLabsKey) headers['X-ElevenLabs-Key'] = elevenLabsKey;

      const publishPlan = buildPublishPlan();

      const res = await apiFetch('/api/autopilot', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          recipe,
          sources: payloadSources,
          output_format: 'auto',
          reframe_mode: reframeMode,
          clip_duration_mode: clipDurationMode,
          ack: true,
          publish: publishPlan,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setBatchId(data.batch_id);
      saveState({ batchId: data.batch_id });
      setProgress(null);
      setPhase('board');
      startPoll(data.batch_id);
    } catch (e) {
      setError(e.message || 'Failed to start autopilot');
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async () => {
    if (!batchId) return;
    try { await apiFetch(`/api/autopilot/${batchId}/cancel`, { method: 'POST' }); } catch { /* ignore */ }
    startPoll(batchId);
  };

  const handleNewBatch = () => {
    stopPoll();
    clearState();
    setBatchId(null);
    setProgress(null);
    setSources([]);
    setAck(false);
    setPhase('setup');
  };

  // ═══════════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════════
  const steps = recipeSteps();

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-4xl mx-auto space-y-8">

        {/* Header */}
        <div className="space-y-3">
          <p className="eyebrow flex items-center gap-2">
            <Bot size={12} /> 03 · AUTOPILOT · UNATTENDED CLIPPING
          </p>
          <h1 className="font-display lowercase text-3xl md:text-4xl text-ink">
            Drop many videos. Walk away. Come back to finished clips.
          </h1>
          <p className="text-muted text-base md:text-lg leading-relaxed max-w-2xl">
            Add every video at once, set your styling once, and Autopilot clips each one and
            auto-applies the recipe — subtitles, hooks, branding, translation — so you land
            straight in a review grid. Publishing stays in your hands.
          </p>
        </div>

        {phase === 'setup' ? (
          <SetupView
            sources={sources}
            urlInput={urlInput}
            setUrlInput={setUrlInput}
            addUrl={addUrl}
            addFiles={addFiles}
            removeSource={removeSource}
            fileInputRef={fileInputRef}
            autoEditOn={autoEditOn}
            setAutoEditOn={setAutoEditOn}
            subtitleCfg={subtitleCfg}
            hookCfg={hookCfg}
            translateCfg={translateCfg}
            setSubtitleCfg={setSubtitleCfg}
            setHookCfg={setHookCfg}
            setTranslateCfg={setTranslateCfg}
            brandingCfg={brandingCfg}
            clearBranding={clearBranding}
            openModal={(module) => setModal({ module, sourceId: null })}
            overrideStateOf={overrideStateOf}
            setOverrideState={setOverrideState}
            setSourceField={updateSource}
            reframeMode={reframeMode}
            setReframeMode={setReframeMode}
            clipDurationMode={clipDurationMode}
            setClipDurationMode={setClipDurationMode}
            steps={steps}
            hasVideoSteps={hasVideoSteps}
            ack={ack}
            setAck={setAck}
            submitting={submitting}
            error={error}
            handleRun={handleRun}
            autoPostOn={autoPostOn}
            setAutoPostOn={setAutoPostOn}
            autoPostGroups={autoPostGroups}
            setAutoPostGroups={setAutoPostGroups}
            autoPostPlatforms={autoPostPlatforms}
            setAutoPostPlatforms={setAutoPostPlatforms}
            autoPostClipMode={autoPostClipMode}
            setAutoPostClipMode={setAutoPostClipMode}
            autoPostMaxClips={autoPostMaxClips}
            setAutoPostMaxClips={setAutoPostMaxClips}
            autoPostSchedule={autoPostSchedule}
            setAutoPostSchedule={setAutoPostSchedule}
            publishingAvailable={publishingAvailable}
            groups={groups}
            rhythmPreviews={rhythmPreviews}
            publishPreflight={publishPreflight}
          />
        ) : (
          <BoardView
            progress={progress}
            onCancel={handleCancel}
            onNewBatch={handleNewBatch}
            geminiApiKey={geminiApiKey}
            elevenLabsKey={elevenLabsKey}
            uploadPostKey={uploadPostKey}
            uploadUserId={uploadUserId}
            isManaged={isManaged}
            publishingAvailable={publishingAvailable}
            groups={groups}
            batchId={batchId}
          />
        )}
      </div>

      {/* Shared recipe / override modals */}
      <SubtitleModal
        isOpen={modal?.module === 'subtitle'}
        onClose={() => setModal(null)}
        onGenerate={applyModule}
        isProcessing={false}
        videoUrl={undefined}
        jobId={null}
        clipIndex={0}
      />
      <HookModal
        isOpen={modal?.module === 'hook'}
        onClose={() => setModal(null)}
        onGenerate={applyModule}
        isProcessing={false}
        videoUrl={undefined}
        durationInSeconds={30}
        batchMode
      />
      <TranslateModal
        isOpen={modal?.module === 'translate'}
        onClose={() => setModal(null)}
        onTranslate={applyModule}
        isProcessing={false}
        videoUrl={undefined}
        hasApiKey={!!elevenLabsKey}
      />
      {/* Branding has two editors behind one module name, told apart by sourceId:
          no sourceId = the batch default (writes the shared openshorts_branding
          slot), a sourceId = that video's sparse {logo:{...}} override. */}
      <Modal
        isOpen={modal?.module === 'branding' && !modal?.sourceId}
        onClose={closeBrandingSettings}
        size="lg"
        eyebrow="AUTOPILOT · BATCH BRANDING"
        title="brand / logo"
        footer={
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-muted truncate">
              Saved as you edit — shared with the Clip Generator.
            </span>
            <button onClick={closeBrandingSettings} className="btn-primary text-sm px-4 py-1.5 shrink-0">
              Done
            </button>
          </div>
        }
      >
        <BrandingSettings embedded />
      </Modal>
      <BrandingOverrideModal
        isOpen={modal?.module === 'branding' && !!modal?.sourceId}
        onClose={() => setModal(null)}
        onApply={applyModule}
        initial={sources.find(s => s.id === modal?.sourceId)?.override?.branding}
        batchDefaults={loadBrandingDefaults()}
        videoLabel={sources.find(s => s.id === modal?.sourceId)?.label}
      />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Setup view
// ─────────────────────────────────────────────────────────────────────────
function SetupView(props) {
  const {
    sources, urlInput, setUrlInput, addUrl, addFiles, removeSource, fileInputRef,
    autoEditOn, setAutoEditOn, subtitleCfg, hookCfg, translateCfg,
    setSubtitleCfg, setHookCfg, setTranslateCfg, openModal,
    brandingCfg, clearBranding,
    overrideStateOf, setOverrideState, setSourceField,
    reframeMode, setReframeMode, clipDurationMode, setClipDurationMode,
    steps, ack, setAck, submitting, error, handleRun, hasVideoSteps,
    autoPostOn, setAutoPostOn, autoPostGroups, setAutoPostGroups,
    autoPostPlatforms, setAutoPostPlatforms, autoPostClipMode, setAutoPostClipMode,
    autoPostMaxClips, setAutoPostMaxClips, autoPostSchedule, setAutoPostSchedule,
    publishingAvailable, groups, rhythmPreviews, publishPreflight,
  } = props;

  const chips = [
    { type: 'subtitle', label: 'Subtitles', on: !!subtitleCfg, clear: () => setSubtitleCfg(null) },
    { type: 'hook', label: 'Viral Hook', on: !!hookCfg, clear: () => setHookCfg(null) },
    // Same gate buildRecipe uses — a logo that exists but is toggled off is "off".
    { type: 'branding', label: 'Branding', on: !!(brandingCfg?.enabled && brandingCfg?.logoDataUrl), clear: clearBranding },
    { type: 'translate', label: 'Translate', on: !!translateCfg, clear: () => setTranslateCfg(null) },
  ];

  // How many videos deviate from the batch framing defaults.
  const framingOverrideCount = sources.filter(
    (s) => s.reframeMode != null || s.clipDurationMode != null,
  ).length;

  // Mirrors handleRun's early-return guards so the button can never look live
  // while the click would silently no-op. Order matches the user's likely order
  // of operations (add videos -> pick styling -> tick the ack).
  const blockReason =
    sources.length === 0 ? 'Add at least one video to start.'
      : (steps.length === 0 && !hasVideoSteps) ? 'Enable at least one styling step above.'
        : !ack ? 'Confirm you have the rights to this content.'
          : null;

  // Concrete paid-call estimate. Only auto_edit (Gemini) and translate
  // (ElevenLabs) hit paid APIs — subtitle/hook/branding are local ffmpeg
  // (batch.py OPERATIONS). Clip count per video is Gemini's call at runtime, so
  // this is an honest range, not a fake precise number. Videos that override a
  // module off are excluded.
  const paidOps = [
    { key: 'auto_edit', label: 'Auto Edit (Gemini)', batchOn: autoEditOn },
    { key: 'translate', label: 'Translate (ElevenLabs)', batchOn: !!translateCfg?.target_language },
  ]
    .map(({ key, label, batchOn }) => {
      const videos = sources.filter((s) => {
        const st = overrideStateOf(s, key);
        if (st === 'off') return false;
        if (st === 'on' || st === 'custom') return true;
        return batchOn;                       // 'inherit' follows the batch recipe
      }).length;
      return {
        label,
        videos,
        min: videos * CLIPS_PER_VIDEO_MIN,
        max: videos * CLIPS_PER_VIDEO_MAX,
      };
    })
    .filter((op) => op.videos > 0);

  return (
    <div className="space-y-8">
      {/* Sources */}
      <div className="card p-5 sm:p-6 space-y-4">
        <div className="flex items-center gap-2">
          <span className="eyebrow">1 · VIDEOS</span>
        </div>

        {/* URL adder */}
        <div className="flex gap-2">
          <div className="flex-1 flex items-center gap-2 px-3 py-2 rounded-input bg-paper3 border border-rule">
            <Link2 size={14} className="text-muted shrink-0" />
            <input
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addUrl(); }}
              placeholder="Paste a YouTube URL and press Enter"
              className="flex-1 bg-transparent text-sm text-ink placeholder:text-muted focus:outline-none"
            />
          </div>
          <button onClick={addUrl} className="btn-quiet px-3 text-xs flex items-center gap-1.5">
            <Plus size={14} /> Add
          </button>
          <button
            onClick={() => fileInputRef.current?.click()}
            className="btn-quiet px-3 text-xs flex items-center gap-1.5"
          >
            <Upload size={14} /> Files
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            multiple
            onChange={(e) => { addFiles(e.target.files); if (fileInputRef.current) fileInputRef.current.value = ''; }}
            className="hidden"
          />
        </div>

        {/* Source list */}
        {sources.length === 0 ? (
          <p className="text-xs text-muted">No videos yet. Add URLs or upload files — mix freely.</p>
        ) : (
          <div className="space-y-2">
            {sources.map((s) => (
              <SourceRow
                key={s.id}
                source={s}
                onRemove={() => removeSource(s.id)}
                overrideStateOf={overrideStateOf}
                setOverrideState={setOverrideState}
                setSourceField={setSourceField}
                batchReframeMode={reframeMode}
                batchClipDurationMode={clipDurationMode}
                publishingAvailable={publishingAvailable}
                groups={groups}
              />
            ))}
          </div>
        )}
      </div>

      {/* Recipe */}
      <div className="card p-5 sm:p-6 space-y-4">
        <span className="eyebrow">2 · STYLING &amp; FRAMING</span>
        <p className="text-xs text-muted -mt-1">
          Applied to every clip of every video. Override any of it per video via
          <span className="text-ink2"> customize</span> in each row above.
        </p>

        <div className="flex flex-wrap gap-2">
          {/* Auto Edit — no config, just a toggle */}
          <button
            onClick={() => setAutoEditOn(v => !v)}
            className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
              autoEditOn ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
            }`}
          >
            {autoEditOn ? '✓ ' : ''}Auto Edit
          </button>

          {chips.map((c) => {
            const on = c.on;
            return (
              <div key={c.type} className="inline-flex items-center">
                <button
                  onClick={() => openModal(c.type)}
                  className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                    on ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
                  }`}
                >
                  {on ? '✓ ' : ''}{c.label}
                </button>
                {on && (
                  <>
                    <button onClick={() => openModal(c.type)} title="Edit" className="ml-1 p-0.5 text-muted hover:text-brass">
                      <Settings2 size={12} />
                    </button>
                    <button onClick={c.clear} title="Remove" className="p-0.5 text-muted hover:text-danger">
                      <X size={12} />
                    </button>
                  </>
                )}
              </div>
            );
          })}
        </div>

        {/* Framing & length — same decision ("how should clips come out"), same
            per-video override panel, so it lives in this card rather than its
            own numbered step. */}
        <div className="pt-4 border-t border-rule space-y-5">
          <div>
            <p className="eyebrow mb-2">Focus / layout</p>
            <ModeSelector
              options={REFRAME_MODES}
              value={reframeMode}
              onChange={setReframeMode}
              cols={3}
            />
            <p className="readout mt-2">
              Gaming or screen recordings? Pick <span className="text-ink2">Full frame</span> so the
              clip stays on the action instead of zooming onto a facecam.
            </p>
          </div>

          <div>
            <p className="eyebrow mb-2">Clip length</p>
            <ModeSelector
              options={CLIP_DURATION_MODES}
              value={clipDurationMode}
              onChange={setClipDurationMode}
              cols={2}
            />
            <p className="readout mt-2">
              <span className="text-ink2">Shortest</span> trims each clip to the tightest cut that
              still makes sense — never shorter than 11 seconds.
            </p>
          </div>
        </div>
      </div>

      {/* Auto-publish (optional — only shown when publishing subsystem is enabled) */}
      {publishingAvailable && (
        <div className="card p-5 sm:p-6 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <span className="eyebrow">3 · AUTO-PUBLISH</span>
            <button
              onClick={() => setAutoPostOn(v => !v)}
              className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                autoPostOn ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
              }`}
            >
              {autoPostOn ? '✓ ' : ''}{autoPostOn ? 'Enabled' : 'Disabled'}
            </button>
          </div>
          <p className="text-xs text-muted -mt-1">
            Automatically post clips to connected social accounts after styling.
            {autoPostOn ? '' : ' Toggle to configure.'}
          </p>

          {autoPostOn && (
            <div className="space-y-4 pt-3 border-t border-rule">
              {/* Group selector */}
              {groups.length > 0 ? (
                <div>
                  <p className="eyebrow mb-2">Post to groups</p>
                  <div className="flex flex-wrap gap-2">
                    {groups.map((g) => {
                      const selected = autoPostGroups.includes(g.id);
                      return (
                        <button
                          key={g.id}
                          onClick={() => {
                            setAutoPostGroups(prev =>
                              selected ? prev.filter(id => id !== g.id) : [...prev, g.id]
                            );
                          }}
                          className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                            selected ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
                          }`}
                        >
                          {selected ? '✓ ' : ''}{g.name || g.id.slice(0, 8)}
                        </button>
                      );
                    })}
                  </div>
                  <p className="text-[11px] text-muted mt-1">
                    Select one or more publishing groups (each contains connected accounts).
                  </p>
                </div>
              ) : (
                <p className="text-xs text-warn">
                  No publishing groups configured. Set up groups in the Publishing tab first.
                </p>
              )}

              {/* Platform filter */}
              <div>
                <p className="eyebrow mb-2">Platform filter (optional)</p>
                <div className="flex flex-wrap gap-2">
                  {['youtube', 'tiktok', 'instagram'].map((p) => {
                    const selected = autoPostPlatforms.includes(p);
                    return (
                      <button
                        key={p}
                        onClick={() => {
                          setAutoPostPlatforms(prev =>
                            selected ? prev.filter(x => x !== p) : [...prev, p]
                          );
                        }}
                        className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                          selected ? 'bg-brass/15 border-brass text-brass' : 'bg-paper3 border-rule text-muted hover:text-ink'
                        }`}
                      >
                        {selected ? '✓ ' : ''}{PLATFORM_LABELS[p] || p}
                      </button>
                    );
                  })}
                </div>
                <p className="text-[11px] text-muted mt-1">
                  Leave empty to post to all platforms in the selected groups.
                </p>
              </div>

              {/* Clip selection */}
              <div>
                <p className="eyebrow mb-2">Which clips to post</p>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    onClick={() => setAutoPostClipMode('all')}
                    className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors ${
                      autoPostClipMode === 'all'
                        ? 'border-[color:var(--color-accent)] text-ink'
                        : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'
                    }`}
                  >
                    <span className="block font-mono text-sm leading-none">All clips</span>
                    <span className="block text-[10px] leading-tight text-center text-muted">Post every clip</span>
                  </button>
                  <button
                    onClick={() => setAutoPostClipMode('first_n')}
                    className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors ${
                      autoPostClipMode === 'first_n'
                        ? 'border-[color:var(--color-accent)] text-ink'
                        : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'
                    }`}
                  >
                    <span className="block font-mono text-sm leading-none">First N clips</span>
                    <span className="block text-[10px] leading-tight text-center text-muted">Limit per video</span>
                  </button>
                </div>
                {autoPostClipMode === 'first_n' && (
                  <div className="mt-2 flex items-center gap-2">
                    <input
                      type="number"
                      min="1"
                      max="15"
                      value={autoPostMaxClips}
                      onChange={(e) => setAutoPostMaxClips(parseInt(e.target.value) || 5)}
                      className="w-16 px-2 py-1 rounded-input border border-rule bg-paper3 text-sm text-ink text-center"
                    />
                    <span className="text-xs text-muted">clips per video</span>
                  </div>
                )}
              </div>

              {/* Scheduling */}
              <div>
                <p className="eyebrow mb-2">Posting schedule</p>
                <div className="grid grid-cols-3 gap-2">
                  <button
                    onClick={() => setAutoPostSchedule('immediate')}
                    className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors ${
                      autoPostSchedule === 'immediate'
                        ? 'border-[color:var(--color-accent)] text-ink'
                        : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'
                    }`}
                  >
                    <span className="block font-mono text-sm leading-none flex items-center gap-1.5">
                      <Share2 size={12} /> Immediate
                    </span>
                    <span className="block text-[10px] leading-tight text-center text-muted">Post as soon as ready</span>
                  </button>
                  <button
                    onClick={() => setAutoPostSchedule('spread')}
                    className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors ${
                      autoPostSchedule === 'spread'
                        ? 'border-[color:var(--color-accent)] text-ink'
                        : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'
                    }`}
                  >
                    <span className="block font-mono text-sm leading-none flex items-center gap-1.5">
                      <Calendar size={12} /> Spread
                    </span>
                    <span className="block text-[10px] leading-tight text-center text-muted">Stagger posts</span>
                  </button>
                  <button
                    onClick={() => setAutoPostSchedule('rhythm')}
                    className={`py-3 px-2 rounded-input border flex flex-col items-center gap-1 transition-colors ${
                      autoPostSchedule === 'rhythm'
                        ? 'border-[color:var(--color-accent)] text-ink'
                        : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'
                    }`}
                  >
                    <span className="block font-mono text-sm leading-none flex items-center gap-1.5">
                      <Clock size={12} /> Rhythm
                    </span>
                    <span className="block text-[10px] leading-tight text-center text-muted">Each group's plan</span>
                  </button>
                </div>

                {autoPostSchedule === 'rhythm' && (
                  <div className="mt-2 space-y-2">
                    {groups.filter(g => autoPostGroups.includes(g.id)).map((g) => {
                      const preview = rhythmPreviews[g.id];
                      const slots = preview?.slots || [];
                      return (
                        <div key={g.id} className="p-2.5 rounded-input bg-paper3 border border-rule">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs text-ink font-medium">{g.name}</span>
                            {g.plan ? (
                              <span className="text-[11px] text-muted font-mono">
                                {g.plan.start_time || '06:00'} · every {g.plan.interval_hours || 6}h · max {g.plan.max_per_day || 3}/day
                              </span>
                            ) : (
                              <span className="text-[11px] text-warn">no plan set — posts when ready</span>
                            )}
                          </div>
                          {slots.length > 0 && (
                            <div className="mt-1.5 flex flex-wrap gap-1.5">
                              {slots.map((s) => (
                                <span key={s} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-paper2 border border-rule text-muted">
                                  {new Date(s).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })}
                                </span>
                              ))}
                            </div>
                          )}
                          {g.plan && preview === null && (
                            <p className="text-[10px] text-muted mt-1">preview unavailable (admin token not set)</p>
                          )}
                        </div>
                      );
                    })}
                    <p className="text-[11px] text-muted">
                      Clips queue on each group's rhythm across the whole run — quota-aware, no collisions,
                      and scheduled on the provider so this machine can be offline at posting time.
                    </p>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Run */}
      <div className="card p-5 sm:p-6 space-y-4">
        <span className="eyebrow">{publishingAvailable ? '4' : '3'} · REVIEW &amp; RUN</span>
        <div className="text-sm text-ink2">
          <p>
            <span className="text-ink font-medium">{sources.length}</span> video{sources.length === 1 ? '' : 's'}
            {steps.length > 0 ? (
              <> · styling: <span className="text-brass">{steps.join(', ')}</span></>
            ) : (
              <> · <span className="text-warn">no styling steps yet</span></>
            )}
          </p>
          <p className="text-xs text-muted mt-1">
            {MODE_LABEL[reframeMode]} framing · {MODE_LABEL[clipDurationMode]} length
            {framingOverrideCount > 0 && (
              <span className="text-brass"> · {framingOverrideCount} video
                {framingOverrideCount === 1 ? '' : 's'} customised</span>
            )}
          </p>
          {publishingAvailable && autoPostOn && publishPreflight().length > 0 && (
            <div className="mt-2 pt-2 border-t border-rule space-y-1">
              {publishPreflight().map((w) => (
                <p key={w} className="text-xs text-warn flex items-start gap-1.5">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" /> {w}
                </p>
              ))}
            </div>
          )}
          {paidOps.length > 0 ? (
            <div className="mt-2 pt-2 border-t border-rule space-y-1">
              {paidOps.map((op) => (
                <p key={op.label} className="text-xs text-muted">
                  <span className="text-warn">{op.label}</span> · ~{op.min}–{op.max} paid calls
                  <span className="text-muted"> across {op.videos} video{op.videos === 1 ? '' : 's'}</span>
                </p>
              ))}
              <p className="text-[11px] text-muted italic">
                Each video yields roughly {CLIPS_PER_VIDEO_MIN}–{CLIPS_PER_VIDEO_MAX} clips, and these
                run once per clip — the exact count is known only after clipping.
              </p>
            </div>
          ) : (
            <p className="text-xs text-muted mt-1">
              No paid APIs in this recipe — subtitles, hooks and branding all render locally.
            </p>
          )}
        </div>

        <label className="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-0.5 accent-brass" />
          <span className="text-xs text-muted leading-relaxed">
            I confirm I own this content or have the rights to process it.
          </span>
        </label>

        {error && <p className="text-xs text-danger">{error}</p>}

        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={handleRun}
            disabled={submitting || !!blockReason}
            title={blockReason || undefined}
            className="btn-primary py-2.5 px-5 text-sm flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {submitting ? 'Starting…' : 'Run Autopilot'}
          </button>
          {/* Say WHY the button is dead, next to the button — not in an error
              line above the fold that a click appears to ignore. */}
          {blockReason && !submitting && (
            <span className="text-xs text-warn">{blockReason}</span>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceRow({
  source, onRemove, overrideStateOf, setOverrideState,
  setSourceField, batchReframeMode, batchClipDurationMode,
  publishingAvailable, groups,
}) {
  const [expanded, setExpanded] = useState(false);
  // Framing lives outside the recipe override object, so count it separately.
  const framingCount =
    (source.reframeMode != null ? 1 : 0) + (source.clipDurationMode != null ? 1 : 0);
  const publishCount = source.publish?.group_ids?.length ? 1 : 0;
  const customCount = Object.keys(source.override || {}).length + framingCount + publishCount;

  return (
    <div className="rounded-input border border-rule bg-paper3">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${source.kind === 'url' ? 'bg-brass/10 text-brass' : 'bg-paper2 text-muted'}`}>
          {source.kind}
        </span>
        <span className="flex-1 text-sm text-ink truncate">{source.label}</span>

        {source.status === 'uploading' && (
          <span className="text-[11px] text-brass shrink-0">{source.progress ?? 0}%</span>
        )}
        {source.status === 'error' && (
          <span className="text-[11px] text-danger shrink-0 flex items-center gap-1"><AlertTriangle size={11} /> failed</span>
        )}

        <button
          onClick={() => setExpanded(v => !v)}
          className={`text-[11px] px-1.5 py-0.5 rounded flex items-center gap-1 shrink-0 border transition-colors ${
            customCount > 0
              ? 'border-brass/40 bg-brass/10 text-brass'
              : 'border-rule text-ink2 hover:text-ink hover:border-rule2'
          }`}
        >
          {customCount > 0 ? `${customCount} custom` : 'customize'}
          <ChevronDown size={12} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
        </button>
        <button onClick={onRemove} title="Remove" className="p-0.5 text-muted hover:text-danger shrink-0">
          <X size={14} />
        </button>
      </div>

      {expanded && (
        <div className="px-3 pb-3 pt-1 border-t border-rule space-y-2">
          <p className="text-[11px] text-muted">
            Everything inherits the batch settings unless you change it here.
          </p>
          {OVERRIDE_MODULES.map((m) => {
            const cur = overrideStateOf(source, m.key);
            return (
              <div key={m.key} className="flex items-center justify-between gap-2">
                <span className="text-xs text-ink2">{m.label}</span>
                <div className="flex gap-1">
                  {m.states.map((st) => (
                    <button
                      key={st}
                      onClick={() => setOverrideState(source.id, m.key, st)}
                      className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
                        cur === st
                          ? 'bg-brass/15 border-brass text-brass'
                          : 'bg-paper2 border-rule text-muted hover:text-ink'
                      }`}
                    >
                      {STATE_LABEL[st]}
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
          {/* Framing/length — separate channel from the recipe override object */}
          <div className="pt-2 mt-1 border-t border-rule space-y-2">
            <InheritRow
              label="Focus / layout"
              options={REFRAME_MODES}
              value={source.reframeMode ?? null}
              batchValue={batchReframeMode}
              onChange={(v) => setSourceField(source.id, { reframeMode: v })}
            />
            <InheritRow
              label="Clip length"
              options={CLIP_DURATION_MODES}
              value={source.clipDurationMode ?? null}
              batchValue={batchClipDurationMode}
              onChange={(v) => setSourceField(source.id, { clipDurationMode: v })}
            />
          </div>

          {/* Per-video publish override — only shown when publishing is available */}
          {publishingAvailable && groups?.length > 0 && (
            <div className="pt-2 mt-1 border-t border-rule space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-ink2">Publish to groups</span>
                <span className="text-[10px] text-muted italic">overrides batch setting</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {groups.map(g => {
                  const selected = source.publish?.group_ids?.includes(g.id);
                  return (
                    <button
                      key={g.id}
                      onClick={() => {
                        const current = source.publish?.group_ids || [];
                        const next = selected
                          ? current.filter(id => id !== g.id)
                          : [...current, g.id];
                        setSourceField(source.id, {
                          publish: next.length ? { group_ids: next } : null
                        });
                      }}
                      className={`px-2 py-0.5 text-[11px] rounded-full border transition-colors ${
                        selected
                          ? 'bg-brass/15 border-brass text-brass'
                          : 'border-rule text-muted hover:text-ink'
                      }`}
                    >
                      {g.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {customCount === 0 && (
            <p className="text-[11px] text-muted italic pt-1">
              Fully inherited from the batch settings.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Board view (live stages + review)
// ─────────────────────────────────────────────────────────────────────────
function BoardView({ progress, onCancel, onNewBatch, geminiApiKey, elevenLabsKey, uploadPostKey, uploadUserId, isManaged, publishingAvailable, groups, batchId }) {
  if (!progress) {
    return (
      <div className="card p-10 flex flex-col items-center justify-center text-muted gap-3">
        <Loader2 size={28} className="animate-spin text-brass" />
        <p className="text-sm lowercase">Loading board…</p>
      </div>
    );
  }

  const running = progress.status === 'running';
  const videos = progress.videos || [];
  // Freeze the clock at finished_at once terminal, so re-opening a finished
  // batch tomorrow still reports how long it actually took.
  const endStamp = progress.finished_at ?? (running ? Date.now() / 1000 : null);
  const elapsed = progress.created_at && endStamp ? endStamp - progress.created_at : 0;
  const remaining = estimateRemaining(progress);

  return (
    <div className="space-y-5">
      {/* Summary bar */}
      <div className="card p-4 flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-ink2">
          <div>
            <span className="text-ink font-medium">{progress.done}</span>/{progress.total} done
            {progress.failed > 0 && <span className="text-danger ml-2">· {progress.failed} failed</span>}
            {progress.status === 'cancelled' && <span className="text-muted ml-2">· cancelled</span>}
            {progress.status === 'completed' && <span className="text-ok ml-2">· complete</span>}
          </div>
          {/* "Walk away and come back" needs an answer to *when*. */}
          {progress.created_at && (
            <div className="text-[11px] text-muted mt-0.5">
              {running ? 'Running for' : 'Took'} {formatDuration(elapsed)}
              {remaining != null && (
                <span> · ~{formatDuration(remaining)} left</span>
              )}
              {running && remaining == null && (
                <span> · estimate after the first video finishes</span>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          {running ? (
            <button onClick={onCancel} className="flex items-center gap-1.5 px-3 py-1.5 rounded-input border border-danger/40 text-xs text-danger hover:bg-danger/10">
              <Square size={12} /> Cancel batch
            </button>
          ) : (
            <button onClick={onNewBatch} className="btn-quiet px-3 py-1.5 text-xs flex items-center gap-1.5">
              <Plus size={12} /> New batch
            </button>
          )}
        </div>
      </div>

      {/* Per-video cards */}
      <div className="space-y-3">
        {videos.map((v) => (
          <VideoBoardCard
            key={v.job_id}
            video={v}
            geminiApiKey={geminiApiKey}
            elevenLabsKey={elevenLabsKey}
            uploadPostKey={uploadPostKey}
            uploadUserId={uploadUserId}
            isManaged={isManaged}
            publishingAvailable={publishingAvailable}
            groups={groups}
            batchId={batchId}
          />
        ))}
      </div>
    </div>
  );
}

function VideoBoardCard({ video, geminiApiKey, elevenLabsKey, uploadPostKey, uploadUserId, isManaged, publishingAvailable, groups, batchId }) {
  const [open, setOpen] = useState(false);
  const [showPublishUI, setShowPublishUI] = useState(false);
  const [publishGroups, setPublishGroups] = useState([]);
  const [publishPlatforms, setPublishPlatforms] = useState([]);
  const [publishMaxClips, setPublishMaxClips] = useState(5);
  // 'immediate' posts now; 'rhythm' queues on each selected group's plan.
  const [publishSchedule, setPublishSchedule] = useState('immediate');
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] = useState(null);

  const meta = STAGE_META[video.stage] || STAGE_META.queued;
  const batch = video.batch;
  const isDone = video.stage === 'done';
  const batchPct = batch && batch.total > 0
    ? Math.round(((batch.completed + (batch.failed?.length || 0)) / batch.total) * 100)
    : 0;

  // Manual auto-publish: trigger publishing for a completed job
  const handleManualPublish = async () => {
    if (!batchId) return;
    setPublishing(true);
    setPublishResult(null);
    try {
      const payload = {
        job_id: video.job_id,
        group_ids: publishGroups,
        platforms: publishPlatforms,
        max_clips: publishMaxClips,
        schedule: publishSchedule,
      };
      const res = await apiFetch(`/api/autopilot/${batchId}/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Publishing failed');
      }
      const data = await res.json();
      setPublishResult({ success: true, message: data.message });
      setShowPublishUI(false);
    } catch (e) {
      setPublishResult({ success: false, message: e.message });
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-3">
        <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${video.kind === 'url' ? 'bg-brass/10 text-brass' : 'bg-paper2 text-muted'}`}>
          {video.kind}
        </span>
        <span className="flex-1 text-sm text-ink truncate">{video.label}</span>
        <span className={`text-xs flex items-center gap-1.5 shrink-0 ${meta.tone}`}>
          {['processing', 'editing'].includes(video.stage) && <Loader2 size={12} className="animate-spin" />}
          {video.stage === 'done' && <Check size={12} />}
          {video.stage === 'failed' && <AlertTriangle size={12} />}
          {meta.label}
        </span>
      </div>

      {/* Styling progress (per-clip batch) */}
      {video.stage === 'editing' && batch && (
        <div className="space-y-1">
          <div className="h-1.5 rounded-full bg-paper3 overflow-hidden">
            <div className="h-full rounded-full bg-brass transition-all duration-500" style={{ width: `${batchPct}%` }} />
          </div>
          <div className="flex justify-between text-[11px] text-muted">
            <span>{batch.completed}/{batch.total} clips styled</span>
            <span>{batch.current_step || ''}</span>
          </div>
        </div>
      )}

      {video.publishing_status && (
        <div className="flex items-center gap-1.5 text-[11px] text-ok">
          <Check size={12} />
          <span>{video.publishing_message || 'Published'}</span>
        </div>
      )}

      {/* Manual auto-publish UI for completed videos */}
      {isDone && publishingAvailable && groups?.length > 0 && !video.publishing_status && (
        <div className="space-y-2">
          {!showPublishUI ? (
            <button
              onClick={() => setShowPublishUI(true)}
              className="text-xs text-brass hover:text-brass/80 flex items-center gap-1"
            >
              <Share2 size={12} />
              Auto-post clips
            </button>
          ) : (
            <div className="space-y-3 p-3 bg-paper3 rounded-input border border-rule">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium">Auto-post configuration</span>
                <button
                  onClick={() => setShowPublishUI(false)}
                  className="text-muted hover:text-danger"
                >
                  <X size={14} />
                </button>
              </div>

              {/* Group selection */}
              <div>
                <label className="text-[11px] text-muted block mb-1">Groups</label>
                <div className="flex flex-wrap gap-1">
                  {groups.map(g => (
                    <button
                      key={g.id}
                      onClick={() => {
                        setPublishGroups(prev =>
                          prev.includes(g.id) ? prev.filter(id => id !== g.id) : [...prev, g.id]
                        );
                      }}
                      className={`px-2 py-0.5 text-[11px] rounded-full border transition-colors ${
                        publishGroups.includes(g.id)
                          ? 'bg-brass/15 border-brass text-brass'
                          : 'border-rule text-muted hover:text-ink'
                      }`}
                    >
                      {g.name}
                    </button>
                  ))}
                </div>
              </div>

              {/* Platform selection */}
              <div>
                <label className="text-[11px] text-muted block mb-1">Platforms</label>
                <div className="flex flex-wrap gap-1">
                  {PLATFORM_OPTIONS.map(p => (
                    <button
                      key={p.value}
                      onClick={() => {
                        setPublishPlatforms(prev =>
                          prev.includes(p.value) ? prev.filter(x => x !== p.value) : [...prev, p.value]
                        );
                      }}
                      className={`px-2 py-0.5 text-[11px] rounded-full border transition-colors flex items-center gap-1 ${
                        publishPlatforms.includes(p.value)
                          ? 'bg-brass/15 border-brass text-brass'
                          : 'border-rule text-muted hover:text-ink'
                      }`}
                    >
                      {p.icon}
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Max clips */}
              <div>
                <label className="text-[11px] text-muted block mb-1">Max clips per video</label>
                <input
                  type="number"
                  min="1"
                  max="15"
                  value={publishMaxClips}
                  onChange={(e) => setPublishMaxClips(parseInt(e.target.value) || 5)}
                  className="w-20 px-2 py-1 text-xs border border-rule rounded-input"
                />
              </div>

              {/* Schedule */}
              <div>
                <label className="text-[11px] text-muted block mb-1">Schedule</label>
                <div className="flex gap-1">
                  {[
                    { value: 'immediate', label: 'Post now' },
                    { value: 'rhythm', label: "Groups' rhythm" },
                  ].map((opt) => (
                    <button
                      key={opt.value}
                      onClick={() => setPublishSchedule(opt.value)}
                      className={`px-2 py-0.5 text-[11px] rounded-full border transition-colors ${
                        publishSchedule === opt.value
                          ? 'bg-brass/15 border-brass text-brass'
                          : 'border-rule text-muted hover:text-ink'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                {publishSchedule === 'rhythm' && (
                  <p className="text-[10px] text-muted mt-1">
                    Clips queue on each group's posting plan (set in the Publishing tab).
                  </p>
                )}
              </div>

              {/* Publish button */}
              <button
                onClick={handleManualPublish}
                disabled={publishing || !publishGroups.length || !publishPlatforms.length}
                className="btn-primary w-full text-xs flex items-center justify-center gap-1.5 disabled:opacity-50"
              >
                {publishing ? <Loader2 size={12} className="animate-spin" /> : <Share2 size={12} />}
                {publishing ? 'Publishing...' : 'Auto-post now'}
              </button>

              {/* Result message */}
              {publishResult && (
                <div className={`text-[11px] ${publishResult.success ? 'text-ok' : 'text-danger'}`}>
                  {publishResult.message}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {video.clip_count > 0 && (
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-muted">{video.clip_count} clip{video.clip_count === 1 ? '' : 's'}</span>
          {isDone && (
            <button onClick={() => setOpen(v => !v)} className="text-xs text-brass hover:text-brass/80 flex items-center gap-1">
              {open ? 'Hide' : 'Review'} clips
              <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
            </button>
          )}
        </div>
      )}

      {open && isDone && (
        <VideoReviewPanel
          jobId={video.job_id}
          geminiApiKey={geminiApiKey}
          elevenLabsKey={elevenLabsKey}
          uploadPostKey={uploadPostKey}
          uploadUserId={uploadUserId}
          isManaged={isManaged}
        />
      )}
    </div>
  );
}

function VideoReviewPanel({ jobId, geminiApiKey, elevenLabsKey, uploadPostKey, uploadUserId, isManaged }) {
  const [clips, setClips] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await apiJson(`/api/status/${jobId}`);
      setClips((data.result?.clips) || []);
    } catch (e) {
      setError(e.message || 'Could not load clips');
    }
  }, [jobId]);

  useEffect(() => { load(); }, [load]);

  const downloadAll = async () => {
    try {
      const res = await apiFetch(`/api/jobs/${jobId}/download-all`);
      if (!res.ok) return;
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `clips-${jobId}.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch { /* ignore */ }
  };

  if (error) return <p className="text-xs text-danger pt-2 border-t border-rule">{error}</p>;
  if (clips === null) {
    return (
      <div className="flex items-center gap-2 text-muted text-xs pt-2 border-t border-rule">
        <Loader2 size={12} className="animate-spin" /> loading clips…
      </div>
    );
  }
  if (!clips.length) return <p className="text-xs text-muted pt-2 border-t border-rule">No clips found.</p>;

  return (
    <div className="pt-3 border-t border-rule space-y-3">
      {/* Subset re-edit + download */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-muted">Re-apply styling to any subset below, or download all.</span>
        <button onClick={downloadAll} className="btn-quiet px-3 py-1.5 text-xs flex items-center gap-1.5">
          <Download size={12} /> Download all
        </button>
      </div>

      <BatchPipeline
        jobId={jobId}
        clipCount={clips.length}
        apiKey={geminiApiKey}
        elevenLabsKey={elevenLabsKey}
        previewVideoUrl={clips[0]?.video_url}
        onComplete={load}
      />

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {clips.map((clip, i) => (
          <ResultCard
            key={`${jobId}-${i}`}
            clip={clip}
            index={i}
            jobId={jobId}
            geminiApiKey={geminiApiKey}
            elevenLabsKey={elevenLabsKey}
            uploadPostKey={uploadPostKey}
            uploadUserId={uploadUserId}
            isManaged={isManaged}
            clipCount={clips.length}
          />
        ))}
      </div>
    </div>
  );
}
