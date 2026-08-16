// Watermark "don't show again" persistence, kept out of the WatermarkModal
// component file so Fast Refresh works there and ResultCard can read the flag
// without importing a component.
export const WATERMARK_DISMISS_KEY = 'os_watermark_notice_dismissed';

export function watermarkNoticeDismissed() {
  try { return localStorage.getItem(WATERMARK_DISMISS_KEY) === '1'; } catch { return false; }
}

export function dismissWatermarkNotice() {
  try { localStorage.setItem(WATERMARK_DISMISS_KEY, '1'); } catch { /* ignore */ }
}
