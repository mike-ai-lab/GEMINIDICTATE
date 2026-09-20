import re
import asyncio
import base64
import ctypes
import json
import logging
import os
import queue
import struct
import subprocess
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import numpy as np
import pyaudio
import sounddevice as sd
import websockets as _conv_ws
from scipy.signal import resample_poly
from google import genai
from google.genai import types

MODEL = "gemini-3.5-transcribe-live"
PROMPT_MODEL = "models/gemini-3.5-flash-lite"   # text→text model for prompt rewriting
PROMPT_FALLBACKS = ["models/gemini-3.5-flash", "models/gemini-2.5-flash-lite"]
DEVICE = 1
RATE_IN = 44100
RATE_OUT = 16000
BLOCK = 2205
VK_F8 = 0x77
VK_Z = 0x5A
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
FINALIZE_SECONDS = 8.0
FOCUS_POLL_MS = 120
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_dictate.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
    encoding="utf-8",
    force=True,
)
log = logging.getLogger("GeminiDictate")


def L(msg, *args):
    try:
        log.info(msg, *args)
    except Exception:
        pass


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_SHIFT = 0x10
VK_LEFT = 0x25
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
CREATE_NO_WINDOW = 0x08000000
WS_EX_NOACTIVATE = 0x08000000
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
HWND_TOPMOST = -1
GA_ROOT = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

q = queue.Queue(maxsize=300)
recording = False
current_future = None
f8_last = False
_f8_was_down = False
_focus_change_count = 0  # consecutive polls showing a different editable during recording
focus_lock = False        # when True, auto-stop on focus change is suppressed
_last_audio_time = 0.0   # kept for compatibility; idle now uses GetLastInputInfo
IDLE_STOP_SECONDS = 15.0 # auto-stop recording after this many seconds of user inactivity

# Progressive paste serialization: async recv() enqueues word batches here;
# the Tkinter main thread drains them one at a time so clipboard set→paste→restore
# never overlaps with the next batch.
_progressive_paste_queue = queue.Queue()
_progressive_paste_busy = False  # True while a paste+restore cycle is in progress


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

def get_idle_seconds():
    """Return seconds since the last keyboard or mouse event (system-wide)."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        idle_ms = kernel32.GetTickCount() - lii.dwTime
        return idle_ms / 1000.0
    return 0.0
mode = "live"
stop_reason = ""
prompt_mode_active = False   # True when the PROMPT panel is open and recording into it
_prompt_confirmed_text = ""  # finalized transcript chunks only (for progressive display dedup)
_prompt_progressive_text = ""  # progressive words accumulated within the current VAD turn
_last_prompt_result = ""    # stores last rewritten output for undo
_prompt_history = []        # list of dicts: {raw, result} — max 10
_history_index = -1         # -1 = not browsing history; 0 = most recent

# ---------------------------------------------------------------------------
# CONV mode state
# ---------------------------------------------------------------------------
CONV_MODEL   = "models/gemini-2.5-flash-native-audio-latest"
CONV_WS_URL  = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
CONV_INPUT_RATE  = 16000
CONV_CHUNK       = 1600      # 100 ms @ 16 kHz
CONV_OUTPUT_RATE = 24000

_conv_is_connected = False
_conv_engine       = None    # AudioEngine instance while session is active
_conv_ai_level     = [0.0]
_conv_user_level   = [0.0]


def _conv_rms(data: bytes) -> float:
    if not data:
        return 0.0
    count = len(data) // 2
    if not count:
        return 0.0
    shorts = struct.unpack(f"{count}h", data[:count * 2])
    return (sum(s * s for s in shorts) / count) ** 0.5


class _ConvAudioEngine:
    """Owns one PyAudio instance + both streams. Mirrors AudioEngine in the
    proven standalone reference — single object, no separate playback thread."""

    def __init__(self, on_status, on_ai_rms, on_user_rms, on_caption):
        self.stop_event    = threading.Event()
        self.audio         = pyaudio.PyAudio()
        self.input_stream  = None
        self.output_stream = None
        self.ws            = None
        self.on_status     = on_status
        self.on_ai_rms     = on_ai_rms
        self.on_user_rms   = on_user_rms
        self.on_caption    = on_caption
        self._ai_buf       = ""
        self._user_buf     = ""

    def start_audio(self):
        self.input_stream = self.audio.open(
            format=pyaudio.paInt16, channels=1,
            rate=CONV_INPUT_RATE, input=True,
            frames_per_buffer=CONV_CHUNK,
        )
        self.output_stream = self.audio.open(
            format=pyaudio.paInt16, channels=1,
            rate=CONV_OUTPUT_RATE, output=True,
        )

    def close_audio(self):
        for s in (self.input_stream, self.output_stream):
            if s:
                try: s.stop_stream()
                except Exception: pass
                try: s.close()
                except Exception: pass
        self.input_stream = self.output_stream = None
        try: self.audio.terminate()
        except Exception: pass

    async def send_microphone(self):
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                chunk = await loop.run_in_executor(
                    None, self.input_stream.read, CONV_CHUNK, False)
                if not chunk:
                    continue
                self.on_user_rms(min(_conv_rms(chunk) / 3500.0, 1.0))
                await self.ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": base64.b64encode(chunk).decode("ascii"),
                            "mimeType": "audio/pcm;rate=16000",
                        }
                    }
                }))
            except Exception:
                break

    async def receive_gemini(self):
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                raw = await self.ws.recv()
                msg = json.loads(raw)
            except Exception:
                break

            sc = msg.get("serverContent")
            if not sc:
                continue

            for part in sc.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    pcm = base64.b64decode(inline["data"])
                    self.on_ai_rms(min(_conv_rms(pcm) / 5000.0, 1.0))
                    self.on_status("speaking")
                    if self.output_stream:
                        await loop.run_in_executor(None, self.output_stream.write, pcm)

            out_text = sc.get("outputTranscription", {}).get("text", "")
            if out_text:
                self._ai_buf += out_text
                self.on_caption("ai", self._ai_buf)

            in_text = sc.get("inputTranscription", {}).get("text", "")
            if in_text:
                self._user_buf += in_text
                self.on_caption("user", self._user_buf)

            if sc.get("turnComplete"):
                self._ai_buf = ""
                self._user_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("listening")

            if sc.get("interrupted"):
                self._ai_buf = ""
                self._user_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("listening")


def _conv_on_ai_rms(v):
    _conv_ai_level[0] = max(_conv_ai_level[0], v)


def _conv_on_user_rms(v):
    _conv_user_level[0] = max(_conv_user_level[0], v)


def _conv_on_status(status):
    root.after(0, _conv_set_ui_status, status)


def _conv_on_caption(speaker, text):
    root.after(0, _conv_push_caption, speaker, text)


def _run_conv_loop():
    """Each session gets its own fresh event loop — same pattern as standalone."""
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    lp.run_until_complete(_gemini_live_conv())


async def _gemini_live_conv():
    global _conv_is_connected, _conv_engine
    key = os.environ.get("GEMINI_API_KEY")
    url = CONV_WS_URL + f"?key={key}"
    try:
        async with _conv_ws.connect(
            url, max_size=None, ping_interval=20, ping_timeout=20
        ) as ws:
            await ws.send(json.dumps({
                "setup": {
                    "model": CONV_MODEL,
                    "generationConfig": {"responseModalities": ["AUDIO"]},
                    "inputAudioTranscription":  {},
                    "outputAudioTranscription": {},
                }
            }))
            resp = json.loads(await ws.recv())
            if "setupComplete" not in resp:
                _conv_on_status("error")
                return

            engine = _ConvAudioEngine(
                on_status   = _conv_on_status,
                on_ai_rms   = _conv_on_ai_rms,
                on_user_rms = _conv_on_user_rms,
                on_caption  = _conv_on_caption,
            )
            engine.ws = ws
            _conv_engine = engine
            engine.start_audio()
            _conv_on_status("listening")

            mic_task = asyncio.create_task(engine.send_microphone())
            rx_task  = asyncio.create_task(engine.receive_gemini())

            while _conv_is_connected and not engine.stop_event.is_set():
                await asyncio.sleep(0.1)

            engine.stop_event.set()
            mic_task.cancel()
            rx_task.cancel()
            engine.close_audio()

    except Exception as ex:
        L("CONV SESSION ERROR: %s\n%s", ex, traceback.format_exc())
        _conv_on_status("error")
        root.after(0, _conv_push_caption, "ai", f"Error: {str(ex)[:60]}")
    finally:
        _conv_is_connected = False
        root.after(0, _on_conv_ended)


def start_conv():
    global _conv_is_connected, _conv_engine
    if _conv_is_connected:
        return
    if not os.environ.get("GEMINI_API_KEY"):
        focus_status.set("GEMINI_API_KEY missing")
        return
    _conv_is_connected = True
    _conv_ai_level[0]   = 0.0
    _conv_user_level[0] = 0.0
    root.after(0, _conv_set_ui_status, "connecting")
    root.after(0, _conv_clear_transcript)
    threading.Thread(target=_run_conv_loop, daemon=True).start()
    L("CONV SESSION STARTED")


def stop_conv():
    global _conv_is_connected
    if not _conv_is_connected:
        return
    _conv_is_connected = False
    if _conv_engine:
        _conv_engine.stop_event.set()
    root.after(0, _conv_set_ui_status, "stopped")
    L("CONV SESSION STOPPED")


def _on_conv_ended():
    """Called on Tk thread when session coroutine exits."""
    global _conv_engine
    _conv_engine = None
    _conv_ai_level[0]   = 0.0
    _conv_user_level[0] = 0.0
    _conv_set_ui_status("stopped")
    # Re-enable mode buttons
    mode_live.config(state="normal")
    mode_buf.config(state="normal")
    mode_pro.config(state="normal")
    try: mode_not.config(state="normal")
    except Exception: pass
    try: mode_conv.config(state="normal")
    except Exception: pass

# Notes
NOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.json")
_notes_data = []
_open_editor_windows = []   # all currently open note editor Toplevel windows

def _notes_load():
    global _notes_data
    try:
        if os.path.exists(NOTES_FILE):
            with open(NOTES_FILE, encoding="utf-8") as f:
                _notes_data = json.load(f)
            return
    except Exception:
        pass
    _notes_data = []

def _notes_save():
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(_notes_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        L("NOTES SAVE ERROR: %s", e)

_notes_load()

# The widget is deliberately NOT used to capture the target on F8.
# Focus is captured only after the user has clicked the desired text field and
# then presses START. A rolling snapshot of the last non-widget UIA focus makes
# that possible even though clicking START necessarily focuses this widget.
last_external_focus = None
recording_focus = None
saved_clipboard_text = None
saved_clipboard_has_text = False
fallback_clipboard_text = ""
fallback_active = False
loop = asyncio.new_event_loop()


def loop_thread():
    asyncio.set_event_loop(loop)
    L("ASYNC LOOP STARTED")
    loop.run_forever()


threading.Thread(target=loop_thread, daemon=True).start()


# ---------------------------------------------------------------------------
# Windows / UI Automation focus inspection
# ---------------------------------------------------------------------------

def win_text(hwnd):
    if not hwnd:
        return ""
    try:
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, n + 1))
        user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value.strip()
    except Exception:
        return ""


def win_class(hwnd):
    if not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, len(buf))
        return buf.value
    except Exception:
        return ""


def hwnd_process_name(hwnd):
    if not hwnd:
        return "unknown"
    try:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return f"PID {pid.value}"
        try:
            size = ctypes.c_ulong(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return f"PID {pid.value}"
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return "unknown"


def foreground_snapshot():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    tid = user32.GetWindowThreadProcessId(hwnd, None)

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", ctypes.c_long * 4),
        ]

    gi = GUITHREADINFO()
    gi.cbSize = ctypes.sizeof(GUITHREADINFO)
    focus = hwnd
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gi)):
        focus = int(gi.hwndFocus or gi.hwndActive or hwnd)

    return {
        "top_hwnd": int(hwnd),
        "focus_hwnd": int(focus),
        "top_pid": int(user32.GetWindowThreadProcessId(hwnd, None)),
        "top_title": win_text(hwnd),
        "top_class": win_class(hwnd),
        "focus_title": win_text(focus),
        "focus_class": win_class(focus),
        "process": hwnd_process_name(hwnd),
    }


class UIAFocusService:
    """Persistent hidden PowerShell UIA reader; no visible console window."""

    SCRIPT = r'''
Add-Type -AssemblyName UIAutomationClient
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$lastEditable = $null

function Test-IsEditable($elem) {
    try {
        $p = $elem.Current
        if (-not $p.IsEnabled)           { return $false }
        if ($p.ProcessId -eq $PID)       { return $false }
        $ct = [string]$p.ControlType.ProgrammaticName
        $cls = [string]$p.ClassName
        # Accept by ControlType
        if ($ct -match 'Edit')     { return $true }
        if ($ct -match 'Document') { return $true }
        # Accept Win32 Edit class even if IsKeyboardFocusable reports False
        if ($cls -match '^Edit$')  { return $true }
        # Accept known editable class names (ProseMirror, CodeMirror, etc.)
        if ($cls -match 'ProseMirror|CodeMirror|ace_text|RichEdit|Scintilla') { return $true }
        # Fallback: ValuePattern or TextPattern
        if (-not $p.IsKeyboardFocusable) { return $false }
        try {
            $null = $elem.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            return $true
        } catch {}
        try {
            $null = $elem.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
            return $true
        } catch {}
    } catch {}
    return $false
}

function Find-EditableDescendant($root) {
    # When FocusedElement returns a container, search for the first editable child.
    try {
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::IsEnabledProperty, $true)
        $kids = $root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants, $cond)
        foreach ($k in $kids) {
            if (Test-IsEditable $k) { return $k }
        }
    } catch {}
    return $null
}

while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        if ($line -eq 'F') {
            if ($null -ne $lastEditable) {
                try {
                    $lastEditable.SetFocus()
                    '{"ok":true,"focus":"restored"}'
                } catch {
                    '{"ok":false,"error":"FOCUS_RESTORE_FAILED"}'
                }
            } else {
                '{"ok":false,"error":"NO_EDITABLE_TARGET"}'
            }
            [Console]::Out.Flush()
            continue
        }
        $e = [System.Windows.Automation.AutomationElement]::FocusedElement
        # If the focused element is not directly editable, check descendants.
        if (-not (Test-IsEditable $e)) {
            $child = Find-EditableDescendant $e
            if ($null -ne $child) { $e = $child }
        }
        $p = $e.Current
        if (Test-IsEditable $e) {
            $lastEditable = $e
        }
        $rid = ''
        try { $rid = ($e.GetRuntimeId() -join '.') } catch {}
        $obj = [ordered]@{
            ok = $true
            Name = [string]$p.Name
            ControlType = [string]$p.ControlType.ProgrammaticName
            ClassName = [string]$p.ClassName
            AutomationId = [string]$p.AutomationId
            IsKeyboardFocusable = [bool]$p.IsKeyboardFocusable
            IsEnabled = [bool]$p.IsEnabled
            NativeWindowHandle = [int]$p.NativeWindowHandle
            ProcessId = [int]$p.ProcessId
            RuntimeId = [string]$rid
        }
        $obj | ConvertTo-Json -Compress
    } catch {
        '{"ok":false,"error":"UIA_QUERY_FAILED"}'
    }
    [Console]::Out.Flush()
}
'''

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.latest = None
        self.stop_event = threading.Event()
        self.focus_request = threading.Event()
        self.thread = None

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            self.proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", self.SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
            L("UIA SERVICE STARTED pid=%s", self.proc.pid)
        except Exception:
            self.proc = None
            L("UIA SERVICE START ERROR\n%s", traceback.format_exc())

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                if not self.proc or self.proc.poll() is not None:
                    break
                command = "F" if self.focus_request.is_set() else "Q"
                self.focus_request.clear()
                with self.lock:
                    self.proc.stdin.write(command + "\n")
                    self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    break
                data = json.loads(line)
                with self.lock:
                    self.latest = data
            except Exception as e:
                L("UIA SERVICE QUERY ERROR: %s", e)
                break
            time.sleep(FOCUS_POLL_MS / 1000.0)
        L("UIA SERVICE WORKER EXIT")

    def snapshot(self):
        with self.lock:
            return dict(self.latest) if isinstance(self.latest, dict) else None

    def restore_last_editable(self):
        if not self.proc or self.proc.poll() is not None:
            return False
        self.focus_request.set()
        return True

    def stop(self):
        self.stop_event.set()
        p = self.proc
        self.proc = None
        if p:
            try:
                p.kill()
            except Exception:
                pass
        L("UIA SERVICE STOPPED")


uia = UIAFocusService()


def widget_hwnd():
    try:
        hwnd = int(root.winfo_id())
        # Tk returns a child HWND here. Use the actual top-level window so
        # focus tracking correctly recognizes the whole floating widget.
        return int(user32.GetAncestor(hwnd, GA_ROOT)) or hwnd
    except Exception:
        return 0


def is_widget_foreground(snap):
    if not snap:
        return False
    wh = widget_hwnd()
    return bool(wh and snap.get("top_hwnd") == wh)


def is_editable_uia(u):
    if not u or not u.get("ok"):
        return False
    if not u.get("IsEnabled"):
        return False
    ct = (u.get("ControlType") or "").lower()
    cls = (u.get("ClassName") or "")
    cls_lower = cls.lower()
    # Reject top-level Window/Pane containers — these are never the input field
    if ct in ("controltype.window", "controltype.pane") and not cls:
        return False
    # Accept standard Edit controls (ControlType)
    if "edit" in ct:
        return True
    # Accept Document controls (browser contenteditable, VS Code, etc.)
    if "document" in ct:
        return True
    # Win32 Edit class — IsKeyboardFocusable can be False for these, accept by class
    if cls == "Edit":
        return True
    # Electron/browser/IDE editable class names
    editable_classes = ("prosemirror", "codemirror", "ace_text", "richtextbox",
                        "richedit", "scintilla", "textbox", "textarea")
    if any(c in cls_lower for c in editable_classes):
        return True
    return False


# Window classes used by Electron, Chrome, VS Code, Kiro, etc.
# When UIA fails to expose the internal input, we trust the window itself.
_ELECTRON_BROWSER_CLASSES = (
    "Chrome_WidgetWin_1",   # Chrome, Electron, VS Code, Kiro
    "MozillaWindowClass",   # Firefox
    "OperaWindowClass",     # Opera
    "ApplicationFrameWindow",  # UWP apps
)

def is_editable_window_fallback(top, u=None):
    """Return True if the foreground window is an Electron/browser app where
    UIA doesn't expose the internal input but the window is clearly an editor.

    Requires that UIA returned a valid snapshot (ok=True) for this window —
    meaning the PS worker polled while this window was genuinely active and
    returned something (even a top-level Window element). This prevents
    false-positives when the window class matches but UIA has no data yet
    (e.g. just switched to a non-focused Electron window).
    """
    if not top:
        return False
    cls = top.get("top_class") or ""
    if cls not in _ELECTRON_BROWSER_CLASSES:
        return False
    # Also require that UIA confirms this window is the active one
    if not u or not u.get("ok"):
        return False
    # Reject our own process
    if u.get("ProcessId") == os.getpid():
        return False
    return True


def focus_identity(u):
    if not u:
        return None
    rid = u.get("RuntimeId")
    ct = (u.get("ControlType") or "").lower()
    # Don't use RuntimeId for top-level Window containers — they're not the input
    if rid and "window" not in ct and "pane" not in ct:
        return ("uia", rid)
    return (
        "fallback",
        u.get("ProcessId"),
        u.get("NativeWindowHandle"),
        u.get("ControlType"),
        u.get("AutomationId"),
        u.get("Name"),
    )


def describe_focus(top, u):
    if not top:
        return "No foreground window"
    app = top.get("process") or "unknown app"
    title = top.get("top_title") or "Untitled window"
    if u and u.get("ok"):
        ct = (u.get("ControlType") or "").replace("ControlType.", "")
        name = u.get("Name") or "unnamed"
        return f"{app}  {title}  {ct}: {name}"
    return f"{app}  {title}  {top.get('focus_class') or 'focused control'}"


def focus_is_same(a, b):
    if not a or not b:
        return False
    # If both have real UIA RuntimeIds (not Window containers), compare by UIA identity
    id_a = focus_identity(a.get("uia"))
    id_b = focus_identity(b.get("uia"))
    if id_a and id_b and id_a[0] == "uia" and id_b[0] == "uia":
        return id_a == id_b
    # For Electron/browser window-fallback targets, compare by top-level hwnd
    hwnd_a = (a.get("top") or {}).get("top_hwnd")
    hwnd_b = (b.get("top") or {}).get("top_hwnd")
    if hwnd_a and hwnd_b:
        return hwnd_a == hwnd_b
    return False


def update_focus_tracking():
    global last_external_focus, _focus_change_count
    top = foreground_snapshot()
    u = uia.snapshot()
    if not top:
        root.after(FOCUS_POLL_MS, update_focus_tracking)
        return

    widget = widget_hwnd()
    outside_widget = top.get("top_hwnd") != widget

    if recording:
        current = {"top": top, "uia": u}
        if recording_focus:
            # Only auto-stop when we positively confirm a DIFFERENT external
            # editable has focus for 2 consecutive polls. This prevents
            # transient UIA reports (notifications, popups, START/STOP clicks)
            # from falsely killing the session.
            # When focus_lock is ON, skip auto-stop entirely — the user wants
            # to stay connected to the captured field across window switches.
            if focus_lock:
                _focus_change_count = 0
            elif u and u.get("ok") and is_editable_uia(u) and not is_widget_foreground(top):
                if not focus_is_same(recording_focus, current):
                    _focus_change_count += 1
                    if _focus_change_count >= 2:
                        L(
                            "EDITABLE FOCUS CHANGED DURING RECORDING old=%r new=%r; AUTO STOP",
                            recording_focus.get("uia"),
                            u,
                        )
                        _focus_change_count = 0
                        root.after(0, stop_record, "focus left captured text field")
                else:
                    _focus_change_count = 0
            else:
                # UIA temporarily unavailable or widget foreground — don't count
                _focus_change_count = 0
                if not u or not u.get("ok"):
                    L("UIA TEMPORARILY UNAVAILABLE DURING RECORDING  keeping session alive")
    elif root.state() != "withdrawn" and outside_widget:
        # Rolling snapshot for START-time capture. F8 does not capture anything.
        # Never store our own process's UIA element as last_external_focus.
        # Also never store Windows system UI (taskbar, search, Start menu).
        our_pid = os.getpid()
        top_cls = top.get("top_class") or ""
        is_system_ui = top_cls in _SYSTEM_UI_CLASSES
        if not (u and u.get("ProcessId") == our_pid) and not is_system_ui:
            last_external_focus = {"top": top, "uia": u}

        editable = not is_system_ui and (
            (is_editable_uia(u) and (not u or u.get("ProcessId") != our_pid))
            or is_editable_window_fallback(top, u)
        )
        # Skip field-ready feedback while CONV is active — irrelevant there
        if mode != "conv":
            if editable:
                focus_status.set("FIELD READY")
                state_label.config(fg="#f4f4f5")
            else:
                focus_status.set(" Dictate")
                state_label.config(fg=MUTED)

    root.after(FOCUS_POLL_MS, update_focus_tracking)


# ---------------------------------------------------------------------------
# Clipboard and keyboard insertion
# ---------------------------------------------------------------------------

def clipboard_get_text():
    """Read the current CF_UNICODETEXT clipboard content without changing it."""
    if not user32.OpenClipboard(None):
        return False, "OpenClipboard failed"
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return True, ""
        p = kernel32.GlobalLock(h)
        if not p:
            return False, "GlobalLock failed"
        try:
            return True, ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    except Exception as e:
        return False, str(e)
    finally:
        user32.CloseClipboard()


def clipboard_restore_text(text, had_text):
    """Restore the user's pre-recording text clipboard, or empty it if none existed."""
    if had_text:
        ok, detail = clipboard_set_and_verify(text or "")
        if ok:
            return True, "original clipboard restored"
        return False, detail
    for _ in range(5):
        if user32.OpenClipboard(None):
            ok = bool(user32.EmptyClipboard())
            user32.CloseClipboard()
            return (ok, "clipboard emptied" if ok else "EmptyClipboard failed")
        time.sleep(0.05)
    return False, "OpenClipboard failed while clearing clipboard"


def restore_saved_clipboard(reason=""):
    """Restore the original clipboard unless a paste fallback is active."""
    global saved_clipboard_text, saved_clipboard_has_text, fallback_clipboard_text, fallback_active
    if fallback_active and fallback_clipboard_text:
        ok, detail = clipboard_restore_text(fallback_clipboard_text, True)
        L("CLIPBOARD RESTORE FALLBACK ok=%s reason=%r chars=%d detail=%s", ok, reason, len(fallback_clipboard_text), detail)
        return ok, detail
    if saved_clipboard_has_text:
        ok, detail = clipboard_restore_text(saved_clipboard_text, True)
    else:
        ok, detail = clipboard_restore_text("", False)
    L("CLIPBOARD RESTORE ok=%s reason=%r detail=%s", ok, reason, detail)
    return ok, detail


def add_clipboard_fallback(text, reason=""):
    """Keep failed/uncertain Gemini output in the clipboard as a safe fallback."""
    global fallback_clipboard_text, fallback_active
    chunk = (text or "").strip()
    if not chunk:
        return
    if fallback_clipboard_text:
        fallback_clipboard_text = (fallback_clipboard_text.rstrip() + " " + chunk).strip()
    else:
        fallback_clipboard_text = chunk
    fallback_active = True
    clipboard_set_and_verify(fallback_clipboard_text)
    L("CLIPBOARD FALLBACK ACTIVE chars=%d reason=%r", len(fallback_clipboard_text), reason)
    root.after(0, lambda: show_clipboard_fallback(True))


def show_clipboard_fallback(active):
    if "clipboard_fallback_label" not in globals():
        return
    clipboard_fallback_label.itemconfigure("clip", state="normal" if active else "hidden")


def clipboard_set_and_verify(text):
    if not text:
        return False, "empty text"
    data = (text + "\0").encode("utf-16le")
    if not user32.OpenClipboard(None):
        return False, "OpenClipboard failed"
    h = 0
    p = 0
    try:
        if not user32.EmptyClipboard():
            return False, "EmptyClipboard failed"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False, "GlobalAlloc failed"
        p = kernel32.GlobalLock(h)
        if not p:
            return False, "GlobalLock failed"
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        p = 0
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            return False, "SetClipboardData failed"
        h = 0
        verify_h = user32.GetClipboardData(CF_UNICODETEXT)
        if not verify_h:
            return False, "GetClipboardData verification failed"
        verify_p = kernel32.GlobalLock(verify_h)
        if not verify_p:
            return False, "clipboard verification GlobalLock failed"
        try:
            raw = ctypes.wstring_at(verify_p)
        finally:
            kernel32.GlobalUnlock(verify_h)
        if raw != text:
            return False, f"clipboard verification mismatch ({len(raw)} vs {len(text)} chars)"
        return True, "clipboard verified"
    except Exception as e:
        return False, str(e)
    finally:
        if p and h:
            kernel32.GlobalUnlock(h)
        if h:
            kernel32.GlobalFree(h)
        user32.CloseClipboard()


def send_ctrl_v():
    # INPUT is a tagged union on Win64. The previous implementation defined
    # only KEYBDINPUT directly inside INPUT, producing the wrong sizeof(INPUT)
    # and causing SendInput to return 0/4 even when the target was focused.
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("u", INPUT_UNION),
        ]

    if ctypes.sizeof(INPUT) != 40 and ctypes.sizeof(ctypes.c_void_p) == 8:
        raise RuntimeError(f"Unexpected Win64 INPUT size: {ctypes.sizeof(INPUT)}")

    inputs = (INPUT * 4)()
    inputs[0].type = 1
    inputs[0].u.ki = KEYBDINPUT(VK_CONTROL, 0, 0, 0, 0)
    inputs[1].type = 1
    inputs[1].u.ki = KEYBDINPUT(VK_V, 0, 0, 0, 0)
    inputs[2].type = 1
    inputs[2].u.ki = KEYBDINPUT(VK_V, 0, KEYEVENTF_KEYUP, 0, 0)
    inputs[3].type = 1
    inputs[3].u.ki = KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, 0)
    sent = user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    if sent != 4:
        err = ctypes.get_last_error()
        raise RuntimeError(f"SendInput returned {sent}/4 (Win32 error {err})")


def restore_target_focus(expected_focus, timeout=0.6):
    """Restore focus to the captured field before pasting.

    Strategy A — UIA SetFocus(): works for any control the PS worker has a live
    AutomationElement for (Notepad, regular Edit controls, ProseMirror, etc.).

    Strategy B — Win32 SetForegroundWindow(): used as fallback for Electron/
    browser apps (Chrome_WidgetWin_1, MozillaWindowClass, etc.) where UIA
    cannot reach the internal input but we can bring the host window to front
    and let the OS route Ctrl+V to whatever is focused inside it.

    Returns (restored: bool, detail: str, used_win32_fallback: bool).
    Callers that fire SendInput after this must honour used_win32_fallback by
    waiting an additional settle period before sending keys.
    """
    if not expected_focus:
        return False, "no target to restore", False
    u = expected_focus.get("uia")
    top = expected_focus.get("top")

    is_uia_target = is_editable_uia(u)
    is_win_fallback = is_editable_window_fallback(top, u)

    target_hwnd = (top or {}).get("top_hwnd") if top else None

    if not is_uia_target and not is_win_fallback and not target_hwnd:
        return False, "no editable target to restore", False

    # --- Strategy A: UIA SetFocus ---
    if is_uia_target:
        if not uia.restore_last_editable():
            return False, "UIA focus restore service unavailable", False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.03)
            snap_top = foreground_snapshot()
            snap_u = uia.snapshot()
            current = {"top": snap_top, "uia": snap_u}
            if focus_is_same(expected_focus, current):
                L("TARGET FOCUS RESTORED runtime=%s top=%s",
                  (u or {}).get("RuntimeId"),
                  hex(snap_top.get("top_hwnd", 0)) if snap_top else "0x0")
                return True, "target focus restored", False
        # UIA SetFocus timed out — fall through to Win32 fallback
        if not target_hwnd:
            return False, "target focus could not be restored", False

    # --- Strategy B: Win32 SetForegroundWindow (Electron/browser fallback, and
    # also used when UIA SetFocus timed out for any app with a known hwnd) ---
    if not target_hwnd:
        return False, "no window handle for fallback focus restore", False
    try:
        # Bring the target window to foreground using the standard Win32 trick:
        # attach to its thread's input queue first so SetForegroundWindow works
        # from a different process.
        target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
        our_tid = kernel32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(our_tid, target_tid, True)
        # Simpler: just check IsIconic (minimized) before calling ShowWindow
        if user32.IsIconic(target_hwnd):
            user32.ShowWindow(target_hwnd, SW_RESTORE)
        # Explicitly grant foreground permission before switching. This is
        # important when GEMINIDICTATE is running behind Kiro/Chrome/Electron.
        try:
            user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        user32.BringWindowToTop(target_hwnd)
        user32.SetForegroundWindow(target_hwnd)
        if attached:
            # Keep the input queues attached long enough to establish the
            # foreground/active window, then detach only after SetFocus has had
            # a chance to route through the target application's UI thread.
            user32.SetActiveWindow(target_hwnd)
            user32.SetFocus(target_hwnd)
            time.sleep(0.08)
            user32.AttachThreadInput(our_tid, target_tid, False)
        # Wait for the OS foreground switch to commit (and for Electron/browser
        # applications to re-focus their internal editor).
        time.sleep(0.18)
        snap_top = foreground_snapshot()
        if snap_top and snap_top.get("top_hwnd") == target_hwnd:
            L("TARGET FOCUS RESTORED (win32 fallback) hwnd=%s", hex(target_hwnd))
            return True, "target focus restored via win32 fallback", True
        # One more explicit foreground attempt after a brief pause.
        time.sleep(0.12)
        user32.AllowSetForegroundWindow(-1)
        user32.BringWindowToTop(target_hwnd)
        user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.18)
        snap_top = foreground_snapshot()
        if snap_top and snap_top.get("top_hwnd") == target_hwnd:
            L("TARGET FOCUS RESTORED (win32 fallback retry) hwnd=%s", hex(target_hwnd))
            return True, "target focus restored via win32 fallback (retry)", True
        return False, "win32 fallback: window did not become foreground", False
    except Exception as e:
        return False, f"win32 fallback exception: {e}", False


def verify_target_focus_after_paste(expected_focus, timeout=0.45):
    """Confirm the captured editable is still the active target after Ctrl+V."""
    if not expected_focus:
        return False, "no expected target"
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        snap = {"top": foreground_snapshot(), "uia": uia.snapshot()}
        last = snap
        if focus_is_same(expected_focus, snap):
            return True, "target focus verified after paste"
        time.sleep(0.05)
    return False, f"target focus verification failed current={last!r}"


def paste_without_stealing_focus(text, expected_focus):
    """Restore and paste into the exact captured field."""
    if not text:
        return False, "empty text"

    restored, detail, used_win32_fallback = restore_target_focus(expected_focus)
    if not restored:
        return False, detail

    ok, detail = clipboard_set_and_verify(text)
    if not ok:
        return False, detail

    # Win32 foreground restoration can complete before Chrome/Electron/other
    # applications have re-established their internal editor focus.
    if used_win32_fallback:
        time.sleep(0.18)

    last_error = None
    for attempt in range(2):
        try:
            send_ctrl_v()
            return True, "paste keystroke sent"
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(0.12)

    return False, last_error or "paste keystroke failed"


# ---------------------------------------------------------------------------
def _clear_clipboard_now():
    """Immediately empty the clipboard. Retries briefly if another app has it open."""
    for _ in range(5):
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()
            return
        time.sleep(0.05)


def insert_live_final(text):
    """Insert one authoritative finalized Gemini transcription chunk."""
    if not text or not text.strip():
        return False, "empty final text"

    # PROMPT mode: land transcription in the widget textbox, not an external field
    if prompt_mode_active:
        global _prompt_confirmed_text
        chunk = text.strip()
        try:
            # Build the full confirmed transcript: all previous turns + this full turn.
            # _prompt_confirmed_text accumulates across multiple VAD turns in one session.
            _prompt_confirmed_text = (_prompt_confirmed_text.rstrip() + " " + chunk).strip() if _prompt_confirmed_text else chunk
            prompt_textbox.config(state="normal")
            _apply_markdown_tags(prompt_textbox, _prompt_confirmed_text)
            _configure_md_tags_input(prompt_textbox)
            prompt_textbox.see("end")
            L("PROMPT MODE TRANSCRIPT CHUNK chars=%d total=%d", len(chunk), len(_prompt_confirmed_text))
        except Exception as e:
            L("PROMPT TEXTBOX INSERT ERROR: %s", e)
        return True, "appended to prompt textbox"
    try:
        replacement = text.strip()
        if replacement.endswith('.'):
            replacement = replacement[:-1] + ','

        ok, detail = clipboard_set_and_verify(replacement + " ")
        if not ok:
            return False, detail

        u_now = uia.snapshot()
        already_focused = recording_focus and focus_is_same(
            recording_focus, {"top": foreground_snapshot(), "uia": u_now}
        )

        used_win32_fallback = False
        if not already_focused:
            restored, restore_detail, used_win32_fallback = restore_target_focus(recording_focus)
            if not restored:
                add_clipboard_fallback(replacement, "target focus restore failed")
                return False, "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"
            L("TARGET FOCUS RESTORED runtime=%s fallback=%s",
              recording_focus.get("uia", {}).get("RuntimeId") if recording_focus else None,
              used_win32_fallback)

        if used_win32_fallback:
            time.sleep(0.18)

        paste_error = None
        for attempt in range(2):
            try:
                send_ctrl_v()
                paste_error = None
                break
            except Exception as e:
                paste_error = str(e)
                L("PASTE ATTEMPT %d FAILED: %s", attempt + 1, paste_error)
                if attempt == 0:
                    time.sleep(0.12)

        if paste_error:
            L("PASTE FAILED after focus restore: %s", paste_error)
            add_clipboard_fallback(replacement, "SendInput failure")
            return False, "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"

        # SendInput succeeding only means Windows accepted the keystrokes. The
        # target editor may still have lost its internal focus while its host
        # window was restored. Verify the exact captured editable before we
        # restore the user's clipboard.
        time.sleep(0.35)
        verified, verify_detail = verify_target_focus_after_paste(recording_focus, 0.45)
        if not verified:
            L("PASTE VERIFICATION FAILED: %s", verify_detail)
            add_clipboard_fallback(replacement, "target focus not verified after paste")
            return False, "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"

        restore_ok, restore_detail = restore_saved_clipboard("live segment pasted")
        if not restore_ok:
            L("CLIPBOARD RESTORE FAILED after verified paste: %s", restore_detail)
            return False, "PASTE VERIFIED — CLIPBOARD RESTORE FAILED: " + restore_detail
        show_clipboard_fallback(False)
        L("LIVE FINAL PASTE SENT chars=%d clipboard_restored=True", len(replacement))
        return True, "final text paste sent; clipboard restored"
    except Exception as e:
        add_clipboard_fallback(replacement, "paste exception")
        return False, "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"

# Gemini live transcription# ---------------------------------------------------------------------------

_SILENCE_THRESHOLD = 200  # int16 RMS below this = silence (adjust if too sensitive)

def mic_cb(indata, frames, time_info, status):
    try:
        if status:
            L("MIC STATUS: %s", status)
        block = indata[:, 0].copy()
        # Feed the main START/mic orb with the same live microphone signal.
        # int16 RMS is normalized to 0..1 for the orb's amplitude mapping.
        try:
            _btn_rms[0] = min(1.0, float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0)
        except Exception:
            pass
        q.put_nowait(block)
    except queue.Full:
        L("MIC QUEUE FULL  DROPPED AUDIO BLOCK")
    except Exception:
        L("MIC CALLBACK ERROR\n%s", traceback.format_exc())


def normalize_transcript_chunks(chunks):
    parts = [str(x).strip() for x in chunks if str(x).strip()]
    if not parts:
        return ""
    text = " ".join(parts)
    text = re.sub(r"\s+([,.;:!?%\)\]\}])", r"\1", text)
    text = re.sub(r"([\(\[\{])\s+", r"\1", text)
    text = re.sub(r"\s+(['-])", r"\1", text)
    return text.strip()
def insert_live_progressive(text):
    """Enqueue a stable word batch for serialized paste on the Tkinter thread."""
    if not text or not text.strip():
        return False, "empty progressive text"
    _progressive_paste_queue.put(text.strip())
    _drain_progressive_queue()
    return True, "queued"


def _drain_progressive_queue():
    """Drain the progressive paste queue one item at a time on the Tkinter thread.
    Called from the main thread only. Does nothing if a paste is already in progress."""
    global _progressive_paste_busy
    if _progressive_paste_busy:
        return
    try:
        batch = _progressive_paste_queue.get_nowait()
    except queue.Empty:
        return
    _progressive_paste_busy = True
    _do_progressive_paste(batch)


def _do_progressive_paste(batch):
    """Set clipboard to batch, paste it. No focus restore — during live recording
    the target field is expected to remain focused. The final paste handles any
    needed focus restore. No clipboard restore either — the original is restored
    once by insert_live_final after the VAD turn completes.
    In PROMPT mode there is no external target — progressive words are shown live
    in the prompt textbox instead of being pasted via clipboard."""
    global _progressive_paste_busy
    try:
        if prompt_mode_active:
            # Show progressive words live in the prompt textbox.
            # Render: confirmed turns so far + all words committed this turn so far.
            # _prompt_progressive_text tracks the running committed text for this turn.
            try:
                global _prompt_progressive_text
                _prompt_progressive_text = (_prompt_progressive_text.rstrip() + " " + batch).strip() if _prompt_progressive_text else batch
                base = _prompt_confirmed_text.rstrip() if _prompt_confirmed_text else ""
                preview = (base + " " + _prompt_progressive_text).strip() if base else _prompt_progressive_text
                prompt_textbox.config(state="normal")
                _apply_markdown_tags(prompt_textbox, preview)
                _configure_md_tags_input(prompt_textbox)
                prompt_textbox.see("end")
                live_insert_status.set(f"LIVE: …{batch[-40:]}")
            except Exception as e:
                L("PROMPT PROGRESSIVE DISPLAY ERROR: %s", e)
            return

        replacement = batch + " "
        ok, detail = clipboard_set_and_verify(replacement)
        if not ok:
            L("PROGRESSIVE CLIPBOARD SET FAILED: %s", detail)
            return

        send_ctrl_v()
        L("PROGRESSIVE PASTE DONE batch=%r", batch)
        live_insert_status.set(f"LIVE: …{batch[-40:]}")
    except Exception as e:
        add_clipboard_fallback(batch, "progressive paste exception")
        L("PROGRESSIVE PASTE EXCEPTION: %s", e)
    finally:
        _progressive_paste_busy = False
        root.after(40, _drain_progressive_queue)


async def record_once(live_mode):
    global recording

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing")

    client = genai.Client(api_key=key)
    # Keep server-side automatic VAD enabled. This lets Gemini finalize speech
    # turns while the user is still recording, which is required for true LIVE
    # dictation. STOP uses audio_stream_end to flush the final active turn.
    cfg = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[]),
    )

    pieces = []
    recv_count = 0
    audio_sent = 0
    recv_done = asyncio.Event()

    # LIVE progressive transcription state.
    # A word is only committed to the target field once it has appeared at the
    # same position in TWO consecutive interim updates unchanged. This prevents
    # pasting Gemini's speculative guesses that get revised on the next frame.
    live_committed_word_count = 0   # words already pasted (never decremented)
    live_committed_text = ""        # exact text pasted so far (for final dedup)
    live_prev_interim_words = []    # word list from the previous interim update
    LIVE_SAFE_TAIL_WORDS = 2        # extra tail held back even after 2-frame confirm

    try:
        L("CONNECTING mode=%s model=%s", "LIVE" if live_mode else "BUFFERED", MODEL)
        async with client.aio.live.connect(model=MODEL, config=cfg) as session:
            L("GEMINI CONNECTED model=%s", MODEL)

            async def recv():
                nonlocal recv_count, live_committed_word_count, live_committed_text, live_prev_interim_words
                try:
                    async for response in session.receive():
                        recv_count += 1
                        sc = getattr(response, "server_content", None)
                        if sc is None:
                            continue

                        # --- Interim: only commit words confirmed in 2 consecutive frames ---
                        interim = getattr(sc, "interim_input_transcription", None)
                        if interim is not None and getattr(interim, "text", None):
                            interim_text = str(interim.text).strip()
                            root.after(0, update_live_transcript, interim_text, False)

                            if live_mode:
                                cur_words = interim_text.split()

                                # Count how many leading words are identical between
                                # the previous interim and the current one.
                                # These have survived at least one Gemini revision cycle.
                                confirmed_len = 0
                                for a, b in zip(live_prev_interim_words, cur_words):
                                    if a.casefold() == b.casefold():
                                        confirmed_len += 1
                                    else:
                                        break

                                # Commit the confirmed prefix minus the safe tail,
                                # but only words beyond what's already been pasted.
                                commit_up_to = max(confirmed_len - LIVE_SAFE_TAIL_WORDS, 0)
                                if commit_up_to > live_committed_word_count:
                                    new_words = cur_words[live_committed_word_count:commit_up_to]
                                    batch = " ".join(new_words)
                                    live_committed_word_count = commit_up_to
                                    live_committed_text = " ".join(cur_words[:commit_up_to])
                                    L("INTERIM PROGRESSIVE batch=%r confirmed_prefix=%d committed_words=%d",
                                      batch, confirmed_len, live_committed_word_count)
                                    # Enqueue on the Tkinter thread — serialized, never overlapping
                                    root.after(0, insert_live_progressive, batch)

                                live_prev_interim_words = cur_words

                        # --- Final: paste only the words not yet committed ---
                        inp = getattr(sc, "input_transcription", None)
                        if inp is not None and getattr(inp, "text", None):
                            txt = str(inp.text).strip()
                            pieces.append(txt)
                            root.after(0, update_live_transcript, txt, True)
                            L("FINAL INPUT TRANSCRIPTION #%d chars=%d text=%r", recv_count, len(txt), txt)
                            if live_mode:
                                final_words = txt.split()
                                # Find how many leading words of the final match
                                # what we already committed, then only paste the rest.
                                skip = 0
                                committed_split = live_committed_text.split() if live_committed_text else []
                                while (skip < len(committed_split)
                                       and skip < len(final_words)
                                       and committed_split[skip].casefold() == final_words[skip].casefold()):
                                    skip += 1

                                if skip < len(committed_split):
                                    # Gemini revised words we already committed —
                                    # we can't retract them, so skip by count only.
                                    skip = min(live_committed_word_count, len(final_words))
                                    L("LIVE FINAL HYPOTHESIS REVISION committed_words=%d final_words=%d skip=%d",
                                      live_committed_word_count, len(final_words), skip)

                                final_remainder = " ".join(final_words[skip:]).strip()
                                # Reset state for next speech turn
                                live_committed_word_count = 0
                                live_committed_text = ""
                                live_prev_interim_words = []

                                if final_remainder or prompt_mode_active:
                                    def insert_final(chunk=final_remainder, full=txt):
                                        global _prompt_progressive_text
                                        # Flush any queued progressive batches first.
                                        while not _progressive_paste_queue.empty():
                                            try:
                                                _progressive_paste_queue.get_nowait()
                                            except queue.Empty:
                                                break
                                        # Reset per-turn progressive accumulator so the
                                        # next turn starts fresh in the textbox.
                                        _prompt_progressive_text = ""
                                        # PROMPT mode gets the full final text so
                                        # _prompt_confirmed_text is complete, not just
                                        # the post-committed remainder.
                                        insert_text = full if prompt_mode_active else chunk
                                        if not insert_text:
                                            restore_saved_clipboard("final: no remainder, restore only")
                                            return
                                        ok, detail = insert_live_final(insert_text)
                                        if ok:
                                            live_insert_status.set(f"LIVE OUTPUT: {len(insert_text):,} chars")
                                        else:
                                            live_insert_status.set(detail)
                                            focus_status.set("CLIPBOARD FALLBACK ACTIVE")
                                            state_label.config(fg="#f59e0b")
                                            L("LIVE FINAL INSERT FAILED chars=%d: %s", len(insert_text), detail)
                                    root.after(0, insert_final)
                                else:
                                    # No remainder and not PROMPT — restore clipboard now.
                                    def restore_only():
                                        while not _progressive_paste_queue.empty():
                                            try:
                                                _progressive_paste_queue.get_nowait()
                                            except queue.Empty:
                                                break
                                        restore_saved_clipboard("final: no remainder, restore only")
                                    root.after(0, restore_only)

                        if getattr(sc, "turn_complete", False):
                            L("SERVER TURN COMPLETE")
                        if getattr(sc, "interrupted", False):
                            L("SERVER INTERRUPTED")
                except asyncio.CancelledError:
                    L("RECEIVER CANCELLED")
                    raise
                except Exception:
                    L("RECEIVER ERROR\n%s", traceback.format_exc())
                finally:
                    recv_done.set()
                    L("RECEIVER EXIT recv_count=%d transcript_chunks=%d", recv_count, len(pieces))

            rt = asyncio.create_task(recv())
            # Automatic VAD is enabled, so no manual activity_start is needed.
            L("AUTOMATIC VAD ACTIVE")

            with sd.InputStream(
                device=DEVICE,
                samplerate=RATE_IN,
                channels=1,
                dtype="int16",
                blocksize=BLOCK,
                callback=mic_cb,
            ):
                L("MIC STREAM OPEN mode=%s", "LIVE" if live_mode else "BUFFERED")
                while recording:
                    try:
                        x = await asyncio.to_thread(q.get, True, 0.1)
                    except queue.Empty:
                        continue
                    y = resample_poly(x, RATE_OUT, RATE_IN).astype(np.int16)
                    payload = y.tobytes()
                    await session.send_realtime_input(
                        audio=types.Blob(data=payload, mime_type="audio/pcm;rate=16000")
                    )
                    audio_sent += 1
                    if audio_sent == 1 or audio_sent % 20 == 0:
                        L("AUDIO SENT block=%d bytes=%d queue=%d", audio_sent, len(payload), q.qsize())

            L("MIC STREAM CLOSED audio_blocks=%d", audio_sent)
            await session.send_realtime_input(audio_stream_end=True)
            L("AUDIO STREAM END SENT mode=%s", "LIVE" if live_mode else "BUFFERED")
            if live_mode:
                # Finalized chunks are inserted during recording. Drain briefly after
                # STOP so the authoritative VAD final is not cancelled prematurely.
                try:
                    await asyncio.wait_for(recv_done.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    L("LIVE FINAL DRAIN TIMEOUT; cancelling receiver")
                if not rt.done():
                    rt.cancel()
                    try:
                        await rt
                    except asyncio.CancelledError:
                        pass
                text = normalize_transcript_chunks(pieces)
            else:
                L("AUDIO STREAM END SENT; waiting %.1fs", FINALIZE_SECONDS)
                try:
                    await asyncio.wait_for(recv_done.wait(), timeout=FINALIZE_SECONDS)
                except asyncio.TimeoutError:
                    L("FINALIZATION TIMEOUT; waiting extra 2s")
                    try:
                        await asyncio.wait_for(recv_done.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        L("FINAL DRAIN TIMEOUT; cancelling receiver")
                if not rt.done():
                    rt.cancel()
                    try:
                        await rt
                    except asyncio.CancelledError:
                        pass
                text = normalize_transcript_chunks(pieces)
            L(
                "SESSION END mode=%s recv_count=%d transcript_chunks=%d chars=%d audio_blocks=%d",
                "LIVE" if live_mode else "BUFFERED",
                recv_count,
                len(pieces),
                len(text),
                audio_sent,
            )
            return text
    except Exception:
        L("SESSION ERROR\n%s", traceback.format_exc())
        # If we accumulated any transcript before the error, return it rather
        # than losing it entirely. Re-raise only if there's nothing to show.
        if pieces:
            partial = normalize_transcript_chunks(pieces)
            L("SESSION PARTIAL RECOVERY chars=%d chunks=%d", len(partial), len(pieces))
            return partial
        raise


# ---------------------------------------------------------------------------
# UI state / workflow
# ---------------------------------------------------------------------------

def set_status(text):
    status.set(text)


def update_live_transcript(text, final):
    if final:
        transcript_preview.set("FINAL: " + text[-650:])
    else:
        transcript_preview.set("LIVE: " + text[-650:])


PROMPT_SYSTEM = (
    "You are an expert AI prompt engineer. "
    "Rewrite the following rough spoken input into a clean, structured, "
    "professional AI agent prompt. Output only the rewritten prompt, nothing else."
)

async def _call_rewrite_stream(raw_text, on_chunk):
    """Stream Gemini text model response, calling on_chunk(text) for each piece."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing")
    client = genai.Client(api_key=key)
    contents = PROMPT_SYSTEM + "\n\nInput: " + raw_text
    last_err = None
    for model in [PROMPT_MODEL] + PROMPT_FALLBACKS:
        try:
            full = []
            for chunk in client.models.generate_content_stream(model=model, contents=contents):
                piece = chunk.text or ""
                if piece:
                    full.append(piece)
                    await asyncio.to_thread(on_chunk, piece)
            result = "".join(full).strip()
            L("PROMPT REWRITE STREAM OK model=%s chars=%d", model, len(result))
            return result
        except Exception as e:
            last_err = e
            L("PROMPT REWRITE STREAM FAIL model=%s err=%s", model, e)
    raise RuntimeError(f"All prompt models failed: {last_err}")


def _resolve_font():
    """Pick best available readable sans-serif font on this Windows machine."""
    import tkinter.font as tkfont
    available = set(tkfont.families())
    for candidate in ("Roboto", "Inter", "Segoe UI Variable", "Segoe UI", "Arial"):
        if candidate in available:
            return candidate
    return "Segoe UI"

_UI_FONT = None  # resolved after root is created


def _apply_markdown_tags(widget, text):
    """Full markdown renderer for tk.Text — supports all standard syntax."""
    widget.config(state="normal")
    widget.delete("1.0", "end")
    if not text:
        return

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # --- Fenced code block ---
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines)
            widget.insert("end", code_text + "\n", "code_block")
            i += 1
            continue

        # --- Table ---
        if "|" in line and i + 1 < len(lines) and re.match(r"^[\|\s\-:]+$", lines[i + 1]):
            # Parse header row
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 1  # skip separator
            # Alignment from separator
            sep_cols = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            aligns = []
            for s in sep_cols:
                if s.startswith(":") and s.endswith(":"):
                    aligns.append("center")
                elif s.endswith(":"):
                    aligns.append("right")
                else:
                    aligns.append("left")
            # Header row
            widget.insert("end", " | ".join(headers) + "\n", "table_header")
            widget.insert("end", "─" * 50 + "\n", "table_divider")
            i += 1
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                widget.insert("end", " | ".join(cells) + "\n", "table_row")
                i += 1
            continue

        # --- Blockquote (nested OK) ---
        if line.startswith(">"):
            depth = 0
            while depth < len(line) and line[depth] == ">":
                depth += 1
            content = line[depth:].lstrip()
            tag = "blockquote2" if depth > 1 else "blockquote"
            _insert_inline(widget, content + "\n", tag)
            i += 1
            continue

        # --- Headings ---
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            content = m.group(2)
            tag = f"h{level}"
            _insert_inline(widget, content + "\n", tag)
            i += 1
            continue

        # --- Horizontal rule ---
        if re.match(r"^[-*_]{3,}\s*$", line):
            widget.insert("end", "─" * 52 + "\n", "hr")
            i += 1
            continue

        # --- Unordered list (nested by indent) ---
        m = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if m:
            indent = len(m.group(1))
            content = m.group(2)
            tag = "bullet2" if indent >= 2 else "bullet"
            prefix = "    • " if indent >= 2 else "  • "
            _insert_inline(widget, prefix + content + "\n", tag)
            i += 1
            continue

        # --- Ordered list ---
        m = re.match(r"^(\s*)\d+[.)]\s+(.*)", line)
        if m:
            indent = len(m.group(1))
            num_m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
            num = num_m.group(2) if num_m else "1"
            content = num_m.group(3) if num_m else m.group(2)
            tag = "numbered2" if indent >= 2 else "numbered"
            prefix = f"      {num}. " if indent >= 2 else f"  {num}. "
            _insert_inline(widget, prefix + content + "\n", tag)
            i += 1
            continue

        # --- Blank line ---
        if not line.strip():
            widget.insert("end", "\n", "normal")
            i += 1
            continue

        # --- Normal paragraph ---
        _insert_inline(widget, line + "\n", "normal")
        i += 1


def _insert_inline(widget, text, base_tag):
    """Insert text with inline markdown (bold, italic, code, links, images)."""
    # Pattern order matters: images before links, bold+italic combos before singles
    pattern = re.compile(
        r"!\[([^\]]*)\]\([^\)]*(?:\s+\"[^\"]*\")?\)"  # image
        r"|\[([^\]]+)\]\(([^\)]+)\)"                   # link
        r"|\*\*\*(.+?)\*\*\*"                          # bold+italic
        r"|___(.+?)___"                                 # bold+italic alt
        r"|\*\*(.+?)\*\*"                               # bold
        r"|__(.+?)__"                                   # bold alt
        r"|\*(.+?)\*"                                   # italic
        r"|_(.+?)_"                                     # italic alt
        r"|`([^`]+)`"                                   # inline code
    )
    pos = 0
    for m in pattern.finditer(text):
        # Plain text before this match
        if m.start() > pos:
            widget.insert("end", text[pos:m.start()], base_tag)
        g = m.groups()
        if g[0] is not None:          # image
            widget.insert("end", f"[image: {g[0]}]", "img")
        elif g[1] is not None:        # link
            widget.insert("end", g[1], "link")
        elif g[3] is not None:        # bold+italic ***
            widget.insert("end", g[3], "bold_italic")
        elif g[4] is not None:        # bold+italic ___
            widget.insert("end", g[4], "bold_italic")
        elif g[5] is not None:        # bold **
            widget.insert("end", g[5], "bold")
        elif g[6] is not None:        # bold __
            widget.insert("end", g[6], "bold")
        elif g[7] is not None:        # italic *
            widget.insert("end", g[7], "italic")
        elif g[8] is not None:        # italic _
            widget.insert("end", g[8], "italic")
        elif g[9] is not None:        # inline code
            widget.insert("end", g[9], "inline_code")
        pos = m.end()
    if pos < len(text):
        widget.insert("end", text[pos:], base_tag)


def _configure_md_tags_input(widget):
    f = _UI_FONT or "Segoe UI"
    widget.tag_configure("normal",      font=(f, 9),               foreground="#94a3b8")
    widget.tag_configure("bold",        font=(f, 9, "bold"),       foreground="#e2e8f0")
    widget.tag_configure("italic",      font=(f, 9, "italic"),     foreground="#94a3b8")
    widget.tag_configure("bold_italic", font=(f, 9, "bold italic"),foreground="#e2e8f0")
    widget.tag_configure("h1",          font=(f, 12, "bold"),      foreground="#f1f5f9", spacing1=6, spacing3=2)
    widget.tag_configure("h2",          font=(f, 10, "bold"),      foreground="#e2e8f0", spacing1=4, spacing3=1)
    widget.tag_configure("h3",          font=(f, 9, "bold"),       foreground="#cbd5e1", spacing1=3)
    widget.tag_configure("h4",          font=(f, 9, "bold"),       foreground="#94a3b8", spacing1=2)
    widget.tag_configure("h5",          font=(f, 9, "bold"),       foreground="#64748b", spacing1=2)
    widget.tag_configure("h6",          font=(f, 9, "italic"),     foreground="#475569", spacing1=2)
    widget.tag_configure("bullet",      font=(f, 9),               foreground="#94a3b8",  lmargin1=8,  lmargin2=18)
    widget.tag_configure("bullet2",     font=(f, 9),               foreground="#64748b",  lmargin1=22, lmargin2=32)
    widget.tag_configure("numbered",    font=(f, 9),               foreground="#94a3b8",  lmargin1=8,  lmargin2=22)
    widget.tag_configure("numbered2",   font=(f, 9),               foreground="#64748b",  lmargin1=22, lmargin2=36)
    widget.tag_configure("blockquote",  font=(f, 9, "italic"),     foreground="#64748b",  lmargin1=12, lmargin2=12)
    widget.tag_configure("blockquote2", font=(f, 9, "italic"),     foreground="#475569",  lmargin1=24, lmargin2=24)
    widget.tag_configure("code_block",  font=("Consolas", 8),      foreground="#fbbf24",  background="#12120e")
    widget.tag_configure("inline_code", font=("Consolas", 8),      foreground="#fbbf24",  background="#12120e")
    widget.tag_configure("table_header",font=(f, 9, "bold"),       foreground="#e2e8f0")
    widget.tag_configure("table_divider",font=("Consolas", 7),     foreground="#334155")
    widget.tag_configure("table_row",   font=(f, 9),               foreground="#94a3b8")
    widget.tag_configure("hr",          font=("Consolas", 7),      foreground="#334155")
    widget.tag_configure("link",        font=(f, 9, "underline"),  foreground="#60a5fa")
    widget.tag_configure("img",         font=(f, 8, "italic"),     foreground="#818cf8")

def _configure_md_tags_result(widget):
    f = _UI_FONT or "Segoe UI"
    widget.tag_configure("normal",      font=(f, 9),               foreground="#cbd5e1")
    widget.tag_configure("bold",        font=(f, 9, "bold"),       foreground="#f1f5f9")
    widget.tag_configure("italic",      font=(f, 9, "italic"),     foreground="#cbd5e1")
    widget.tag_configure("bold_italic", font=(f, 9, "bold italic"),foreground="#f1f5f9")
    widget.tag_configure("h1",          font=(f, 13, "bold"),      foreground="#34d399", spacing1=8, spacing3=3)
    widget.tag_configure("h2",          font=(f, 11, "bold"),      foreground="#6ee7b7", spacing1=6, spacing3=2)
    widget.tag_configure("h3",          font=(f, 10, "bold"),      foreground="#a7f3d0", spacing1=4, spacing3=1)
    widget.tag_configure("h4",          font=(f, 9, "bold"),       foreground="#cbd5e1", spacing1=3)
    widget.tag_configure("h5",          font=(f, 9, "bold"),       foreground="#94a3b8", spacing1=2)
    widget.tag_configure("h6",          font=(f, 9, "italic"),     foreground="#64748b", spacing1=2)
    widget.tag_configure("bullet",      font=(f, 9),               foreground="#94a3b8",  lmargin1=10, lmargin2=20)
    widget.tag_configure("bullet2",     font=(f, 9),               foreground="#64748b",  lmargin1=26, lmargin2=36)
    widget.tag_configure("numbered",    font=(f, 9),               foreground="#94a3b8",  lmargin1=10, lmargin2=24)
    widget.tag_configure("numbered2",   font=(f, 9),               foreground="#64748b",  lmargin1=26, lmargin2=40)
    widget.tag_configure("blockquote",  font=(f, 9, "italic"),     foreground="#34d399",  lmargin1=14, lmargin2=14)
    widget.tag_configure("blockquote2", font=(f, 9, "italic"),     foreground="#6ee7b7",  lmargin1=28, lmargin2=28)
    widget.tag_configure("code_block",  font=("Consolas", 8),      foreground="#fbbf24",  background="#12120e")
    widget.tag_configure("inline_code", font=("Consolas", 8),      foreground="#fbbf24",  background="#12120e")
    widget.tag_configure("table_header",font=(f, 9, "bold"),       foreground="#34d399")
    widget.tag_configure("table_divider",font=("Consolas", 7),     foreground="#064e3b")
    widget.tag_configure("table_row",   font=(f, 9),               foreground="#cbd5e1")
    widget.tag_configure("hr",          font=("Consolas", 7),      foreground="#064e3b")
    widget.tag_configure("link",        font=(f, 9, "underline"),  foreground="#34d399")
    widget.tag_configure("img",         font=(f, 8, "italic"),     foreground="#6ee7b7")

def submit_prompt():
    """Stream Gemini rewrite into the result box with markdown rendering."""
    raw = prompt_textbox.get("1.0", "end-1c").strip()
    if not raw:
        prompt_status.set("Nothing to rewrite — record something first")
        return
    prompt_submit_btn.config(state="disabled")
    prompt_status.set("⏳ Rewriting…")
    prompt_result_box.config(state="normal")
    prompt_result_box.delete("1.0", "end")

    _streamed_chunks = []

    def on_chunk(piece):
        _streamed_chunks.append(piece)

    fut = asyncio.run_coroutine_threadsafe(_call_rewrite_stream(raw, on_chunk), loop)
    _last_rendered = [0]  # how many chars of joined text we've rendered

    def _stream_tick():
        full_so_far = "".join(_streamed_chunks)
        if full_so_far:
            # Re-render with markdown on every tick (fast enough for streaming)
            _apply_markdown_tags(prompt_result_box, full_so_far)
            prompt_result_box.see("end")

        if not fut.done():
            root.after(80, _stream_tick)
            return

        # Final pass
        try:
            result = fut.result()
            _apply_markdown_tags(prompt_result_box, result)
            prompt_result_box.see("end")
            clipboard_set_and_verify(result)
            prompt_status.set("✓ Copied to clipboard")
            # Save for undo
            global _last_prompt_result
            _last_prompt_result = result
            # Save to history
            global _prompt_history, _history_index
            entry = {"raw": prompt_textbox.get("1.0", "end-1c").strip(), "result": result}
            _prompt_history.insert(0, entry)
            if len(_prompt_history) > 10:
                _prompt_history.pop()
            _history_index = -1
            try:
                undo_btn.config(state="normal", fg="#d4d4d8", bg=_P["panel_bg"])
                _update_history_nav()
            except Exception:
                pass
            L("PROMPT RESULT chars=%d", len(result))
        except Exception as e:
            prompt_status.set(f"⚠ {str(e)[:60]}")
            L("PROMPT REWRITE ERROR: %s", e)
        finally:
            prompt_submit_btn.config(state="normal")

    root.after(80, _stream_tick)


def clear_prompt():
    """Clear output only (not input), saving it for undo."""
    global _last_prompt_result
    current = prompt_result_box.get("1.0", "end-1c").strip()
    if current:
        _last_prompt_result = current
        try:
            undo_btn.config(state="normal", fg="#d4d4d8", bg=_P["panel_bg"])
        except Exception:
            pass
    prompt_result_box.config(state="normal")
    prompt_result_box.delete("1.0", "end")
    prompt_status.set("")


def new_prompt():
    """Reset both input and output to a fresh state."""
    global _last_prompt_result, _history_index
    # Save current output for undo before wiping
    current = prompt_result_box.get("1.0", "end-1c").strip()
    if current:
        _last_prompt_result = current
        try:
            undo_btn.config(state="normal", fg="#d4d4d8", bg=_P["panel_bg"])
        except Exception:
            pass
    _history_index = -1
    prompt_textbox.config(state="normal")
    prompt_textbox.delete("1.0", "end")
    prompt_result_box.config(state="normal")
    prompt_result_box.delete("1.0", "end")
    prompt_status.set("")
    _char_count_var.set("0 chars")
    try:
        _update_history_nav()
    except Exception:
        pass


def undo_prompt():
    """Restore the last rewritten output."""
    if not _last_prompt_result:
        prompt_status.set("Nothing to undo")
        return
    _apply_markdown_tags(prompt_result_box, _last_prompt_result)
    prompt_result_box.see("end")
    prompt_status.set("↩ Restored")
    try:
        undo_btn.config(state="disabled", fg=_P["muted2"])
    except Exception:
        pass


def _history_prev():
    """Navigate to an older history entry."""
    global _history_index
    if not _prompt_history:
        return
    new_idx = _history_index + 1
    if new_idx >= len(_prompt_history):
        return
    _history_index = new_idx
    _load_history_entry(_history_index)


def _history_next():
    """Navigate to a newer history entry (or back to current draft)."""
    global _history_index
    if _history_index <= 0:
        _history_index = -1
        prompt_status.set("")
        _update_history_nav()
        return
    _history_index -= 1
    _load_history_entry(_history_index)


def _load_history_entry(idx):
    """Display a history entry without triggering a new generation."""
    if idx < 0 or idx >= len(_prompt_history):
        return
    entry = _prompt_history[idx]
    prompt_textbox.config(state="normal")
    prompt_textbox.delete("1.0", "end")
    prompt_textbox.insert("end", entry["raw"])
    _apply_markdown_tags(prompt_result_box, entry["result"])
    prompt_result_box.see("1.0")
    total = len(_prompt_history)
    prompt_status.set(f"History {total - idx}/{total}")
    _update_char_count()
    _update_history_nav()


def _update_history_nav():
    """Enable/disable nav arrows based on current position."""
    try:
        can_prev = len(_prompt_history) > 0 and _history_index < len(_prompt_history) - 1
        can_next = _history_index >= 0
        hist_prev_btn.config(state="normal" if can_prev else "disabled",
                             fg="#d4d4d8" if can_prev else _P["muted2"])
        hist_next_btn.config(state="normal" if can_next else "disabled",
                             fg="#d4d4d8" if can_next else _P["muted2"])
        if _prompt_history:
            total = len(_prompt_history)
            pos = total - _history_index if _history_index >= 0 else total
            hist_count_label.config(text=f"{pos}/{total}")
        else:
            hist_count_label.config(text="")
    except Exception:
        pass


def toggle_conv_panel(show):
    """Show/hide the CONV orb row (attached to main widget) and caption Toplevel."""
    if show:
        prompt_panel.pack_forget()
        notes_panel.pack_forget()
        try:
            conv_panel.pack(fill="x", padx=4, pady=(0, 4))
        except Exception:
            pass
        _conv_caps_show()
    else:
        if _conv_is_connected:
            stop_conv()
        try:
            conv_panel.pack_forget()
        except Exception:
            pass
        _conv_caps_hide()


def toggle_prompt_panel(show):
    """Expand/collapse the PROMPT panel and resize the widget."""
    global prompt_mode_active
    prompt_mode_active = show
    if show:
        notes_panel.pack_forget()
        prompt_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
    else:
        prompt_panel.pack_forget()


def toggle_notes_panel(show):
    """Expand/collapse the Notes panel and resize the widget."""
    if show:
        prompt_panel.pack_forget()
        _notes_render_list()
        notes_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _notes_canvas.bind_all("<MouseWheel>", _notes_mousewheel)
    else:
        notes_panel.pack_forget()
        _notes_canvas.unbind_all("<MouseWheel>")


def reset_widget_state():
    global last_external_focus, recording_focus, stop_reason, fallback_clipboard_text, fallback_active
    last_external_focus = None
    recording_focus = None
    stop_reason = ""
    fallback_clipboard_text = ""
    fallback_active = False
    show_clipboard_fallback(False)
    focus_status.set(" Dictate")
    state_label.config(fg=MUTED)
    capture_label.set("")
    output_label.set("")
    transcript_preview.set("")
    live_insert_status.set("")
    result_label.set("")
    status.set("READY")
    # Reset prompt panel status label if it exists
    try:
        prompt_status.set("")
    except Exception:
        pass


def start_record():
    global recording, current_future, recording_focus, stop_reason, fallback_clipboard_text, fallback_active
    global saved_clipboard_text, saved_clipboard_has_text
    if current_future and not current_future.done():
        status.set("FINALIZING  please wait")
        L("START IGNORED  previous transcription is still finalizing")
        return

    # At START click time the widget may momentarily be foreground, causing
    # uia.snapshot() to return our own TkChild. Explicitly discard any UIA
    # snapshot that belongs to our own process before picking a candidate.
    our_pid = os.getpid()
    top_now = foreground_snapshot()
    u_now = uia.snapshot()

    # If UIA is reporting our own process, treat it as unavailable.
    if u_now and u_now.get("ProcessId") == our_pid:
        u_now = None

    current_focus = {"top": top_now, "uia": u_now}
    is_system_ui_foreground = bool(top_now and (top_now.get("top_class") or "") in _SYSTEM_UI_CLASSES)

    # Prefer live UIA if it's a valid external editable and NOT system UI,
    # otherwise fall back to the last known external focus.
    if (not is_system_ui_foreground
            and is_editable_uia(u_now)
            and top_now and top_now.get("top_hwnd") != widget_hwnd()):
        candidate = current_focus
    else:
        candidate = last_external_focus

    if not candidate or not (is_editable_uia(candidate.get("uia")) or is_editable_window_fallback(candidate.get("top"), candidate.get("uia"))):
        # PROMPT mode doesn't need an external text field — output goes into the widget
        if not prompt_mode_active:
            status.set("TEXT FIELD NOT READY")
            focus_status.set(" CLICK A TEXT FIELD")
            L("START REJECTED  current UIA focus is not editable current=%r candidate=%r", current_focus, candidate)
            return

    if prompt_mode_active:
        # Sentinel focus: no external target; insert_live_final will route to textbox
        recording_focus = {"top": None, "uia": None, "_prompt": True}
        description = "PROMPT PANEL"
        top = {}
        u = {}
    else:
        recording_focus = candidate
        top = candidate["top"]
        u = candidate["uia"]
        description = describe_focus(top, u)

        # The START click can move Windows focus onto the widget. Immediately
        # restore the captured field so the user gets the caret back without having
        # to click the text field a second time.
        restored, restore_detail, _used_win32_fallback = restore_target_focus(recording_focus)
        if not restored:
            recording_focus = None
            status.set("TEXT FIELD NOT READY")
            focus_status.set(" CLICK A TEXT FIELD")
            L("START REJECTED  could not restore target focus: %s", restore_detail)
            return

    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break

    # Flush any stale progressive paste batches from a previous session
    while not _progressive_paste_queue.empty():
        try:
            _progressive_paste_queue.get_nowait()
        except queue.Empty:
            break

    # Reset prompt progressive display state
    global _prompt_confirmed_text, _prompt_progressive_text
    _prompt_confirmed_text = ""
    _prompt_progressive_text = ""

    # Start each session with a clean fallback state. The current clipboard is
    # preserved as the user's baseline; if a paste fails later, only failed
    # Gemini output is promoted to the safe clipboard fallback.
    fallback_clipboard_text = ""
    fallback_active = False
    show_clipboard_fallback(False)

    # Preserve the user's existing text clipboard for the whole dictation
    # session. Gemini segments temporarily use the clipboard for Ctrl+V, but
    # the user's clipboard must be restored when the session finishes.
    clip_saved_ok, clip_saved_value = clipboard_get_text()
    saved_clipboard_has_text = bool(clip_saved_ok and clip_saved_value)
    saved_clipboard_text = clip_saved_value if saved_clipboard_has_text else None
    L("CLIPBOARD SNAPSHOT before recording has_text=%s chars=%d detail=%s",
      saved_clipboard_has_text,
      len(saved_clipboard_text or ""),
      "ok" if clip_saved_ok else clip_saved_value)

    recording = True
    stop_reason = ""
    mode_name = "LIVE" if mode == "live" else "BUFFERED"
    capture_label.set("INPUT FIELD: " + description)
    output_label.set("OUTPUT: " + ("LIVE INTO THIS FIELD" if mode == "live" else "THIS FIELD IF STILL FOCUSED AT STOP"))
    focus_status.set(" RECORDING")
    state_label.config(fg=STOP)
    result_label.set("Clipboard: will contain final transcript\nInput field: recording")
    transcript_preview.set("")
    live_insert_status.set("")
    status.set(f"RECORDING {mode_name}  speak now")
    if focus_lock:
        # Show the locked target app so user knows where output goes
        locked_app = (top.get("process") or "?").replace(".exe", "")
        focus_status.set(f"LOCKED: {locked_app}")
    else:
        focus_status.set(" RECORDING")
    btn.unbind("<Button-1>")
    btn.bind("<Button-1>", lambda e: stop_record("STOP button"))
    _btn_recording[0] = True
    _btn_rms[0] = 0.0
    _btn_orb_level[0] = 0.0
    _btn_orb_phase[0] = 0.0
    _btn_draw("recording")
    root.after(25, _btn_animate)
    mode_live.config(state="disabled")
    mode_buf.config(state="disabled")
    mode_pro.config(state="disabled")
    try:
        mode_not.config(state="disabled")
    except Exception:
        pass
    L(
        "START ACCEPTED mode=%s top=%s focus_uia=%r description=%r",
        mode_name,
        hex(top.get("top_hwnd", 0)),
        u,
        description,
    )
    current_future = asyncio.run_coroutine_threadsafe(
        record_once(mode == "live" or prompt_mode_active), loop
    )
    root.after(100, poll_result)


def stop_record(reason="user stop"):
    global recording, stop_reason, _focus_change_count
    if not recording:
        return
    stop_reason = reason
    recording = False
    _focus_change_count = 0
    if prompt_mode_active:
        status.set("STOPPING  finishing transcription")
    elif mode == "live":
        status.set("STOPPING  flushing last segment")
    else:
        status.set("FINALIZING  completing Gemini transcription")
    focus_status.set(" STOPPING")
    state_label.config(fg="#f59e0b")
    _btn_recording[0] = False
    _btn_draw("disabled")
    btn.unbind("<Button-1>")
    mode_live.config(state="disabled")
    mode_buf.config(state="disabled")
    mode_pro.config(state="disabled")
    L("STOP received reason=%r", reason)


def show_error_in_status(msg, duration_ms=4000):
    """Flash an error message in the focus_status label, then restore idle state."""
    # Truncate and clean up common API error noise
    clean = msg
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        clean = "429 rate limit — wait and retry"
    elif "1006" in msg or "abnormal closure" in msg.lower():
        clean = "connection dropped — retry"
    elif "APIError" in msg:
        clean = msg.split("APIError:")[-1].strip()[:60]
    else:
        clean = msg[:60]
    focus_status.set(f"⚠ {clean}")
    state_label.config(fg="#f59e0b")
    root.after(duration_ms, lambda: (
        focus_status.set(" Dictate"),
        state_label.config(fg=MUTED)
    ) if not recording else None)


def buffered_output(text):
    """Clipboard always gets the full result. Field gets it only if still focused."""
    clip_ok, clip_detail = clipboard_set_and_verify(text)
    L("BUFFERED CLIPBOARD ok=%s detail=%s", clip_ok, clip_detail)

    input_ok = False
    input_detail = "not attempted"
    if clip_ok:
        restored, restore_detail, used_win32_fallback = restore_target_focus(recording_focus)
        if restored:
            if used_win32_fallback:
                time.sleep(0.18)
            try:
                send_ctrl_v()
                time.sleep(0.35)
                verified, verify_detail = verify_target_focus_after_paste(recording_focus, 0.45)
                if not verified:
                    raise RuntimeError(verify_detail)
                restore_saved_clipboard("buffered result pasted")
                input_ok = True
                input_detail = "paste keystroke sent to captured text field"
                show_clipboard_fallback(False)
            except Exception as e:
                time.sleep(0.12)
                try:
                    send_ctrl_v()
                    time.sleep(0.35)
                    verified, verify_detail = verify_target_focus_after_paste(recording_focus, 0.45)
                    if not verified:
                        raise RuntimeError(verify_detail)
                    restore_saved_clipboard("buffered result pasted on retry")
                    input_ok = True
                    input_detail = "paste keystroke sent on retry"
                    show_clipboard_fallback(False)
                except Exception as retry_error:
                    add_clipboard_fallback(text, "buffered paste failed")
                    input_detail = "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"
        else:
            add_clipboard_fallback(text, "buffered target focus restore failed")
            input_detail = "PASTE FAILED — CLIPBOARD FALLBACK ACTIVE"
    return clip_ok, clip_detail, input_ok, input_detail

def poll_result():
    global current_future, recording_focus, recording
    if not current_future:
        return
    if not current_future.done():
        root.after(100, poll_result)
        return

    try:
        text = current_future.result()
        mode_name = "LIVE" if mode == "live" else ("PROMPT" if prompt_mode_active else "BUFFERED")
        if prompt_mode_active:
            # In PROMPT mode, live chunks already landed in the textbox via insert_live_final.
            # The final normalized text is the complete transcript — append anything not yet shown.
            if text:
                existing = prompt_textbox.get("1.0", "end-1c").strip()
                if not existing:
                    prompt_textbox.config(state="normal")
                    prompt_textbox.insert("end", text)
                    prompt_textbox.see("end")
            prompt_status.set("Ready — click Rewrite or record more")
            status.set("DONE  speak recorded")
        elif mode == "live":
            result_label.set("LIVE INPUT: inserted directly into the field")
            status.set("DONE LIVE")
        else:
            if text:
                status.set("OUTPUTTING BUFFERED RESULT")
                root.update_idletasks()
                clip_ok, clip_detail, input_ok, input_detail = buffered_output(text)
                result_label.set(
                    f"Clipboard: {'SUCCESS  verified' if clip_ok else 'FAILED  ' + clip_detail}\n"
                    f"Input field: {'SUCCESS  inserted' if input_ok else 'NOT INSERTED  ' + input_detail}"
                )
                status.set(f"DONE BUFFERED  {len(text):,} characters")
                transcript_preview.set("FINAL: " + text[-650:])
            else:
                result_label.set("Clipboard: NOT ATTEMPTED\nInput field: NOT ATTEMPTED  no transcript")
                status.set("DONE  no transcription received")
        L("FINAL UI RESULT mode=%s chars=%d stop_reason=%r", mode_name, len(text), stop_reason)
    except Exception as e:
        L("FUTURE ERROR %r\n%s", e, traceback.format_exc())
        err_short = str(e)[:120]
        result_label.set(f"Error: {err_short}")
        status.set("ERROR  session failed — ready to retry")
        root.after(0, show_error_in_status, str(e))
    finally:
        # Always recover to a fully usable state regardless of how we got here.
        recording = False
        recording_focus = None
        current_future = None
        _btn_recording[0] = False
        _btn_draw("idle")
        btn.unbind("<Button-1>")
        btn.bind("<Button-1>", lambda e: start_record())
        mode_live.config(state="normal")
        mode_buf.config(state="normal")
        mode_pro.config(state="normal")
        try:
            mode_not.config(state="normal")
        except Exception:
            pass
        focus_status.set(" Dictate")
        state_label.config(fg=MUTED)
        show_clipboard_fallback(False)


def set_mode(new_mode):
    global mode
    if recording:
        return
    mode = new_mode
    SEL_BG   = "#2d2d35"
    UNSEL_BG = "#09090b"
    SEL_FG   = "#f4f4f5"
    UNSEL_FG = "#52525b"
    is_prompt = (mode == "prompt")
    is_notes  = (mode == "notes")
    is_conv   = (mode == "conv")
    mode_live.config(bg=SEL_BG if mode == "live"     else UNSEL_BG,
                     fg=SEL_FG if mode == "live"     else UNSEL_FG)
    mode_buf.config( bg=SEL_BG if mode == "buffered" else UNSEL_BG,
                     fg=SEL_FG if mode == "buffered" else UNSEL_FG)
    mode_pro.config( bg=SEL_BG if is_prompt          else UNSEL_BG,
                     fg=SEL_FG if is_prompt          else UNSEL_FG)
    mode_not.config( bg=SEL_BG if is_notes           else UNSEL_BG,
                     fg=SEL_FG if is_notes           else UNSEL_FG)
    mode_conv.config(bg=SEL_BG if is_conv            else UNSEL_BG,
                     fg=SEL_FG if is_conv            else UNSEL_FG)

    # Set geometry FIRST — before panels pack, to avoid the resize glitch
    x, y = root.winfo_x(), root.winfo_y()
    if is_prompt or is_notes or is_conv:
        root.geometry(f"500x120+{x}+{y}" if is_conv else f"500x500+{x}+{y}")
    else:
        root.geometry(f"500x54+{x}+{y}")

    toggle_conv_panel(is_conv)
    toggle_prompt_panel(is_prompt)
    toggle_notes_panel(is_notes)

    # Hide main mic in NTS and CONV
    if is_notes or is_conv:
        btn.grid_remove()
    else:
        btn.grid()

    # Hide lock button on PRO, NTS and CONV
    if is_prompt or is_notes or is_conv:
        lock_btn.grid_remove()
    else:
        lock_btn.grid()
    if is_prompt:
        mode_help.set("PROMPT")
        focus_status.set(" Dictate prompt")
        state_label.config(fg=MUTED)
    elif is_notes:
        mode_help.set("NOTES")
        focus_status.set(" Dictate")
        state_label.config(fg=MUTED)
    elif is_conv:
        mode_help.set("CONV")
        focus_status.set(" Voice Chat")
        state_label.config(fg=MUTED)
    else:
        mode_help.set("LIVE" if mode == "live" else "BUFFER")
    L("MODE CHANGED mode=%s", mode)


def toggle_mode():
    set_mode("buffered" if mode == "live" else "live")


def toggle_focus_lock():
    global focus_lock
    focus_lock = not focus_lock
    if focus_lock:
        _draw_lock(True)
        L("FOCUS LOCK ON  auto-stop on focus change disabled")
    else:
        _draw_lock(False)
        L("FOCUS LOCK OFF  auto-stop on focus change enabled")


def open_widget():
    # IMPORTANT: F8 does NOT capture a target. It only opens and resets the widget.
    if recording:
        stop_record("F8 pressed")
        return
    reset_widget_state()
    uia.start()
    root.deiconify()
    root.update_idletasks()
    root.lift()
    root.attributes("-topmost", True)
    _assert_topmost()
    root.after(30, _apply_rounded_corners)
    L("F8 WIDGET OPENED  target intentionally NOT captured")


def close_widget():
    if recording:
        stop_record("widget closed")
    if _conv_is_connected:
        stop_conv()
    _conv_caps_hide()
    root.withdraw()
    root.attributes("-topmost", False)
    reset_widget_state()
    L("WIDGET HIDDEN")


hotkey_queue = queue.Queue()
_hotkey_registered = False

# Window classes belonging to Windows shell / system UI — never valid dictation
# targets. Clicking START while these are foreground should be rejected.
_SYSTEM_UI_CLASSES = {
    "Shell_TrayWnd",          # taskbar
    "Shell_SecondaryTrayWnd", # secondary monitor taskbar
    "Windows.UI.Core.CoreWindow",  # Windows Search, Action Center, Start menu
    "SearchPane",             # legacy search pane
    "NotifyIconOverflowWindow",
    "MultitaskingViewFrame",  # Task View
    "XamlExplorerHostIslandWindow",  # Win11 widgets / start
    "Progman",                # desktop
    "WorkerW",                # desktop worker
}


def _assert_topmost():
    """Force the widget above all other windows including system UI overlays."""
    if root.state() == "withdrawn":
        return
    try:
        hwnd = user32.GetAncestor(int(root.winfo_id()), GA_ROOT) or int(root.winfo_id())
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception:
        pass


def _topmost_keepalive():
    """Re-assert HWND_TOPMOST every 500ms — beats Windows shell overlays."""
    _assert_topmost()
    root.after(500, _topmost_keepalive)



def hotkey_message_thread():
    """Register F8 as a real Windows global hotkey; no foreground focus required."""
    global _hotkey_registered
    if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F8):
        L("F8 GLOBAL HOTKEY REGISTER FAILED")
        return
    _hotkey_registered = True
    L("F8 GLOBAL HOTKEY REGISTERED")
    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("time", ctypes.c_uint),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]
    msg = MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and int(msg.wParam) == 1:
                hotkey_queue.put_nowait("F8")
    finally:
        user32.UnregisterHotKey(None, 1)
        L("F8 GLOBAL HOTKEY UNREGISTERED")


def hotkey_poll():
    try:
        while True:
            hotkey_queue.get_nowait()
            # F8 toggles: open if hidden, hide if visible
            if root.state() == "withdrawn":
                open_widget()
            else:
                close_widget()
    except queue.Empty:
        pass

    # Fallback: if global hotkey registration failed, poll F8 key state directly.
    if not _hotkey_registered:
        global _f8_was_down
        is_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
        if is_down and not _f8_was_down:
            if root.state() == "withdrawn":
                open_widget()
            else:
                close_widget()
        _f8_was_down = is_down

    root.after(30, hotkey_poll)


def idle_stop_poll():
    """Auto-stop recording if no keyboard/mouse interaction for IDLE_STOP_SECONDS."""
    if recording:
        idle = get_idle_seconds()
        if idle >= IDLE_STOP_SECONDS:
            L("IDLE AUTO-STOP  no user interaction for %.1fs", idle)
            focus_status.set(" IDLE — stopping")
            state_label.config(fg="#f59e0b")
            root.after(0, stop_record, "idle auto-stop (no interaction for 15s)")
        elif idle >= (IDLE_STOP_SECONDS - 5):
            remaining = max(1, int(IDLE_STOP_SECONDS - idle) + 1)
            focus_status.set(f"⏱ idle stop in {remaining}s")
            state_label.config(fg="#f59e0b")
        else:
            if focus_lock:
                pass
            else:
                focus_status.set(" RECORDING")
                state_label.config(fg=STOP)
    root.after(500, idle_stop_poll)


def restart_app():
    """Kill any stale gemini_dictate processes, then relaunch."""
    import sys
    L("RESTART REQUESTED — killing stale processes then relaunching")
    _kill_stale_processes()
    bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "START_Gemini_Dictate.bat")
    try:
        subprocess.Popen(
            [bat], shell=True, creationflags=CREATE_NO_WINDOW,
            cwd=os.path.dirname(bat)
        )
    except Exception as e:
        L("RESTART LAUNCH ERROR: %s", e)
    root.after(300, lambda: os._exit(0))


def _kill_stale_processes():
    """Kill other gemini_dictate.py processes (not this one)."""
    our_pid = os.getpid()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
        )
        for line in result.stdout.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try:
                    pid = int(parts[1])
                    if pid != our_pid:
                        # Check if it's running our script
                        cmd_result = subprocess.run(
                            ["wmic", "process", "where", f"ProcessId={pid}",
                             "get", "CommandLine", "/FORMAT:CSV"],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
                        )
                        if "gemini_dictate" in cmd_result.stdout.lower():
                            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                           creationflags=CREATE_NO_WINDOW, capture_output=True)
                            L("KILLED STALE PROCESS pid=%d", pid)
                except Exception:
                    pass
    except Exception as e:
        L("KILL STALE ERROR: %s", e)


# ---------------------------------------------------------------------------
# Single-instance enforcement — kill any previous instance before starting
# ---------------------------------------------------------------------------
_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".geminidictate.pid")

def _enforce_single_instance():
    """Kill any existing gemini_dictate process, then write our own PID."""
    our_pid = os.getpid()
    # Read stale PID file and kill that process first
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            if old_pid != our_pid:
                subprocess.run(["taskkill", "/F", "/PID", str(old_pid)],
                               creationflags=CREATE_NO_WINDOW, capture_output=True)
                L("KILLED PREVIOUS INSTANCE pid=%d", old_pid)
        except Exception:
            pass
    # Also scan for any other python processes running our script
    _kill_stale_processes()
    # Write our PID
    try:
        open(_PID_FILE, "w").write(str(our_pid))
    except Exception:
        pass

_enforce_single_instance()

# ---------------------------------------------------------------------------
# Compact dictation widget
# ---------------------------------------------------------------------------
_start_time = time.strftime("%Y-%m-%d %H:%M:%S")
L("━" * 60)
L("▶  GEMINIDICTATE SESSION START  %s  PID=%s", _start_time, os.getpid())
L("━" * 60)
L("cwd=%s", os.getcwd())
L("model=%s  api_key=%s", MODEL, "YES" if os.environ.get("GEMINI_API_KEY") else "MISSING")

root = tk.Tk()
root.title("GeminiDictate")
root.geometry("500x54+700+50")
root.resizable(False, False)
root.configure(bg="#121215")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.protocol("WM_DELETE_WINDOW", close_widget)
root.withdraw()

# App icon — used in taskbar and Alt+Tab even with overrideredirect
try:
    _ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geminidictate.ico")
    _logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini-dictate-logo.png")
    if os.path.exists(_ico_path):
        root.iconbitmap(_ico_path)
    if os.path.exists(_logo_path):
        from PIL import Image, ImageTk
        _logo_img = Image.open(_logo_path).resize((32, 32), Image.LANCZOS)
        _logo_tk  = ImageTk.PhotoImage(_logo_img)
        root.iconphoto(True, _logo_tk)
except Exception as _e:
    L("ICON LOAD ERROR: %s", _e)

_UI_FONT = _resolve_font()
L("UI FONT resolved to: %s", _UI_FONT)

def _apply_ui_font_to_all():
    """Walk all widgets and replace Segoe UI with the resolved font."""
    if _UI_FONT == "Segoe UI":
        return  # already correct, skip
    def _walk(w):
        try:
            cfg = w.config()
            if "font" in cfg:
                current = w.cget("font")
                if isinstance(current, str) and "Segoe UI" in current:
                    w.config(font=current.replace("Segoe UI", _UI_FONT))
                elif isinstance(current, (tuple, list)):
                    new = tuple(
                        _UI_FONT if (isinstance(p, str) and p == "Segoe UI") else p
                        for p in current
                    )
                    w.config(font=new)
        except Exception:
            pass
        for child in w.winfo_children():
            _walk(child)
    root.after(200, lambda: _walk(root))

# The widget is intentionally non-activating. Clicking START/STOP must NOT
# steal keyboard focus from the real text field.
def make_widget_no_activate():
    try:
        hwnd = int(root.winfo_id())
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle | WS_EX_NOACTIVATE)
        root.update_idletasks()
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | 0x0020 | 0x0040)
        root.update()
        L("WIDGET WS_EX_NOACTIVATE + BORDERLESS PILL APPLIED hwnd=%s", hex(hwnd))
    except Exception:
        L("WIDGET NO-ACTIVATE ERROR\n%s", traceback.format_exc())

root.after_idle(make_widget_no_activate)


def _apply_rounded_corners(r=12):
    """Clip the main window to a rounded rectangle using a Win32 region."""
    try:
        hwnd = int(user32.GetAncestor(int(root.winfo_id()), GA_ROOT))
        w    = root.winfo_width()
        h    = root.winfo_height()
        if w <= 1 or h <= 1:
            return
        d    = r * 2
        rgn  = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


_rounded_corners_job = [None]

def _reapply_rounded_corners(e=None):
    if _rounded_corners_job[0]:
        root.after_cancel(_rounded_corners_job[0])
    _rounded_corners_job[0] = root.after(80, _apply_rounded_corners)


root.bind("<Configure>", _reapply_rounded_corners)

status = tk.StringVar(value="READY")
focus_status = tk.StringVar(value=" CLICK TEXT FIELD")
capture_label = tk.StringVar(value="")
output_label = tk.StringVar(value="")
transcript_preview = tk.StringVar(value="")
live_insert_status = tk.StringVar(value="")
result_label = tk.StringVar(value="")
mode_help = tk.StringVar(value="LIVE")

# Fixed-width pill matching the supplied mockup: the state/validation area,
# mode switch, action button, and close button each have a permanent column.
# Nothing is packed relative to the text length, so state changes cannot move
# the controls around.
BG = "#121215"
BORDER = "#303036"
TEXT = "#e4e4e7"
MUTED = "#8b8b95"
ACTIVE = "#3f3f46"
STOP = "#22c55e"  # recording state color

frame = tk.Frame(root, padx=1, pady=1, bd=1, relief="solid", bg=BORDER)
frame.pack(fill="both", expand=True)
pill = tk.Frame(frame, padx=9, pady=6, bg=BG)
pill.pack(fill="x")

# Permanent columns: 1) status, 2) mode, 3) action, 4) lock, 5) close.
pill.grid_columnconfigure(0, minsize=150, weight=1)
pill.grid_columnconfigure(1, minsize=160, weight=0)
pill.grid_columnconfigure(2, minsize=75,  weight=0)
pill.grid_columnconfigure(3, minsize=28,  weight=0)
pill.grid_columnconfigure(4, minsize=28,  weight=0)
pill.grid_rowconfigure(0, minsize=42, weight=1)

state_label = tk.Label(
    pill, textvariable=focus_status, font=("Segoe UI", 9, "bold"),
    fg=MUTED, bg=BG, anchor="w"
)
state_label.grid(row=0, column=0, sticky="w", padx=(2, 28))

# Compact non-text clipboard fallback indicator. Drawn with Canvas so it
# cannot suffer from font/encoding corruption. It appears only when output
# could not be safely inserted and the Gemini text was left in the clipboard.
clipboard_fallback_canvas = tk.Canvas(
    pill, width=18, height=18, bg=BG, highlightthickness=0, bd=0
)
clipboard_fallback_canvas.grid(row=0, column=0, sticky="e", padx=(0, 6))
clipboard_fallback_canvas.create_rectangle(5, 5, 15, 16, outline="#22c55e", width=1.5, tags="clip")
clipboard_fallback_canvas.create_rectangle(7, 2, 13, 6, outline="#22c55e", width=1.5, tags="clip")
clipboard_fallback_canvas.create_line(8, 9, 12, 9, fill="#22c55e", width=1.2, tags="clip")
clipboard_fallback_canvas.create_line(8, 12, 12, 12, fill="#22c55e", width=1.2, tags="clip")
clipboard_fallback_canvas.itemconfigure("clip", state="hidden")
clipboard_fallback_label = clipboard_fallback_canvas

mode_wrap = tk.Frame(pill, bg="#1a1a1f", bd=0, relief="flat", width=200, height=34)
mode_wrap.grid(row=0, column=1, sticky="w", padx=(0, 8))
mode_wrap.grid_propagate(False)
mode_wrap.grid_columnconfigure(0, weight=1, uniform="mode")
mode_wrap.grid_columnconfigure(1, weight=1, uniform="mode")
mode_wrap.grid_columnconfigure(2, weight=1, uniform="mode")
mode_wrap.grid_columnconfigure(3, weight=1, uniform="mode")
mode_wrap.grid_columnconfigure(4, weight=1, uniform="mode")

# Draw rounded rect background on mode_wrap using a Canvas overlay
_mw_canvas = tk.Canvas(mode_wrap, bg=BG, highlightthickness=0, bd=0)
_mw_canvas.place(x=0, y=0, relwidth=1, relheight=1)

def _draw_mode_wrap_bg(e=None):
    c = _mw_canvas
    c.delete("all")
    w = c.winfo_width() or 200
    h = c.winfo_height() or 34
    r = 8
    pts = [r,0, w-r,0, w,0, w,r, w,h-r, w,h, w-r,h, r,h, 0,h, 0,h-r, 0,r, 0,0]
    c.create_polygon(pts, smooth=True, fill="#1a1a1f", outline="#2d2d3a", width=1)

_mw_canvas.bind("<Configure>", _draw_mode_wrap_bg)
mode_wrap.bind("<Configure>", _draw_mode_wrap_bg)
mode_live = tk.Button(mode_wrap, text="LIVE", command=lambda: set_mode("live"),
    font=(_UI_FONT or "Segoe UI", 8, "bold"), fg="#f4f4f5", bg="#2d2d35",
    activebackground="#52525b", activeforeground="white", relief="flat", bd=0,
    cursor="hand2")
mode_live.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
mode_buf = tk.Button(mode_wrap, text="BUF", command=lambda: set_mode("buffered"),
    font=(_UI_FONT or "Segoe UI", 8, "bold"), fg=MUTED, bg="#09090b",
    activebackground="#27272a", activeforeground=TEXT, relief="flat", bd=0,
    cursor="hand2")
mode_buf.grid(row=0, column=1, sticky="nsew", padx=2, pady=2)
mode_pro = tk.Button(mode_wrap, text="PRO", command=lambda: set_mode("prompt"),
    font=(_UI_FONT or "Segoe UI", 8, "bold"), fg=MUTED, bg="#09090b",
    activebackground="#27272a", activeforeground=TEXT, relief="flat", bd=0,
    cursor="hand2")
mode_pro.grid(row=0, column=2, sticky="nsew", padx=2, pady=2)
mode_not = tk.Button(mode_wrap, text="NTS", command=lambda: set_mode("notes"),
    font=(_UI_FONT or "Segoe UI", 8, "bold"), fg=MUTED, bg="#09090b",
    activebackground="#27272a", activeforeground=TEXT, relief="flat", bd=0,
    cursor="hand2")
mode_not.grid(row=0, column=3, sticky="nsew", padx=2, pady=2)
mode_conv = tk.Button(mode_wrap, text="CONV", command=lambda: set_mode("conv"),
    font=(_UI_FONT or "Segoe UI", 8, "bold"), fg=MUTED, bg="#09090b",
    activebackground="#27272a", activeforeground=TEXT, relief="flat", bd=0,
    cursor="hand2")
mode_conv.grid(row=0, column=4, sticky="nsew", padx=2, pady=2)
mode_label = mode_live

btn = tk.Canvas(
    pill, width=32, height=32,
    bg=BG, highlightthickness=0, bd=0, cursor="hand2"
)
btn.grid(row=0, column=2, sticky="w", padx=(0, 2))

# ── shared orb/mic-icon helpers used by both main btn and notes editor ──────
_BTN_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MaterialIcons-Regular.ttf")

def _draw_mic_icon(canvas, cx, cy, size=22, color="#18181b"):
    """Render the Material Icons mic glyph centred on (cx, cy)."""
    try:
        from PIL import Image as _I, ImageDraw as _ID, ImageFont as _IF, ImageTk as _ITk
        fnt = _IF.truetype(_BTN_FONT_PATH, size)
        img = _I.new("RGBA", (int(size*1.6), int(size*1.6)), (0, 0, 0, 0))
        _ID.Draw(img).text((img.width//2, img.height//2), "\ue029",
                           font=fnt, fill=color, anchor="mm")
        ph = _ITk.PhotoImage(img)
        canvas._mic_icon_ph = ph
        canvas.create_image(cx, cy, image=ph)
    except Exception:
        canvas.create_text(cx, cy, text="REC", font=(_UI_FONT or "Segoe UI", 8, "bold"), fill=color)

_btn_orb_level = [0.0]
_btn_orb_phase = [0.0]
_btn_recording = [False]   # mirrors global `recording` for canvas animation
_btn_rms       = [0.0]

def _btn_draw(state="idle"):
    """Redraw the main START/STOP canvas button."""
    btn.delete("all")
    W = int(btn.cget("width"))
    H = int(btn.cget("height"))
    cx, cy = W // 2, H // 2

    if state == "idle":
        btn.configure(bg=BG)
        # White mic on the dark pill; no colored button background.
        _draw_mic_icon(btn, cx, cy, size=22, color="#ffffff")

    elif state == "recording":
        btn.configure(bg=BG)
        rms = _btn_rms[0]
        target = max(0.0, min(1.0, (max(rms, 0.0) / 0.045) ** 0.55))
        prev = _btn_orb_level[0]
        _btn_orb_level[0] = prev + (target - prev) * (0.62 if target > prev else 0.30)
        level = _btn_orb_level[0]
        _btn_orb_phase[0] += 0.34
        breathe = (np.sin(_btn_orb_phase[0]) + 1.0) * 0.035
        visual = max(0.0, min(1.0, level + breathe))

        # Clean microphone activity bars directly on the dark pill.
        # No amber circle, fill, glow, or surrounding background.
        bar_count, bar_w, spacing = 5, 2, 4
        total_w = bar_count * bar_w + (bar_count - 1) * spacing
        x0 = cx - total_w / 2
        bar_scale = 2.0 + visual * 7.5
        for bi in range(bar_count):
            wave = 0.55 + 0.45 * np.sin(_btn_orb_phase[0] * 1.8 + bi * 1.15)
            bh = max(2.0, bar_scale * wave)
            bx = x0 + bi * (bar_w + spacing)
            btn.create_rectangle(bx, cy-bh, bx+bar_w, cy+bh,
                                 fill="#22c55e", outline="")

    elif state == "disabled":
        btn.configure(bg=BG)
        _draw_mic_icon(btn, cx, cy, size=22, color="#71717a")

def _btn_animate():
    if _btn_recording[0]:
        _btn_draw("recording")
        root.after(25, _btn_animate)   # 40 fps

_btn_draw("idle")
btn.bind("<Button-1>", lambda e: start_record() if not recording else stop_record("STOP button"))

lock_btn = tk.Canvas(
    pill, width=22, height=22, bg=BG, highlightthickness=0, bd=0, cursor="hand2"
)
lock_btn.grid(row=0, column=3, sticky="e", padx=(0, 2))

def _draw_lock(locked):
    lock_btn.delete("all")
    col = TEXT if locked else MUTED
    # shackle arc (top half of lock)
    lock_btn.create_arc(6, 2, 16, 13, start=0, extent=180, outline=col, width=1.5, style="arc")
    # body rectangle
    lock_btn.create_rectangle(4, 10, 18, 20, outline=col, width=1.5)
    # keyhole
    lock_btn.create_oval(9, 13, 13, 17, outline=col, width=1.2)

_draw_lock(False)
lock_btn.bind("<Button-1>", lambda e: toggle_focus_lock())

close_btn = tk.Button(
    pill, text="X", command=close_widget, width=2, height=1,
    relief="flat", bd=0, font=("Segoe UI", 10, "bold"), fg=MUTED, bg=BG,
    activebackground="#27272a", activeforeground=TEXT, cursor="hand2"
)
close_btn.grid(row=0, column=4, sticky="e", padx=(3, 6))

# ---------------------------------------------------------------------------
# PROMPT mode panel (hidden until PRO mode is selected)
# ---------------------------------------------------------------------------

# Color palette matching the React reference design
_P = {
    "panel_bg":      "#0d0d12",   # main panel bg
    "input_bg":      "#1a1a24",   # raw speech box bg — lighter than panel
    "input_border":  "#2a2a3a",
    "input_border_h":"#3a3a4e",
    "output_bg":     "#161620",   # output box bg — slightly lighter than panel
    "output_border": "#252535",
    "accent":        "#10b981",
    "accent_dim":    "#065f46",
    "accent_text":   "#34d399",
    "accent_badge":  "#064e3b",
    "muted":         "#475569",
    "muted2":        "#334155",
    "text_main":     "#cbd5e1",
    "text_dim":      "#64748b",
    "btn_rewrite_bg":"#10b981",
    "btn_clear_bg":  "#1e1e2a",
    "divider":       "#1e1e2a",
    "mono_font":     "Consolas",
}

prompt_panel = tk.Frame(frame, bg=_P["panel_bg"], padx=0, pady=0)
# Not packed by default — toggle_prompt_panel() shows/hides it

prompt_status = tk.StringVar(value="")

# Top accent line
tk.Frame(prompt_panel, bg=_P["accent"], height=2).pack(fill="x")

_panel_inner = tk.Frame(prompt_panel, bg=_P["panel_bg"], padx=10, pady=8)
_panel_inner.pack(fill="both", expand=True)

# --- Input section header ---
input_header = tk.Frame(_panel_inner, bg=_P["panel_bg"])
input_header.pack(fill="x", pady=(0, 4))
tk.Label(input_header, text="RAW SPEECH INPUT",
         font=(_UI_FONT or "Segoe UI", 7, "bold"),
         fg=_P["muted"], bg=_P["panel_bg"], anchor="w").pack(side="left")
tk.Label(input_header, text="speak → stop → rewrite",
         font=(_UI_FONT or "Segoe UI", 7),
         fg=_P["muted2"], bg=_P["panel_bg"], anchor="e").pack(side="right")

# ---------------------------------------------------------------------------
# Rounded-border wrapper for Text widgets in the PRO panel
# ---------------------------------------------------------------------------
def _rounded_box(parent, r=10, fill="#1e1e2a", outline="#2e2e3e"):
    """Frame with a Canvas drawn smooth rounded-rect border.
    Uses pack geometry so height is driven by the child widget."""
    outer = tk.Frame(parent, bg=_P["panel_bg"])
    # Canvas sits behind as a border; child packs on top
    _c = tk.Canvas(outer, bg=_P["panel_bg"], highlightthickness=0, bd=0)
    _c.place(x=0, y=0, relwidth=1, relheight=1)
    inner = tk.Frame(outer, bg=fill)
    inner.pack(fill="both", expand=True, padx=r//2, pady=r//2)

    def _redraw(e=None):
        _c.delete("all")
        w = outer.winfo_width()
        h = outer.winfo_height()
        if w < 4 or h < 4:
            return
        pts = [r,0, w-r,0, w,0, w,r, w,h-r, w,h, w-r,h, r,h, 0,h, 0,h-r, 0,r, 0,0]
        _c.create_polygon(pts, smooth=True, fill=fill, outline=outline, width=1)

    outer.bind("<Configure>", _redraw)
    return outer, inner

# Input box
_input_outer, _input_inner = _rounded_box(
    _panel_inner, r=8,
    fill=_P["input_bg"], outline=_P["input_border"]
)
_input_outer.pack(fill="x", pady=(0, 4))

prompt_textbox = tk.Text(
    _input_inner, height=4, wrap="word",
    font=(_UI_FONT or "Segoe UI", 9),
    fg=_P["text_main"], bg=_P["input_bg"],
    insertbackground=_P["muted"],
    relief="flat", bd=0,
    padx=10, pady=7,
    spacing1=2, spacing3=1,
    selectbackground="#1e3a5f", selectforeground="#e2e8f0",
)
prompt_textbox.pack(fill="both", expand=True)
input_border = _input_outer
_configure_md_tags_input(prompt_textbox)

# Char count footer
_input_footer = tk.Frame(_panel_inner, bg=_P["panel_bg"])
_input_footer.pack(fill="x", pady=(2, 6))
_char_count_var = tk.StringVar(value="0 chars")
tk.Label(_input_footer, textvariable=_char_count_var,
         font=(_P["mono_font"], 7), fg=_P["text_dim"], bg=_P["panel_bg"],
         anchor="e").pack(side="right")

def _update_char_count(event=None):
    n = len(prompt_textbox.get("1.0", "end-1c"))
    _char_count_var.set(f"{n} chars")
prompt_textbox.bind("<KeyRelease>", _update_char_count)

def _make_context_menu(widget, allow_paste=False):
    """Custom dark context menu matching the NTS tab style."""
    _CTX_BG    = "#1e1e24"
    _CTX_HOVER = "#2d2d35"
    _CTX_FG    = "#e4e4e7"
    _CTX_DIM   = "#52525b"
    _CTX_SEP   = "#2a2a35"
    _CTX_FONT  = (_UI_FONT or "Segoe UI", 10)

    _ctx = [None]
    _sel = [None]

    def _close():
        if _ctx[0]:
            try: _ctx[0].destroy()
            except Exception: pass
            _ctx[0] = None

    def _restore():
        if _sel[0]:
            try:
                widget.tag_add("sel", _sel[0][0], _sel[0][1])
                widget.mark_set("insert", _sel[0][1])
            except Exception:
                pass

    def _show(e):
        try:
            _sel[0] = (widget.index("sel.first"), widget.index("sel.last"))
        except Exception:
            _sel[0] = None
        widget.after(1, _restore)
        _close()

        has_sel  = _sel[0] is not None
        can_paste = allow_paste and bool(root.clipboard_get() if hasattr(root, 'clipboard_get') else False)
        try:
            can_paste = allow_paste and bool(root.clipboard_get())
        except Exception:
            can_paste = False

        def _do(cmd):
            _restore()
            widget.after(1, cmd)

        items = [
            ("⧉  Copy",       lambda: _do(lambda: widget.event_generate("<<Copy>>")),    not has_sel),
        ]
        if allow_paste:
            items.append(("✂  Cut",  lambda: _do(lambda: widget.event_generate("<<Cut>>")),   not has_sel))
            items.append(("⧈  Paste",lambda: _do(lambda: widget.event_generate("<<Paste>>")), not can_paste))
        items.append(None)
        items.append(("⊞  Select All", lambda: widget.tag_add("sel", "1.0", "end"), False))
        if allow_paste:
            items.append(None)
            items.append(("✕  Clear", lambda: widget.delete("1.0", "end"), False))

        win = tk.Toplevel(widget)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=_CTX_SEP)
        _ctx[0] = win

        outer = tk.Frame(win, bg=_CTX_SEP, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=_CTX_BG)
        inner.pack(fill="both", expand=True, padx=0, pady=4)

        for item in items:
            if item is None:
                tk.Frame(inner, bg=_CTX_SEP, height=1).pack(fill="x", padx=12, pady=3)
                continue
            txt, cmd, disabled = item
            fg  = _CTX_DIM if disabled else _CTX_FG
            row = tk.Frame(inner, bg=_CTX_BG, cursor="arrow" if disabled else "hand2")
            row.pack(fill="x")
            lbl = tk.Label(row, text=txt, font=_CTX_FONT,
                           fg=fg, bg=_CTX_BG, anchor="w", padx=20, pady=6)
            lbl.pack(fill="x")
            if not disabled:
                def _on_enter(ev, r=row, l=lbl):
                    r.config(bg=_CTX_HOVER); l.config(bg=_CTX_HOVER)
                def _on_leave(ev, r=row, l=lbl):
                    r.config(bg=_CTX_BG);    l.config(bg=_CTX_BG)
                def _on_click(ev, c=cmd):
                    _close(); c()
                for w in (row, lbl):
                    w.bind("<Enter>",    _on_enter)
                    w.bind("<Leave>",    _on_leave)
                    w.bind("<Button-1>", _on_click)

        win.update_idletasks()
        mw, mh = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{min(e.x_root, sw-mw-4)}+{min(e.y_root, sh-mh-4)}")

        win.bind("<FocusOut>", lambda ev: _close())
        widget.bind("<Button-1>", lambda ev: _close(), "+")

    widget.bind("<Button-3>", lambda e: (_show(e), "break")[1])

_make_context_menu(prompt_textbox, allow_paste=True)

# ---------------------------------------------------------------------------
# Icon toolbar: [✦ Rewrite] [⊕] [↩] [✕]  +  status (truncated)  +  [←][n][→]
# ---------------------------------------------------------------------------
# Tooltip helper
def _make_tooltip(widget, text):
    tip = None
    def _enter(e):
        nonlocal tip
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, font=(_UI_FONT or "Segoe UI", 8),
                 bg="#1e1e2a", fg=TEXT, padx=6, pady=3,
                 relief="flat", bd=1).pack()
        tip.geometry(f"+{e.x_root+12}+{e.y_root+16}")
    def _leave(e):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None
    widget.bind("<Enter>", _enter)
    widget.bind("<Leave>", _leave)

toolbar = tk.Frame(_panel_inner, bg=_P["panel_bg"])
toolbar.pack(fill="x", pady=(0, 6))

_HOVER_BG = "#2a2a35"

def _icon_btn(parent, text, cmd, tooltip_text, fg="#d4d4d8",
              bold=False, disabled=False, green=False, mat_icon=None, icon_size=16):
    """Flat icon button. If mat_icon codepoint given, renders it via MaterialIcons."""
    f = (_UI_FONT or "Segoe UI", 10, "bold") if bold else (_UI_FONT or "Segoe UI", 11)
    if green:
        b = tk.Button(parent, text=text, command=cmd,
                      font=f, fg="#0a0a0a", bg=_P["btn_rewrite_bg"],
                      activebackground="#34d399", activeforeground="#0a0a0a",
                      relief="flat", bd=0, cursor="hand2",
                      padx=14, pady=4,
                      state="disabled" if disabled else "normal")
        _make_tooltip(b, tooltip_text)
        return b

    # Try to render Material Icon glyph via PIL
    if mat_icon:
        try:
            from PIL import Image as _PI, ImageDraw as _PD, ImageFont as _PF, ImageTk as _PT
            _sz = icon_size
            _fnt = _PF.truetype(_BTN_FONT_PATH, _sz)
            _img = _PI.new("RGBA", (_sz + 8, _sz + 8), (0, 0, 0, 0))
            _ic = "#2a5a45" if disabled else fg
            _PD.Draw(_img).text((_img.width//2, _img.height//2), mat_icon,
                                font=_fnt, fill=_ic, anchor="mm")
            _ph = _PT.PhotoImage(_img)
            b = tk.Label(parent, image=_ph, bg=_P["panel_bg"],
                         cursor="arrow" if disabled else "hand2",
                         padx=4, pady=2)
            b._mat_ph = _ph  # keep ref
            b._mat_icon = mat_icon
            b._mat_sz = _sz
            b._mat_fg = fg
            b._mat_disabled = [disabled]

            def _refresh_icon(color):
                try:
                    _img2 = _PI.new("RGBA", (_sz + 8, _sz + 8), (0, 0, 0, 0))
                    _PD.Draw(_img2).text((_img2.width//2, _img2.height//2), mat_icon,
                                        font=_fnt, fill=color, anchor="mm")
                    ph2 = _PT.PhotoImage(_img2)
                    b._mat_ph = ph2
                    b.config(image=ph2)
                except Exception:
                    pass

            if not disabled:
                b.bind("<Enter>",    lambda e: _refresh_icon("#ffffff"))
                b.bind("<Leave>",    lambda e: _refresh_icon(fg))
                b.bind("<Button-1>", lambda e: cmd() if not b._mat_disabled[0] else None)

            orig_config = b.config
            def _patched_config(**kw):
                if "state" in kw:
                    st = kw.pop("state")
                    b._mat_disabled[0] = (st == "disabled")
                    _refresh_icon("#2a5a45" if b._mat_disabled[0] else fg)
                    b.config(cursor="arrow" if b._mat_disabled[0] else "hand2")
                if kw:
                    orig_config(**kw)
            b.config = _patched_config

            _make_tooltip(b, tooltip_text)
            return b
        except Exception:
            pass  # fall through to text button

    b = tk.Button(parent, text=text, command=cmd,
                  font=f, fg=fg, bg=_P["panel_bg"],
                  activebackground=_HOVER_BG, activeforeground="#ffffff",
                  relief="flat", bd=0, cursor="hand2",
                  padx=6, pady=3,
                  state="disabled" if disabled else "normal")
    b.bind("<Enter>", lambda e: b.config(bg=_HOVER_BG) if str(b.cget("state")) != "disabled" else None)
    b.bind("<Leave>", lambda e: b.config(bg=_P["panel_bg"]))
    _make_tooltip(b, tooltip_text)
    return b

# Material Icons codepoints
_MI_NEW   = "\ue145"   # add_circle_outline
_MI_UNDO  = "\ue166"   # undo
_MI_CLEAR = "\ue872"   # delete

# Action buttons (left side)
prompt_submit_btn = _icon_btn(toolbar, "✦  Rewrite", submit_prompt, "Rewrite Prompt", bold=True, green=True)
prompt_submit_btn.pack(side="left")

_new_btn = _icon_btn(toolbar, "\ue145", new_prompt, "New — clear both fields",
                     mat_icon=_MI_NEW, icon_size=18)
_new_btn.pack(side="left", padx=(4, 0))

undo_btn = _icon_btn(toolbar, "\ue166", undo_prompt, "Undo — restore last output",
                     disabled=True, mat_icon=_MI_UNDO, icon_size=18)
undo_btn.pack(side="left", padx=(2, 0))

_clr_btn = _icon_btn(toolbar, "\ue872", clear_prompt, "Clear output",
                     mat_icon=_MI_CLEAR, icon_size=18)
_clr_btn.pack(side="left", padx=(2, 0))

# Status label
_status_frame = tk.Frame(toolbar, bg=_P["panel_bg"])
_status_frame.pack(side="left", fill="x", expand=True, padx=(8, 4))
_status_lbl = tk.Label(_status_frame, textvariable=prompt_status,
                       font=(_UI_FONT or "Segoe UI", 8),
                       fg=_P["accent_text"], bg=_P["panel_bg"],
                       anchor="w", justify="left", wraplength=1)
_status_lbl.pack(fill="x", expand=True)

def _update_status_wrap(event=None):
    try:
        w = _status_frame.winfo_width()
        if w > 10:
            _status_lbl.config(wraplength=w)
    except Exception:
        pass
_status_frame.bind("<Configure>", _update_status_wrap)

hist_count_label = tk.Label(toolbar, text="",
                            font=(_P["mono_font"], 7),
                            fg=_P["muted"], bg=_P["panel_bg"],
                            padx=2, anchor="e")
hist_count_label.pack(side="left")

hist_prev_btn = _icon_btn(toolbar, "←", _history_prev, "Older history", fg="#8b8b95", disabled=True)
hist_prev_btn.pack(side="left", padx=(1, 0))

hist_next_btn = _icon_btn(toolbar, "→", _history_next, "Newer history", fg="#8b8b95", disabled=True)
hist_next_btn.pack(side="left", padx=(1, 0))

# --- Divider ---
tk.Frame(_panel_inner, bg=_P["divider"], height=1).pack(fill="x", pady=(0, 6))

# --- Output section header ---
output_header = tk.Frame(_panel_inner, bg=_P["panel_bg"])
output_header.pack(fill="x", pady=(0, 6))

# "AI REWRITTEN PROMPT" badge — rounded pill via Canvas
_badge_c = tk.Canvas(output_header, height=22, bg=_P["panel_bg"],
                     highlightthickness=0, bd=0)
_badge_c.pack(side="left")

def _draw_badge(e=None):
    _badge_c.delete("all")
    lbl = "AI REWRITTEN PROMPT"
    # measure text width
    _badge_c.update_idletasks()
    tmp = _badge_c.create_text(-999, -999, text=lbl,
                               font=(_P["mono_font"], 7, "bold"))
    bb = _badge_c.bbox(tmp); _badge_c.delete(tmp)
    tw = (bb[2] - bb[0]) if bb else 120
    pw = tw + 20  # padding
    _badge_c.config(width=pw)
    h = 22
    r = 5
    pts = [r,0, pw-r,0, pw,0, pw,r, pw,h-r, pw,h, pw-r,h, r,h, 0,h, 0,h-r, 0,r, 0,0]
    _badge_c.create_polygon(pts, smooth=True,
                            fill=_P["accent_badge"], outline="")
    _badge_c.create_text(pw//2, h//2, text=lbl,
                         font=(_P["mono_font"], 7, "bold"),
                         fill=_P["accent_text"], anchor="center")

_badge_c.bind("<Configure>", _draw_badge)
_badge_c.after(10, _draw_badge)

tk.Label(output_header, text="✓ auto-copied",
         font=(_UI_FONT or "Segoe UI", 7),
         fg=_P["accent_dim"], bg=_P["panel_bg"], anchor="e").pack(side="right")

# Output box — rounded border
_result_outer, _result_inner = _rounded_box(
    _panel_inner, r=8,
    fill=_P["output_bg"], outline=_P["output_border"]
)
_result_outer.pack(fill="both", expand=True)
result_border = _result_outer

prompt_result_box = tk.Text(
    _result_inner, wrap="word",
    font=(_UI_FONT or "Segoe UI", 9),
    fg=_P["text_main"], bg=_P["output_bg"],
    insertbackground=_P["accent_text"],
    relief="flat", bd=0,
    padx=10, pady=8,
    spacing1=2, spacing3=2,
    selectbackground="#1e3a5f", selectforeground="#e2e8f0",
    cursor="xterm",
)
prompt_result_box.pack(fill="both", expand=True)
_configure_md_tags_result(prompt_result_box)
_make_context_menu(prompt_result_box)

# ---------------------------------------------------------------------------
# Allow the widget to be moved without changing focus semantics.
def drag_start(event):
    root._drag_x = event.x_root - root.winfo_x()
    root._drag_y = event.y_root - root.winfo_y()


def drag_move(event):
    root.geometry(f"+{event.x_root - root._drag_x}+{event.y_root - root._drag_y}")
    # Keep caption window anchored below widget while dragging
    try: _conv_caps_reposition()
    except Exception: pass

for w in (frame, pill):
    w.bind("<Button-1>", drag_start)
    w.bind("<B1-Motion>", drag_move)

# ---------------------------------------------------------------------------
# NOTES panel
# ---------------------------------------------------------------------------
_N = {
    "panel_bg":     "#0d0d12",
    "card_bg":      "#111118",
    "card_border":  "#1e1e2a",
    "accent":       "#f59e0b",
    "accent_dim":   "#78350f",
    "accent_text":  "#fbbf24",
    "search_bg":    "#0c0c10",
    "search_border":"#1e1e2a",
    "text_main":    "#e4e4e7",
    "text_dim":     "#64748b",
    "text_muted":   "#475569",
    "del_fg":       "#475569",
    "divider":      "#1a1a24",
}

notes_panel = tk.Frame(frame, bg=_N["panel_bg"], padx=0, pady=0)
# Not packed by default

# Amber accent line — matches PRO's green line height
tk.Frame(notes_panel, bg=_N["accent"], height=2).pack(fill="x")

_ni = tk.Frame(notes_panel, bg=_N["panel_bg"], padx=10, pady=8)
_ni.pack(fill="both", expand=True)

# --- Header row: "QUICK NOTES" label left, search middle, "+ NEW NOTE" right ---
# Matches PRO's single input_header row height exactly
nh = tk.Frame(_ni, bg=_N["panel_bg"])
nh.pack(fill="x", pady=(0, 4))

tk.Label(nh, text="QUICK NOTES", font=(_UI_FONT or "Segoe UI", 7, "bold"),
         fg=_N["text_muted"], bg=_N["panel_bg"], anchor="w",
         bd=0, padx=0, pady=0).pack(side="left")

_notes_add_lbl = tk.Label(nh, text="+ NEW NOTE",
                           font=(_UI_FONT or "Segoe UI", 7, "bold"),
                           fg=_N["accent_text"], bg=_N["panel_bg"],
                           cursor="hand2", anchor="e", bd=0, padx=0, pady=0)
_notes_add_lbl.pack(side="right")

# --- Search bar ---
_nsb = tk.Frame(_ni, bg=_N["search_border"], bd=0)
_nsb.pack(fill="x", pady=(0, 4))
_nsb_inner = tk.Frame(_nsb, bg=_N["search_bg"], padx=0, pady=0)
_nsb_inner.pack(fill="x", padx=1, pady=1)

_notes_search_var = tk.StringVar()
_notes_search_entry = tk.Entry(_nsb_inner, textvariable=_notes_search_var,
    bg=_N["search_bg"], fg=_N["text_main"],
    insertbackground=_N["text_dim"],
    font=(_UI_FONT or "Segoe UI", 9), bd=0, highlightthickness=0)
_notes_search_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), ipady=4)
_notes_search_entry.insert(0, "Search notes...")
_notes_search_entry.config(fg=_N["text_muted"])
_notes_search_entry.bind("<FocusIn>",
    lambda e: _notes_search_entry.delete(0, "end")
    if _notes_search_entry.get() == "Search notes..." else None)
_notes_search_entry.bind("<FocusOut>",
    lambda e: (_notes_search_entry.insert(0, "Search notes...")
               if not _notes_search_entry.get() else None))

tk.Label(_nsb_inner, text="⌕", font=(_UI_FONT or "Segoe UI", 10),
         fg=_N["text_dim"], bg=_N["search_bg"]).pack(side="right", padx=6)

# --- Scrollable list ---
_notes_list_container = tk.Frame(_ni, bg=_N["panel_bg"])
_notes_list_container.pack(fill="both", expand=True)

_notes_canvas = tk.Canvas(_notes_list_container, bg=_N["panel_bg"], highlightthickness=0, bd=0)

# Custom dark scrollbar — a narrow Canvas drawn manually, no OS chrome
_SB_W       = 6          # thumb width
_SB_BG      = _N["panel_bg"]
_SB_THUMB   = "#3a3a45"
_SB_HOVER   = "#52525b"

_notes_sb_canvas = tk.Canvas(_notes_list_container, width=_SB_W + 4,
                              bg=_SB_BG, highlightthickness=0, bd=0)
_notes_sb_canvas.pack(side="right", fill="y")
_notes_canvas.pack(side="left", fill="both", expand=True)

_sb_thumb_id  = None
_sb_drag_data = {}

def _sb_draw(first, last):
    """Redraw the thumb given scroll fractions (0.0–1.0)."""
    global _sb_thumb_id
    _notes_sb_canvas.delete("all")
    h = _notes_sb_canvas.winfo_height()
    if h <= 1:
        return
    first, last = float(first), float(last)
    if last - first >= 1.0:          # content fits — no thumb
        return
    ty = int(first * h) + 2
    ty2 = int(last  * h) - 2
    ty2 = max(ty2, ty + 16)          # minimum thumb height
    _sb_thumb_id = _notes_sb_canvas.create_rectangle(
        2, ty, _SB_W + 2, ty2,
        fill=_SB_THUMB, outline="", tags="thumb")

def _sb_set(first, last):
    _sb_draw(first, last)

_notes_canvas.configure(yscrollcommand=_sb_set)

def _sb_click(e):
    h = _notes_sb_canvas.winfo_height()
    if h <= 1: return
    _notes_canvas.yview_moveto(e.y / h)

def _sb_drag_start(e):
    _sb_drag_data["y"] = e.y
    items = _notes_sb_canvas.find_withtag("thumb")
    _sb_drag_data["thumb_y1"] = _notes_sb_canvas.coords(items[0])[1] if items else e.y

def _sb_drag_move(e):
    h = _notes_sb_canvas.winfo_height()
    if h <= 1: return
    dy = e.y - _sb_drag_data.get("y", e.y)
    _sb_drag_data["y"] = e.y
    _notes_canvas.yview_scroll(int(dy), "units")

def _sb_hover(e):
    _notes_sb_canvas.itemconfig("thumb", fill=_SB_HOVER)
def _sb_leave(e):
    _notes_sb_canvas.itemconfig("thumb", fill=_SB_THUMB)

_notes_sb_canvas.bind("<Button-1>",   _sb_click)
_notes_sb_canvas.bind("<ButtonPress-1>",  _sb_drag_start)
_notes_sb_canvas.bind("<B1-Motion>",  _sb_drag_move)
_notes_sb_canvas.bind("<Enter>",      _sb_hover)
_notes_sb_canvas.bind("<Leave>",      _sb_leave)
_notes_sb_canvas.bind("<Configure>",  lambda e: _notes_canvas.event_generate("<Configure>"))

_notes_scroll_frame = tk.Frame(_notes_canvas, bg=_N["panel_bg"])
_notes_cwin = _notes_canvas.create_window((0, 0), window=_notes_scroll_frame, anchor="nw")

def _notes_update_scrollregion(e=None):
    _notes_canvas.update_idletasks()
    bbox = _notes_canvas.bbox("all")
    if bbox:
        ch = _notes_canvas.winfo_height()
        _notes_canvas.configure(scrollregion=(0, 0, bbox[2], max(bbox[3], ch)))
    # refresh thumb
    _notes_canvas.update_idletasks()
    try:
        first, last = _notes_canvas.yview()
        _sb_draw(first, last)
    except Exception:
        pass

_notes_scroll_frame.bind("<Configure>", _notes_update_scrollregion)

def _notes_configure_canvas(e):
    _notes_canvas.itemconfig(_notes_cwin, width=e.width)
    _notes_update_scrollregion()

_notes_canvas.bind("<Configure>", _notes_configure_canvas)

def _notes_mousewheel(e):
    if _notes_scroll_frame.winfo_reqheight() > _notes_canvas.winfo_height():
        _notes_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")


def _notes_render_list():
    for w in _notes_scroll_frame.winfo_children():
        w.destroy()
    q = _notes_search_entry.get().strip().lower()
    if q == "search notes...":
        q = ""
    filtered = [n for n in _notes_data
                if q in n.get("title", "").lower() or q in n.get("text", "").lower()]
    # pinned first
    filtered.sort(key=lambda n: (0 if n.get("pinned") else 1))
    if not filtered:
        tk.Label(_notes_scroll_frame, text="No notes yet -- click + NEW NOTE",
                 font=(_UI_FONT or "Segoe UI", 9),
                 fg=_N["text_dim"], bg=_N["panel_bg"]).pack(pady=20)
        return
    for note in filtered:
        _notes_make_card(_notes_scroll_frame, note)


def _notes_make_card(parent, note):
    is_pinned = bool(note.get("pinned"))
    stripe_color = _N["accent"] if not is_pinned else "#22c55e"

    # Rounded card container drawn via Canvas polygon
    card_outer = tk.Frame(parent, bg=_N["panel_bg"])
    card_outer.pack(fill="x", pady=(6, 2))

    _cc = tk.Canvas(card_outer, bg=_N["panel_bg"], highlightthickness=0, bd=0)
    _cc.place(x=0, y=0, relwidth=1, relheight=1)

    card = tk.Frame(card_outer, bg=_N["card_bg"], padx=10, pady=8)
    card.pack(fill="x", padx=2, pady=2)

    def _draw_card_border(e=None):
        _cc.delete("all")
        w = card_outer.winfo_width()
        h = card_outer.winfo_height()
        if w < 4 or h < 4:
            return
        r = 8
        pts = [r,0, w-r,0, w,0, w,r, w,h-r, w,h, w-r,h, r,h, 0,h, 0,h-r, 0,r, 0,0]
        _cc.create_polygon(pts, smooth=True,
                           fill=_N["card_bg"], outline=_N["card_border"], width=1)
        # accent stripe at top
        _cc.create_line(r, 1, w-r, 1, fill=stripe_color, width=2)

    card_outer.bind("<Configure>", _draw_card_border)

    ch = tk.Frame(card, bg=_N["card_bg"])
    ch.pack(fill="x")

    del_cv = tk.Canvas(ch, width=14, height=14, bg=_N["card_bg"],
                       highlightthickness=0, cursor="hand2")
    del_cv.create_line(3, 3, 11, 11, fill=_N["del_fg"], width=1.5)
    del_cv.create_line(11, 3, 3, 11, fill=_N["del_fg"], width=1.5)
    del_cv.pack(side="right")
    del_cv.bind("<Button-1>", lambda e, n=note: _notes_delete(n))

    pin_color = "#22c55e" if is_pinned else _N["del_fg"]
    pin_cv = tk.Canvas(ch, width=14, height=14, bg=_N["card_bg"],
                       highlightthickness=0, cursor="hand2")
    # Canvas pin: vertical line + diamond head
    pin_cv.create_line(7, 7, 7, 13, fill=pin_color, width=1.5)
    pin_cv.create_polygon(4, 2, 10, 2, 12, 7, 7, 9, 2, 7, fill=pin_color, outline="")
    pin_cv.pack(side="right", padx=(0, 4))
    pin_cv.bind("<Button-1>", lambda e, n=note: _notes_toggle_pin(n))

    ts = note.get("time") or note.get("updated_at", "")
    if ts and isinstance(ts, (int, float)):
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")
    if ts:
        tk.Label(ch, text=str(ts), font=(_UI_FONT or "Segoe UI", 8),
                 fg=_N["accent_text"], bg=_N["card_bg"]).pack(side="right", padx=(0, 6))

    first_line = (note.get("text", "") or "").split("\n")[0][:50]
    title_lbl = tk.Label(card, text=first_line,
                         font=(_UI_FONT or "Segoe UI", 9, "bold"),
                         fg=_N["text_main"], bg=_N["card_bg"], anchor="w")
    title_lbl.pack(fill="x", pady=(3, 0))

    preview = (note.get("text", "") or "").replace("\n", " ")
    if len(preview) > 85:
        preview = preview[:82] + "..."
    text_lbl = tk.Label(card, text=preview,
                        font=(_UI_FONT or "Segoe UI", 9),
                        fg=_N["text_dim"], bg=_N["card_bg"],
                        justify="left", anchor="w", wraplength=400)
    text_lbl.pack(fill="x", pady=(2, 0))

    for w in (card, card_outer, title_lbl, text_lbl):
        w.bind("<Double-Button-1>", lambda e, n=note: _notes_open_editor(n))


def _notes_delete(note):
    _notes_data.remove(note)
    _notes_save()
    _notes_render_list()


def _notes_toggle_pin(note):
    note["pinned"] = not note.get("pinned", False)
    _notes_save()
    _notes_render_list()


def _notes_open_editor(note=None):
    # Pause canvas mousewheel while dialog open
    _notes_canvas.unbind_all("<MouseWheel>")

    import datetime as _dt
    dlg = tk.Toplevel(root)
    dlg.overrideredirect(True)
    dlg.configure(bg=BORDER)
    dlg.attributes("-topmost", True)

    # Track this editor so screenshot can hide all open editors
    _open_editor_windows.append(dlg)

    # Center over main widget — must happen before update_idletasks
    root.update_idletasks()
    rx, ry = root.winfo_x(), root.winfo_y()
    rw, rh = root.winfo_width(), root.winfo_height()
    dlg.geometry(f"400x320+{rx + (rw - 400)//2}+{ry + (rh - 320)//2}")

    def _apply_dlg_rounded(r=12):
        try:
            hwnd = int(user32.GetAncestor(int(dlg.winfo_id()), GA_ROOT))
            w = dlg.winfo_width(); h = dlg.winfo_height()
            if w > 1 and h > 1:
                rgn = gdi32.CreateRoundRectRgn(0, 0, w+1, h+1, r*2, r*2)
                user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    _dlg_rnd_job = [None]
    def _schedule_dlg_rounded(e=None):
        if _dlg_rnd_job[0]:
            try: dlg.after_cancel(_dlg_rnd_job[0])
            except Exception: pass
        _dlg_rnd_job[0] = dlg.after(80, _apply_dlg_rounded)

    dlg.after(40, _apply_dlg_rounded)
    dlg.bind("<Configure>", _schedule_dlg_rounded)

    # 1px border wrapper
    _dlg_outer = tk.Frame(dlg, bg=BORDER, padx=1, pady=1)
    _dlg_outer.pack(fill="both", expand=True)

    def _on_close():
        if dlg in _open_editor_windows:
            _open_editor_windows.remove(dlg)
        _notes_canvas.bind_all("<MouseWheel>", _notes_mousewheel)
        dlg.destroy()

    dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # overridden after mic setup below

    # Amber header bar
    hdr = tk.Frame(_dlg_outer, bg=_N["accent"], padx=8, pady=6)
    hdr.pack(fill="x")

    def _save():
        import re as _re
        # Walk the text widget dump to collect text and image positions.
        # Images are replaced with [IMG:N] markers in the saved text,
        # and their base64 data stored in note["images"].
        raw_parts = []
        img_b64_list = []
        img_idx = 0
        for item in t_body.dump("1.0", "end", image=True, text=True):
            kind, value, index = item
            if kind == "text":
                raw_parts.append(value)
            elif kind == "image":
                # Find the PhotoImage object that matches this image name
                b64 = None
                for ph in getattr(t_body, "_images", []):
                    if str(ph) == value:
                        b64 = getattr(ph, "_b64data", None)
                        break
                if b64:
                    raw_parts.append(f"[IMG:{img_idx}]")
                    img_b64_list.append(b64)
                    img_idx += 1
        content = "".join(raw_parts).strip()
        if not content or content == "Take a note...":
            _on_close()
            return
        ts = _dt.datetime.now().strftime("%I:%M %p").lstrip("0")
        record = {
            "id":    int(_dt.datetime.now().timestamp()),
            "title": content.split("\n")[0][:60],
            "time":  ts,
            "text":  content,
        }
        if img_b64_list:
            record["images"] = img_b64_list
        if note:
            note.update(record)
            note["id"] = note.get("id", record["id"])
        else:
            _notes_data.insert(0, record)
        _notes_save()
        _notes_render_list()
        _on_close()

    save_cv = tk.Canvas(hdr, width=24, height=24, bg=_N["accent"],
                       highlightthickness=0, cursor="hand2")
    save_cv.pack(side="left")
    save_cv.create_line(6, 5, 18, 5, fill="#0a0a0a", width=1.5)
    save_cv.create_line(6, 5, 6, 19, fill="#0a0a0a", width=1.5)
    save_cv.create_line(18, 5, 18, 19, fill="#0a0a0a", width=1.5)
    save_cv.create_line(6, 19, 18, 19, fill="#0a0a0a", width=1.5)
    save_cv.create_rectangle(8, 6, 16, 11, outline="#0a0a0a", width=1.2)
    save_cv.create_rectangle(9, 14, 15, 19, outline="#0a0a0a", width=1.2)
    save_cv.bind("<Button-1>", lambda e: _save())

    close_cv = tk.Canvas(hdr, width=24, height=24, bg=_N["accent"],
                         highlightthickness=0, cursor="hand2")
    close_cv.pack(side="right", padx=(6, 0))
    close_cv.create_line(6, 6, 18, 18, fill="#0a0a0a", width=1.5)
    close_cv.create_line(18, 6, 6, 18, fill="#0a0a0a", width=1.5)
    close_cv.bind("<Button-1>", lambda e: _on_close())

    # Drag — bind on header and all children except interactive ones
    def _hdr_ds(e): dlg._dx = e.x_root - dlg.winfo_x(); dlg._dy = e.y_root - dlg.winfo_y()
    def _hdr_dm(e): dlg.geometry(f"+{e.x_root - dlg._dx}+{e.y_root - dlg._dy}")
    hdr.bind("<Button-1>",  _hdr_ds, "+")
    hdr.bind("<B1-Motion>", _hdr_dm, "+")

    # --- Mic / orb button ---
    _ORB_W, _ORB_H = 32, 32
    _mic_active   = [False]
    _mic_stream   = [None]
    _mic_future   = [None]
    _mic_stop_evt = [None]
    _mic_rms      = [0.0]   # live RMS updated from audio thread
    _orb_level    = [0.0]   # smoothed visual level; kept separate from raw audio
    _orb_phase    = [0.0]

    orb_cv = tk.Canvas(hdr, width=_ORB_W, height=_ORB_H,
                       bg=_N["accent"], highlightthickness=0, cursor="hand2")
    orb_cv.pack(side="right", padx=(0, 4))

    def _orb_draw(rms=0.0):
        orb_cv.delete("all")
        cx, cy = _ORB_W // 2, _ORB_H // 2

        if not _mic_active[0]:
            _draw_mic_icon(orb_cv, cx, cy, size=22, color="#0a0a0a")
            return

        # RMS from PortAudio is usually a small float (often 0.005–0.20).
        # Use a sensitive nonlinear mapping so normal speech produces visible motion.
        target = max(0.0, min(1.0, (max(rms, 0.0) / 0.045) ** 0.55))
        prev = _orb_level[0]
        # Fast attack, slightly slower release: responsive without jitter.
        _orb_level[0] = prev + (target - prev) * (0.62 if target > prev else 0.30)
        level = _orb_level[0]
        _orb_phase[0] += 0.34

        # Small idle breathing + strong voice-reactive expansion.
        breathe = (np.sin(_orb_phase[0]) + 1.0) * 0.035
        visual = max(0.0, min(1.0, level + breathe))

        r = 8.0 + visual * 7.0
        glow = r + 2.5 + visual * 2.0

        orb_cv.create_oval(cx-glow, cy-glow, cx+glow, cy+glow,
                           outline="#f59e0b", width=1)
        orb_cv.create_oval(cx-r, cy-r, cx+r, cy+r,
                           fill="#f59e0b", outline="")

        # Deterministic waveform bars: no random generation or NumPy RNG per frame.
        bar_count, bar_w, spacing = 5, 2, 4
        total_w = bar_count * bar_w + (bar_count - 1) * spacing
        x0 = cx - total_w / 2
        bar_scale = 2.0 + visual * 7.5
        for bi in range(bar_count):
            wave = 0.55 + 0.45 * np.sin(_orb_phase[0] * 1.8 + bi * 1.15)
            bh = max(2.0, bar_scale * wave)
            bx = x0 + bi * (bar_w + spacing)
            orb_cv.create_rectangle(bx, cy-bh, bx+bar_w, cy+bh,
                                    fill="#0a0a0a", outline="")

    _orb_draw()

    def _orb_animate():
        if _mic_active[0]:
            _orb_draw(_mic_rms[0])
            dlg.after(25, _orb_animate)   # 40 fps: noticeably more responsive

    def _mic_toggle(e=None):
        if _mic_active[0]:
            _mic_stop()
        else:
            _mic_start()

    def _mic_start():
        if _mic_active[0]:
            return
        _mic_active[0] = True
        _mic_stop_evt[0] = threading.Event()
        stop_evt = _mic_stop_evt[0]

        # Run Gemini live transcription in the existing async loop,
        # output appended directly into t_body
        async def _transcribe_to_note():
            try:
                client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
                cfg = types.LiveConnectConfig(
                    response_modalities=["TEXT"],
                    input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[]),
                )
                async with client.aio.live.connect(model=MODEL, config=cfg) as session:
                    audio_q: asyncio.Queue = asyncio.Queue()

                    def _audio_cb(indata, frames, t_info, status):
                        chunk = indata[:, 0].copy()
                        rms = float(np.sqrt(np.mean(chunk ** 2)))
                        _mic_rms[0] = rms
                        pcm16 = (resample_poly(chunk, RATE_OUT, RATE_IN) * 32767).astype(np.int16)
                        audio_q.put_nowait(pcm16.tobytes())

                    stream = sd.InputStream(samplerate=RATE_IN, channels=1,
                                            dtype="float32", blocksize=BLOCK,
                                            device=DEVICE, callback=_audio_cb)

                    async def _sender():
                        with stream:
                            while not stop_evt.is_set():
                                try:
                                    chunk = await asyncio.wait_for(audio_q.get(), 0.1)
                                    await session.send_realtime_input(
                                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                                    )
                                except asyncio.TimeoutError:
                                    pass

                    async def _receiver():
                        nts_prev_words = []
                        nts_committed_count = 0
                        NTS_SAFE_TAIL = 2
                        async for response in session.receive():
                            if stop_evt.is_set():
                                break
                            sc = getattr(response, "server_content", None)
                            if sc is None:
                                continue

                            # --- Interim: commit stable confirmed prefix word-by-word ---
                            interim = getattr(sc, "interim_input_transcription", None)
                            itxt = getattr(interim, "text", None) if interim is not None else None
                            if itxt:
                                cur_words = itxt.strip().split()
                                confirmed_len = 0
                                for a, b in zip(nts_prev_words, cur_words):
                                    if a.casefold() == b.casefold():
                                        confirmed_len += 1
                                    else:
                                        break
                                commit_up_to = max(confirmed_len - NTS_SAFE_TAIL, 0)
                                if commit_up_to > nts_committed_count:
                                    new_words = cur_words[nts_committed_count:commit_up_to]
                                    batch = " ".join(new_words)
                                    nts_committed_count = commit_up_to
                                    def _append_progressive(t=batch):
                                        cur = t_body.get("1.0", "end-1c")
                                        if cur in ("", "Take a note..."):
                                            t_body.delete("1.0", "end")
                                            t_body.config(fg=_N["text_main"])
                                            t_body.insert("1.0", t + " ")
                                        else:
                                            if not cur.endswith(" "):
                                                t_body.insert("end", " ")
                                            t_body.insert("end", t + " ")
                                        t_body.see("end")
                                    root.after(0, _append_progressive)
                                nts_prev_words = cur_words

                            # --- Final: append only the words not yet committed ---
                            inp = getattr(sc, "input_transcription", None)
                            txt = getattr(inp, "text", None) if inp is not None else None
                            if txt:
                                final_words = txt.strip().split()
                                skip = min(nts_committed_count, len(final_words))
                                remainder = " ".join(final_words[skip:]).strip()
                                nts_prev_words = []
                                nts_committed_count = 0
                                if remainder:
                                    def _append_final(t=remainder):
                                        cur = t_body.get("1.0", "end-1c")
                                        if cur in ("", "Take a note..."):
                                            t_body.delete("1.0", "end")
                                            t_body.config(fg=_N["text_main"])
                                            t_body.insert("1.0", t + " ")
                                        else:
                                            if not cur.endswith(" "):
                                                t_body.insert("end", " ")
                                            t_body.insert("end", t + " ")
                                        t_body.see("end")
                                    root.after(0, _append_final)

                    await asyncio.gather(_sender(), _receiver())
            except Exception as ex:
                L("NOTES MIC ERROR: %s", ex)

        _mic_future[0] = asyncio.run_coroutine_threadsafe(_transcribe_to_note(), loop)
        dlg.after(50, _orb_animate)

    def _mic_stop():
        if not _mic_active[0]:
            return
        _mic_active[0] = False
        _mic_rms[0] = 0.0
        if _mic_stop_evt[0]:
            _mic_stop_evt[0].set()
        if _mic_future[0] and not _mic_future[0].done():
            _mic_future[0].cancel()
        _orb_draw(0.0)

    orb_cv.bind("<Button-1>", _mic_toggle)

    # Stop mic if dialog closes mid-recording
    _orig_on_close = _on_close
    def _on_close():
        _mic_stop()
        _orig_on_close()
    dlg.protocol("WM_DELETE_WINDOW", _on_close)

    # --- Formatting footer — must be packed BEFORE t_body so expand=True doesn't eat it ---
    _FMT_FONT  = _UI_FONT or "Segoe UI"
    _FTR_BG    = _N["panel_bg"]
    _FTR_ICON  = "#94a3b8"
    _FTR_HOVER = "#e2e8f0"
    _FTR_SEP   = _N["divider"]
    _BTN_SIZE  = 36

    fmt_footer = tk.Frame(_dlg_outer, bg=_FTR_BG, height=44)
    fmt_footer.pack(fill="x", side="bottom")
    fmt_footer.pack_propagate(False)
    tk.Frame(fmt_footer, bg=_FTR_SEP, height=1).pack(fill="x")
    fmt_inner = tk.Frame(fmt_footer, bg=_FTR_BG)
    fmt_inner.pack(fill="both", expand=True, padx=8)

    def _make_fmt_btn(parent, draw_fn, command):
        cv = tk.Canvas(parent, width=_BTN_SIZE, height=_BTN_SIZE,
                       bg=_FTR_BG, highlightthickness=0, cursor="hand2")
        cv.pack(side="left", padx=2, pady=4)
        draw_fn(cv, _FTR_ICON)
        cv.bind("<Enter>",    lambda e: (cv.delete("all"), draw_fn(cv, _FTR_HOVER)))
        cv.bind("<Leave>",    lambda e: (cv.delete("all"), draw_fn(cv, _FTR_ICON)))
        cv.bind("<Button-1>", lambda e: command())
        return cv

    def _draw_bold(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        cv.create_text(cx, cy, text="B", font=(_FMT_FONT, 13, "bold"), fill=col, anchor="center")
    def _draw_italic(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        cv.create_text(cx, cy, text="I", font=(_FMT_FONT, 13, "italic"), fill=col, anchor="center")
    def _draw_underline(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        cv.create_text(cx, cy, text="U", font=(_FMT_FONT, 13), fill=col, anchor="center")
        cv.create_line(cx-6, cy+9, cx+6, cy+9, fill=col, width=1.5)
    def _draw_strikethrough(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        cv.create_text(cx, cy, text="ab", font=(_FMT_FONT, 11), fill=col, anchor="center")
        cv.create_line(cx-8, cy, cx+8, cy, fill=col, width=1.5)
    def _draw_bullets(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        for dy in (-5, 0, 5):
            cv.create_oval(cx-11, cy+dy-1, cx-8, cy+dy+2, fill=col, outline="")
            cv.create_line(cx-5, cy+dy, cx+10, cy+dy, fill=col, width=1.5)
    def _draw_image(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        cv.create_rectangle(cx-9, cy-7, cx+9, cy+7, outline=col, width=1.5)
        cv.create_line(cx-7, cy+4, cx-2, cy-1, cx+3, cy+2, cx+7, cy-2, fill=col, width=1.2)
        cv.create_oval(cx+4, cy-5, cx+7, cy-2, fill=col, outline="")

    def _draw_screenshot(cv, col):
        cx, cy = _BTN_SIZE//2, _BTN_SIZE//2
        # Outer screen rectangle
        cv.create_rectangle(cx-9, cy-7, cx+9, cy+6, outline=col, width=1.5)
        # Crosshair
        cv.create_line(cx, cy-4, cx, cy+3, fill=col, width=1.2)
        cv.create_line(cx-4, cy, cx+4, cy, fill=col, width=1.2)
        # Corner brackets top-left
        cv.create_line(cx-9, cy-4, cx-9, cy-7, cx-5, cy-7, fill=col, width=1.5)
        # Corner brackets bottom-right
        cv.create_line(cx+9, cy+3, cx+9, cy+6, cx+5, cy+6, fill=col, width=1.5)

    # Placeholder lambdas — real commands assigned after t_body is created
    _fmt_cmds = [None, None, None, None, None, None, None]
    _fmt_btns = [
        _make_fmt_btn(fmt_inner, _draw_bold,          lambda: _fmt_cmds[0]()),
        _make_fmt_btn(fmt_inner, _draw_italic,        lambda: _fmt_cmds[1]()),
        _make_fmt_btn(fmt_inner, _draw_underline,     lambda: _fmt_cmds[2]()),
        _make_fmt_btn(fmt_inner, _draw_strikethrough, lambda: _fmt_cmds[3]()),
        _make_fmt_btn(fmt_inner, _draw_bullets,       lambda: _fmt_cmds[4]()),
        _make_fmt_btn(fmt_inner, _draw_image,         lambda: _fmt_cmds[5]()),
        _make_fmt_btn(fmt_inner, _draw_screenshot,    lambda: _fmt_cmds[6]()),
    ]

    t_body = tk.Text(_dlg_outer, bg=_N["card_bg"], fg=_N["text_main"],
                     insertbackground=_N["text_dim"],
                     font=(_UI_FONT or "Segoe UI", 10), bd=0, relief="flat",
                     padx=12, pady=10, wrap="word", undo=True, maxundo=-1,
                     selectbackground="#1e3a5f", selectforeground="#e2e8f0")
    t_body.pack(fill="both", expand=True)

    # Configure formatting tags
    t_body.tag_configure("bold",          font=(_FMT_FONT, 10, "bold"))
    t_body.tag_configure("italic",        font=(_FMT_FONT, 10, "italic"))
    t_body.tag_configure("underline",     font=(_FMT_FONT, 10), underline=True)
    t_body.tag_configure("strikethrough", font=(_FMT_FONT, 10), overstrike=True)
    t_body.tag_configure("bold_italic",   font=(_FMT_FONT, 10, "bold italic"))
    t_body.tag_configure("bold_underline",     font=(_FMT_FONT, 10, "bold"), underline=True)
    t_body.tag_configure("italic_underline",   font=(_FMT_FONT, 10, "italic"), underline=True)

    def _fmt_toggle(tag):
        try:
            s, e2 = t_body.index("sel.first"), t_body.index("sel.last")
        except tk.TclError:
            return
        if tag in t_body.tag_names(s):
            t_body.tag_remove(tag, s, e2)
        else:
            t_body.tag_add(tag, s, e2)
        t_body.focus_set()

    def _fmt_bullets():
        try:
            sl = int(t_body.index("sel.first").split(".")[0])
            el = int(t_body.index("sel.last").split(".")[0])
        except tk.TclError:
            sl = el = int(t_body.index("insert").split(".")[0])
        for ln in range(sl, el + 1):
            txt = t_body.get(f"{ln}.0", f"{ln}.end")
            if txt.startswith("• "):
                t_body.delete(f"{ln}.0", f"{ln}.2")
            elif txt.startswith("•"):
                t_body.delete(f"{ln}.0", f"{ln}.1")
            else:
                t_body.insert(f"{ln}.0", "• ")
        t_body.focus_set()

    def _fmt_image():
        from tkinter import filedialog
        from PIL import Image as _Img, ImageTk as _ITk
        import base64, io
        path = filedialog.askopenfilename(
            parent=dlg, title="Insert image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            img = _Img.open(path).convert("RGBA")
            if img.width > 360:
                img = img.resize((360, int(img.height * 360 / img.width)), _Img.LANCZOS)
            # Store PNG bytes as base64 on the photo object for serialization
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            photo = _ITk.PhotoImage(img)
            photo._b64data = b64  # attach for save serialization
            if not hasattr(t_body, "_images"):
                t_body._images = []
            t_body._images.append(photo)
            t_body.image_create("insert", image=photo, padx=4, pady=4)
            t_body.focus_set()
        except Exception as ex:
            L("NOTES IMAGE INSERT ERROR: %s", ex)

    # Wire up the deferred commands now that t_body exists
    _fmt_cmds[0] = lambda: _fmt_toggle("bold")
    _fmt_cmds[1] = lambda: _fmt_toggle("italic")
    _fmt_cmds[2] = lambda: _fmt_toggle("underline")
    _fmt_cmds[3] = lambda: _fmt_toggle("strikethrough")
    _fmt_cmds[4] = _fmt_bullets
    _fmt_cmds[5] = _fmt_image

    def _fmt_screenshot():
        """Hide ALL editor windows + main widget, rubber-band select, capture region."""
        import base64, io, uuid, os as _os, datetime as _dt2
        from PIL import ImageGrab, ImageTk as _ITk2

        was_root_visible = root.state() != "withdrawn"

        # Withdraw every open editor, not just this one
        hidden_editors = [w for w in _open_editor_windows
                          if w.winfo_exists() and w.state() != "withdrawn"]

        def _restore_windows():
            if was_root_visible:
                root.deiconify()
            for w in hidden_editors:
                try: w.deiconify()
                except Exception: pass
            t_body.focus_set()

        def _do_grab(box):
            x1, y1, x2, y2 = box
            if x2 - x1 < 4 or y2 - y1 < 4:
                _restore_windows()
                return
            try:
                img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
                ts  = _dt2.datetime.now().strftime("%Y%m%d_%H%M%S")
                uid = str(uuid.uuid4())[:8].upper()
                fname     = f"GEMDIC_{ts}_{uid}.png"
                dl        = _os.path.join(_os.path.expanduser("~"), "Downloads")
                save_path = _os.path.join(dl, fname)
                img.save(save_path, format="PNG")
                L("SCREENSHOT SAVED: %s", save_path)
                if img.width > 360:
                    from PIL import Image as _PI
                    rs = getattr(_PI, "LANCZOS", getattr(getattr(_PI, "Resampling", None), "LANCZOS", 1))
                    img = img.resize((360, int(img.height * 360 / img.width)), rs)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64   = base64.b64encode(buf.getvalue()).decode("ascii")
                photo = _ITk2.PhotoImage(img)
                photo._b64data = b64
                if not hasattr(t_body, "_images"):
                    t_body._images = []
                t_body._images.append(photo)
                _restore_windows()
                # If the widget only has the placeholder, clear it first
                if t_body.get("1.0", "end-1c") == _PH:
                    t_body.delete("1.0", "end")
                    t_body.config(fg=_N["text_main"])
                t_body.image_create("insert", image=photo, padx=4, pady=4)
                t_body.focus_set()
            except Exception as ex:
                L("SCREENSHOT GRAB ERROR: %s", ex)
                _restore_windows()

        def _show_overlay():
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            overlay = tk.Toplevel()
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.geometry(f"{sw}x{sh}+0+0")
            overlay.attributes("-alpha", 0.5)
            overlay.configure(bg="#050510")
            cv = tk.Canvas(overlay, bg="#050510", cursor="crosshair",
                           highlightthickness=0)
            cv.pack(fill="both", expand=True)
            cv.create_text(sw//2, 30,
                           text="Drag to select a region    |    ESC to cancel",
                           fill="#f59e0b", font=("Segoe UI", 13, "bold"), anchor="center")
            rect_id = [None]; shade_ids = []; start = [0, 0]; coords = [None]

            def _clear():
                if rect_id[0]: cv.delete(rect_id[0]); rect_id[0] = None
                for s in shade_ids: cv.delete(s)
                shade_ids.clear()

            def _on_press(e):
                start[0], start[1] = e.x, e.y; _clear()

            def _on_drag(e):
                _clear()
                x0, y0, x1, y1 = start[0], start[1], e.x, e.y
                rect_id[0] = cv.create_rectangle(x0, y0, x1, y1,
                    outline="#f59e0b", width=3)
                shade_ids.append(cv.create_rectangle(x0+3, y0+3, x1-3, y1-3,
                    outline="#ffffff", width=1, dash=(6, 3)))
                w, h = abs(x1-x0), abs(y1-y0)
                lx = min(x0,x1)+w//2; ly = max(y0,y1)+18
                shade_ids.append(cv.create_rectangle(lx-32, ly-10, lx+32, ly+10,
                    fill="#1a1a2e", outline="#f59e0b", width=1))
                shade_ids.append(cv.create_text(lx, ly, text=f"{w} x {h}",
                    fill="#f59e0b", font=("Segoe UI", 9, "bold"), anchor="center"))

            def _on_release(e):
                coords[0] = (min(start[0],e.x), min(start[1],e.y),
                             max(start[0],e.x), max(start[1],e.y))
                overlay.destroy()
                root.after(120, lambda: _do_grab(coords[0]))

            def _on_escape(e):
                overlay.destroy(); _restore_windows()

            cv.bind("<ButtonPress-1>",   _on_press)
            cv.bind("<B1-Motion>",       _on_drag)
            cv.bind("<ButtonRelease-1>", _on_release)
            overlay.bind("<Escape>",     _on_escape)
            overlay.focus_force()

        # Hide everything first, then schedule overlay via root's event loop
        for w in hidden_editors:
            try: w.withdraw()
            except Exception: pass
        if was_root_visible:
            root.withdraw()
        root.after(200, _show_overlay)

    _fmt_cmds[6] = _fmt_screenshot

    _PH = "Take a note..."
    def _ph_in(e):
        # Only clear if the widget contains ONLY the placeholder text (no images)
        content = t_body.get("1.0", "end-1c")
        has_images = bool(t_body.dump("1.0", "end", image=True))
        if content == _PH and not has_images:
            t_body.delete("1.0", "end")
            t_body.config(fg=_N["text_main"])
    def _ph_out(e):
        content = t_body.get("1.0", "end-1c").strip()
        has_images = bool(t_body.dump("1.0", "end", image=True))
        if not content and not has_images:
            t_body.insert("1.0", _PH)
            t_body.config(fg=_N["text_dim"])
    if note:
        saved_text = note.get("text", "")
        saved_images = note.get("images", [])
        if saved_images:
            # Re-insert text with embedded images at [IMG:N] markers
            import re as _re2, base64 as _b64, io as _io
            from PIL import Image as _Img2, ImageTk as _ITk2
            if not hasattr(t_body, "_images"):
                t_body._images = []
            parts = _re2.split(r"(\[IMG:\d+\])", saved_text)
            for part in parts:
                m = _re2.match(r"\[IMG:(\d+)\]", part)
                if m:
                    idx = int(m.group(1))
                    if idx < len(saved_images):
                        try:
                            img_bytes = _b64.b64decode(saved_images[idx])
                            img = _Img2.open(_io.BytesIO(img_bytes))
                            photo = _ITk2.PhotoImage(img)
                            photo._b64data = saved_images[idx]
                            t_body._images.append(photo)
                            t_body.image_create("end", image=photo, padx=4, pady=4)
                        except Exception as ex:
                            L("NOTES IMAGE RESTORE ERROR idx=%d: %s", idx, ex)
                else:
                    if part:
                        t_body.insert("end", part)
        else:
            t_body.insert("1.0", saved_text)
    else:
        t_body.insert("1.0", _PH)
        t_body.config(fg=_N["text_dim"])
    t_body.bind("<FocusIn>",  _ph_in)
    t_body.bind("<FocusOut>", _ph_out)

    # --- Custom dark context menu ---
    _CTX_BG      = "#1e1e24"
    _CTX_HOVER   = "#2d2d35"
    _CTX_FG      = "#e4e4e7"
    _CTX_FG_DIM  = "#52525b"
    _CTX_SEP     = "#2a2a35"
    _CTX_FONT    = (_UI_FONT or "Segoe UI", 10)

    _ctx_win = [None]
    _saved_sel = [None]   # (start, end) saved before right-click clears it

    def _ctx_close():
        if _ctx_win[0]:
            try: _ctx_win[0].destroy()
            except Exception: pass
            _ctx_win[0] = None

    def _restore_sel():
        if _saved_sel[0]:
            try:
                t_body.tag_add("sel", _saved_sel[0][0], _saved_sel[0][1])
                t_body.mark_set("insert", _saved_sel[0][1])
            except Exception:
                pass

    def _ctx_show(e):
        # Save selection BEFORE right-click clears it
        try:
            _saved_sel[0] = (t_body.index("sel.first"), t_body.index("sel.last"))
        except Exception:
            _saved_sel[0] = None

        # Prevent right-click from moving the cursor / clearing selection
        t_body.after(1, _restore_sel)

        _ctx_close()

        has_sel = _saved_sel[0] is not None

        can_paste = False
        try:
            can_paste = bool(root.clipboard_get())
        except Exception:
            pass

        can_undo = False
        try:
            t_body.edit_undo(); t_body.edit_redo()
            can_undo = True
        except Exception:
            pass

        can_redo = False
        try:
            t_body.edit_redo(); t_body.edit_undo()
            can_redo = True
        except Exception:
            pass

        def _do(cmd):
            _restore_sel()
            t_body.after(1, cmd)

        menu_items = [
            ("Copy",  lambda: _do(lambda: t_body.event_generate("<<Copy>>")),  not has_sel),
            ("Cut",   lambda: _do(lambda: t_body.event_generate("<<Cut>>")),   not has_sel),
            ("Paste", lambda: _do(lambda: t_body.event_generate("<<Paste>>")), not can_paste),
            None,
            ("Undo",  lambda: _do(lambda: t_body.edit_undo()), not can_undo),
            ("Redo",  lambda: _do(lambda: t_body.edit_redo()), not can_redo),
        ]

        win = tk.Toplevel(dlg)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=_CTX_BG)
        _ctx_win[0] = win

        outer = tk.Frame(win, bg=_CTX_SEP, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=_CTX_BG)
        inner.pack(fill="both", expand=True, padx=0, pady=4)

        for item in menu_items:
            if item is None:
                tk.Frame(inner, bg=_CTX_SEP, height=1).pack(fill="x", padx=12, pady=3)
                continue
            label_text, cmd, disabled = item
            fg = _CTX_FG_DIM if disabled else _CTX_FG
            row = tk.Frame(inner, bg=_CTX_BG, cursor="arrow" if disabled else "hand2")
            row.pack(fill="x", padx=0)
            lbl = tk.Label(row, text=label_text, font=_CTX_FONT,
                           fg=fg, bg=_CTX_BG, anchor="w",
                           padx=20, pady=6)
            lbl.pack(fill="x")

            if not disabled:
                def _enter(ev, r=row, l=lbl):
                    r.config(bg=_CTX_HOVER); l.config(bg=_CTX_HOVER)
                def _leave(ev, r=row, l=lbl):
                    r.config(bg=_CTX_BG);    l.config(bg=_CTX_BG)
                def _click(ev, c=cmd):
                    _ctx_close(); dlg.after(10, c)
                row.bind("<Enter>",    _enter)
                row.bind("<Leave>",    _leave)
                row.bind("<Button-1>", _click)
                lbl.bind("<Enter>",    _enter)
                lbl.bind("<Leave>",    _leave)
                lbl.bind("<Button-1>", _click)

        # Position next to cursor, keep on screen
        win.update_idletasks()
        mw = win.winfo_reqwidth()
        mh = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        mx = min(e.x_root, sw - mw - 4)
        my = min(e.y_root, sh - mh - 4)
        win.geometry(f"+{mx}+{my}")

        # Close on any click outside
        win.bind("<FocusOut>", lambda ev: _ctx_close())
        dlg.bind("<Button-1>",   lambda ev: _ctx_close(), "+")
        t_body.bind("<Button-1>", lambda ev: _ctx_close(), "+")

    t_body.bind("<Button-3>", lambda e: (_ctx_show(e), "break")[1])

    dlg.bind("<Control-Return>", lambda e: _save())

    def _dlg_scroll(e):
        t_body.yview_scroll(int(-1 * (e.delta / 120)), "units")
    dlg.bind_all("<MouseWheel>", _dlg_scroll)

    # Resize grip — overlay the actual bottom-right corner of the Toplevel.
    # The editor uses overrideredirect(), so Windows does not provide a native
    # resize border. A child of fmt_footer can be covered by fmt_inner; placing
    # the handle directly on dlg guarantees it remains the topmost resize target.
    _RESIZE_GRIP = 30
    grip = tk.Canvas(
        dlg, width=_RESIZE_GRIP, height=_RESIZE_GRIP,
        bg=_FTR_BG, highlightthickness=0, bd=0,
        cursor="size_nw_se"
    )
    grip.place(relx=1.0, rely=1.0, anchor="se", x=-1, y=-1)

    # Draw a clearly visible but compact diagonal resize affordance.
    grip.create_line(8, 22, 22, 8, fill=_N["text_dim"], width=1.2)
    grip.create_line(14, 22, 22, 14, fill=_N["text_dim"], width=1.2)
    grip.create_line(20, 22, 22, 20, fill=_N["text_dim"], width=1.2)

    def _grip_press(e):
        dlg._resize_start_x = e.x_root
        dlg._resize_start_y = e.y_root
        dlg._resize_start_w = dlg.winfo_width()
        dlg._resize_start_h = dlg.winfo_height()
        return "break"

    def _grip_drag(e):
        dx = e.x_root - dlg._resize_start_x
        dy = e.y_root - dlg._resize_start_y
        nw = max(320, dlg._resize_start_w + dx)
        nh = max(240, dlg._resize_start_h + dy)
        dlg.geometry(f"{int(nw)}x{int(nh)}")
        return "break"

    grip.bind("<ButtonPress-1>", _grip_press)
    grip.bind("<B1-Motion>", _grip_drag)
    grip.bind("<ButtonRelease-1>", lambda e: "break")

    t_body.focus_set()
    if note:
        t_body.mark_set("insert", "end")


_notes_add_lbl.bind("<Button-1>", lambda e: _notes_open_editor())
_notes_search_entry.bind("<KeyRelease>", lambda e: _notes_render_list())

# ---------------------------------------------------------------------------
# CONV panel — Voice conversation (Gemini 2.5 Flash Native Audio)
# ---------------------------------------------------------------------------
import math as _math
import random as _random
import time as _time
import struct as _struct

# Palette for CONV
_CV = {
    "panel_bg":   "#1a1a22",
    "orb_row_bg": "#1a1a22",
    "accent":     "#818cf8",
    "muted":      "#475569",
    "you_fg":     "#F3F4F6",
    "gem_fg":     "#F3F4F6",
    "cap_pill_bg":"#12131C",
    "cap_border": "#2B2D3E",
    "orb_user":   "#EF4444",
    "orb_ai":     "#8B5CF6",
    "transp_key": "#010101",
}

# ---------------------------------------------------------------------------
# CONV panel — orb row packed inside the main widget (frame)
# ---------------------------------------------------------------------------
_conv_cap_rows  = []
_MAX_CAPTIONS   = 6
_ORB_ROW_H      = 64   # height of the orb row section
_CAP_W          = 490  # caption strip width (matches main widget)
_CAP_MARGIN     = 5

def _conv_blend(fg, bg, a):
    fr,fg2,fb = int(fg[1:3],16),int(fg[3:5],16),int(fg[5:7],16)
    br,bg2,bb = int(bg[1:3],16),int(bg[3:5],16),int(bg[5:7],16)
    r=int(br+(fr-br)*a); g=int(bg2+(fg2-bg2)*a); b=int(bb+(fb-bb)*a)
    return f"#{r:02x}{g:02x}{b:02x}"

# --- Orb row frame (packed into `frame`, same parent as prompt_panel/notes_panel)
conv_panel = tk.Frame(frame, bg=_CV["orb_row_bg"], height=_ORB_ROW_H)
tk.Frame(conv_panel, bg=_CV["accent"], height=2).pack(fill="x", side="top")

_conv_orb_canvas = tk.Canvas(conv_panel, height=_ORB_ROW_H - 2,
                              bg=_CV["orb_row_bg"], highlightthickness=0, bd=0,
                              cursor="hand2")
_conv_orb_canvas.pack(fill="x", expand=True)

_CONV_STATUS_LABELS = {
    "connecting": (_CV["orb_user"],  "CONNECTING..."),
    "listening":  (_CV["accent"],    "LISTENING"),
    "speaking":   (_CV["orb_ai"],    "GEMINI SPEAKING"),
    "stopped":    (_CV["muted"],     "TAP TO START"),
    "error":      ("#f87171",        "ERROR"),
}
_conv_last_status = ["stopped"]


def _conv_draw_orbs(state="stopped"):
    c = _conv_orb_canvas
    c.delete("all")
    W  = c.winfo_width() or 492
    H  = _ORB_ROW_H - 2
    cy = H // 2
    R  = 22
    t  = _time.time()
    ui = _conv_user_level[0]
    ai = _conv_ai_level[0]

    def _draw_orb(cx, lvl, color):
        idle = 0.05 + 0.03 * _math.sin(t * 3.0)
        lv   = max(lvl, idle)
        for i in range(3, 0, -1):
            rr  = R + lv * 12 * (i / 3)
            col = _conv_blend(color, _CV["orb_row_bg"], (45/i)/255.0)
            c.create_oval(cx-rr, cy-rr, cx+rr, cy+rr,
                          fill="", outline=col, width=1.5)
        rr = R * (0.8 + lv * 0.3)
        c.create_oval(cx-rr, cy-rr, cx+rr, cy+rr, fill=color, outline="")
        hr = rr * 0.35
        c.create_oval(cx-hr*0.5, cy-rr*0.5, cx+hr*0.5, cy-rr*0.5+hr,
                      fill="#ffffff", outline="")

    _draw_orb(R + 10, ui, _CV["orb_user"])     # user — left
    _draw_orb(W - R - 10, ai, _CV["orb_ai"])  # AI   — right

    col, txt = _CONV_STATUS_LABELS.get(state, (_CV["muted"], state.upper()))
    c.create_text(W // 2, cy, text=txt,
                  font=(_UI_FONT or "Segoe UI", 9, "bold"),
                  fill=col, anchor="center")

_conv_orb_canvas.bind("<Configure>", lambda e: _conv_draw_orbs(_conv_last_status[0]))


# ---------------------------------------------------------------------------
# Caption Toplevel — transparent, floats below the expanded main widget
# ---------------------------------------------------------------------------
_TRANSP      = _CV["transp_key"]
_conv_cap_win  = [None]
_conv_caps_c   = [None]

def _conv_build_caps():
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=_TRANSP)
    try:
        win.attributes("-transparentcolor", _TRANSP)
    except Exception:
        pass
    win.withdraw()
    caps_c = tk.Canvas(win, width=_CAP_W, height=0,
                       bg=_TRANSP, highlightthickness=0)
    caps_c.pack()
    _conv_cap_win[0] = win
    _conv_caps_c[0]  = caps_c

_conv_build_caps()


def _conv_caps_show():
    """Position caption window below the expanded main widget."""
    win = _conv_cap_win[0]
    if win is None:
        return
    root.update_idletasks()
    rx = root.winfo_x()
    ry = root.winfo_y()
    rh = root.winfo_height()
    rw = root.winfo_width()
    cx = rx + (rw - _CAP_W) // 2
    cy = ry + rh + 4
    cap_h = max(_conv_caps_c[0].winfo_reqheight(), 0)
    win.geometry(f"{_CAP_W}x{max(cap_h,1)}+{cx}+{cy}")
    win.deiconify()
    win.lift()


def _conv_caps_hide():
    if _conv_cap_win[0]:
        _conv_cap_win[0].withdraw()


def _conv_caps_reposition(e=None):
    """Keep caption window snapped below the main widget when it moves."""
    if mode != "conv":
        return
    win = _conv_cap_win[0]
    if win is None or win.state() == "withdrawn":
        return
    rx = root.winfo_x()
    ry = root.winfo_y()
    rh = root.winfo_height()
    rw = root.winfo_width()
    cx = rx + (rw - _CAP_W) // 2
    cy = ry + rh + 4
    win.geometry(f"+{cx}+{cy}")

frame.bind("<Configure>", _conv_caps_reposition, add="+")


def _conv_relayout_captions():
    c = _conv_caps_c[0]
    if c is None:
        return
    c.delete("all")
    UI_FONT = _UI_FONT or "Segoe UI"
    pad_h   = 8
    badge_x = 12
    text_x  = badge_x + 14
    wrap_w  = _CAP_W - text_x - 16
    y       = 0
    total_h = 0
    r       = 12  # corner radius for smooth polygon

    def _rounded_rect(x0, y0, x1, y1, fill, outline):
        # Smooth polygon — no jagged arcs
        pts = [
            x0+r, y0,   x1-r, y0,
            x1,   y0,   x1,   y0+r,
            x1,   y1-r, x1,   y1,
            x1-r, y1,   x0+r, y1,
            x0,   y1,   x0,   y1-r,
            x0,   y0+r, x0,   y0,
        ]
        c.create_polygon(pts, smooth=True, fill=fill,
                         outline=outline, width=1)

    for speaker, text in _conv_cap_rows:
        tmp = c.create_text(0, -999, text=text, font=(UI_FONT, 9),
                            width=wrap_w, anchor="nw")
        bb  = c.bbox(tmp); c.delete(tmp)
        th  = (bb[3] - bb[1]) if bb else 14
        row_h = max(th + pad_h * 2, 32)

        _rounded_rect(2, y, _CAP_W - 2, y + row_h,
                      fill=_CV["cap_pill_bg"], outline=_CV["cap_border"])

        dot_c = _CV["orb_ai"] if speaker == "ai" else _CV["orb_user"]
        by    = y + (row_h - 8) // 2
        c.create_oval(badge_x, by, badge_x + 8, by + 8,
                      fill=dot_c, outline="")

        fg = _CV["gem_fg"] if speaker == "ai" else _CV["you_fg"]
        c.create_text(text_x, y + pad_h, text=text, fill=fg,
                      font=(UI_FONT, 9), width=wrap_w, anchor="nw")
        y       += row_h + _CAP_MARGIN
        total_h += row_h + _CAP_MARGIN

    c.config(height=max(total_h, 0))
    # Reposition the caption window
    win = _conv_cap_win[0]
    if win and total_h > 0:
        root.update_idletasks()
        rx2 = root.winfo_x()
        ry2 = root.winfo_y()
        rh2 = root.winfo_height()
        rw2 = root.winfo_width()
        cx2 = rx2 + (rw2 - _CAP_W) // 2
        cy2 = ry2 + rh2 + 4
        win.geometry(f"{_CAP_W}x{total_h}+{cx2}+{cy2}")
        if mode == "conv":
            win.deiconify()
            win.lift()
        if mode == "conv":
            win.deiconify()
            win.lift()


def _conv_set_ui_status(status):
    _conv_last_status[0] = status
    if status == "listening":
        focus_status.set(" Voice Chat — Listening")
        state_label.config(fg=_CV["accent"])
    elif status == "speaking":
        focus_status.set(" Voice Chat — Gemini Speaking")
        state_label.config(fg=_CV["orb_ai"])
    elif status == "connecting":
        focus_status.set(" Voice Chat — Connecting")
        state_label.config(fg=_CV["orb_user"])
    elif status == "stopped":
        focus_status.set(" Voice Chat")
        state_label.config(fg=MUTED)
    elif status == "error":
        focus_status.set(" Voice Chat — Error")
        state_label.config(fg="#f87171")
    _conv_draw_orbs(status)


def _conv_push_caption(speaker, text):
    if _conv_cap_rows and _conv_cap_rows[-1][0] == speaker:
        _conv_cap_rows[-1][1] = text
    else:
        _conv_cap_rows.append([speaker, text])
    if len(_conv_cap_rows) > _MAX_CAPTIONS:
        del _conv_cap_rows[:-_MAX_CAPTIONS]
    _conv_relayout_captions()


def _conv_clear_transcript():
    _conv_cap_rows.clear()
    c = _conv_caps_c[0]
    if c:
        c.delete("all")
        c.config(height=0)
    if _conv_cap_win[0]:
        _conv_cap_win[0].withdraw()


# Tap orb row to toggle session
def _conv_orb_tap(e):
    if _conv_is_connected:
        stop_conv()
        for _mb in (mode_live, mode_buf, mode_pro, mode_not, mode_conv):
            try: _mb.config(state="normal")
            except Exception: pass
    else:
        for _mb in (mode_live, mode_buf, mode_pro, mode_not, mode_conv):
            try: _mb.config(state="disabled")
            except Exception: pass
        start_conv()

_conv_orb_canvas.bind("<Button-1>", _conv_orb_tap)

# Animation loop — always running
def _conv_animate():
    if _conv_is_connected:
        _conv_ai_level[0]   *= 0.88
        _conv_user_level[0] *= 0.88
    _conv_draw_orbs(_conv_last_status[0])
    root.after(30, _conv_animate)

root.after(120, _conv_animate)

# Patch _on_conv_ended
_orig_on_conv_ended = _on_conv_ended
def _on_conv_ended():
    _orig_on_conv_ended()
    try:
        for _mb in (mode_live, mode_buf, mode_pro, mode_not, mode_conv):
            try: _mb.config(state="normal")
            except Exception: pass
        _conv_draw_orbs("stopped")
    except Exception:
        pass
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# System tray icon
# ---------------------------------------------------------------------------
# Thread-safe queue: tray callbacks post commands here; Tk polls and executes
_tray_queue = queue.Queue()

def _tray_poll():
    """Drain tray command queue on the Tk main thread — no cross-thread Tk calls."""
    try:
        while True:
            cmd = _tray_queue.get_nowait()
            if cmd == "show":
                if root.state() == "withdrawn":
                    open_widget()
                else:
                    close_widget()
            elif cmd == "kill_restart":
                _kill_stale_processes()
                restart_app()
            elif cmd == "kill_procs":
                _kill_stale_processes()
            elif cmd == "close":
                close_widget()
            elif cmd == "exit":
                try: _tray_icon.stop()
                except Exception: pass
                if recording:
                    stop_record("exit")
                try: uia.stop()
                except Exception: pass
                try: os.remove(_PID_FILE)
                except Exception: pass
                os._exit(0)
    except queue.Empty:
        pass
    root.after(100, _tray_poll)
def _make_tray_icon_image():
    """Load the app logo PNG for the tray icon."""
    try:
        from PIL import Image
        logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini-dictate-logo.png")
        img = Image.open(logo).convert("RGBA").resize((64, 64), Image.LANCZOS)
        return img
    except Exception:
        # Fallback: generate amber GD circle
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, 62, 62], fill="#f59e0b")
        return img


def _tray_show(icon, item):
    _tray_queue.put("show")


def _tray_kill_restart(icon, item):
    _tray_queue.put("kill_restart")


def _tray_kill_processes(icon, item):
    _tray_queue.put("kill_procs")


def _tray_close_widget(icon, item):
    _tray_queue.put("close")


def _tray_exit(icon, item):
    _tray_queue.put("exit")



import pystray

_tray_menu = pystray.Menu(
    pystray.MenuItem("Show / Hide Widget  (F8)", _tray_show, default=True),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Kill Stale Processes & Restart", _tray_kill_restart),
    pystray.MenuItem("Kill Stale Processes", _tray_kill_processes),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Close Widget", _tray_close_widget),
    pystray.MenuItem("Exit", _tray_exit),
)

_tray_icon = pystray.Icon(
    "GeminiDictate",
    _make_tray_icon_image(),
    "GeminiDictate",
    _tray_menu,
)

threading.Thread(target=_tray_icon.run, daemon=True).start()
L("TRAY ICON STARTED")

uia.start()
threading.Thread(target=hotkey_message_thread, daemon=True).start()
root.after(FOCUS_POLL_MS, update_focus_tracking)
root.after(30, hotkey_poll)
root.after(100, _tray_poll)
root.after(500, _topmost_keepalive)
root.after(2000, idle_stop_poll)
_apply_ui_font_to_all()

root.mainloop()

# Cleanup on normal exit
try:
    uia.stop()
except Exception:
    pass
try:
    _tray_icon.stop()
except Exception:
    pass
try:
    os.remove(_PID_FILE)
except Exception:
    pass

