import { useState, useEffect, useRef } from 'react';
import { Upload, RotateCcw } from 'lucide-react';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';

// Per-video branding override for Autopilot.
//
// Two things can differ from the batch: the logo IMAGE and its GEOMETRY.
// Geometry always merges over the batch values via resolve_cascade. The image is
// optional — leave it inherited and the backend falls back to the batch logo;
// upload one and it wins for this video only (each child job has its own dir, so
// both write branding_logo.png without colliding).
//
// Emits the RenderOptions branding shape: { logo: { enabled: true, ... } }, with
// logo_image_data present only when this video carries its own image.

const POSITION_OPTIONS = [
  { value: 'top_left', label: 'top left' },
  { value: 'top_right', label: 'top right' },
  { value: 'bottom_left', label: 'bottom left' },
  { value: 'bottom_right', label: 'bottom right' },
];

export default function BrandingOverrideModal({
  isOpen, onClose, onApply, initial, batchDefaults, videoLabel,
}) {
  const inherited = batchDefaults?.enabled && batchDefaults?.logoDataUrl
    ? batchDefaults
    : null;
  const initLogo = initial?.logo || {};

  const [ownLogo, setOwnLogo] = useState(null);
  const [position, setPosition] = useState('bottom_right');
  const [sizePct, setSizePct] = useState(15);
  const [opacity, setOpacity] = useState(100);
  const [marginPx, setMarginPx] = useState(20);
  const fileInputRef = useRef(null);

  // Seed from the existing override, else the batch defaults, else the model
  // defaults. Re-seeds each time the modal opens so a cancelled edit is discarded.
  useEffect(() => {
    if (!isOpen) return;
    setOwnLogo(initLogo.logo_image_data || null);
    setPosition(initLogo.position || inherited?.position || 'bottom_right');
    setSizePct(initLogo.size_pct ?? inherited?.size_pct ?? 15);
    setOpacity(Math.round((initLogo.opacity ?? inherited?.opacity ?? 1) * 100));
    setMarginPx(initLogo.margin_px ?? inherited?.margin_px ?? 20);
    // Seeding is intentionally open-triggered: `initial`/`batchDefaults` are
    // fresh objects each render, so depending on them would reset mid-edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const previewLogo = ownLogo || inherited?.logoDataUrl || null;
  // Nothing to draw: no batch logo to inherit and none uploaded here.
  const missingImage = !previewLogo;

  const handleFileSelect = (e) => {
    const file = e.target.files?.[0];
    if (!file || !file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = (ev) => setOwnLogo(ev.target.result);
    reader.readAsDataURL(file);
  };

  const handleApply = () => {
    if (missingImage) return;
    const logo = {
      enabled: true,
      position,
      size_pct: sizePct,
      opacity: opacity / 100,
      margin_px: marginPx,
    };
    // Only send an image when this video overrides it; otherwise the backend
    // reuses the batch logo.
    if (ownLogo) logo.logo_image_data = ownLogo;
    onApply({ logo });
  };

  const previewPosition = () => {
    const m = 6;
    switch (position) {
      case 'top_left': return { top: m, left: m };
      case 'top_right': return { top: m, right: m };
      case 'bottom_left': return { bottom: m, left: m };
      default: return { bottom: m, right: m };
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      size="lg"
      eyebrow="AUTOPILOT · PER-VIDEO BRANDING"
      title="branding for this video"
      footer={
        <div className="flex items-center justify-between gap-3">
          <span className="text-[11px] text-muted truncate">
            {missingImage
              ? 'Upload a logo for this video, or set a batch logo first.'
              : ownLogo
                ? 'Uses its own logo — the batch logo is ignored here.'
                : 'Uses the batch logo with the position and size set here.'}
          </span>
          <div className="flex gap-2 shrink-0">
            <button onClick={onClose} className="btn-ghost text-sm px-3 py-1.5">Cancel</button>
            <button
              onClick={handleApply}
              disabled={missingImage}
              className="btn-primary text-sm px-4 py-1.5 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Apply
            </button>
          </div>
        </div>
      }
    >
      {videoLabel && (
        <p className="text-xs text-muted mb-4 truncate">
          Overrides branding for <span className="text-ink">{videoLabel}</span> only.
        </p>
      )}

      <div className="flex flex-col sm:flex-row gap-6">
        {/* Preview + image source */}
        <div className="flex flex-col items-center gap-3">
          <div className="relative w-32 h-56 bg-black rounded-card border border-rule overflow-hidden">
            <div className="absolute inset-0 flex items-center justify-center text-muted text-[10px] opacity-30">
              preview
            </div>
            {previewLogo && (
              <img
                src={previewLogo}
                alt="Logo preview"
                className="absolute object-contain"
                style={{
                  ...previewPosition(),
                  width: `${Math.max(16, sizePct * 1.2)}px`,
                  opacity: opacity / 100,
                }}
              />
            )}
          </div>

          <span className={`text-[10px] px-2 py-0.5 rounded-full border ${
            ownLogo
              ? 'border-brass/40 bg-brass/10 text-brass'
              : 'border-rule bg-paper2 text-muted'
          }`}>
            {ownLogo ? 'own logo' : inherited ? 'batch logo' : 'no logo'}
          </span>

          <div className="flex flex-col items-center gap-1.5">
            <button
              onClick={() => fileInputRef.current?.click()}
              className="flex items-center gap-1.5 text-xs text-brass hover:text-brass/80 transition-colors"
            >
              <Upload size={12} /> {ownLogo ? 'Replace logo' : 'Upload own logo'}
            </button>
            {ownLogo && inherited && (
              <button
                onClick={() => { setOwnLogo(null); if (fileInputRef.current) fileInputRef.current.value = ''; }}
                className="flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
              >
                <RotateCcw size={11} /> Back to batch logo
              </button>
            )}
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            onChange={handleFileSelect}
            className="hidden"
          />
        </div>

        {/* Geometry — merges over the batch values */}
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
            <label className="text-xs text-muted mb-1 block">Size: {sizePct}% of width</label>
            <input
              type="range" min="5" max="40" step="1" value={sizePct}
              onChange={(e) => setSizePct(Number(e.target.value))}
              className="w-full accent-brass"
            />
          </div>

          <div>
            <label className="text-xs text-muted mb-1 block">Opacity: {opacity}%</label>
            <input
              type="range" min="10" max="100" step="5" value={opacity}
              onChange={(e) => setOpacity(Number(e.target.value))}
              className="w-full accent-brass"
            />
          </div>

          <div>
            <label className="text-xs text-muted mb-1 block">Margin: {marginPx}px</label>
            <input
              type="range" min="0" max="100" step="5" value={marginPx}
              onChange={(e) => setMarginPx(Number(e.target.value))}
              className="w-full accent-brass"
            />
          </div>
        </div>
      </div>
    </Modal>
  );
}
