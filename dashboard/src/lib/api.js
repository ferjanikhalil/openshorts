// Centralized API client for cloud mode.
// Adds the Authorization: Bearer header from the stored session token, and turns
// a 402 (quota exceeded) into a typed QuotaError the UI can catch to prompt a top-up.
import { getApiUrl } from '../config';

export const AUTH_TOKEN_KEY = 'openshorts_auth';

export const getToken = () => localStorage.getItem(AUTH_TOKEN_KEY) || '';
export const setToken = (t) => localStorage.setItem(AUTH_TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(AUTH_TOKEN_KEY);

export class QuotaError extends Error {
  constructor(detail) {
    super('quota_exceeded');
    this.name = 'QuotaError';
    this.minutesRequired = detail?.minutes_required;
    this.minutesRemaining = detail?.minutes_remaining;
  }
}

// Get direct backend URL for large file uploads (bypass Vite proxy)
// In dev mode, backend is at http://localhost:8000
function getDirectBackendUrl(path) {
  if (window.location.port === '5173' || window.location.port === '5174') {
    const baseUrl = `${window.location.protocol}//${window.location.hostname}:8000`;
    return `${baseUrl}${path}`;
  }
  return getApiUrl(path);
}

// Chunk size: 100MB. Each chunk is a separate small XHR — won't drop.
const CHUNK_SIZE = 100 * 1024 * 1024;

/**
 * Upload a single chunk via XHR. Returns a promise that resolves when the
 * chunk is received by the backend.
 */
function uploadOneChunk(uploadUrl, formData, token, headers) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (_) {
          resolve({ status: 'ok' });
        }
      } else if (xhr.status === 402) {
        let detail = {};
        try { detail = JSON.parse(xhr.responseText); detail = detail.detail || detail; } catch { /* non-JSON body: keep default {} */ }
        reject(new QuotaError(detail));
      } else {
        reject(new Error(`Chunk upload failed: HTTP ${xhr.status} ${xhr.statusText}`));
      }
    });

    xhr.addEventListener('error', () => reject(new Error('Chunk upload: network error')));
    xhr.addEventListener('abort', () => reject(new Error('Chunk upload: cancelled')));
    xhr.addEventListener('timeout', () => reject(new Error('Chunk upload: timed out')));

    xhr.open('POST', uploadUrl);
    xhr.timeout = 10 * 60 * 1000; // 10 min per chunk

    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    headers.forEach((value, key) => {
      if (key !== 'Authorization') {
        xhr.setRequestHeader(key, value);
      }
    });

    xhr.send(formData);
  });
}

/**
 * Upload a large file in chunks. Splits the file into CHUNK_SIZE pieces,
 * uploads each one separately, then calls /api/upload/complete to assemble
 * and start processing.
 */
async function chunkedUpload(file, acknowledged, outputFormat, reframeMode, clipDurationMode, branding, headers, token, onProgress) {
  const uploadId = crypto.randomUUID();
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
  const chunkUrl = getDirectBackendUrl('/api/upload/chunk');

  console.log(`[ChunkedUpload] Splitting ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB) into ${totalChunks} chunks`);

  // Upload each chunk sequentially
  let bytesUploaded = 0;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);

    const formData = new FormData();
    formData.append('upload_id', uploadId);
    formData.append('chunk_index', String(i));
    formData.append('total_chunks', String(totalChunks));
    formData.append('chunk', chunk, file.name);

    console.log(`[ChunkedUpload] Sending chunk ${i + 1}/${totalChunks} (${((end - start) / 1024 / 1024).toFixed(1)}MB)`);

    await uploadOneChunk(chunkUrl, formData, token, headers);

    bytesUploaded += (end - start);

    // Report overall progress
    if (onProgress) {
      onProgress({
        loaded: bytesUploaded,
        total: file.size,
        percent: Math.round((bytesUploaded / file.size) * 100),
        chunkIndex: i + 1,
        totalChunks,
      });
    }
  }

  console.log(`[ChunkedUpload] All ${totalChunks} chunks uploaded. Sending complete signal...`);

  // Tell the backend to assemble chunks and start processing
  const completeUrl = getDirectBackendUrl('/api/upload/complete');
  const completeFormData = new FormData();
  completeFormData.append('upload_id', uploadId);
  completeFormData.append('acknowledged', String(acknowledged));
  completeFormData.append('output_format', outputFormat || 'auto');
  completeFormData.append('reframe_mode', reframeMode || 'auto');
  completeFormData.append('clip_duration_mode', clipDurationMode || 'auto');
  if (branding) {
    completeFormData.append('branding', typeof branding === 'string' ? branding : JSON.stringify(branding));
  }

  // The complete endpoint can take a few seconds (assembling 2GB) — use a
  // longer timeout and track it as the final 100% step.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();

    xhr.addEventListener('load', () => {
      const response = {
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        statusText: xhr.statusText,
        json: () => {
          try { return Promise.resolve(JSON.parse(xhr.responseText)); }
          catch (_) { return Promise.resolve({}); }
        },
        text: () => Promise.resolve(xhr.responseText),
        clone: function () { return this; },
      };

      if (xhr.status === 402) {
        let detail = {};
        try { detail = JSON.parse(xhr.responseText); detail = detail.detail || detail; } catch { /* non-JSON body: keep default {} */ }
        reject(new QuotaError(detail));
        return;
      }

      resolve(response);
    });

    xhr.addEventListener('error', () => reject(new Error('Upload complete: network error')));
    xhr.addEventListener('abort', () => reject(new Error('Upload complete: cancelled')));
    xhr.addEventListener('timeout', () => reject(new Error('Upload complete: timed out')));

    xhr.open('POST', completeUrl);
    xhr.timeout = 10 * 60 * 1000; // 10 min for assembly

    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    headers.forEach((value, key) => {
      if (key !== 'Authorization') {
        xhr.setRequestHeader(key, value);
      }
    });

    xhr.send(completeFormData);
  });
}

/**
 * Autopilot source upload: chunk a file up to the assemble-only
 * /api/autopilot/upload endpoint and return the server file ref (NOT a job).
 * Unlike apiFetch's chunkedUpload — which finishes by calling /api/upload/complete
 * and auto-enqueues a single-video job — this stops at assembly so the caller can
 * hand the ref to POST /api/autopilot. Works for small files too (1 chunk).
 * Resolves to { status:'assembled', file_ref, filename, size }.
 */
export async function uploadAutopilotSource(file, onProgress) {
  const token = getToken();
  const uploadId = crypto.randomUUID();
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE) || 1;
  const chunkUrl = getDirectBackendUrl('/api/autopilot/upload');
  const headers = new Headers();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let bytesUploaded = 0;
  let last = null;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);

    const formData = new FormData();
    formData.append('upload_id', uploadId);
    formData.append('chunk_index', String(i));
    formData.append('total_chunks', String(totalChunks));
    formData.append('chunk', chunk, file.name);

    last = await uploadOneChunk(chunkUrl, formData, token, headers);
    bytesUploaded += (end - start);
    if (onProgress) {
      onProgress({
        loaded: bytesUploaded,
        total: file.size,
        percent: Math.round((bytesUploaded / file.size) * 100),
      });
    }
  }

  if (!last || !last.file_ref) {
    throw new Error('Upload did not return a file reference');
  }
  return last;
}

// Drop-in fetch wrapper. Always attaches the bearer token when present.
// For file uploads with onProgress, uses chunked upload for large files.
export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  // Extract progress callback if provided
  const { onProgress, ...fetchOptions } = options;

  // For file uploads with progress tracking:
  // - Files > 100MB use chunked upload (split into 100MB pieces)
  // - Smaller files use a single XHR with native progress events
  if (onProgress && fetchOptions.body instanceof FormData) {
    const file = fetchOptions.body.get('file');

    if (file && file.size > CHUNK_SIZE) {
      // Large file → chunked upload
      console.log('[Upload] File is large, using chunked upload');
      return chunkedUpload(
        file,
        fetchOptions.body.get('acknowledged'),
        fetchOptions.body.get('output_format'),
        fetchOptions.body.get('reframe_mode'),
        fetchOptions.body.get('clip_duration_mode'),
        fetchOptions.body.get('branding'),
        headers,
        token,
        onProgress
      );
    }

    // Small file → single XHR with progress tracking
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const uploadUrl = getDirectBackendUrl(path);

      console.log('[Upload] Starting single-file upload to:', uploadUrl);

      let lastPercent = 0;

      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable && onProgress) {
          const progress = {
            loaded: e.loaded,
            total: e.total,
            percent: Math.round((e.loaded / e.total) * 100),
          };
          lastPercent = progress.percent;
          onProgress(progress);
        }
      });

      xhr.addEventListener('load', () => {
        console.log('[Upload] Load event, status:', xhr.status);
        const response = {
          ok: xhr.status >= 200 && xhr.status < 300,
          status: xhr.status,
          statusText: xhr.statusText,
          json: () => Promise.resolve(JSON.parse(xhr.responseText)),
          text: () => Promise.resolve(xhr.responseText),
          clone: function () { return this; },
        };

        if (xhr.status === 402) {
          let detail = {};
          try {
            detail = JSON.parse(xhr.responseText);
            detail = detail.detail || detail;
          } catch (_) { /* ignore */ }
          reject(new QuotaError(detail));
          return;
        }

        resolve(response);
      });

      xhr.addEventListener('error', () => {
        console.error('[Upload] Error event');
        reject(new Error(`Upload failed at ${lastPercent}% - network error`));
      });
      xhr.addEventListener('abort', () => reject(new Error('Upload cancelled')));
      xhr.addEventListener('timeout', () => {
        reject(new Error(`Upload timed out at ${lastPercent}%`));
      });

      xhr.open('POST', uploadUrl);
      xhr.timeout = 30 * 60 * 1000; // 30 min for small files

      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      headers.forEach((value, key) => {
        if (key !== 'Authorization') {
          xhr.setRequestHeader(key, value);
        }
      });

      console.log('[Upload] Sending', file?.size, 'bytes');
      xhr.send(fetchOptions.body);
    });
  }

  // Regular fetch for non-upload requests
  const res = await fetch(getApiUrl(path), { ...fetchOptions, headers });

  if (res.status === 402) {
    let detail = {};
    try {
      const body = await res.clone().json();
      detail = body.detail || body;
    } catch (_) { /* ignore */ }
    throw new QuotaError(detail);
  }
  return res;
}

// Convenience JSON helper.
export async function apiJson(path, options = {}) {
  const res = await apiFetch(path, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}
