import React, { useState, useEffect, useRef } from 'react';
import { ChevronDown, Upload, X } from 'lucide-react';
import SegmentedControl from './ui/SegmentedControl';
import { loadBrandingDefaults, saveBrandingDefaults } from '../lib/branding';

const POSITION_OPTIONS = [
    { value: 'top_left', label: 'TL' },
    { value: 'top_right', label: 'TR' },
    { value: 'bottom_left', label: 'BL' },
    { value: 'bottom_right', label: 'BR' },
];

function BrandingSection({ config, onChange }) {
    const fileInputRef = useRef(null);

    const update = (field, value) => {
        onChange({ ...config, [field]: value });
    };

    const handleFileSelect = (e) => {
        const file = e.target.files?.[0];
        if (!file || !file.type.startsWith('image/')) return;
        const reader = new FileReader();
        reader.onload = (ev) => onChange({ ...config, logoDataUrl: ev.target.result, enabled: true });
        reader.readAsDataURL(file);
    };

    return (
        <div className="space-y-3">
            <div className="flex items-center gap-3">
                {config.logoDataUrl ? (
                    <div className="relative group">
                        <img
                            src={config.logoDataUrl}
                            alt="Logo"
                            className="w-10 h-10 object-contain rounded border border-rule bg-black/20"
                        />
                        <button
                            onClick={() => { update('logoDataUrl', null); update('enabled', false); }}
                            className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-red-500 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                        >
                            <X size={10} />
                        </button>
                    </div>
                ) : (
                    <button
                        onClick={() => fileInputRef.current?.click()}
                        className="w-10 h-10 rounded border border-dashed border-rule flex items-center justify-center text-muted hover:text-brass hover:border-brass transition-colors"
                    >
                        <Upload size={14} />
                    </button>
                )}
                <input
                    ref={fileInputRef}
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    onChange={handleFileSelect}
                    className="hidden"
                />
                <span className="text-xs text-muted">
                    {config.logoDataUrl ? 'Logo uploaded' : 'Upload PNG logo'}
                </span>
            </div>

            {config.logoDataUrl && (
                <div className="grid grid-cols-2 gap-3">
                    <div>
                        <label className="text-[10px] text-muted mb-1 block">Position</label>
                        <SegmentedControl
                            options={POSITION_OPTIONS}
                            value={config.position || 'bottom_right'}
                            onChange={(v) => update('position', v)}
                            columns={4}
                            size="sm"
                        />
                    </div>
                    <div className="space-y-2">
                        <div>
                            <label className="text-[10px] text-muted block">
                                Size: {config.size_pct ?? 15}%
                            </label>
                            <input
                                type="range" min="5" max="40" step="1"
                                value={config.size_pct ?? 15}
                                onChange={(e) => update('size_pct', Number(e.target.value))}
                                className="w-full accent-brass h-1"
                            />
                        </div>
                        <div>
                            <label className="text-[10px] text-muted block">
                                Opacity: {Math.round((config.opacity ?? 1) * 100)}%
                            </label>
                            <input
                                type="range" min="10" max="100" step="5"
                                value={Math.round((config.opacity ?? 1) * 100)}
                                onChange={(e) => update('opacity', Number(e.target.value) / 100)}
                                className="w-full accent-brass h-1"
                            />
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

export default function RenderOptionsPanel() {
    const [expanded, setExpanded] = useState(false);
    const [branding, setBranding] = useState(() => {
        const saved = loadBrandingDefaults();
        return saved || { enabled: false, logoDataUrl: null, position: 'bottom_right', size_pct: 15, opacity: 1, margin_px: 20 };
    });

    useEffect(() => {
        saveBrandingDefaults(branding);
    }, [branding]);

    const hasActiveOptions = branding.enabled && branding.logoDataUrl;

    return (
        <div className="mt-3 border border-rule rounded-card overflow-hidden">
            <button
                type="button"
                onClick={() => setExpanded(!expanded)}
                className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-paper3/50 transition-colors"
            >
                <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-ink2 lowercase">Render options</span>
                    {hasActiveOptions && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-brass/15 text-brass font-medium">
                            branding on
                        </span>
                    )}
                </div>
                <ChevronDown size={14} className={`text-muted transition-transform ${expanded ? 'rotate-180' : ''}`} />
            </button>

            {expanded && (
                <div className="px-4 pb-4 border-t border-rule">
                    <div className="pt-3">
                        <div className="flex items-center justify-between mb-2">
                            <div className="flex items-center gap-2">
                                <label className="text-xs text-ink2 lowercase font-medium">Branding</label>
                                <button
                                    type="button"
                                    onClick={() => setBranding(prev => ({ ...prev, enabled: !prev.enabled }))}
                                    disabled={!branding.logoDataUrl}
                                    className={`w-8 h-4 rounded-full transition-colors relative disabled:opacity-40 ${branding.enabled ? 'bg-brass' : 'bg-rule'}`}
                                >
                                    <span className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform ${branding.enabled ? 'left-4' : 'left-0.5'}`} />
                                </button>
                            </div>
                        </div>
                        <BrandingSection config={branding} onChange={setBranding} />
                    </div>
                </div>
            )}
        </div>
    );
}
