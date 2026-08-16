import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Loader2, RotateCcw, Upload } from 'lucide-react';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';
import { apiFetch } from '../lib/api';
import { getApiUrl } from '../config';

const POSITION_OPTIONS = [
    { value: 'top_left', label: 'top left' },
    { value: 'top_right', label: 'top right' },
    { value: 'bottom_left', label: 'bottom left' },
    { value: 'bottom_right', label: 'bottom right' },
];

export default function BrandingEditor({ isOpen, onClose, jobId, clipIndex, onApplied }) {
    const [config, setConfig] = useState(null);
    const [overrides, setOverrides] = useState({});
    const [loading, setLoading] = useState(true);
    const [applying, setApplying] = useState(null); // 'clip' | 'all' | null
    const [uploading, setUploading] = useState(false);
    const [logoVersion, setLogoVersion] = useState(0); // cache-buster for re-uploads
    const [error, setError] = useState(null);
    const fileInputRef = useRef(null);

    const clipOverride = overrides[String(clipIndex)] || null;
    const hasOverride = !!clipOverride;

    // Effective config = default merged with clip override
    const effective = config ? {
        ...config.logo,
        ...(clipOverride?.logo || {}),
    } : null;

    const logoUrl = config?.logo?.image_path
        ? getApiUrl(`/videos/${jobId}/${config.logo.image_path}?v=${logoVersion}`)
        : null;

    const refreshOptions = useCallback(async () => {
        const res = await apiFetch(`/api/jobs/${jobId}/render-options`);
        if (!res.ok) throw new Error('Failed to load render options');
        const data = await res.json();
        setConfig(data.default?.branding || data.default);
        setOverrides(data.clip_overrides || {});
        return data;
    }, [jobId]);

    useEffect(() => {
        if (!isOpen || !jobId) return;
        setLoading(true);
        setError(null);
        refreshOptions()
            .catch(e => setError(e.message))
            .finally(() => setLoading(false));
    }, [isOpen, jobId, refreshOptions]);

    if (!isOpen) return null;

    const handleLogoUpload = async (e) => {
        const file = e.target.files?.[0];
        if (fileInputRef.current) fileInputRef.current.value = '';
        if (!file) return;
        if (!file.type.startsWith('image/')) {
            setError('Logo must be a PNG, JPG, or WebP image.');
            return;
        }
        setUploading(true);
        setError(null);
        try {
            const form = new FormData();
            form.append('file', file);
            const res = await apiFetch(`/api/jobs/${jobId}/render-options/logo`, {
                method: 'POST',
                body: form,
            });
            if (!res.ok) throw new Error(await res.text());
            await refreshOptions();
            setLogoVersion(v => v + 1); // bust the cache so the new logo shows
        } catch (err) {
            setError(err.message);
        } finally {
            setUploading(false);
        }
    };

    const updateField = (field, value) => {
        setConfig(prev => ({
            ...prev,
            logo: { ...prev.logo, [field]: value },
        }));
    };

    const buildChanges = () => {
        if (!effective) return {};
        return {
            branding: {
                logo: {
                    enabled: effective.enabled,
                    position: effective.position,
                    size_pct: effective.size_pct,
                    opacity: effective.opacity,
                    margin_px: effective.margin_px,
                },
            },
        };
    };

    const handleApply = async (scope) => {
        setApplying(scope);
        setError(null);
        try {
            const body = { scope, changes: buildChanges() };
            if (scope === 'clip') body.clip_index = clipIndex;

            const res = await apiFetch(`/api/jobs/${jobId}/render-options`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();

            await refreshOptions();

            onApplied?.(data);
            if (scope === 'all') onClose();
        } catch (e) {
            setError(e.message);
        } finally {
            setApplying(null);
        }
    };

    const handleResetOverride = async () => {
        setApplying('reset');
        setError(null);
        try {
            const res = await apiFetch(`/api/jobs/${jobId}/render-options/overrides/${clipIndex}`, {
                method: 'DELETE',
            });
            if (!res.ok) throw new Error(await res.text());

            await refreshOptions();
            onApplied?.({ success: true, reset: true });
        } catch (e) {
            setError(e.message);
        } finally {
            setApplying(null);
        }
    };

    const logoPreviewPosition = () => {
        if (!effective) return {};
        const m = 8;
        switch (effective.position) {
            case 'top_left': return { top: m, left: m };
            case 'top_right': return { top: m, right: m };
            case 'bottom_left': return { bottom: m, left: m };
            default: return { bottom: m, right: m };
        }
    };

    return (
        <Modal isOpen={isOpen} onClose={onClose} size="md" eyebrow="EDITOR · BRANDING" title="logo overlay">
            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <Loader2 size={24} className="animate-spin text-brass" />
                </div>
            ) : (
                <div className="space-y-5">
                    {/* Upload section */}
                    <div className="flex items-center justify-between px-3 py-2.5 rounded-input bg-paper3 border border-rule">
                        <div className="flex items-center gap-2">
                            <Upload size={14} className="text-brass" />
                            <span className="text-xs text-ink2">
                                {logoUrl ? 'Replace logo' : 'Upload logo'}
                            </span>
                        </div>
                        <button
                            onClick={() => fileInputRef.current?.click()}
                            disabled={uploading}
                            className="btn-quiet py-1.5 px-3 text-xs flex items-center gap-1.5"
                        >
                            {uploading ? (
                                <>
                                    <Loader2 size={12} className="animate-spin" />
                                    Uploading...
                                </>
                            ) : (
                                'Choose PNG/JPG'
                            )}
                        </button>
                        <input
                            ref={fileInputRef}
                            type="file"
                            accept="image/png,image/jpeg,image/jpg,image/webp"
                            onChange={handleLogoUpload}
                            className="hidden"
                        />
                    </div>
                    {hasOverride && (
                        <div className="flex items-center justify-between px-3 py-2 rounded-input bg-paper3 border border-brass/30">
                            <span className="text-xs text-brass font-medium">Custom override for this clip</span>
                            <button
                                onClick={handleResetOverride}
                                disabled={applying === 'reset'}
                                className="flex items-center gap-1 text-xs text-muted hover:text-ink transition-colors"
                            >
                                {applying === 'reset' ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                                Reset to default
                            </button>
                        </div>
                    )}

                    {/* Preview */}
                    <div className="relative mx-auto w-36 h-64 bg-black rounded-card border border-rule overflow-hidden">
                        <div className="absolute inset-0 flex items-center justify-center text-muted text-xs opacity-40">
                            9:16
                        </div>
                        {effective?.enabled && logoUrl && (
                            <img
                                src={logoUrl}
                                alt="Logo preview"
                                className="absolute object-contain"
                                style={{
                                    ...logoPreviewPosition(),
                                    opacity: effective.opacity,
                                    width: `${Math.max(12, effective.size_pct * 0.6)}px`,
                                }}
                            />
                        )}
                    </div>

                    {/* Enable/Disable */}
                    <div className="flex items-center justify-between">
                        <label className="text-sm text-ink2">Logo enabled</label>
                        <button
                            onClick={() => updateField('enabled', !effective?.enabled)}
                            className={`w-10 h-5 rounded-full transition-colors relative ${effective?.enabled ? 'bg-brass' : 'bg-rule'}`}
                        >
                            <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${effective?.enabled ? 'left-5' : 'left-0.5'}`} />
                        </button>
                    </div>

                    {/* Position */}
                    <div>
                        <label className="text-xs text-muted mb-2 block">Position</label>
                        <SegmentedControl
                            options={POSITION_OPTIONS}
                            value={effective?.position || 'bottom_right'}
                            onChange={(v) => updateField('position', v)}
                            columns={2}
                            size="sm"
                        />
                    </div>

                    {/* Size */}
                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Size: {Math.round(effective?.size_pct || 15)}% of width
                        </label>
                        <input
                            type="range"
                            min="5"
                            max="40"
                            step="1"
                            value={effective?.size_pct || 15}
                            onChange={(e) => updateField('size_pct', Number(e.target.value))}
                            className="w-full accent-brass"
                        />
                    </div>

                    {/* Opacity */}
                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Opacity: {Math.round((effective?.opacity ?? 1) * 100)}%
                        </label>
                        <input
                            type="range"
                            min="0"
                            max="100"
                            step="5"
                            value={Math.round((effective?.opacity ?? 1) * 100)}
                            onChange={(e) => updateField('opacity', Number(e.target.value) / 100)}
                            className="w-full accent-brass"
                        />
                    </div>

                    {/* Margin */}
                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Margin: {effective?.margin_px ?? 20}px
                        </label>
                        <input
                            type="range"
                            min="0"
                            max="100"
                            step="5"
                            value={effective?.margin_px ?? 20}
                            onChange={(e) => updateField('margin_px', Number(e.target.value))}
                            className="w-full accent-brass"
                        />
                    </div>

                    {error && <p className="text-xs text-red-400">{error}</p>}

                    {/* Apply buttons */}
                    <div className="flex gap-2 pt-2 border-t border-rule">
                        <button
                            onClick={() => handleApply('clip')}
                            disabled={!!applying}
                            className="flex-1 btn-quiet py-2.5 px-3 text-xs flex items-center justify-center gap-1.5"
                        >
                            {applying === 'clip' && <Loader2 size={14} className="animate-spin" />}
                            Apply to this clip
                        </button>
                        <button
                            onClick={() => handleApply('all')}
                            disabled={!!applying}
                            className="flex-1 btn-primary py-2.5 px-3 text-xs flex items-center justify-center gap-1.5"
                        >
                            {applying === 'all' && <Loader2 size={14} className="animate-spin" />}
                            Apply to all clips
                        </button>
                    </div>
                </div>
            )}
        </Modal>
    );
}
