import React, { useState, useEffect, useRef } from 'react';
import { 
  Mic, MicOff, Play, Square, Copy, Check, Sparkles, RefreshCw, 
  Settings, Sliders, Eye, Code2, Terminal, Volume2, ShieldCheck, 
  Layers, ChevronDown, Wand2, Zap, ArrowRight, CornerDownLeft, 
  Maximize2, Minimize2, Trash2, CheckCircle2, AlertCircle, FileText
} from 'lucide-react';

const PRESETS = [
  {
    id: 'markdown',
    title: 'Markdown Spec Request',
    raw: "ADD A MARKDWN RENDERER TO PRPERLY RENDER ALL MARKDWN SYNTAXES SUCH AS THE BELW\n\n# Markdown syntax guide",
    rewritten: `# Markdown Engine Implementation Spec

## Objective
Configure the AI agent to include and utilize a fully compliant Markdown renderer. Ensure all output containing Markdown syntax is accurately parsed and styled according to system design tokens.

### Reference Syntax Requirements:
- **Headers**: Support levels \`h1\` through \`h6\` with distinct visual scale.
- **Emphasis**: Render *italic*, **bold**, and ~~strikethrough~~ formatting smoothly.
- **Code Blocks**: Syntax highlighting for \`jsx\`, \`typescript\`, and \`json\`.
- **Lists**: Nested ordered and unordered lists with custom bullet spacing.`
  },
  {
    id: 'refactor',
    title: 'React Performance Optimization',
    raw: "fix the re-rendering lag in the dashboard list component when filtering items by tag",
    rewritten: `Perform a performance audit and refactor on the dashboard items list component:

1. **Memoization**: Wrap item list row components in \`React.memo\` with custom equality comparison.
2. **State Decoupling**: Isolate tag selection filter state into a localized context or custom selector to avoid top-level layout re-renders.
3. **Virtualization**: Integrate \`useVirtualizer\` for handling datasets exceeding 500 items smoothly.`
  },
  {
    id: 'system_prompt',
    title: 'Agent Persona Definition',
    raw: "make the assistant sound like an expert senior software architect who gives concise actionable code fixes",
    rewritten: `Adhere strictly to the following System Architecture Persona:

- **Role**: Principal Systems Architect & Lead Frontend Engineer.
- **Tone**: Direct, technical, highly actionable, zero fluff.
- **Output Standard**: Prioritize modular code structure, type safety, visual edge-case handling, and immediate previewability.`
  }
];

const THEMES = {
  emerald: {
    name: 'Cyber Emerald',
    primary: 'from-emerald-500 to-teal-400',
    accentBg: 'bg-emerald-500/10',
    accentBorder: 'border-emerald-500/30',
    accentText: 'text-emerald-400',
    glow: 'shadow-emerald-500/20',
    ring: 'focus:ring-emerald-500/50',
    badge: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40'
  },
  cyan: {
    name: 'Neon Cyan',
    primary: 'from-cyan-500 to-blue-500',
    accentBg: 'bg-cyan-500/10',
    accentBorder: 'border-cyan-500/30',
    accentText: 'text-cyan-400',
    glow: 'shadow-cyan-500/20',
    ring: 'focus:ring-cyan-500/50',
    badge: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40'
  },
  violet: {
    name: 'Electric Violet',
    primary: 'from-violet-500 to-fuchsia-500',
    accentBg: 'bg-violet-500/10',
    accentBorder: 'border-violet-500/30',
    accentText: 'text-violet-400',
    glow: 'shadow-violet-500/20',
    ring: 'focus:ring-violet-500/50',
    badge: 'bg-violet-500/20 text-violet-300 border-violet-500/40'
  },
  amber: {
    name: 'Solar Amber',
    primary: 'from-amber-500 to-orange-500',
    accentBg: 'bg-amber-500/10',
    accentBorder: 'border-amber-500/30',
    accentText: 'text-amber-400',
    glow: 'shadow-amber-500/20',
    ring: 'focus:ring-amber-500/50',
    badge: 'bg-amber-500/20 text-amber-300 border-amber-500/40'
  }
};

function AudioWaveVisualizer({ isRecording, activeTheme }) {
  return (
    <div className="flex items-center gap-1 h-5 px-2 py-0.5 rounded-full bg-black/40 border border-white/10 backdrop-blur-md">
      {[40, 75, 30, 90, 50, 100, 60, 35, 80, 45].map((heightPct, idx) => (
        <span
          key={idx}
          className={`w-0.5 rounded-full transition-all duration-150 ${
            isRecording 
              ? `${activeTheme.accentText} bg-current animate-pulse` 
              : 'bg-white/20'
          }`}
          style={{
            height: isRecording ? `${Math.max(20, (heightPct * (Math.sin(Date.now() / 150 + idx) + 1.2)) / 2)}%` : '20%',
            animationDelay: `${idx * 80}ms`
          }}
        />
      ))}
    </div>
  );
}

function MiniMarkdownRenderer({ content, activeTheme }) {
  if (!content) return <span className="text-slate-500 italic">No output generated yet...</span>;

  const lines = content.split('\n');
  return (
    <div className="space-y-2 text-xs leading-relaxed font-sans text-slate-200">
      {lines.map((line, idx) => {
        if (line.startsWith('# ')) {
          return (
            <h1 key={idx} className={`text-base font-bold font-mono tracking-tight pb-1 border-b border-white/10 ${activeTheme.accentText} flex items-center gap-1.5 mt-2`}>
              <span className="opacity-40 text-xs">#</span> {line.replace('# ', '')}
            </h1>
          );
        }
        if (line.startsWith('## ')) {
          return (
            <h2 key={idx} className="text-sm font-semibold font-mono tracking-tight text-white flex items-center gap-1.5 mt-2">
              <span className="text-slate-500 text-xs">##</span> {line.replace('## ', '')}
            </h2>
          );
        }
        if (line.startsWith('### ')) {
          return (
            <h3 key={idx} className="text-xs font-semibold text-slate-300 font-mono flex items-center gap-1 mt-1.5">
              <span className="text-slate-600 text-[10px]">###</span> {line.replace('### ', '')}
            </h3>
          );
        }
        if (line.startsWith('- ')) {
          const text = line.replace('- ', '');
          return (
            <div key={idx} className="flex items-start gap-2 pl-1 my-0.5">
              <span className={`w-1.5 h-1.5 rounded-full ${activeTheme.accentText} bg-current mt-1.5 shrink-0`} />
              <span className="text-slate-300">{formatInlineFormatting(text)}</span>
            </div>
          );
        }
        if (/^\d+\./.test(line)) {
          const num = line.match(/^\d+/)[0];
          const text = line.replace(/^\d+\.\s*/, '');
          return (
            <div key={idx} className="flex items-start gap-2 pl-1 my-0.5">
              <span className={`font-mono text-[10px] font-bold ${activeTheme.accentText} shrink-0 mt-0.5`}>{num}.</span>
              <span className="text-slate-300">{formatInlineFormatting(text)}</span>
            </div>
          );
        }
        if (!line.trim()) {
          return <div key={idx} className="h-1" />;
        }
        return <p key={idx} className="text-slate-300">{formatInlineFormatting(line)}</p>;
      })}
    </div>
  );
}

function formatInlineFormatting(text) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith('`') && part.endsWith('`')) {
      return (
        <code key={i} className="px-1.5 py-0.5 mx-0.5 rounded bg-black/60 border border-white/10 font-mono text-[11px] text-amber-300">
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i} className="font-semibold text-white">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('*') && part.endsWith('*')) {
      return <em key={i} className="italic text-slate-200">{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

export default function App() {
  const [selectedTheme, setSelectedTheme] = useState('emerald');
  const [mode, setMode] = useState('PRO'); // LIVE | BUF | PRO
  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [rawText, setRawText] = useState(PRESETS[0].raw);
  const [rewrittenText, setRewrittenText] = useState(PRESETS[0].rewritten);
  const [viewMode, setViewMode] = useState('rendered'); // 'rendered' | 'code'
  const [copied, setCopied] = useState(false);
  const [selectedPreset, setSelectedPreset] = useState('markdown');
  const [tone, setTone] = useState('technical');
  const [widgetPosition, setWidgetPosition] = useState('floating'); // 'floating' | 'docked'
  const [isExpanded, setIsExpanded] = useState(false);
  const [history, setHistory] = useState([]);
  
  const activeTheme = THEMES[selectedTheme];

  // Dynamic simulation of voice input typing
  const toggleRecording = () => {
    if (isRecording) {
      setIsRecording(false);
      triggerAIRewrite();
    } else {
      setIsRecording(true);
      setRawText("");
      let simulatedSpeech = "Create an optimized React table component with server side pagination and markdown tooltips";
      let currentIndex = 0;
      
      const speechInterval = setInterval(() => {
        if (currentIndex <= simulatedSpeech.length) {
          setRawText(simulatedSpeech.slice(0, currentIndex));
          currentIndex++;
        } else {
          clearInterval(speechInterval);
        }
      }, 40);
    }
  };

  const triggerAIRewrite = () => {
    setIsProcessing(true);
    setTimeout(() => {
      let promptPrefix = "";
      if (tone === 'technical') promptPrefix = "Architectural Technical Specification:\n\n";
      if (tone === 'concise') promptPrefix = "Direct Execution Directive:\n\n";
      
      setRewrittenText(`${promptPrefix}Configure high-performance React table system matching exact requirements:\n\n1. **Data Virtualization**: Handle up to 10,000 rows efficiently.\n2. **Server Pagination**: Integrated query params state sync.\n3. **Markdown Tooltips**: Render inline markdown descriptions for headers.`);
      setIsProcessing(false);
      
      setHistory(prev => [{
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
        raw: rawText || "Dictated prompt",
        mode
      }, ...prev.slice(0, 4)]);
    }, 900);
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(rewrittenText);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const loadPreset = (preset) => {
    setSelectedPreset(preset.id);
    setRawText(preset.raw);
    setRewrittenText(preset.rewritten);
  };

  return (
    <div className="min-h-screen bg-[#070709] text-slate-100 font-sans selection:bg-emerald-500/30 selection:text-emerald-200 relative overflow-x-hidden flex flex-col">
      {/* Dynamic Ambient Background Gradients */}
      <div className="fixed inset-0 pointer-events-none opacity-40">
        <div className={`absolute -top-40 -left-40 w-96 h-96 rounded-full bg-gradient-to-br ${activeTheme.primary} blur-[120px] transition-all duration-700`} />
        <div className="absolute top-1/2 right-10 w-[500px] h-[500px] rounded-full bg-indigo-600/10 blur-[150px]" />
        <div className="absolute inset-0 bg-[linear-gradient(to_right,#ffffff05_1px,transparent_1px),linear-gradient(to_bottom,#ffffff05_1px,transparent_1px)] bg-[size:32px_32px]" />
      </div>

      {/* Navigation & Control Dashboard */}
      <header className="relative z-20 border-b border-white/10 bg-[#0c0c10]/80 backdrop-blur-md px-6 py-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className={`p-2.5 rounded-xl bg-gradient-to-br ${activeTheme.primary} shadow-lg ${activeTheme.glow} text-black font-black`}>
            <Wand2 className="w-5 h-5 text-black" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-bold tracking-tight text-white font-mono">FIELD READY <span className="text-xs px-2 py-0.5 rounded-full border border-white/20 bg-white/5 font-sans font-normal text-slate-400">HUD v2.5</span></h1>
            </div>
            <p className="text-xs text-slate-400">Next-Generation Speech-to-Prompt AI Engineering Interface</p>
          </div>
        </div>

        {/* Top Bar Quick Controls */}
        <div className="flex items-center gap-3">
          {/* Theme Selector */}
          <div className="flex items-center gap-1.5 bg-black/40 border border-white/10 p-1 rounded-xl">
            {Object.keys(THEMES).map((key) => (
              <button
                key={key}
                onClick={() => setSelectedTheme(key)}
                className={`px-2.5 py-1 text-xs font-medium rounded-lg transition-all flex items-center gap-1.5 ${
                  selectedTheme === key 
                    ? 'bg-white/15 text-white shadow' 
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <span className={`w-2 h-2 rounded-full bg-gradient-to-br ${THEMES[key].primary}`} />
                <span className="capitalize">{key}</span>
              </button>
            ))}
          </div>

          {/* Preset Selector */}
          <div className="relative">
            <select
              value={selectedPreset}
              onChange={(e) => {
                const p = PRESETS.find(x => x.id === e.target.value);
                if (p) loadPreset(p);
              }}
              className="bg-black/40 border border-white/10 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-medium focus:outline-none focus:border-white/30 cursor-pointer appearance-none pr-8"
            >
              {PRESETS.map((p) => (
                <option key={p.id} value={p.id} className="bg-[#121218] text-white">{p.title}</option>
              ))}
            </select>
            <ChevronDown className="w-3.5 h-3.5 text-slate-400 absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
          </div>
        </div>
      </header>

      {/* Main Workspace Frame */}
      <main className="flex-1 relative z-10 max-w-7xl w-full mx-auto p-6 grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
        
        {/* Left Column: Context & Prompt Controls */}
        <section className="lg:col-span-5 space-y-6">
          <div className="bg-[#101016]/90 border border-white/10 rounded-2xl p-5 backdrop-blur-xl space-y-4 shadow-xl">
            <div className="flex items-center justify-between pb-3 border-b border-white/10">
              <div className="flex items-center gap-2">
                <Sliders className={`w-4 h-4 ${activeTheme.accentText}`} />
                <h2 className="text-sm font-semibold tracking-wide text-white font-mono uppercase">Prompt Optimizer Specs</h2>
              </div>
              <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded bg-white/5 border border-white/10 text-slate-400">Active</span>
            </div>

            {/* Tone Selector */}
            <div className="space-y-2">
              <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
                <span>Transformation Tone</span>
                <span className="text-[11px] text-slate-500">Auto-applied on speech finish</span>
              </label>
              <div className="grid grid-cols-3 gap-2">
                {[
                  { id: 'technical', label: 'Technical Spec' },
                  { id: 'concise', label: 'Concise Fix' },
                  { id: 'creative', label: 'Expanded AI' }
                ].map((t) => (
                  <button
                    key={t.id}
                    onClick={() => setTone(t.id)}
                    className={`px-3 py-2 text-xs rounded-xl font-medium border transition-all text-center ${
                      tone === t.id 
                        ? `${activeTheme.accentBg} ${activeTheme.accentBorder} ${activeTheme.accentText} border` 
                        : 'bg-black/30 border-white/5 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Widget Position Mode */}
            <div className="space-y-2 pt-2">
              <label className="text-xs font-medium text-slate-300">Layout Preview Mode</label>
              <div className="flex gap-2">
                <button
                  onClick={() => setWidgetPosition('floating')}
                  className={`flex-1 py-2 px-3 rounded-xl border text-xs font-medium flex items-center justify-center gap-2 transition-all ${
                    widgetPosition === 'floating'
                      ? 'bg-white/10 border-white/30 text-white shadow-inner'
                      : 'bg-black/20 border-white/5 text-slate-400 hover:text-white'
                  }`}
                >
                  <Layers className="w-3.5 h-3.5" /> Floating Overlay HUD
                </button>
                <button
                  onClick={() => setWidgetPosition('docked')}
                  className={`flex-1 py-2 px-3 rounded-xl border text-xs font-medium flex items-center justify-center gap-2 transition-all ${
                    widgetPosition === 'docked'
                      ? 'bg-white/10 border-white/30 text-white shadow-inner'
                      : 'bg-black/20 border-white/5 text-slate-400 hover:text-white'
                  }`}
                >
                  <Terminal className="w-3.5 h-3.5" /> Docked Side Inspector
                </button>
              </div>
            </div>

            {/* Prompt Preset Library Quick Buttons */}
            <div className="pt-2">
              <label className="text-xs font-medium text-slate-300 mb-2 block">Quick Test Scenarios</label>
              <div className="space-y-2">
                {PRESETS.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => loadPreset(p)}
                    className={`w-full text-left p-2.5 rounded-xl border transition-all flex items-center justify-between group ${
                      selectedPreset === p.id
                        ? 'bg-white/10 border-white/20 text-white'
                        : 'bg-black/30 border-white/5 text-slate-400 hover:bg-white/5 hover:text-slate-200'
                    }`}
                  >
                    <div className="truncate pr-2">
                      <p className="text-xs font-medium text-slate-200">{p.title}</p>
                      <p className="text-[10px] text-slate-500 truncate font-mono mt-0.5">{p.raw}</p>
                    </div>
                    <ArrowRight className="w-3.5 h-3.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 text-slate-400" />
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* History Log */}
          {history.length > 0 && (
            <div className="bg-[#101016]/80 border border-white/10 rounded-2xl p-4 backdrop-blur-md space-y-3">
              <h3 className="text-xs font-mono font-semibold text-slate-400 uppercase tracking-wider flex items-center justify-between">
                <span>Recent Enhancements</span>
                <span className="text-[10px] text-slate-500 font-sans">{history.length} items</span>
              </h3>
              <div className="space-y-2">
                {history.map((item, idx) => (
                  <div key={idx} className="p-2 rounded-lg bg-black/40 border border-white/5 text-xs flex justify-between items-center">
                    <span className="text-slate-300 truncate max-w-[200px] font-mono">{item.raw}</span>
                    <span className="text-[10px] font-mono text-slate-500">{item.time}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>


        {/* Right Column: Interactive Widget Display Showcase */}
        <section className="lg:col-span-7 flex flex-col items-center justify-center min-h-[600px] bg-black/40 border border-white/10 rounded-3xl p-6 relative overflow-hidden backdrop-blur-2xl">
          
          {/* Subtle Grid Accent */}
          <div className="absolute inset-0 bg-[radial-gradient(#ffffff0a_1px,transparent_1px)] [background-size:16px_16px] pointer-events-none" />

          {/* Live Status Header Overlay */}
          <div className="absolute top-4 left-6 right-6 flex justify-between items-center text-xs font-mono text-slate-500">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-ping" />
              LIVE PREVIEW CANVAS
            </span>
            <span>STYLE: {activeTheme.name.toUpperCase()}</span>
          </div>

          {/* ========================================================================= */}
          {/* REDESIGNED WIDGET HERO COMPONENT STARTS HERE */}
          {/* ========================================================================= */}
          <div 
            className={`w-full max-w-[540px] transition-all duration-500 z-10 ${
              widgetPosition === 'floating'
                ? 'shadow-2xl shadow-black/90 hover:scale-[1.01]'
                : 'border-l-4 border-l-emerald-500'
            }`}
          >
            {/* Main Container Glass Frame */}
            <div className={`relative bg-[#0d0d12]/95 border border-white/15 rounded-2xl overflow-hidden backdrop-blur-2xl shadow-2xl transition-all ${activeTheme.glow}`}>
              
              {/* Header Control Strip */}
              <div className="flex items-center justify-between px-3.5 py-2.5 bg-[#14141c]/90 border-b border-white/10">
                {/* Status Indicator & Label */}
                <div className="flex items-center gap-2.5">
                  <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-black/50 border border-white/10 text-[11px] font-mono font-bold tracking-wider">
                    <span className={`w-2 h-2 rounded-full ${isRecording ? 'bg-rose-500 animate-pulse' : activeTheme.accentText + ' bg-current'}`} />
                    <span className="text-white">FIELD READY</span>
                  </div>

                  {/* Dynamic Audio Visualizer Bar */}
                  <AudioWaveVisualizer isRecording={isRecording} activeTheme={activeTheme} />
                </div>

                {/* Mode Switcher Buttons */}
                <div className="flex items-center gap-1 bg-black/60 p-1 rounded-xl border border-white/10">
                  {(['LIVE', 'BUF', 'PRO']).map((m) => (
                    <button
                      key={m}
                      onClick={() => setMode(m)}
                      className={`px-2.5 py-0.5 text-[10px] font-mono font-bold rounded-lg transition-all ${
                        mode === m 
                          ? `${activeTheme.accentBg} ${activeTheme.accentText} border ${activeTheme.accentBorder} shadow-sm` 
                          : 'text-slate-400 hover:text-slate-200'
                      }`}
                    >
                      {m}
                    </button>
                  ))}
                </div>

                {/* Primary Recording / Action Switch */}
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={toggleRecording}
                    className={`flex items-center gap-1.5 px-3 py-1 text-xs font-mono font-bold rounded-xl transition-all shadow-md ${
                      isRecording 
                        ? 'bg-rose-500 text-white shadow-rose-500/30 animate-pulse' 
                        : `bg-gradient-to-r ${activeTheme.primary} text-black hover:brightness-110`
                    }`}
                  >
                    {isRecording ? <Square className="w-3.5 h-3.5 fill-current" /> : <Mic className="w-3.5 h-3.5 fill-current" />}
                    <span>{isRecording ? 'STOP' : 'START'}</span>
                  </button>

                  <button 
                    onClick={() => setIsExpanded(!isExpanded)}
                    className="p-1.5 text-slate-400 hover:text-white rounded-lg hover:bg-white/10 transition-colors"
                    title="Toggle Expand Inspector"
                  >
                    {isExpanded ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                  </button>
                </div>
              </div>

              {/* Main Widget Workspace Area */}
              <div className="p-3.5 space-y-3">
                
                {/* 1. Raw Speech Display Area */}
                <div className="relative group bg-black/50 border border-white/10 rounded-xl p-3 focus-within:border-white/30 transition-all">
                  <div className="flex items-center justify-between text-[10px] font-mono tracking-widest text-slate-400 uppercase mb-1.5">
                    <span className="flex items-center gap-1">
                      <Volume2 className="w-3 h-3 text-slate-500" />
                      RAW SPEECH INPUT
                    </span>
                    <span className="text-slate-500 lowercase font-sans text-[11px]">speak → stop → rewrite</span>
                  </div>

                  <textarea
                    value={rawText}
                    onChange={(e) => setRawText(e.target.value)}
                    placeholder="Speak or type raw instructions..."
                    rows={isExpanded ? 3 : 2}
                    className="w-full bg-transparent text-xs text-slate-200 font-sans leading-relaxed focus:outline-none resize-none placeholder-slate-600 font-normal"
                  />

                  {/* Floating Action Overlay for Raw Box */}
                  <div className="flex items-center justify-between pt-1 border-t border-white/5">
                    <button 
                      onClick={() => setRawText('')}
                      className="text-[10px] font-mono text-slate-500 hover:text-rose-400 flex items-center gap-1 transition-colors"
                    >
                      <Trash2 className="w-3 h-3" /> Clear Text
                    </button>
                    <span className="text-[10px] font-mono text-slate-600">{rawText.length} chars</span>
                  </div>
                </div>

                {/* 2. Primary Transformer Action Toolbar */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={triggerAIRewrite}
                    disabled={isProcessing}
                    className={`flex-1 py-2 px-3 rounded-xl border text-xs font-mono font-bold flex items-center justify-center gap-2 transition-all ${
                      isProcessing 
                        ? 'bg-white/5 border-white/10 text-slate-400 cursor-wait' 
                        : `${activeTheme.accentBg} ${activeTheme.accentBorder} ${activeTheme.accentText} hover:brightness-125 hover:shadow-lg`
                    }`}
                  >
                    <Sparkles className={`w-3.5 h-3.5 ${isProcessing ? 'animate-spin' : ''}`} />
                    <span>{isProcessing ? 'TRANSFORMING SPEECH...' : 'REWRITE PROMPT'}</span>
                  </button>

                  <button
                    onClick={handleCopy}
                    className="py-2 px-3 rounded-xl bg-black/40 hover:bg-white/10 border border-white/10 text-xs font-mono text-slate-300 flex items-center gap-1.5 transition-all"
                  >
                    {copied ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                    <span>{copied ? 'COPIED' : 'COPY'}</span>
                  </button>
                </div>

                {/* 3. Rewritten Prompt Result Display */}
                <div className="bg-[#08080c] border border-white/10 rounded-xl p-3.5 space-y-2 relative overflow-hidden">
                  
                  {/* Subtle Top Accent Bar */}
                  <div className={`absolute top-0 left-0 right-0 h-0.5 bg-gradient-to-r ${activeTheme.primary}`} />

                  {/* Header Bar */}
                  <div className="flex items-center justify-between border-b border-white/10 pb-2">
                    <div className="flex items-center gap-2">
                      <span className={`px-1.5 py-0.5 text-[9px] font-mono font-bold rounded ${activeTheme.badge}`}>
                        AI REWRITTEN PROMPT
                      </span>
                      <span className="text-[10px] text-emerald-400/80 font-mono flex items-center gap-1">
                        <Check className="w-2.5 h-2.5" /> Auto-copied
                      </span>
                    </div>

                    {/* View Switcher: Rendered vs Raw Code */}
                    <div className="flex items-center gap-1 bg-black/60 border border-white/10 rounded-lg p-0.5">
                      <button
                        onClick={() => setViewMode('rendered')}
                        className={`p-1 rounded text-[10px] flex items-center gap-1 font-mono transition-all ${
                          viewMode === 'rendered' ? 'bg-white/20 text-white' : 'text-slate-500 hover:text-slate-300'
                        }`}
                        title="Rendered Markdown View"
                      >
                        <Eye className="w-3 h-3" />
                      </button>
                      <button
                        onClick={() => setViewMode('code')}
                        className={`p-1 rounded text-[10px] flex items-center gap-1 font-mono transition-all ${
                          viewMode === 'code' ? 'bg-white/20 text-white' : 'text-slate-500 hover:text-slate-300'
                        }`}
                        title="Raw Markdown Source"
                      >
                        <Code2 className="w-3 h-3" />
                      </button>
                    </div>
                  </div>

                  {/* Output Content Window */}
                  <div className={`pt-1 font-sans text-xs transition-all ${isExpanded ? 'max-h-[320px]' : 'max-h-[180px]'} overflow-y-auto pr-1 custom-scrollbar`}>
                    {isProcessing ? (
                      <div className="py-8 text-center space-y-2">
                        <RefreshCw className={`w-5 h-5 mx-auto animate-spin ${activeTheme.accentText}`} />
                        <p className="text-xs font-mono text-slate-400">Restructuring raw speech with system rules...</p>
                      </div>
                    ) : viewMode === 'rendered' ? (
                      <MiniMarkdownRenderer content={rewrittenText} activeTheme={activeTheme} />
                    ) : (
                      <pre className="font-mono text-[11px] text-amber-300/90 whitespace-pre-wrap leading-relaxed selection:bg-amber-500/30">
                        {rewrittenText}
                      </pre>
                    )}
                  </div>
                </div>

              </div>

              {/* Status Footer */}
              <div className="bg-black/80 px-3.5 py-1.5 border-t border-white/5 flex items-center justify-between text-[10px] font-mono text-slate-500">
                <span className="flex items-center gap-1">
                  <ShieldCheck className="w-3 h-3 text-emerald-500" />
                  Markdown Renderer Ready
                </span>
                <span>Press <kbd className="px-1 bg-white/10 rounded text-slate-300">Space</kbd> to Dictate</span>
              </div>

            </div>
          </div>
          {/* ========================================================================= */}
          {/* REDESIGNED WIDGET HERO COMPONENT ENDS HERE */}
          {/* ========================================================================= */}

        </section>

      </main>

      {/* Footer info */}
      <footer className="relative z-10 border-t border-white/5 bg-black/60 py-3 px-6 text-center text-xs font-mono text-slate-500">
        Engineered for Next-Gen Developer Workflows • HUD Pro AI Widget Redesign
      </footer>
    </div>
  );
}