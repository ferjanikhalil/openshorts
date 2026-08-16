import React, { useState, useEffect } from 'react';
import { Key, Eye, EyeOff, Check, Server, Sparkles } from 'lucide-react';

// Two INDEPENDENT settings:
//  - General LLM Provider (OmniRoute / OpenAI-compatible): powers clip generation
//    and all standard LLM requests. Backs `llm_config` in localStorage.
//  - Gemini API Key: used ONLY by Auto Edit (Files API / multimodal, which OmniRoute
//    can't serve) plus the untouched Thumbnail/SaaShorts endpoints. Backs
//    `gemini_key` in localStorage.
// The two are decoupled: configuring OmniRoute no longer clobbers the Gemini key
// (which was the source of Auto Edit's API_KEY_INVALID).
export default function KeyInput({ onKeySet, savedKey, llmConfig, onLlmConfigChange }) {
    // Gemini key (Auto Edit) — independent state, persisted to `gemini_key`.
    const [geminiKey, setGeminiKey] = useState(savedKey || '');
    const [geminiVisible, setGeminiVisible] = useState(false);
    const [geminiSaved, setGeminiSaved] = useState(!!savedKey);

    // General provider (OmniRoute / OpenAI-compatible) — persisted to `llm_config`.
    const [baseUrl, setBaseUrl] = useState(llmConfig?.baseUrl || '');
    const [model, setModel] = useState(llmConfig?.model || '');
    const [providerKey, setProviderKey] = useState(llmConfig?.apiKey || '');
    const [providerVisible, setProviderVisible] = useState(false);
    const [providerSaved, setProviderSaved] = useState(!!(llmConfig?.baseUrl && llmConfig?.model));

    useEffect(() => {
        if (savedKey !== undefined && savedKey !== null) {
            setGeminiKey(savedKey);
            setGeminiSaved(!!savedKey);
        }
    }, [savedKey]);

    useEffect(() => {
        if (llmConfig) {
            setBaseUrl(llmConfig.baseUrl || '');
            setModel(llmConfig.model || '');
            setProviderKey(llmConfig.apiKey || '');
            setProviderSaved(!!(llmConfig.baseUrl && llmConfig.model));
        }
    }, [llmConfig]);

    const handleSaveGemini = () => {
        if (geminiKey.trim().length > 0) {
            onKeySet(geminiKey);
            setGeminiSaved(true);
        }
    };

    const handleSaveProvider = () => {
        if (baseUrl.trim() && model.trim()) {
            onLlmConfigChange?.({
                provider: 'openai_compat',
                baseUrl: baseUrl.trim(),
                model: model.trim(),
                apiKey: providerKey,
            });
            setProviderSaved(true);
        }
    };

    const providerReady = baseUrl.trim().length > 0 && model.trim().length > 0;
    const geminiReady = geminiKey.trim().length > 0;

    return (
        <div className="space-y-6 mb-8">
            {/* ─── General LLM Provider (OmniRoute / OpenAI-compatible) ─── */}
            <div className="card p-4 sm:p-6 animate-fade">
                <div className="flex items-center gap-3 mb-4">
                    <div className="p-2 bg-paper3 rounded-input text-brass">
                        <Server size={18} />
                    </div>
                    <div>
                        <h2 className="font-display lowercase text-lg text-ink">General LLM Provider</h2>
                        <p className="text-xs text-muted">Used for clip generation and standard AI requests.</p>
                    </div>
                </div>

                <div className="space-y-3">
                    <div>
                        <label className="text-xs text-muted mb-1 block">Base URL</label>
                        <input
                            type="text"
                            value={baseUrl}
                            onChange={(e) => { setBaseUrl(e.target.value); setProviderSaved(false); }}
                            placeholder="http://localhost:20128/v1"
                            className="input-field font-mono text-sm"
                        />
                    </div>
                    <div className="flex flex-col sm:flex-row gap-3">
                        <div className="sm:flex-1">
                            <label className="text-xs text-muted mb-1 block">API Key (optional for local)</label>
                            <div className="relative">
                                <input
                                    type={providerVisible ? 'text' : 'password'}
                                    value={providerKey}
                                    onChange={(e) => { setProviderKey(e.target.value); setProviderSaved(false); }}
                                    placeholder="sk-... (optional)"
                                    className="input-field pr-12 font-mono text-sm"
                                />
                                <button
                                    onClick={() => setProviderVisible(!providerVisible)}
                                    className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink transition-colors"
                                >
                                    {providerVisible ? <EyeOff size={18} /> : <Eye size={18} />}
                                </button>
                            </div>
                        </div>
                        <div className="sm:flex-1">
                            <label className="text-xs text-muted mb-1 block">Model</label>
                            <input
                                type="text"
                                value={model}
                                onChange={(e) => { setModel(e.target.value); setProviderSaved(false); }}
                                placeholder="besto, gpt-4o, llama3.1..."
                                className="input-field font-mono text-sm"
                            />
                        </div>
                    </div>
                    <button
                        onClick={handleSaveProvider}
                        disabled={!providerReady || providerSaved}
                        className={providerSaved ? 'badge-ok px-4 cursor-default' : 'btn-primary'}
                    >
                        {providerSaved ? <><Check size={14} /> Ready</> : 'Save Provider'}
                    </button>
                </div>
                <p className="mt-3 text-xs text-muted">
                    Works with OmniRoute, Ollama, vLLM, LM Studio, OpenRouter, NVIDIA NIM, LiteLLM, or any OpenAI-compatible endpoint.
                </p>
            </div>

            {/* ─── Gemini API Key (Auto Edit) ─── */}
            <div className="card p-4 sm:p-6 animate-fade">
                <div className="flex items-center gap-3 mb-4">
                    <div className="p-2 bg-paper3 rounded-input text-brass">
                        <Sparkles size={18} />
                    </div>
                    <div>
                        <h2 className="font-display lowercase text-lg text-ink">Gemini API Key (Auto Edit)</h2>
                        <p className="text-xs text-muted">Auto Edit uses Gemini's video Files API, which OmniRoute can't serve.</p>
                    </div>
                </div>

                <div className="flex flex-col sm:flex-row gap-3">
                    <div className="relative sm:flex-1">
                        <input
                            type={geminiVisible ? 'text' : 'password'}
                            value={geminiKey}
                            onChange={(e) => { setGeminiKey(e.target.value); setGeminiSaved(false); }}
                            placeholder="AIzaSy..."
                            className="input-field pr-12 font-mono"
                        />
                        <button
                            onClick={() => setGeminiVisible(!geminiVisible)}
                            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink transition-colors"
                        >
                            {geminiVisible ? <EyeOff size={18} /> : <Eye size={18} />}
                        </button>
                    </div>
                    <button
                        onClick={handleSaveGemini}
                        disabled={!geminiReady || geminiSaved}
                        className={geminiSaved ? 'badge-ok px-4 cursor-default' : 'btn-primary'}
                    >
                        {geminiSaved ? <><Check size={14} /> Ready</> : 'Set Key'}
                    </button>
                </div>
                <p className="mt-3 text-xs text-muted">
                    Your key is stored locally in your browser.
                    <br />
                    <a
                        href="https://aistudio.google.com/app/apikey"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-brass hover:underline mt-1 inline-block"
                    >
                        Get your free Gemini API Key here →
                    </a>
                </p>
            </div>
        </div>
    );
}
