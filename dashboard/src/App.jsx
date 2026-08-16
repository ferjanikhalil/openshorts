import React, { useState, useEffect, useRef } from 'react';
import { Upload, Sparkles, Youtube, Instagram, Share2, ChevronDown, Check, Activity, LayoutDashboard, Settings, Plus, History, X, Terminal, Shield, LayoutGrid, Image, Globe, RotateCcw, Calendar, AlertTriangle, KeyRound, Bot, Mail, Loader2, Download, FolderOpen, Send } from 'lucide-react';
import KeyInput from './components/KeyInput';
import MediaInput from './components/MediaInput';
import ResultCard from './components/ResultCard';
import ProcessingAnimation from './components/ProcessingAnimation';
// import Gallery from './components/Gallery';
import ThumbnailStudio from './components/ThumbnailStudio';
import SaaShortsTab from './components/SaaShortsTab';
import UGCGallery from './components/UGCGallery';
import ScheduleWeekModal from './components/ScheduleWeekModal';
import UsageMeter from './components/UsageMeter';
import TopUpModal from './components/TopUpModal';
import StarBanner from './components/StarBanner';
import PlanChoiceModal from './components/PlanChoiceModal';
import TrialUpgradeModal from './components/TrialUpgradeModal';
import LoginModal from './components/LoginModal';
import TrialGate from './components/TrialGate';
import AdvancedBanner from './components/AdvancedBanner';
import HistoryTab from './components/HistoryTab';
import RenderOptionsPanel from './components/RenderOptionsPanel';
import { loadBrandingDefaults } from './lib/branding';
import BatchPipeline from './components/BatchPipeline';
import BrandingSettings from './components/BrandingSettings';
import AutopilotTab from './components/AutopilotTab';
import ProfileMenu from './components/ProfileMenu';
import PublishingTab from './components/publishing/PublishingTab';
import PublishModal from './components/publishing/PublishModal';
import { publishingHealth } from './lib/publishing';
import Modal from './components/ui/Modal';
import { useAuth } from './contexts/auth-context';
import { apiFetch, apiJson, QuotaError } from './lib/api';

// Enhanced "Encryption" using XOR + Base64 with a Salt
// This is better than plain Base64 but still client-side.
const SECRET_KEY = import.meta.env.VITE_ENCRYPTION_KEY || "OpenShorts-Static-Salt-Change-Me";
const ENCRYPTION_PREFIX = "ENC:";

const encrypt = (text) => {
  if (!text) return '';
  try {
    const xor = text.split('').map((c, i) =>
      String.fromCharCode(c.charCodeAt(0) ^ SECRET_KEY.charCodeAt(i % SECRET_KEY.length))
    ).join('');
    return ENCRYPTION_PREFIX + btoa(xor);
  } catch (e) {
    console.error("Encryption failed", e);
    return text;
  }
};

const decrypt = (text) => {
  if (!text) return '';
  if (text.startsWith(ENCRYPTION_PREFIX)) {
    try {
      const raw = text.slice(ENCRYPTION_PREFIX.length);
      // Check if it's plain base64 or our custom XOR (simple try)
      const xor = atob(raw);
      const result = xor.split('').map((c, i) =>
        String.fromCharCode(c.charCodeAt(0) ^ SECRET_KEY.charCodeAt(i % SECRET_KEY.length))
      ).join('');
      return result;
    } catch (e) {
      // Fallback if decryption fails (might be old plain text)
      return '';
    }
  }
  // Backward compatibility: If no prefix, assume old plain text (or return empty if you want to force re-login)
  // For migration: Return text as is, so it populates the field, and next save will encrypt it.
  return text;
};

// Simple TikTok icon sine Lucide might not have it or it varies
const TikTokIcon = ({ size = 16, className = "" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" className={className}>
    <path d="M19.589 6.686a4.793 4.793 0 0 1-3.77-4.245V2h-3.445v13.672a2.896 2.896 0 0 1-5.201 1.743l-.002-.001.002.001a2.895 2.895 0 0 1 3.183-4.51v-3.5a6.329 6.329 0 0 0-5.394 10.692 6.33 6.33 0 0 0 10.857-4.424V8.687a8.182 8.182 0 0 0 4.773 1.526V6.79a4.831 4.831 0 0 1-1.003-.104z" />
  </svg>
);

// Cloud accounts get an auto-generated opaque id (os_<hash>) as username —
// meaningless to the user, so the selector shows connected networks instead.
const isAutoProfileId = (username) => /^os_[0-9a-f]/i.test(username || "");

const ProfileNetworkIcons = ({ profile, size = 12 }) => (
  <span className="flex items-center gap-1.5">
    <span className={profile?.connected?.includes('tiktok') ? 'text-ink' : 'text-muted opacity-40'}>
      <TikTokIcon size={size} />
    </span>
    <span className={profile?.connected?.includes('instagram') ? 'text-ink' : 'text-muted opacity-40'}>
      <Instagram size={size} />
    </span>
    <span className={profile?.connected?.includes('youtube') ? 'text-ink' : 'text-muted opacity-40'}>
      <Youtube size={size} />
    </span>
  </span>
);

const UserProfileSelector = ({ profiles, selectedUserId, onSelect }) => {
  const [isOpen, setIsOpen] = useState(false);

  if (!profiles || profiles.length === 0) return null;

  const selectedProfile = profiles.find(p => p.username === selectedUserId) || profiles[0];
  const autoId = isAutoProfileId(selectedProfile?.username);

  return (
    <div className="relative z-50">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center justify-between bg-paper2 border border-rule2 rounded-input px-3 py-2 text-sm text-ink2 hover:bg-paper3 transition-colors min-w-[180px]"
      >
        <span className="flex items-center gap-2">
          <div className="w-5 h-5 rounded-full bg-paper3 border border-rule flex items-center justify-center font-mono text-micro text-brass">
            {autoId ? "S" : (selectedProfile?.username?.substring(0, 1).toUpperCase() || "U")}
          </div>
          {autoId ? (
            <ProfileNetworkIcons profile={selectedProfile} size={13} />
          ) : (
            <span className="font-medium text-ink truncate max-w-[100px]">{selectedProfile?.username || "Select User"}</span>
          )}
        </span>
        <ChevronDown size={14} className={`text-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <div className="absolute top-full mt-2 right-0 w-64 card overflow-hidden">
          <div className="max-h-60 overflow-y-auto custom-scrollbar">
            {profiles.map((profile) => (
              <button
                key={profile.username}
                onClick={() => {
                  onSelect(profile.username);
                  setIsOpen(false);
                }}
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-paper3 transition-colors text-left group border-b border-rule last:border-0"
              >
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 rounded-full bg-paper3 flex items-center justify-center font-mono text-micro text-ink border border-rule shrink-0">
                    {isAutoProfileId(profile.username) ? "S" : profile.username.substring(0, 2).toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink2 group-hover:text-ink transition-colors truncate">
                      {isAutoProfileId(profile.username)
                        ? `Social profile ${profiles.indexOf(profile) + 1}`
                        : profile.username}
                    </div>
                    <div className="flex gap-2 mt-0.5">
                      {/* Status indicators */}
                      <div className={`flex items-center gap-1 ${profile.connected.includes('tiktok') ? 'text-ink2' : 'text-muted opacity-40'}`}>
                        <TikTokIcon size={10} />
                      </div>
                      <div className={`flex items-center gap-1 ${profile.connected.includes('instagram') ? 'text-ink2' : 'text-muted opacity-40'}`}>
                        <Instagram size={10} />
                      </div>
                      <div className={`flex items-center gap-1 ${profile.connected.includes('youtube') ? 'text-ink2' : 'text-muted opacity-40'}`}>
                        <Youtube size={10} />
                      </div>
                    </div>
                  </div>
                </div>
                {selectedUserId === profile.username && <Check size={14} className="text-brass shrink-0" />}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

const SESSION_KEY = 'openshorts_session';
const SESSION_MAX_AGE = 86400000; // 24 hours (matches server job retention)
const RECENT_PROJECTS_KEY = 'openshorts_recent_projects';
const MAX_RECENT_PROJECTS = 10;

// Mock polling function
const pollJob = async (jobId) => {
  const res = await apiFetch(`/api/status/${jobId}`);
  if (!res.ok) throw new Error('Status check failed');
  return res.json();
};

// Header dropdown listing projects worked on in this browser, so the user can
// jump back to one after clicking "New Project".
const RecentProjectsMenu = ({ projects, currentJobId, reopeningId, onReopen }) => {
  const [isOpen, setIsOpen] = useState(false);
  if (!projects || projects.length === 0) return null;

  const fmt = (ts) => {
    if (!ts) return '';
    const mins = Math.round((Date.now() - ts) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.round(hrs / 24)}d ago`;
  };

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen((v) => !v)}
        className="btn-quiet px-3 py-1.5 text-xs"
        title="Return to a recent project"
      >
        <History size={14} />
        <span className="hidden sm:inline">Recent</span>
        <ChevronDown size={12} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} />
          <div className="absolute top-full left-0 mt-2 w-72 card overflow-hidden z-50">
            <div className="px-4 py-2 border-b border-rule">
              <span className="readout">Recent projects</span>
            </div>
            <div className="max-h-80 overflow-y-auto custom-scrollbar">
              {projects.map((p) => (
                <button
                  key={p.jobId}
                  onClick={() => { setIsOpen(false); onReopen(p); }}
                  disabled={!!reopeningId}
                  className="w-full flex items-center justify-between gap-3 px-4 py-3 hover:bg-paper3 transition-colors text-left border-b border-rule last:border-0 disabled:opacity-50"
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink2 truncate">
                      {p.title || 'Untitled project'}
                    </div>
                    <div className="readout mt-0.5">
                      {p.clipCount ? `${p.clipCount} clip${p.clipCount === 1 ? '' : 's'} · ` : ''}{fmt(p.timestamp)}
                    </div>
                  </div>
                  {reopeningId === p.jobId
                    ? <Loader2 size={14} className="animate-spin text-brass shrink-0" />
                    : p.jobId === currentJobId
                      ? <Check size={14} className="text-brass shrink-0" />
                      : <FolderOpen size={14} className="text-muted shrink-0" />}
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
};

function App() {
  // Cloud auth/billing session (inert when billing is disabled).
  const { billingEnabled, isManaged, isSignedIn, me, plan, refreshMe } = useAuth();
  const [showLogin, setShowLogin] = useState(false);
  const [showTopUp, setShowTopUp] = useState(false);
  const [showPlanChoice, setShowPlanChoice] = useState(false);
  const [showTrialUpgrade, setShowTrialUpgrade] = useState(false);
  const [topUpInfo, setTopUpInfo] = useState({});
  // Durable R2 URLs (per clip index) for the current job — used as a fallback when
  // the ephemeral local /videos/ files have been cleaned up (e.g. after a reload).
  const [durableClips, setDurableClips] = useState({});

  const [apiKey, setApiKey] = useState(localStorage.getItem('gemini_key') || '');
  const [llmConfig, setLlmConfig] = useState(() => {
    try { return JSON.parse(localStorage.getItem('llm_config') || 'null'); } catch { return null; }
  });
  // Social API State - Load encrypted or plain
  const [uploadPostKey, setUploadPostKey] = useState(() => {
    const stored = localStorage.getItem('uploadPostKey_v3');
    if (stored) return decrypt(stored);
    return '';
  });
  // ElevenLabs API State - Load encrypted
  const [elevenLabsKey, setElevenLabsKey] = useState(() => {
    const stored = localStorage.getItem('elevenLabsKey_v1');
    if (stored) return decrypt(stored);
    return '';
  });

  // fal.ai API State - Load encrypted
  const [falKey, setFalKey] = useState(() => {
    const stored = localStorage.getItem('falKey_v1');
    if (stored) return decrypt(stored);
    return '';
  });

  const [uploadUserId, setUploadUserId] = useState(() => localStorage.getItem('uploadUserId') || '');
  const [userProfiles, setUserProfiles] = useState([]); // List of {username, connected: []}
  const [showKeyModal, setShowKeyModal] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState('idle'); // idle, processing, complete, error
  const [results, setResults] = useState(null);
  // Bulk subtitles: apply one style to every clip of the job (triggered from
  // within a clip's subtitle modal via "apply to all").
  const [bulkSub, setBulkSub] = useState({ running: false, current: 0, total: 0, errors: 0 });
  const [downloadingAll, setDownloadingAll] = useState(false);
  // Pre-flight quality gate: { info: {max_height, min_height, cookies_invalid}, data }
  const [qualityGate, setQualityGate] = useState(null);
  const [logs, setLogs] = useState([]);
  const [logsVisible, setLogsVisible] = useState(true);
  const [processingMedia, setProcessingMedia] = useState(null);
  const [activeTab, setActiveTab] = useState('dashboard'); // dashboard, settings
  // Reopened-project state (paid mode): per-clip {index, server_file, active_layers}
  // restored from the backend so ResultCards resume editing where they left off.
  const [projectState, setProjectState] = useState(null);
  // True when the current job was reopened from the library: its source video
  // was never persisted, so the session must not fall back to /api/source.
  const [noSource, setNoSource] = useState(false);

  const [sessionRecovered, setSessionRecovered] = useState(false);
  const [showScheduleWeek, setShowScheduleWeek] = useState(false);

  // Automated publishing is an optional subsystem (PUBLISHING_ENABLED). Probed
  // once so the nav entry and the batch "auto-post" button only appear on a
  // deployment that actually has it — the routes 404 otherwise.
  const [publishingOn, setPublishingOn] = useState(false);
  const [showPublish, setShowPublish] = useState(false);

  // Recent projects (this browser): a lightweight list so the user can jump back
  // to a project after clicking "New Project". Persisted in localStorage.
  const [recentProjects, setRecentProjects] = useState(() => {
    try { return JSON.parse(localStorage.getItem(RECENT_PROJECTS_KEY) || '[]'); }
    catch { return []; }
  });
  const [reopeningRecent, setReopeningRecent] = useState(null);

  // Upsert the given job into the recent-projects list (newest first, capped).
  const rememberProject = (entry) => {
    if (!entry?.jobId) return;
    setRecentProjects((prev) => {
      const next = [
        { ...entry, timestamp: Date.now() },
        ...prev.filter((p) => p.jobId !== entry.jobId),
      ].slice(0, MAX_RECENT_PROJECTS);
      try { localStorage.setItem(RECENT_PROJECTS_KEY, JSON.stringify(next)); } catch { /* full */ }
      return next;
    });
  };

  const forgetProject = (projectJobId) => {
    setRecentProjects((prev) => {
      const next = prev.filter((p) => p.jobId !== projectJobId);
      try { localStorage.setItem(RECENT_PROJECTS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  };

  // Silent-success "saved" states for the settings key inputs (design.md: no alert popups)
  const [elevenLabsSaved, setElevenLabsSaved] = useState(false);
  const [falSaved, setFalSaved] = useState(false);

  // Sync state for original video playback
  const [syncedTime, setSyncedTime] = useState(0);
  const [isSyncedPlaying, setIsSyncedPlaying] = useState(false);
  const [syncTrigger, setSyncTrigger] = useState(0);

  const handleClipPlay = (startTime) => {
    setSyncedTime(startTime);
    setIsSyncedPlaying(true);
    setSyncTrigger(prev => prev + 1);
  };

  const handleClipPause = () => {
    setIsSyncedPlaying(false);
  };

  // --- Project persistence (paid mode) ---
  // Debounced sync of each clip's browser-only edit state (Remotion layers +
  // current server file) to the backend, so a reopened project resumes intact.
  const clipStateSync = useRef({ jobId: null, pending: {}, timer: null });

  const flushClipState = () => {
    const s = clipStateSync.current;
    if (s.timer) { clearTimeout(s.timer); s.timer = null; }
    const entries = Object.entries(s.pending);
    if (!s.jobId || entries.length === 0) return;
    const clips = entries.map(([i, v]) => ({
      index: Number(i),
      active_layers: v.activeLayers,
      server_file: v.serverVideoFile,
    }));
    s.pending = {};
    apiFetch(`/api/projects/${s.jobId}/state`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clips }),
    }).catch(() => {});
  };

  const handleClipStateChange = (index, state) => {
    if (!isManaged || !jobId) return;
    const s = clipStateSync.current;
    if (s.jobId !== jobId) { s.pending = {}; s.jobId = jobId; }
    s.pending[index] = state;
    if (s.timer) clearTimeout(s.timer);
    s.timer = setTimeout(flushClipState, 2000);
  };

  // Reopen an archived project from the History tab: the backend re-downloads
  // its files from R2 into the server's working dir and returns the full state.
  const restoreProject = async (projectJobId) => {
    const data = await apiJson(`/api/projects/${projectJobId}/restore`, { method: 'POST' });
    flushClipState();
    setProjectState(data.project_state || null);
    setNoSource(true);
    setJobId(data.job_id);
    setResults(data.result || null);
    setLogs(['♻️ Project restored from your library.']);
    setProcessingMedia(null);
    setQualityGate(null);
    setStatus('complete');
    setActiveTab('dashboard');
    rememberProject({
      jobId: data.job_id,
      title: data.result?.clips?.[0]?.title || '',
      clipCount: data.result?.clips?.length || 0,
      managed: true,
    });
  };

  // Reopen a project from the header's "Recent" menu. Managed users get a full
  // R2 restore; otherwise we re-poll the still-live job (server keeps it ~1h).
  const reopenRecentProject = async (entry) => {
    if (!entry?.jobId || reopeningRecent) return;
    if (entry.jobId === jobId && status !== 'idle') return; // already open
    setReopeningRecent(entry.jobId);
    try {
      if (isManaged || entry.managed) {
        await restoreProject(entry.jobId);
      } else {
        const data = await pollJob(entry.jobId);
        if (!data || (data.status !== 'completed' && !data.result)) {
          throw new Error('expired');
        }
        flushClipState();
        setProjectState(null);
        setNoSource(false);
        setJobId(entry.jobId);
        setResults(data.result || null);
        setLogs(['♻️ Reopened a recent project.']);
        setProcessingMedia({ type: 'server', payload: `/api/source/${entry.jobId}` });
        setQualityGate(null);
        setStatus('complete');
        setActiveTab('dashboard');
        rememberProject({ ...entry });
      }
    } catch (e) {
      forgetProject(entry.jobId);
      alert('This project is no longer available — it may have expired.');
    } finally {
      setReopeningRecent(null);
    }
  };

  // Apply one subtitle style to every clip of the job, sequentially.
  const handleBulkSubtitles = async (options) => {
    const clips = results?.clips || [];
    const total = clips.length;
    if (!total) return;
    setBulkSub({ running: true, current: 0, total, errors: 0 });
    let errors = 0;
    for (let i = 0; i < total; i++) {
      setBulkSub({ running: true, current: i + 1, total, errors });
      try {
        const res = await apiFetch('/api/subtitle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: jobId,
            clip_index: i,
            position: options.position,
            font_size: options.fontSize,
            font_name: options.fontName,
            font_color: options.fontColor,
            border_color: options.borderColor,
            border_width: options.borderWidth,
            bg_color: options.bgColor,
            bg_opacity: options.bgOpacity,
            style: options.style || 'classic',
            highlight_color: options.highlightColor || '#FFD700',
            effect: options.effect || 'none',
            base_opacity: options.baseOpacity ?? 1.0,
            uppercase: options.uppercase || false,
            // Chain from the clip's current server file (its video_url basename).
            input_filename: (clips[i].video_url || '').split('/').pop(),
          }),
        });
        if (!res.ok) errors++;
      } catch {
        errors++;
      }
    }
    setBulkSub({ running: false, current: total, total, errors });
    // Refresh results so each ResultCard picks up its new subtitled video_url.
    try {
      const data = await pollJob(jobId);
      if (data.result) setResults(data.result);
    } catch { /* keep current results */ }
  };

  const handleDownloadAll = async () => {
    if (!jobId) return;
    setDownloadingAll(true);
    try {
      const res = await apiFetch(`/api/jobs/${jobId}/download-all`);
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `openshorts_clips_${(jobId || '').slice(0, 8)}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Download failed: ${e.message}`);
    } finally {
      setDownloadingAll(false);
    }
  };

  // Session Recovery: Restore on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(SESSION_KEY);
      if (!saved) return;
      const session = JSON.parse(saved);
      if (Date.now() - session.timestamp > SESSION_MAX_AGE) {
        localStorage.removeItem(SESSION_KEY);
        return;
      }
      if (session.jobId && session.status && session.status !== 'idle') {
        setJobId(session.jobId);
        setResults(session.results || null);
        // Restore the source preview. Older sessions (or uploads) saved no
        // media, so fall back to the backend-served source for this job —
        // except for reopened projects, whose source was never persisted.
        if (session.processingMedia) setProcessingMedia(session.processingMedia);
        else if (!session.noSource) setProcessingMedia({ type: 'server', payload: `/api/source/${session.jobId}` });
        if (session.noSource) setNoSource(true);
        if (session.projectState) setProjectState(session.projectState);
        if (session.activeTab) setActiveTab(session.activeTab);
        // If was processing, resume polling; if complete/error, just show results
        setStatus(session.status === 'processing' ? 'processing' : session.status);
        setSessionRecovered(true);
        setTimeout(() => setSessionRecovered(false), 5000);
      }
    } catch (e) {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  // Session Recovery: Save state changes
  useEffect(() => {
    if (status === 'idle') {
      localStorage.removeItem(SESSION_KEY);
      return;
    }
    try {
      // URL (YouTube) media serializes as-is. Uploaded 'file' media is a blob
      // that can't be persisted, so point the recovered preview at the source
      // served by the backend instead of dropping it.
      let persistMedia = null;
      if (processingMedia?.type === 'url') persistMedia = processingMedia;
      else if (processingMedia && jobId) persistMedia = { type: 'server', payload: `/api/source/${jobId}` };
      const sessionData = {
        jobId,
        status,
        results,
        processingMedia: persistMedia,
        activeTab,
        noSource,
        projectState,
        timestamp: Date.now()
      };
      localStorage.setItem(SESSION_KEY, JSON.stringify(sessionData));
    } catch (e) {
      // localStorage full or serialization error - ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, status, results, activeTab, noSource, projectState]);

  // Record every completed job into the Recent menu so "New Project" never
  // strands the user's finished work.
  useEffect(() => {
    if (status === 'complete' && jobId && results?.clips?.length) {
      rememberProject({
        jobId,
        title: results.clips[0]?.title || '',
        clipCount: results.clips.length,
        managed: isManaged,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, jobId, results]);

  useEffect(() => {
    // Encrypt Gemini Key too for consistency if desired, but user asked specifically about Social integration not saving well.
    // For now keeping gemini plain for compatibility unless requested.
    if (apiKey) localStorage.setItem('gemini_key', apiKey);
  }, [apiKey]);

  useEffect(() => {
    if (llmConfig) localStorage.setItem('llm_config', JSON.stringify(llmConfig));
  }, [llmConfig]);

  useEffect(() => {
    if (uploadPostKey) {
      localStorage.setItem('uploadPostKey_v3', encrypt(uploadPostKey));
    }
    if (uploadUserId) {
      localStorage.setItem('uploadUserId', uploadUserId);
    }
  }, [uploadPostKey, uploadUserId]);

  useEffect(() => {
    if (elevenLabsKey) {
      localStorage.setItem('elevenLabsKey_v1', encrypt(elevenLabsKey));
    }
  }, [elevenLabsKey]);

  useEffect(() => {
    if (falKey) {
      localStorage.setItem('falKey_v1', encrypt(falKey));
    }
  }, [falKey]);

  useEffect(() => {
    if ((uploadPostKey || isManaged) && userProfiles.length === 0) {
      fetchUserProfiles({ silent: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadPostKey, isManaged]);

  // For managed users, fetch the durable R2 URLs of the current job's clips so the
  // preview can fall back to them when the local files have been cleaned up.
  useEffect(() => {
    if (!isManaged || !jobId || !(results?.clips?.length)) { setDurableClips({}); return; }
    let cancelled = false;
    apiJson('/api/history')
      .then((d) => {
        if (cancelled) return;
        const map = {};
        for (const v of (d.videos || [])) {
          if (v.job_id === jobId && v.clip_index != null) map[v.clip_index] = v.view_url;
        }
        setDurableClips(map);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isManaged, jobId, results]);

  useEffect(() => {
    let interval;
    if ((status === 'processing' || status === 'completed') && jobId) {
      interval = setInterval(async () => {
        try {
          const data = await pollJob(jobId);
          console.log("Job status:", data);

          // Update results if available (real-time)
          if (data.result) {
            setResults(data.result);
          }

          if (data.status === 'completed') {
            setStatus('complete');
            clearInterval(interval);
          } else if (data.status === 'failed') {
            setStatus('error');
            const errorMsg = data.error || (data.logs && data.logs.length > 0 ? data.logs[data.logs.length - 1] : "Process failed");
            setLogs(prev => [...prev, "Error: " + errorMsg]);
            clearInterval(interval);
          } else {
            // Update logs if available
            if (data.logs) setLogs(data.logs);
          }
        } catch (e) {
          console.error("Polling error", e);
        }
      }, 2000);
    }
    return () => clearInterval(interval);
  }, [status, jobId]);


  // silent: background auto-fetch — never alert(), just log. Managed users need
  // no local key (the server resolves its own); BYOK sends the header.
  const fetchUserProfiles = async ({ silent = false } = {}) => {
    if (!uploadPostKey && !isManaged) return;
    try {
      const res = await apiFetch('/api/social/user', {
        headers: uploadPostKey ? { 'X-Upload-Post-Key': uploadPostKey } : {}
      });
      if (!res.ok) throw new Error("Failed to fetch");
      const data = await res.json();
      if (data.profiles && data.profiles.length > 0) {
        setUserProfiles(data.profiles);
        // Auto select first if none selected
        if (!uploadUserId) {
          setUploadUserId(data.profiles[0].username);
        }
      } else if (!silent) {
        alert("No profiles found for this API Key.");
      }
    } catch (e) {
      if (!silent) alert("Error fetching User Profiles. Please check key.");
      console.error(e);
    }
  };

  // Hosted is paid-only (no BYOK core). Self-host uses BYOK keys.
  // `keysMissing` now means "self-host BYOK keys missing" — it never fires on hosted.
  // Two INDEPENDENT keys (self-host BYOK): a general provider (OmniRoute) for clip
  // generation, and a Gemini key required by Auto Edit (Files API / multimodal).
  // Legacy: a Gemini key alone still counts as a general provider (resolve_llm_env's
  // default branch falls back to Gemini), so existing Gemini-only users keep working.
  const hasGeneralProvider = llmConfig?.provider === 'openai_compat'
    ? !!(llmConfig.baseUrl && llmConfig.model)
    : !!apiKey;
  const hasGeminiKey = !!apiKey;
  const keysMissing = !billingEnabled && (!hasGeneralProvider || !hasGeminiKey || !uploadPostKey);
  const needsPlan = billingEnabled && !isManaged;   // hosted, signed-out or no active plan/trial

  // Fresh sign-up: show the welcome plan-choice popup once (AuthContext set the
  // flag after the auth redirect). Fires for free users too, so it's gated on
  // being signed in rather than on entitlement.
  useEffect(() => {
    if (billingEnabled && isSignedIn) {
      let flagged = false;
      try { flagged = localStorage.getItem('os_show_plan_choice') === '1'; } catch (_) { /* ignore */ }
      if (flagged) {
        setShowPlanChoice(true);
        try { localStorage.removeItem('os_show_plan_choice'); } catch (_) { /* ignore */ }
      }
    }
  }, [billingEnabled, isSignedIn]);

  // One probe at mount. publishingHealth() swallows its own errors and reports
  // `enabled: false`, so a deployment without publishing simply hides it.
  useEffect(() => {
    let cancelled = false;
    publishingHealth().then((h) => { if (!cancelled) setPublishingOn(!!h.enabled); });
    return () => { cancelled = true; };
  }, []);

  // Included in the plan (fully managed, no keys): Clip Generator + YouTube Studio.  // Advanced (bring your own fal.ai + ElevenLabs keys): AI Shorts + AI Agent.
  const INCLUDED_TOOL_TABS = ['dashboard', 'thumbnails'];
  const ADVANCED_TOOL_TABS = ['saasshorts', 'ai-agent'];
  const TOOL_NAMES = { dashboard: 'the Clip Generator', thumbnails: 'the YouTube Studio' };
  const gateThisTab = needsPlan && INCLUDED_TOOL_TABS.includes(activeTab);      // included tool, no plan yet
  const advancedThisTab = billingEnabled && ADVANCED_TOOL_TABS.includes(activeTab); // BYOK-notice tools

  // Managed users connect their socials via Upload-Post's branded hosted page.
  const handleConnectSocials = async () => {
    try {
      const { access_url } = await apiJson('/api/social/connect', { method: 'POST' });
      // Same tab so the connect page's redirectUrl brings the user back into the app.
      if (access_url) window.location.href = access_url;
    } catch (e) {
      alert('Could not open the connection page. Please try again.');
    }
  };

  // Open the Upload-Post white-label page (which includes the scheduling calendar)
  // in a new tab, for consulting/managing scheduled posts from the dashboard.
  const handleOpenCalendar = async () => {
    try {
      const { access_url } = await apiJson('/api/social/connect', { method: 'POST' });
      if (access_url) window.open(access_url, '_blank', 'noopener');
    } catch (e) {
      alert('Could not open the calendar. Please try again.');
    }
  };

  const handleProcess = async (data, forceLowQuality = false) => {
    // Hosted: must be signed in AND on an active plan/trial. Self-host: BYOK keys.
    if (billingEnabled) {
      if (!isSignedIn) { setShowLogin(true); return; }
      if (!isManaged) { window.location.hash = '#/pricing'; return; }
    } else if (keysMissing) {
      setShowKeyModal(true);
      return;
    }
    setStatus('processing');
    setLogs(["Starting process..."]);
    setResults(null);
    setProcessingMedia(data);
    setQualityGate(null);
    setProjectState(null);
    setNoSource(false);

    try {
      let body;
      // Route clip generation to the configured provider. OmniRoute → X-LLM-*
      // (don't also send X-Gemini-Key here; Auto Edit handles its own Gemini key
      // separately). Legacy Gemini-only users fall back to X-Gemini-Key for the
      // general pipeline. Managed users rely on the bearer token apiFetch sets.
      const headers = {};
      if (llmConfig?.provider === 'openai_compat') {
        headers['X-LLM-Provider'] = 'openai_compat';
        headers['X-LLM-Base-URL'] = llmConfig.baseUrl || '';
        headers['X-LLM-Key'] = llmConfig.apiKey || '';
        headers['X-LLM-Model'] = llmConfig.model || '';
      } else if (apiKey) {
        headers['X-Gemini-Key'] = apiKey;
      }

      // Upload progress callback for file uploads
      const onProgress = data.type === 'file' ? (progress) => {
        setLogs([`Uploading video... ${progress.percent}% (${Math.round(progress.loaded / 1024 / 1024)}MB / ${Math.round(progress.total / 1024 / 1024)}MB)`]);
      } : null;

      if (data.type === 'url') {
        headers['Content-Type'] = 'application/json';
        const jsonBody = {
          url: data.payload,
          acknowledged: !!data.acknowledged,
          output_format: data.outputFormat || 'auto',
          reframe_mode: data.reframeMode || 'auto',
          clip_duration_mode: data.clipDurationMode || 'auto',
          force_low_quality: forceLowQuality,
        };
        const brandingDefaults = loadBrandingDefaults();
        if (brandingDefaults?.enabled && brandingDefaults?.logoDataUrl) {
          jsonBody.branding = {
            logo: {
              enabled: true,
              position: brandingDefaults.position || 'bottom_right',
              size_pct: brandingDefaults.size_pct ?? 15,
              opacity: brandingDefaults.opacity ?? 1,
              margin_px: brandingDefaults.margin_px ?? 20,
              logo_image_data: brandingDefaults.logoDataUrl,
            },
          };
        }
        body = JSON.stringify(jsonBody);
      } else {
        const formData = new FormData();
        formData.append('file', data.payload);
        formData.append('acknowledged', data.acknowledged ? 'true' : 'false');
        formData.append('output_format', data.outputFormat || 'auto');
        formData.append('reframe_mode', data.reframeMode || 'auto');
        formData.append('clip_duration_mode', data.clipDurationMode || 'auto');
        const brandingDefaults = loadBrandingDefaults();
        if (brandingDefaults?.enabled && brandingDefaults?.logoDataUrl) {
          formData.append('branding', JSON.stringify({
            logo: {
              enabled: true,
              position: brandingDefaults.position || 'bottom_right',
              size_pct: brandingDefaults.size_pct ?? 15,
              opacity: brandingDefaults.opacity ?? 1,
              margin_px: brandingDefaults.margin_px ?? 20,
              logo_image_data: brandingDefaults.logoDataUrl,
            },
          }));
        }
        body = formData;
      }

      const res = await apiFetch('/api/process', { method: 'POST', headers, body, onProgress });

      if (!res.ok) throw new Error(await res.text());
      const resData = await res.json();

      // Quality gate: the source is below the min resolution — ask before burning
      // 20 min on it. On confirm we resend with force_low_quality.
      if (resData.needs_confirmation) {
        setStatus('idle');
        setQualityGate({ info: resData.quality_check, data });
        return;
      }

      setJobId(resData.job_id);
      setLogs(["Upload complete. Starting video processing..."]);

    } catch (e) {
      if (e instanceof QuotaError) {
        setStatus('idle');
        // Trial users hit the trial minute cap → prompt them to activate the plan
        // now (unlocks full minutes). Active users → offer a top-up.
        if (me?.status === 'trialing') {
          setShowTrialUpgrade(true);
        } else {
          setTopUpInfo({ required: e.minutesRequired, remaining: e.minutesRemaining });
          setShowTopUp(true);
        }
        return;
      }
      setStatus('error');
      setLogs(l => [...l, `Error starting job: ${e.message}`]);
    }
  };

  const handleReset = () => {
    // Flush any pending edit-state sync before dropping the project: the clips
    // themselves are already archived to R2 as they were edited.
    flushClipState();
    setStatus('idle');
    setJobId(null);
    setResults(null);
    setLogs([]);
    setProcessingMedia(null);
    setProjectState(null);
    setNoSource(false);
    localStorage.removeItem(SESSION_KEY);
  };

  // --- UI Components ---

  const Sidebar = () => {
    const navItems = [
      { id: 'dashboard', ord: '01', icon: LayoutDashboard, label: 'Clip Generator' },
      { id: 'saasshorts', ord: '02', icon: Sparkles, label: 'AI Shorts', byok: true },
      { id: 'ai-agent', ord: '03', icon: Bot, label: 'AI Agent', byok: true },
      { id: 'ugc-gallery', ord: '04', icon: LayoutGrid, label: 'UGC Gallery' },
      { id: 'thumbnails', ord: '05', icon: Image, label: 'YouTube Studio' },
      ...(publishingOn ? [{ id: 'publishing', ord: '06', icon: Send, label: 'Publishing' }] : []),
      ...(billingEnabled && isSignedIn ? [{ id: 'history', ord: '07', icon: History, label: 'History' }] : []),
      { id: 'settings', ord: '08', icon: Settings, label: 'Settings' },
    ];

    return (
      <div className="w-20 lg:w-64 bg-paper2 border-r border-rule flex flex-col h-full shrink-0 transition-all duration-300">
        <a href="#landing" className="p-6 flex items-center gap-3" title="go to landing page">
          <div className="w-8 h-8 bg-paper3 rounded-input flex items-center justify-center shrink-0 overflow-hidden border border-rule">
            <img src="/logo-openshorts.png" alt="Logo" className="w-full h-full object-cover" />
          </div>
          <span className="font-display lowercase text-lg text-ink hidden lg:block">openshorts</span>
        </a>

        <nav className="flex-1 px-4 py-4 space-y-1">
          {navItems.map((item) => {
            const NavIcon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => setActiveTab(item.id)}
                className={`relative w-full flex items-center gap-3 px-3 py-2.5 rounded-input transition-colors ${isActive ? 'bg-paper3 text-ink' : 'text-muted hover:text-ink2 hover:bg-paper3/50'}`}
              >
                {isActive && (
                  <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 bg-brass rounded-full" aria-hidden="true" />
                )}
                <NavIcon size={18} className={`shrink-0 ${isActive ? 'text-brass' : ''}`} />
                <span className="text-sm lowercase hidden lg:block flex-1 text-left truncate">{item.label}</span>
                {item.byok && <span className="readout hidden lg:block">BYOK</span>}
                <span className="readout hidden lg:block">{item.ord}</span>
              </button>
            );
          })}
        </nav>

        <div className="p-4 border-t border-rule space-y-1">
          <a
            href="#landing"
            className="flex items-center gap-2 px-3 py-1.5 text-xs lowercase text-muted hover:text-ink2 transition-colors"
          >
            <Globe size={14} className="shrink-0" />
            <span className="hidden lg:block truncate">landing page</span>
          </a>
          <a
            href="https://github.com/mutonby/openshorts"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 px-3 py-1.5 text-xs lowercase text-muted hover:text-ink2 transition-colors"
          >
            <svg height="14" viewBox="0 0 16 16" version="1.1" width="14" aria-hidden="true" fill="currentColor" className="shrink-0"><path fillRule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path></svg>
            <span className="hidden lg:block truncate">open source</span>
          </a>
          {billingEnabled && (
            <a
              href="#/pricing"
              className="flex items-center gap-2 px-3 py-1.5 text-xs lowercase text-muted hover:text-ink2 transition-colors"
            >
              <Sparkles size={14} className="shrink-0" />
              <span className="hidden lg:block truncate">plans &amp; pricing</span>
            </a>
          )}
          <a
            href="mailto:info@openshorts.app"
            className="flex items-center gap-2 px-3 py-1.5 text-xs lowercase text-muted hover:text-ink2 transition-colors"
          >
            <Mail size={14} className="shrink-0" />
            <span className="hidden lg:block truncate">info@openshorts.app</span>
          </a>
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-screen bg-paper overflow-hidden">
      <Sidebar />

      <main className="flex-1 flex flex-col h-full overflow-hidden relative">
        {/* Top Header */}
        <header className="h-14 border-b border-rule bg-paper flex items-center justify-between px-6 shrink-0 z-10">
          <div className="flex items-center gap-4">
            {status !== 'idle' && (
              <button
                onClick={handleReset}
                className="btn-quiet px-3 py-1.5 text-xs"
              >
                <Plus size={14} />
                <span className="hidden sm:inline">New Project</span>
              </button>
            )}
            {activeTab === 'dashboard' && (
              <RecentProjectsMenu
                projects={recentProjects}
                currentJobId={jobId}
                reopeningId={reopeningRecent}
                onReopen={reopenRecentProject}
              />
            )}
          </div>

          <div className="flex items-center gap-4">
            {userProfiles.length > 0 && (
              <UserProfileSelector
                profiles={userProfiles}
                selectedUserId={uploadUserId}
                onSelect={setUploadUserId}
              />
            )}

            {/* Cloud: minutes meter + account/sign-in */}
            {billingEnabled && isManaged && (
              <UsageMeter onClick={() => { window.location.hash = '#/account'; }} />
            )}
            {billingEnabled && isSignedIn && !isManaged && (
              <button onClick={() => setShowPlanChoice(true)}
                className="btn-primary px-4 py-2 text-xs">
                Choose a plan
              </button>
            )}
            {billingEnabled && !isSignedIn && (
              <button onClick={() => setShowLogin(true)}
                className="btn-ghost px-4 py-2 text-xs">
                Sign in
              </button>
            )}
            {billingEnabled && isSignedIn && <ProfileMenu />}

            {keysMissing && (
              <button
                onClick={() => (billingEnabled && !isSignedIn ? setShowLogin(true) : setActiveTab('settings'))}
                className="badge-warn hover:brightness-125 transition-all"
                title="Configure API keys or choose a plan"
              >
                <AlertTriangle size={12} />
                <span className="hidden sm:inline">
                  {!apiKey && !uploadPostKey
                    ? 'Gemini & Upload-Post keys missing'
                    : !apiKey
                      ? 'Gemini API Key Missing'
                      : 'Upload-Post API Key Missing'}
                </span>
                <span className="sm:hidden">keys missing</span>
              </button>
            )}
          </div>
        </header>

        {/* Persistent Missing Keys Banner — visible on every screen */}
        {keysMissing && activeTab !== 'settings' && (
          <div className="mx-4 sm:mx-6 mt-3 px-4 py-3 bg-paper2 border border-rule rounded-card flex flex-wrap items-center justify-between gap-3 sm:gap-4 shrink-0 animate-fade">
            <div className="flex items-center gap-3 text-sm text-ink2">
              <KeyRound size={16} className="shrink-0 text-warn" />
              <div>
                <span className="font-medium text-ink">Required API keys missing.</span>{' '}
                <span className="text-muted">
                  {!apiKey && !uploadPostKey
                    ? 'Set your Gemini and Upload-Post API keys to use OpenShorts.'
                    : !apiKey
                      ? 'Set your Gemini API key to use OpenShorts.'
                      : 'Set your Upload-Post API key to use OpenShorts.'}
                </span>
              </div>
            </div>
            <button
              onClick={() => setActiveTab('settings')}
              className="btn-quiet px-3 py-1.5 text-xs shrink-0"
            >
              Go to Settings
            </button>
          </div>
        )}

        {/* Session Recovery Banner */}
        {sessionRecovered && (
          <div className="mx-6 mt-2 px-4 py-3 bg-paper2 border border-rule rounded-card flex items-center justify-between animate-fade shrink-0">
            <div className="flex items-center gap-2 text-sm text-ink2">
              <RotateCcw size={16} className="text-brass" />
              <span className="font-medium">Session recovered</span>
              <span className="text-muted text-xs">Your previous work has been restored.</span>
            </div>
            <button onClick={() => setSessionRecovered(false)} className="text-muted hover:text-ink transition-colors">
              <X size={14} />
            </button>
          </div>
        )}

        {/* Included tools (Clip Generator, YouTube Studio): non-blocking trial prompt. */}
        {gateThisTab && <TrialGate toolName={TOOL_NAMES[activeTab] || 'this'} />}

        {/* Advanced tools (AI Shorts, AI Agent): BYOK fal.ai + ElevenLabs notice. */}
        {advancedThisTab && <AdvancedBanner needsPlan={needsPlan} onKeys={() => setActiveTab('settings')} />}

        {/* Main Workspace */}
        <div className="flex-1 overflow-hidden relative">

          {/* View: Settings */}
          {activeTab === 'settings' && (
            <div className="h-full overflow-y-auto p-4 sm:p-8 max-w-2xl mx-auto animate-fade">
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 mb-8">
                <div>
                  <p className="eyebrow mb-1.5">07 · SETTINGS</p>
                  <h1 className="font-display lowercase text-2xl text-ink">Settings</h1>
                </div>
                <div className="flex items-center gap-2 text-xs text-muted mt-1">
                  <Shield size={12} className="text-ok shrink-0" /> Privacy: keys only live in your browser (sent to backend just to process)
                </div>
              </div>
              {isManaged ? (
                <div className="card p-6 mb-2">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-3">
                      <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                        <Shield size={16} className="text-brass" />
                      </div>
                      <h2 className="text-base font-medium text-ink lowercase">Included in your plan</h2>
                    </div>
                    <span className="badge-ok">Managed</span>
                  </div>
                  <p className="text-xs text-muted mb-5 leading-relaxed">
                    Your plan includes the <strong>Clip Generator</strong> and <strong>YouTube Studio</strong>,
                    fully managed — no API keys required. AI Shorts &amp; dubbing use your own fal.ai / ElevenLabs
                    keys (below). Connect your social accounts to publish directly.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={handleConnectSocials} className="btn-primary py-2 px-4 text-sm">
                      <Share2 size={16} /> Connect social accounts
                    </button>
                    <button onClick={handleOpenCalendar} className="btn-quiet py-2 px-4 text-sm">
                      <Calendar size={16} /> Content calendar
                    </button>
                  </div>
                </div>
              ) : billingEnabled ? (
                <div className="card p-6 mb-2">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-3">
                      <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                        <Sparkles size={16} className="text-brass" />
                      </div>
                      <h2 className="text-base font-medium text-ink lowercase">Choose your plan</h2>
                    </div>
                    <span className="badge-ok">Free plan available</span>
                  </div>
                  <p className="text-xs text-muted mb-5 leading-relaxed">
                    Generate shorts with zero setup — no API keys needed. Start free with 20 min/month, or go paid from $12/mo. Cancel anytime.
                  </p>
                  <button onClick={() => setShowPlanChoice(true)} className="btn-primary py-2 px-4 text-sm">
                    <Sparkles size={16} /> Choose a plan
                  </button>
                </div>
              ) : (
                <>
              <KeyInput onKeySet={setApiKey} savedKey={apiKey} llmConfig={llmConfig} onLlmConfigChange={setLlmConfig} />

              <div className="card p-4 sm:p-6 mt-8">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                      <Share2 size={16} className="text-brass" />
                    </div>
                    <h2 className="text-base font-medium text-ink lowercase">Social Integration</h2>
                  </div>
                  <span className="badge-warn">Required</span>
                </div>
                <p className="text-xs text-muted mb-6 leading-relaxed">
                  Required to publish your clips to TikTok, Instagram Reels, and YouTube Shorts via <strong>Upload-Post</strong>.
                  Includes a <strong>free tier</strong> (no credit card required).
                </p>
                <div className="space-y-4">
                  <label className="block text-sm text-muted">Upload-Post API Key</label>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <input
                      type="password"
                      value={uploadPostKey}
                      onChange={(e) => setUploadPostKey(e.target.value)}
                      className="input-field"
                      placeholder="ey..."
                    />
                    <button onClick={fetchUserProfiles} className="btn-quiet py-2 px-4 text-sm">
                      Connect
                    </button>
                  </div>
                  <p className="text-xs text-muted leading-relaxed">
                    Connect your Upload-Post account to enable one-click publishing.
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2">
                      <a href="https://app.upload-post.com/login" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">1. Login</span>
                        <span className="text-xs text-muted">Register account</span>
                      </a>
                      <a href="https://app.upload-post.com/manage-users" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">2. Profiles</span>
                        <span className="text-xs text-muted">Create & Connect</span>
                      </a>
                      <a href="https://app.upload-post.com/api-keys" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">3. API Key</span>
                        <span className="text-xs text-muted">Generate key</span>
                      </a>
                    </div>
                    <br />
                    <span className="text-muted">
                      Keys are only stored in your browser. They are sent to the backend only to process your request, never stored server-side.
                    </span>
                  </p>
                </div>
              </div>

                </>
              )}

              <div className="card p-4 sm:p-6 mt-8">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                      <Globe size={16} className="text-brass" />
                    </div>
                    <h2 className="text-base font-medium text-ink lowercase">Video Translation</h2>
                  </div>
                  <span className="readout">BYOK</span>
                </div>
                <p className="text-xs text-muted mb-6 leading-relaxed">
                  For <strong>AI Shorts &amp; dubbing</strong> — bring your own key. Translate your clips to different
                  languages using <strong>ElevenLabs</strong> AI dubbing (billed by ElevenLabs). Not covered by your plan.
                </p>
                <div className="space-y-4">
                  <label className="block text-sm text-muted">ElevenLabs API Key</label>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <input
                      type="password"
                      value={elevenLabsKey}
                      onChange={(e) => setElevenLabsKey(e.target.value)}
                      className="input-field"
                      placeholder="sk_..."
                    />
                    <button
                      onClick={() => {
                        if (elevenLabsKey) {
                          localStorage.setItem('elevenLabsKey_v1', encrypt(elevenLabsKey));
                          setElevenLabsSaved(true);
                          setTimeout(() => setElevenLabsSaved(false), 2000);
                        }
                      }}
                      className={elevenLabsSaved ? 'badge-ok px-4' : 'btn-quiet py-2 px-4 text-sm'}
                    >
                      {elevenLabsSaved ? <><Check size={12} /> saved</> : 'Save'}
                    </button>
                  </div>
                  <p className="text-xs text-muted leading-relaxed">
                    Get your API key from ElevenLabs to enable video translation.
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                      <a href="https://elevenlabs.io/sign-up" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">1. Sign Up</span>
                        <span className="text-xs text-muted">Create account</span>
                      </a>
                      <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">2. API Key</span>
                        <span className="text-xs text-muted">Generate key</span>
                      </a>
                    </div>
                    <br />
                    <span className="text-muted">
                      Keys are only stored in your browser. They are sent to the backend only to process your request, never stored server-side.
                    </span>
                  </p>
                </div>
              </div>

              <BrandingSettings />

              <div className="card p-4 sm:p-6 mt-8">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-input bg-paper3 flex items-center justify-center shrink-0">
                      <Sparkles size={16} className="text-brass" />
                    </div>
                    <h2 className="text-base font-medium text-ink lowercase">AI Shorts (UGC Videos)</h2>
                  </div>
                  <span className="readout">BYOK</span>
                </div>
                <p className="text-xs text-muted mb-6 leading-relaxed">
                  Generate UGC-style videos with AI actors for any product or business using <strong>fal.ai</strong>.
                  <strong> Not covered by your plan</strong> — bring your own fal.ai + ElevenLabs keys (billed by those
                  providers, ~$0.65-2 per video). Your plan still covers the AI script &amp; orchestration.
                </p>
                <div className="space-y-4">
                  <label className="block text-sm text-muted">fal.ai API Key</label>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <input
                      type="password"
                      value={falKey}
                      onChange={(e) => setFalKey(e.target.value)}
                      className="input-field"
                      placeholder="fal_..."
                    />
                    <button
                      onClick={() => {
                        if (falKey) {
                          localStorage.setItem('falKey_v1', encrypt(falKey));
                          setFalSaved(true);
                          setTimeout(() => setFalSaved(false), 2000);
                        }
                      }}
                      className={falSaved ? 'badge-ok px-4' : 'btn-quiet py-2 px-4 text-sm'}
                    >
                      {falSaved ? <><Check size={12} /> saved</> : 'Save'}
                    </button>
                  </div>
                  <p className="text-xs text-muted leading-relaxed">
                    Get your API key from fal.ai to enable AI actor video generation.
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                      <a href="https://fal.ai/dashboard/keys" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">1. Sign Up</span>
                        <span className="text-xs text-muted">Create fal.ai account</span>
                      </a>
                      <a href="https://fal.ai/dashboard/keys" target="_blank" rel="noopener noreferrer" className="p-2 border border-rule rounded-input hover:bg-paper3 transition-colors flex flex-col gap-1">
                        <span className="text-ink2 font-medium">2. API Key</span>
                        <span className="text-xs text-muted">Generate key</span>
                      </a>
                    </div>
                    <br />
                    <span className="text-muted">
                      Keys are only stored in your browser. Sent to backend only to process requests.
                    </span>
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* View: SaaS Shorts */}
          {activeTab === 'saasshorts' && (
            <SaaShortsTab geminiApiKey={apiKey} elevenLabsKey={elevenLabsKey} falKey={falKey} uploadPostKey={uploadPostKey} uploadUserId={uploadUserId} managed={isManaged} />
          )}

          {/* View: AI Agent (Autopilot) */}
          {activeTab === 'ai-agent' && (
            <AutopilotTab
              geminiApiKey={apiKey}
              llmConfig={llmConfig}
              elevenLabsKey={elevenLabsKey}
              uploadPostKey={uploadPostKey}
              uploadUserId={uploadUserId}
              isManaged={isManaged}
            />
          )}

          {/* View: UGC Gallery */}
          {activeTab === 'ugc-gallery' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="max-w-6xl mx-auto p-6 md:p-8">
                <UGCGallery />
              </div>
            </div>
          )}

          {/* View: History */}
          {activeTab === 'history' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="max-w-6xl mx-auto p-6 md:p-8">
                <HistoryTab onReopenProject={restoreProject} />
              </div>
            </div>
          )}

          {/* View: Publishing (automated posting) */}
          {activeTab === 'publishing' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="p-6 md:p-8">
                <PublishingTab />
              </div>
            </div>
          )}

          {activeTab === 'thumbnails' && (
            <ThumbnailStudio geminiApiKey={apiKey} uploadPostKey={uploadPostKey} uploadUserId={uploadUserId} managed={isManaged} />
          )}

          {/* View: Gallery */}
          {/* {activeTab === 'gallery' && (
            <Gallery />
          )} */}

          {/* View: Dashboard (Idle) */}
          {activeTab === 'dashboard' && status === 'idle' && (
            <div className="h-full overflow-y-auto custom-scrollbar animate-fade">
              <div className="min-h-full flex flex-col items-center justify-center px-4 py-6 sm:p-6">
              <div className="max-w-xl w-full text-center space-y-8">
                <div className="space-y-4">
                  <p className="eyebrow">01 · CLIP GENERATOR</p>
                  <h1 className="font-display lowercase text-4xl md:text-5xl text-ink">
                    Create Viral Shorts
                  </h1>
                  <p className="text-muted text-lg">
                    Drop your long-form video below to instantly generate viral clips with AI.
                  </p>
                </div>

                <MediaInput onProcess={handleProcess} isProcessing={status === 'processing'} />
                <RenderOptionsPanel />

                <div className="flex flex-wrap items-center justify-center gap-4 sm:gap-8 text-muted text-sm">
                  <span className="flex items-center gap-2"><Youtube size={16} /> YouTube</span>
                  <span className="flex items-center gap-2"><Instagram size={16} /> Instagram</span>
                  <span className="flex items-center gap-2"><TikTokIcon size={16} /> TikTok</span>
                </div>
              </div>
              </div>
            </div>
          )}

          {/* View: Processing / Results (Split View) */}
          {activeTab === 'dashboard' && (status === 'processing' || status === 'complete' || status === 'error') && (
            <div className="h-full flex flex-col md:flex-row gap-4 p-4 overflow-y-auto md:overflow-y-hidden custom-scrollbar animate-fade">

              {/* Left Panel: Preview & Status */}
              <div className={`${status === 'complete' ? 'w-full md:w-[30%] lg:w-[25%]' : 'w-full md:w-[55%] lg:w-[60%]'} md:h-full flex flex-col shrink-0 md:shrink card p-4 sm:p-6 overflow-y-auto custom-scrollbar transition-all duration-700 ease-in-out`}>
                <div className="mb-6 flex items-center justify-between">
                  <h2 className="text-sm font-medium text-ink lowercase flex items-center gap-2">
                    <Activity className={`text-brass ${status === 'processing' ? 'animate-pulse' : ''}`} size={18} />
                    Live Analysis
                  </h2>
                  <span className={status === 'processing' ? 'badge-brass' :
                    status === 'complete' ? 'badge-ok' :
                      'badge-danger'
                    }>
                    {status.toUpperCase()}
                  </span>
                </div>

                {/* Video Preview */}
                {processingMedia && (
                  <ProcessingAnimation
                    media={processingMedia}
                    isComplete={status === 'complete'}
                    syncedTime={syncedTime}
                    isSyncedPlaying={isSyncedPlaying}
                    syncTrigger={syncTrigger}
                  />
                )}

                {/* The wait is dead time — the best moment to ask for a star. */}
                {status === 'processing' && (
                  <div className="my-3">
                    <StarBanner message="Free while it renders?" />
                  </div>
                )}

                {/* Logs Terminal */}
                <div className={`bg-paper rounded-card border border-rule overflow-hidden flex flex-col transition-all duration-500 ${status === 'complete' ? 'h-32 min-h-0 opacity-50 hover:opacity-100' : 'flex-1 min-h-[200px]'}`}>
                  <div className="px-4 py-2 border-b border-rule flex items-center justify-between bg-paper2 shrink-0">
                    <span className="readout flex items-center gap-2">
                      <Terminal size={12} /> System Logs
                    </span>
                    <button onClick={() => setLogsVisible(!logsVisible)} className="text-muted hover:text-ink transition-colors">
                      {logsVisible ? <ChevronDown size={14} /> : <ChevronDown size={14} className="rotate-180" />}
                    </button>
                  </div>
                  {logsVisible && (
                    <div className="flex-1 p-4 overflow-y-auto font-mono text-xs space-y-1.5 custom-scrollbar text-muted">
                      {logs.map((log, i) => (
                        <div key={i} className={`flex gap-2 ${log.toLowerCase().includes('error') ? 'text-danger' : 'text-muted'}`}>
                          <span className="text-muted opacity-50 shrink-0">{new Date().toLocaleTimeString()}</span>
                          <span>{log}</span>
                        </div>
                      ))}
                      {status === 'processing' && (
                        <div className="animate-pulse text-brass">_</div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* Right Panel: Results Grid */}
              <div className={`${status === 'complete' ? 'w-full md:w-[70%] lg:w-[75%]' : 'w-full md:w-[45%] lg:w-[40%]'} md:h-full flex flex-col shrink-0 md:shrink card p-4 sm:p-6 transition-all duration-700 ease-in-out`}>
                <h2 className="font-display lowercase text-xl text-ink mb-6 flex flex-wrap items-center gap-2 shrink-0">
                  Generated Shorts
                  {results?.clips?.length > 0 && (
                    <span className="readout bg-paper3 px-2.5 py-1 rounded-full ml-auto">
                      {results.clips.length} Clips
                    </span>
                  )}
                  {results?.cost_analysis && !isManaged && (
                    <span className="readout bg-paper3 px-2.5 py-1 rounded-full ml-2" title={`Input: ${results.cost_analysis.input_tokens} | Output: ${results.cost_analysis.output_tokens}`}>
                      GEMINI · ${results.cost_analysis.total_cost.toFixed(5)}
                    </span>
                  )}
                  {results?.clips?.length > 0 && status === 'complete' && (
                    <div className="flex items-center gap-2 ml-auto">
                      <button
                        onClick={handleDownloadAll}
                        disabled={downloadingAll}
                        className="btn-ghost px-3 py-2 text-xs"
                        title="Download all clips as a ZIP"
                      >
                        {downloadingAll
                          ? <><Loader2 size={14} className="animate-spin" />zipping…</>
                          : <><Download size={14} />download all</>}
                      </button>
                      {results.clips.length > 1 && (
                        <button
                          onClick={() => setShowScheduleWeek(true)}
                          className="btn-primary px-4 py-2 text-xs"
                        >
                          <Calendar size={14} />
                          schedule week
                        </button>
                      )}
                      {publishingOn && (
                        <button
                          onClick={() => setShowPublish(true)}
                          className="btn-ghost px-4 py-2 text-xs"
                          title="Post these clips to your connected accounts, spaced out"
                        >
                          <Send size={14} />
                          auto-post
                        </button>
                      )}
                    </div>
                  )}
                </h2>

                {status === 'complete' && results?.clips?.length > 0 && (
                  <div className="mb-2 space-y-2">
                    <StarBanner message="Happy with your clips?" />
                    {plan === 'free' && (
                      <button
                        onClick={() => { window.location.hash = '#/pricing'; }}
                        className="w-full text-left px-3 py-2 rounded-input bg-paper3 border border-rule text-sm text-muted hover:text-ink transition-colors"
                      >
                        Free clips carry a watermark and expire in 7 days — <span className="text-brass">upgrade to remove both</span>.
                      </button>
                    )}
                    <BatchPipeline
                      jobId={jobId}
                      clipCount={results.clips.length}
                      apiKey={apiKey}
                      elevenLabsKey={elevenLabsKey}
                      previewVideoUrl={results.clips[0]?.video_url}
                      onComplete={async () => {
                        try {
                          const data = await pollJob(jobId);
                          if (data.result) setResults(data.result);
                        } catch { /* keep current */ }
                      }}
                    />
                  </div>
                )}

                <div className="flex-1 overflow-y-auto custom-scrollbar p-1">
                  {results && results.clips && results.clips.length > 0 ? (
                    <div className={`grid gap-4 pb-10 ${status === 'complete' ? 'grid-cols-1 xl:grid-cols-2' : 'grid-cols-1'}`}>
                      {results.clips.map((clip, i) => (
                        <ResultCard
                          key={`${jobId}-${i}`}
                          clip={clip}
                          index={i}
                          jobId={jobId}
                          initialState={projectState?.clips?.find((c) => c.index === i) || null}
                          onStateChange={handleClipStateChange}
                          durableUrl={durableClips[i]}
                          uploadPostKey={uploadPostKey}
                          uploadUserId={uploadUserId}
                          geminiApiKey={apiKey}
                          elevenLabsKey={elevenLabsKey}
                          isManaged={isManaged}
                          connectedPlatforms={(userProfiles.find((p) => p.username === uploadUserId) || userProfiles[0])?.connected ?? null}
                          onConnectSocials={isManaged ? handleConnectSocials : null}
                          onPlay={(time) => handleClipPlay(time)}
                          onPause={handleClipPause}
                          onBulkSubtitle={handleBulkSubtitles}
                          clipCount={results.clips.length}
                          bulkProgress={bulkSub}
                        />
                      ))}
                    </div>
                  ) : (
                    status === 'processing' ? (
                      <div className="h-full flex flex-col items-center justify-center text-muted space-y-4">
                        <Loader2 size={32} className="animate-spin text-brass" />
                        <p className="text-sm lowercase">Waiting for clips...</p>
                      </div>
                    ) : status === 'error' ? (
                      <div className="h-full flex flex-col items-center justify-center text-danger space-y-2">
                        <p>Generation failed.</p>
                      </div>
                    ) : null
                  )}
                </div>
              </div>

            </div>
          )}

        </div>

      </main>

      {/* Missing API Key Modal */}
      <Modal
        isOpen={showKeyModal}
        onClose={() => setShowKeyModal(false)}
        eyebrow="SETUP"
        title={!hasGeneralProvider && !apiKey && !uploadPostKey
          ? 'Required API Keys Missing'
          : (!hasGeneralProvider && !apiKey)
            ? 'LLM Provider Missing'
            : !apiKey
              ? 'Gemini API Key Required'
              : 'Upload-Post API Key Required'}
        footer={
          <div className="flex gap-3">
            <button
              onClick={() => setShowKeyModal(false)}
              className="btn-ghost flex-1 px-4 py-2 text-sm"
            >
              Cancel
            </button>
            <button
              onClick={() => { setShowKeyModal(false); setActiveTab('settings'); }}
              className="btn-primary flex-1 px-4 py-2 text-sm"
            >
              Go to Settings
            </button>
          </div>
        }
      >
        <div className="space-y-4">
          <p className="text-sm text-muted">
            OpenShorts needs an <strong className="text-ink2">LLM provider</strong> (OmniRoute or a Gemini key) for clip generation, a separate <strong className="text-ink2">Gemini</strong> key for Auto Edit, and an <strong className="text-ink2">Upload-Post</strong> key for posting. All have free tiers.
          </p>

          {!hasGeneralProvider && (
            <div className="rounded-input p-4 space-y-2 border border-rule2">
              <p className="text-xs font-medium text-ink flex items-center gap-2">
                <AlertTriangle size={12} className="text-warn" />
                General LLM Provider (clip generation)
              </p>
              <p className="text-xs text-muted">
                Set an OmniRoute / OpenAI-compatible endpoint (Base URL + Model) in Settings — or just set the Gemini key below, which also works as the general provider.
              </p>
            </div>
          )}

          {/* Gemini block */}
          <div className={`rounded-input p-4 space-y-2 border ${!apiKey ? 'border-rule2' : 'border-rule opacity-70'}`}>
            <p className="text-xs font-medium text-ink flex items-center gap-2">
              {apiKey ? <Check size={12} className="text-ok" /> : <AlertTriangle size={12} className="text-warn" />}
              Gemini API Key {apiKey && <span className="text-ok">— set</span>}
            </p>
            {!apiKey && (
              <>
                <ol className="text-xs text-muted space-y-1 list-decimal list-inside">
                  <li>Go to <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener noreferrer" className="text-brass underline">aistudio.google.com/app/apikey</a></li>
                  <li>Sign in with your Google account</li>
                  <li>Click "Create API Key"</li>
                  <li>Copy the key and paste it below</li>
                </ol>
                <input
                  type="text"
                  placeholder="Paste your Gemini API key here..."
                  className="input-field"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && e.target.value.trim()) {
                      setApiKey(e.target.value.trim());
                    }
                  }}
                />
              </>
            )}
          </div>

          {/* Upload-Post block */}
          <div className={`rounded-input p-4 space-y-2 border ${!uploadPostKey ? 'border-rule2' : 'border-rule opacity-70'}`}>
            <p className="text-xs font-medium text-ink flex items-center gap-2">
              {uploadPostKey ? <Check size={12} className="text-ok" /> : <AlertTriangle size={12} className="text-warn" />}
              Upload-Post API Key {uploadPostKey && <span className="text-ok">— set</span>}
            </p>
            {!uploadPostKey && (
              <>
                <p className="text-xs text-muted">
                  Required to publish your clips to TikTok, Instagram Reels, and YouTube Shorts. Free tier available, no credit card needed.
                </p>
                <ol className="text-xs text-muted space-y-1 list-decimal list-inside">
                  <li>Register at <a href="https://app.upload-post.com/login" target="_blank" rel="noopener noreferrer" className="text-brass underline">app.upload-post.com</a></li>
                  <li>Connect your TikTok, Instagram, or YouTube accounts</li>
                  <li>Go to <a href="https://app.upload-post.com/api-keys" target="_blank" rel="noopener noreferrer" className="text-brass underline">API Keys</a> and generate one</li>
                  <li>Paste it below</li>
                </ol>
                <input
                  type="text"
                  placeholder="Paste your Upload-Post API key here..."
                  className="input-field"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && e.target.value.trim()) {
                      setUploadPostKey(e.target.value.trim());
                    }
                  }}
                />
              </>
            )}
          </div>
        </div>
      </Modal>

      <ScheduleWeekModal
        isOpen={showScheduleWeek}
        onClose={() => setShowScheduleWeek(false)}
        clips={results?.clips || []}
        jobId={jobId}
        uploadPostKey={uploadPostKey}
        uploadUserId={uploadUserId}
        isManaged={isManaged}
      />

      {/* Automated publishing: whole-job mode (no clip_index) */}
      {publishingOn && showPublish && (
        <PublishModal
          isOpen={showPublish}
          onClose={() => setShowPublish(false)}
          jobId={jobId}
          clipCount={results?.clips?.length || 0}
        />
      )}

      {/* Pre-flight quality gate */}
      {qualityGate && (
        <Modal isOpen={true} onClose={() => setQualityGate(null)} size="md" eyebrow="HEADS UP" title="low source quality">
          <div className="space-y-4">
            <p className="text-sm text-ink2">
              YouTube only offers <span className="text-brass font-semibold">{qualityGate.info.max_height}p</span> for this video
              (below the {qualityGate.info.min_height}p we recommend). Processing anyway will produce lower-quality clips.
            </p>
            {qualityGate.info.cookies_invalid && (
              <p className="text-xs text-muted">
                Your YouTube cookies look expired — refreshing them (export again from an incognito window) often unlocks HD.
              </p>
            )}
            <div className="flex gap-2 justify-end pt-2">
              <button onClick={() => setQualityGate(null)} className="btn-ghost">cancel</button>
              <button
                onClick={() => { const d = qualityGate.data; setQualityGate(null); handleProcess(d, true); }}
                className="btn-primary"
              >
                process anyway
              </button>
            </div>
          </div>
        </Modal>
      )}


      {showLogin && <LoginModal onClose={() => setShowLogin(false)} />}
      {showPlanChoice && <PlanChoiceModal onClose={() => setShowPlanChoice(false)} />}
      {showTopUp && (
        <TopUpModal
          onClose={() => setShowTopUp(false)}
          required={topUpInfo.required}
          remaining={topUpInfo.remaining}
        />
      )}
      {showTrialUpgrade && (
        <TrialUpgradeModal
          plan={plan}
          onActivated={refreshMe}
          onClose={() => setShowTrialUpgrade(false)}
        />
      )}
    </div>
  );
}

export default App;
