import re
import asyncio
import ctypes
import json
import logging
import os
import queue
import subprocess
import threading
import time
import traceback
import tkinter as tk

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from google import genai
from google.genai import types

MODEL = "gemini-3.5-transcribe-live"
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
mode = "live"
stop_reason = ""

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

def mic_cb(indata, frames, time_info, status):
    try:
        if status:
            L("MIC STATUS: %s", status)
        q.put_nowait(indata[:, 0].copy())
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

    try:
        L("CONNECTING mode=%s model=%s", "LIVE" if live_mode else "BUFFERED", MODEL)
        async with client.aio.live.connect(model=MODEL, config=cfg) as session:
            L("GEMINI CONNECTED model=%s", MODEL)

            async def recv():
                nonlocal recv_count
                try:
                    async for response in session.receive():
                        recv_count += 1
                        sc = getattr(response, "server_content", None)
                        if sc is None:
                            continue

                        # INTERIM TRANSCRIPTION IS INTENTIONALLY NOT WRITTEN TO THE FIELD.
                        # It is only a hypothesis and replacing it repeatedly was the source
                        # of unreliable caret/undo/clipboard behavior. LIVE now behaves like
                        # buffered mode: only authoritative finalized chunks are inserted.

                        inp = getattr(sc, "input_transcription", None)
                        if inp is not None and getattr(inp, "text", None):
                            txt = inp.text
                            pieces.append(txt)
                            root.after(0, update_live_transcript, txt, True)
                            L("FINAL INPUT TRANSCRIPTION #%d chars=%d text=%r", recv_count, len(txt), txt)
                            if live_mode:
                                def insert_final(chunk=txt):
                                    ok, detail = insert_live_final(chunk)
                                    if ok:
                                        live_insert_status.set(f"LIVE OUTPUT: {len(chunk):,} chars")
                                    else:
                                        live_insert_status.set(detail)
                                        focus_status.set("CLIPBOARD FALLBACK ACTIVE")
                                        state_label.config(fg="#f59e0b")
                                        L("LIVE FINAL INSERT FAILED chars=%d: %s", len(chunk), detail)
                                root.after(0, insert_final)

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
        status.set("TEXT FIELD NOT READY")
        focus_status.set(" CLICK A TEXT FIELD")
        L("START REJECTED  current UIA focus is not editable current=%r candidate=%r", current_focus, candidate)
        return

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
        focus_status.set(f"🔒 {locked_app}")
    else:
        focus_status.set(" RECORDING")
    btn.config(text="  STOP", command=lambda: stop_record("STOP button"), state="normal",
               bg=STOP, fg="#ffffff", activebackground="#fb7185", activeforeground="#ffffff")
    mode_live.config(state="disabled")
    mode_buf.config(state="disabled")
    L(
        "START ACCEPTED mode=%s top=%s focus_uia=%r description=%r",
        mode_name,
        hex(top.get("top_hwnd", 0)),
        u,
        description,
    )
    current_future = asyncio.run_coroutine_threadsafe(record_once(mode == "live"), loop)
    root.after(100, poll_result)


def stop_record(reason="user stop"):
    global recording, stop_reason, _focus_change_count
    if not recording:
        return
    stop_reason = reason
    recording = False
    _focus_change_count = 0
    if mode == "live":
        status.set("STOPPING  flushing last segment")
    else:
        status.set("FINALIZING  completing Gemini transcription")
    focus_status.set(" STOPPING")
    state_label.config(fg="#f59e0b")
    btn.config(text="", state="disabled", bg="#27272a", fg="#a1a1aa", activebackground="#27272a", activeforeground="#a1a1aa")
    mode_live.config(state="disabled")
    mode_buf.config(state="disabled")
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
        mode_name = "LIVE" if mode == "live" else "BUFFERED"
        if mode == "live":
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
        btn.config(text="  START", command=start_record, state="normal",
                   bg="#f4f4f5", fg="#18181b", activebackground="#ffffff", activeforeground="#09090b")
        mode_live.config(state="normal")
        mode_buf.config(state="normal")
        focus_status.set(" Dictate")
        state_label.config(fg=MUTED)
        show_clipboard_fallback(False)


def set_mode(new_mode):
    global mode
    if recording:
        return
    mode = new_mode
    selected_bg = ACTIVE
    unselected_bg = "#09090b"
    mode_live.config(bg=selected_bg if mode == "live" else unselected_bg,
                     fg=TEXT if mode == "live" else MUTED)
    mode_buf.config(bg=selected_bg if mode == "buffered" else unselected_bg,
                    fg=TEXT if mode == "buffered" else MUTED)
    mode_help.set("LIVE" if mode == "live" else "BUFFER")
    L("MODE CHANGED mode=%s", mode)


def toggle_mode():
    set_mode("buffered" if mode == "live" else "live")


def toggle_focus_lock():
    global focus_lock
    focus_lock = not focus_lock
    if focus_lock:
        lock_btn.config(text="🔒", fg=TEXT)
        L("FOCUS LOCK ON  auto-stop on focus change disabled")
    else:
        lock_btn.config(text="🔓", fg=MUTED)
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
    L("F8 WIDGET OPENED  target intentionally NOT captured")


def close_widget():
    if recording:
        stop_record("widget closed")
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
            if root.state() == "withdrawn":
                open_widget()
    except queue.Empty:
        pass

    # Fallback: if global hotkey registration failed, poll F8 key state directly.
    # GetAsyncKeyState returns negative (high bit set) when the key is currently down.
    if not _hotkey_registered:
        global _f8_was_down
        is_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
        if is_down and not _f8_was_down:
            if root.state() == "withdrawn":
                open_widget()
        _f8_was_down = is_down

    root.after(30, hotkey_poll)


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
root.geometry("460x54+700+50")
root.resizable(False, False)
root.configure(bg="#121215")
root.overrideredirect(True)
root.attributes("-topmost", True)
root.protocol("WM_DELETE_WINDOW", close_widget)
root.withdraw()

# The widget is intentionally non-activating. Clicking START/STOP must NOT
# steal keyboard focus from the real text field.
def make_widget_no_activate():
    try:
        hwnd = int(root.winfo_id())
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle | WS_EX_NOACTIVATE)
        root.update_idletasks()
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | 0x0020 | 0x0040)
        # Tk draws the fixed pill itself. Do not apply a Windows region here:
        # the root is still withdrawn at startup and its measured size can be
        # 1x1, which would clip the entire widget.
        root.update()
        L("WIDGET WS_EX_NOACTIVATE + BORDERLESS PILL APPLIED hwnd=%s", hex(hwnd))
    except Exception:
        L("WIDGET NO-ACTIVATE ERROR\n%s", traceback.format_exc())

root.after_idle(make_widget_no_activate)

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
STOP = "#e11d48"

frame = tk.Frame(root, padx=1, pady=1, bd=1, relief="solid", bg=BORDER)
frame.pack(fill="both", expand=True)
pill = tk.Frame(frame, padx=9, pady=6, bg=BG)
pill.pack(fill="both", expand=True)

# Permanent columns: 1) status, 2) mode, 3) action, 4) lock, 5) close.
pill.grid_columnconfigure(0, minsize=205, weight=0)
pill.grid_columnconfigure(1, minsize=90, weight=0)
pill.grid_columnconfigure(2, minsize=75, weight=0)
pill.grid_columnconfigure(3, minsize=28, weight=0)
pill.grid_columnconfigure(4, minsize=24, weight=0)
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

mode_wrap = tk.Frame(pill, bg="#09090b", bd=1, relief="solid", width=90, height=30)
mode_wrap.grid(row=0, column=1, sticky="w", padx=(0, 8))
mode_wrap.grid_propagate(False)
mode_wrap.grid_columnconfigure(0, weight=1, uniform="mode")
mode_wrap.grid_columnconfigure(1, weight=1, uniform="mode")
mode_live = tk.Button(mode_wrap, text="LIVE", command=lambda: set_mode("live"),
    font=("Segoe UI", 8, "bold"), fg=TEXT, bg=ACTIVE,
    activebackground="#52525b", activeforeground="white", relief="flat", bd=0,
    cursor="hand2")
mode_live.grid(row=0, column=0, sticky="nsew")
mode_buf = tk.Button(mode_wrap, text="BUF", command=lambda: set_mode("buffered"),
    font=("Segoe UI", 8, "bold"), fg=MUTED, bg="#09090b",
    activebackground="#27272a", activeforeground=TEXT, relief="flat", bd=0,
    cursor="hand2")
mode_buf.grid(row=0, column=1, sticky="nsew")
mode_label = mode_live

btn = tk.Button(
    pill, text="  START", command=start_record,
    font=("Segoe UI", 8, "bold"), width=9, height=1,
    relief="flat", bd=0, bg="#f4f4f5", fg="#18181b",
    activebackground="#ffffff", activeforeground="#09090b", cursor="hand2"
)
btn.grid(row=0, column=2, sticky="w")

lock_btn = tk.Button(
    pill, text="🔓", command=toggle_focus_lock, width=2, height=1,
    relief="flat", bd=0, font=("Segoe UI", 11), fg=MUTED, bg=BG,
    activebackground="#27272a", activeforeground=TEXT, cursor="hand2"
)
lock_btn.grid(row=0, column=3, sticky="e", padx=(0, 2))

close_btn = tk.Button(
    pill, text="X", command=close_widget, width=2, height=1,
    relief="flat", bd=0, font=("Segoe UI", 10, "bold"), fg=MUTED, bg=BG,
    activebackground="#27272a", activeforeground=TEXT, cursor="hand2"
)
close_btn.grid(row=0, column=4, sticky="e", padx=(3, 0))

# Allow the widget to be moved without changing focus semantics.
def drag_start(event):
    root._drag_x = event.x_root - root.winfo_x()
    root._drag_y = event.y_root - root.winfo_y()


def drag_move(event):
    root.geometry(f"+{event.x_root - root._drag_x}+{event.y_root - root._drag_y}")

for w in (frame, pill):
    w.bind("<Button-1>", drag_start)
    w.bind("<B1-Motion>", drag_move)

uia.start()
threading.Thread(target=hotkey_message_thread, daemon=True).start()
root.after(FOCUS_POLL_MS, update_focus_tracking)
root.after(30, hotkey_poll)
root.after(500, _topmost_keepalive)
root.mainloop()

try:
    uia.stop()
except Exception:
    pass














