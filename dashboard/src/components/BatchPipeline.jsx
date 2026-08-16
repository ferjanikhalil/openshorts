import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Play, Square, RotateCcw, Loader2, AlertTriangle, Settings2, X } from 'lucide-react';
import { apiFetch, apiJson } from '../lib/api';
import { getApiUrl } from '../config';
import HookModal from './HookModal';
import SubtitleModal from './SubtitleModal';
import TranslateModal from './TranslateModal';
import BrandingEditor from './BrandingEditor';

const OPERATIONS = [
  { type: 'auto_edit', label: 'Auto Edit', configurable: false },
  { type: 'subtitle', label: 'Subtitles', configurable: true },
  { type: 'hook', label: 'Viral Hook', configurable: true },
  { type: 'branding', label: 'Branding', configurable: true },
  { type: 'translate', label: 'Translate', configurable: true },
];

// Map a modal's (mostly camelCase) output to the snake_case config the batch
// backend processors expect. Mirrors the single-clip handlers in ResultCard so
// batch and per-clip runs produce identical results.
function toBatchConfig(type, opts) {
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
    // Only pin a text when the user typed one. Blank → each clip keeps its own
    // AI-generated hook (backend falls back to per-clip viral_hook_text).
    if (opts.text && opts.text.trim()) cfg.text = opts.text.trim();
    return cfg;
  }
  if (type === 'translate') {
    return { target_language: opts.targetLanguage };
  }
  return {};
}

export default function BatchPipeline({ jobId, clipCount, apiKey, elevenLabsKey, previewVideoUrl, onComplete }) {
  // Raw clip URL → absolute API URL for modal previews (optional; modals
  // degrade gracefully when it's missing).
  const resolvedPreviewUrl = previewVideoUrl ? getApiUrl(previewVideoUrl) : undefined;
  const [selected, setSelected] = useState({});
  const [configs, setConfigs] = useState({});
  const [modal, setModal] = useState(null); // 'hook' | 'subtitle' | 'translate' | 'branding' | null
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  const isRunning = progress?.status === 'running';

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const data = await apiJson(`/api/jobs/${jobId}/batch`);
        setProgress(data);
        if (data.status !== 'running') {
          stopPolling();
          if (onComplete) onComplete();
        }
      } catch {
        stopPolling();
      }
    }, 1500);
  }, [jobId, stopPolling, onComplete]);

  useEffect(() => stopPolling, [stopPolling]);

  // Check for an already-running batch on mount
  useEffect(() => {
    if (!jobId) return;
    apiJson(`/api/jobs/${jobId}/batch`).then((data) => {
      if (data.status === 'running') {
        setProgress(data);
        startPolling();
      } else if (data.status === 'completed' || data.status === 'cancelled') {
        setProgress(data);
      }
    }).catch(() => {});
  }, [jobId, startPolling]);

  // Toggle a chip. Configurable ops open their editor first and only become
  // selected once the user applies settings; deselect is immediate.
  const toggle = (op) => {
    if (isRunning) return;
    if (selected[op.type]) {
      setSelected((prev) => ({ ...prev, [op.type]: false }));
      return;
    }
    if (op.configurable) {
      setModal(op.type);
      return;
    }
    setSelected((prev) => ({ ...prev, [op.type]: true }));
  };

  const captureConfig = (type, opts) => {
    setConfigs((prev) => ({ ...prev, [type]: toBatchConfig(type, opts) }));
    setSelected((prev) => ({ ...prev, [type]: true }));
    setModal(null);
  };

  // Branding persists its settings server-side (render-options); selecting it
  // just tells the batch to apply whatever is saved. Let BrandingEditor manage
  // its own open/close (it closes on "apply to all").
  const selectBranding = () => {
    setSelected((prev) => ({ ...prev, branding: true }));
  };

  const buildOperations = (ops) => ops.map((op) => {
    const config = { ...(configs[op.type] || {}) };
    if (op.type === 'auto_edit' && apiKey) config.api_key = apiKey;
    if (op.type === 'translate' && elevenLabsKey) config.api_key = elevenLabsKey;
    return { type: op.type, config };
  });

  const submitBatch = async (operations, clipIndices, expectedTotal) => {
    const headers = { 'Content-Type': 'application/json' };
    if (elevenLabsKey) headers['X-ElevenLabs-Key'] = elevenLabsKey;

    const body = { operations };
    if (clipIndices) body.clip_indices = clipIndices;

    const res = await apiFetch(`/api/jobs/${jobId}/batch`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      setError(errBody.detail || `Batch failed (${res.status})`);
      return false;
    }

    setProgress({ status: 'running', total: expectedTotal, completed: 0, failed: [], current_clip: 0, current_step: '' });
    startPolling();
    return true;
  };

  const handleRun = async () => {
    const ops = OPERATIONS.filter((op) => selected[op.type]);
    if (!ops.length) return;
    setError(null);
    try {
      await submitBatch(buildOperations(ops), null, clipCount);
    } catch (e) {
      setError(e.message || 'Failed to start batch');
    }
  };

  const handleCancel = async () => {
    try {
      await apiFetch(`/api/jobs/${jobId}/batch/cancel`, { method: 'POST' });
    } catch { /* ignore */ }
  };

  const handleRetryFailed = async () => {
    if (!progress?.failed?.length) return;
    setError(null);
    const failedIndices = progress.failed.map((f) => f.clip_index);
    const ops = OPERATIONS.filter((op) => selected[op.type]);
    if (!ops.length) return;
    try {
      await submitBatch(buildOperations(ops), failedIndices, failedIndices.length);
    } catch (e) {
      setError(e.message || 'Failed to retry');
    }
  };

  const selectedCount = OPERATIONS.filter((op) => selected[op.type]).length;
  const pct = progress && progress.total > 0
    ? Math.round(((progress.completed + (progress.failed?.length || 0)) / progress.total) * 100)
    : 0;

  return (
    <div className="rounded-input border border-rule bg-paper2 px-4 py-3 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <span className="eyebrow">BATCH PIPELINE</span>
        {isRunning && (
          <span className="flex items-center gap-1.5 text-xs text-brass">
            <Loader2 size={12} className="animate-spin" />
            {progress.current_step || 'processing'}…
          </span>
        )}
      </div>

      {/* Operation toggles */}
      <div className="flex flex-wrap gap-2">
        {OPERATIONS.map((op) => {
          const on = selected[op.type];
          return (
            <div key={op.type} className="inline-flex items-center">
              <button
                onClick={() => toggle(op)}
                disabled={isRunning}
                className={`px-2.5 py-1 rounded-full text-xs border transition-colors ${
                  on
                    ? 'bg-brass/15 border-brass text-brass'
                    : 'bg-paper3 border-rule text-muted hover:text-ink hover:border-rule2'
                } ${isRunning ? 'opacity-50 cursor-not-allowed' : ''}`}
              >
                {on ? '✓ ' : ''}{op.label}
              </button>
              {/* Edit settings / clear, shown for configured chips */}
              {on && op.configurable && !isRunning && (
                <button
                  onClick={() => setModal(op.type)}
                  title="Edit settings"
                  className="ml-1 p-0.5 text-muted hover:text-brass transition-colors"
                >
                  <Settings2 size={12} />
                </button>
              )}
              {on && !isRunning && (
                <button
                  onClick={() => setSelected((prev) => ({ ...prev, [op.type]: false }))}
                  title="Remove"
                  className="p-0.5 text-muted hover:text-danger transition-colors"
                >
                  <X size={12} />
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* Progress bar */}
      {progress && progress.status !== 'idle' && (
        <div className="space-y-1.5">
          <div className="h-1.5 rounded-full bg-paper3 overflow-hidden">
            <div
              className="h-full rounded-full bg-brass transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[11px] text-muted">
            <span>
              {progress.completed}/{progress.total} clips
              {progress.failed?.length > 0 && (
                <span className="text-danger ml-1.5">· {progress.failed.length} failed</span>
              )}
              {progress.status === 'cancelled' && <span className="ml-1.5">· cancelled</span>}
            </span>
            <span>
              {isRunning && `clip ${progress.current_clip + 1}/${progress.total}`}
            </span>
          </div>

          {/* Failed clips detail */}
          {!isRunning && progress.failed?.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {progress.failed.map((f, i) => (
                <span key={i} className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-danger/10 text-danger">
                  <AlertTriangle size={10} />
                  clip {f.clip_index + 1}: {f.step}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <p className="text-xs text-danger">{error}</p>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2">
        {!isRunning ? (
          <>
            <button
              onClick={handleRun}
              disabled={selectedCount === 0}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-input bg-brass text-black text-xs font-medium hover:bg-brass/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Play size={12} />
              Run Batch{selectedCount > 0 ? ` (${selectedCount})` : ''}
            </button>
            {!isRunning && progress?.failed?.length > 0 && (
              <button
                onClick={handleRetryFailed}
                disabled={selectedCount === 0}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-input border border-rule text-xs text-ink2 hover:bg-paper3 transition-colors disabled:opacity-40"
              >
                <RotateCcw size={12} />
                Retry Failed ({progress.failed.length})
              </button>
            )}
          </>
        ) : (
          <button
            onClick={handleCancel}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-input border border-danger/40 text-xs text-danger hover:bg-danger/10 transition-colors"
          >
            <Square size={12} />
            Cancel
          </button>
        )}
      </div>

      {/* Settings modals — capture config for every clip in the batch */}
      <HookModal
        isOpen={modal === 'hook'}
        onClose={() => setModal(null)}
        onGenerate={(opts) => captureConfig('hook', opts)}
        isProcessing={false}
        videoUrl={resolvedPreviewUrl}
        durationInSeconds={30}
        batchMode
      />

      <SubtitleModal
        isOpen={modal === 'subtitle'}
        onClose={() => setModal(null)}
        onGenerate={(opts) => captureConfig('subtitle', opts)}
        isProcessing={false}
        videoUrl={resolvedPreviewUrl}
        jobId={jobId}
        clipIndex={0}
      />

      <TranslateModal
        isOpen={modal === 'translate'}
        onClose={() => setModal(null)}
        onTranslate={(opts) => captureConfig('translate', opts)}
        isProcessing={false}
        videoUrl={resolvedPreviewUrl}
        hasApiKey={!!elevenLabsKey}
      />

      <BrandingEditor
        isOpen={modal === 'branding'}
        onClose={() => setModal(null)}
        jobId={jobId}
        clipIndex={0}
        onApplied={selectBranding}
      />
    </div>
  );
}
