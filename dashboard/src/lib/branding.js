// Shared branding-defaults persistence (localStorage). Kept out of the
// RenderOptionsPanel component file so Fast Refresh works there and so
// AutopilotTab / App can reuse the same loader without importing a component.
const STORAGE_KEY = 'openshorts_branding';

export function loadBrandingDefaults() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) return JSON.parse(raw);
    } catch { /* ignore */ }
    return null;
}

export function saveBrandingDefaults(config) {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    } catch { /* ignore */ }
}
