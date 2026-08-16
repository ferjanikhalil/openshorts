import React, { useState, useEffect, useRef } from 'react';
import { Upload, X, Loader2 } from 'lucide-react';
import SegmentedControl from './ui/SegmentedControl';

const POSITION_OPTIONS = [
    { value: 'top_left', label: 'top left' },
    { value: 'top_right', label: 'top right' },
    { value: 'bottom_left', label: 'bottom left' },
    { value: 'bottom_right', label: 'bottom right' },
];

const STORAGE_KEY = 'openshorts_branding';

function loadBrandingDefaults() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) return JSON.parse(raw);
    } catch { /* ignore */ }
    return null;
}

function saveBrandingDefaults(config) {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    } catch { /* ignore */ }
}

// `embedded` drops the outer card chrome so this can sit inside a modal
// (Autopilot mounts it that way). Default keeps the standalone settings-page look.
export default function BrandingSettings({ embedded = false }) {
    const [enabled, setEnabled] = useState(false);
    const [logoDataUrl, setLogoDataUrl] = useState(null);
    const [position, setPosition] = useState('bottom_right');
    const [sizePct, setSizePct] = useState(15);
    const [opacity, setOpacity] = useState(100);
    const [marginPx, setMarginPx] = useState(20);
    const fileInputRef = useRef(null);

    useEffect(() => {
        const saved = loadBrandingDefaults();
        if (saved) {
            setEnabled(saved.enabled ?? false);
            setLogoDataUrl(saved.logoDataUrl || null);
            setPosition(saved.position || 'bottom_right');
            setSizePct(saved.size_pct ?? 15);
            setOpacity(Math.round((saved.opacity ?? 1) * 100));
            setMarginPx(saved.margin_px ?? 20);
        }
    }, []);

    useEffect(() => {
        saveBrandingDefaults({
            enabled,
            logoDataUrl,
            position,
            size_pct: sizePct,
            opacity: opacity / 100,
            margin_px: marginPx,
        });
    }, [enabled, logoDataUrl, position, sizePct, opacity, marginPx]);

    const handleFileSelect = (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        if (!file.type.startsWith('image/')) return;
        const reader = new FileReader();
        reader.onload = (ev) => setLogoDataUrl(ev.target.result);
        reader.readAsDataURL(file);
    };

    const handleRemoveLogo = () => {
        setLogoDataUrl(null);
        setEnabled(false);
        if (fileInputRef.current) fileInputRef.current.value = '';
    };

    const logoPreviewPosition = () => {
        const m = 6;
        switch (position) {
            case 'top_left': return { top: m, left: m };
            case 'top_right': return { top: m, right: m };
            case 'bottom_left': return { bottom: m, left: m };
            default: return { bottom: m, right: m };
        }
    };

    return (
        <div className={embedded ? '' : 'card p-4 sm:p-6 mt-8'}>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                        <Upload size={16} className="text-brass" />
                    </div>
                    <h2 className="text-base font-medium text-ink lowercase">Brand / Logo</h2>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                    <span className="text-xs text-muted">{enabled ? 'On' : 'Off'}</span>
                    <button
                        type="button"
                        onClick={() => setEnabled(!enabled)}
                        disabled={!logoDataUrl}
                        className={`w-10 h-5 rounded-full transition-colors relative disabled:opacity-40 ${enabled ? 'bg-brass' : 'bg-rule'}`}
                    >
                        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${enabled ? 'left-5' : 'left-0.5'}`} />
                    </button>
                </label>
            </div>

            <p className="text-xs text-muted mb-5 leading-relaxed">
                Your default branding, shared by the Clip Generator and Autopilot. You can still
                adjust it per-clip after generation{embedded ? ', or per-video via customize' : ''}.
            </p>

            <div className="flex flex-col sm:flex-row gap-6">
                {/* Left: Upload + Preview */}
                <div className="flex flex-col items-center gap-3">
                    <div className="relative w-32 h-56 bg-black rounded-card border border-rule overflow-hidden">
                        <div className="absolute inset-0 flex items-center justify-center text-muted text-[10px] opacity-30">
                            preview
                        </div>
                        {enabled && logoDataUrl && (
                            <img
                                src={logoDataUrl}
                                alt="Logo preview"
                                className="absolute object-contain"
                                style={{
                                    ...logoPreviewPosition(),
                                    width: `${Math.max(16, sizePct * 1.2)}px`,
                                    opacity: opacity / 100,
                                }}
                            />
                        )}
                    </div>

                    {logoDataUrl ? (
                        <button onClick={handleRemoveLogo} className="flex items-center gap-1 text-xs text-muted hover:text-red-400 transition-colors">
                            <X size={12} /> Remove logo
                        </button>
                    ) : (
                        <button
                            onClick={() => fileInputRef.current?.click()}
                            className="flex items-center gap-1.5 text-xs text-brass hover:text-brass/80 transition-colors"
                        >
                            <Upload size={12} /> Upload PNG
                        </button>
                    )}
                    <input
                        ref={fileInputRef}
                        type="file"
                        accept="image/png,image/jpeg,image/webp"
                        onChange={handleFileSelect}
                        className="hidden"
                    />
                </div>

                {/* Right: Controls */}
                <div className="flex-1 space-y-4">
                    <div>
                        <label className="text-xs text-muted mb-2 block">Position</label>
                        <SegmentedControl
                            options={POSITION_OPTIONS}
                            value={position}
                            onChange={setPosition}
                            columns={2}
                            size="sm"
                        />
                    </div>

                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Size: {sizePct}% of width
                        </label>
                        <input
                            type="range"
                            min="5"
                            max="40"
                            step="1"
                            value={sizePct}
                            onChange={(e) => setSizePct(Number(e.target.value))}
                            className="w-full accent-brass"
                        />
                    </div>

                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Opacity: {opacity}%
                        </label>
                        <input
                            type="range"
                            min="10"
                            max="100"
                            step="5"
                            value={opacity}
                            onChange={(e) => setOpacity(Number(e.target.value))}
                            className="w-full accent-brass"
                        />
                    </div>

                    <div>
                        <label className="text-xs text-muted mb-1 block">
                            Margin: {marginPx}px
                        </label>
                        <input
                            type="range"
                            min="0"
                            max="100"
                            step="5"
                            value={marginPx}
                            onChange={(e) => setMarginPx(Number(e.target.value))}
                            className="w-full accent-brass"
                        />
                    </div>
                </div>
            </div>
        </div>
    );
}
