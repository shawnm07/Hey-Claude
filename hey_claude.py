"""
Hey Claude: a local wake word that hands spoken commands to Claude Code.

  "Hey Claude, close Chrome"   /   "Hey Claude" ... (chime) ... "add this to my tasks"

Pipeline
  mic -> energy gate -> faster-whisper small.en (local) -> wake word?
     -> instant local action (open/close app, volume, media keys, time/date)   ~0.2 s
     -> or a warm Claude Code session, model picked by complexity:
          Haiku  simple one-step commands
          Sonnet tasks/calendar, anything about the screen, multi-step
          Opus   research, writing, "think it through"
        "this/that/on my screen" also attaches screenshots of every monitor.
  -> spoken reply (Kokoro, local)  +  a floating status pill on the primary monitor.

Conversation
  * If Claude replies with a question, it listens for the answer right away
    (no wake word needed) for ANSWER_WINDOW_S.
  * Commands within 15 minutes share context across all models and the instant
    actions ("open Spotify" ... "close it"). After 15 quiet minutes it starts fresh.
  * workspace/memory/permanent.md : "add X to your memory" / "forget X" (user-controlled)
    workspace/memory/learned.md   : rewritten automatically when a conversation
                                    ends, from what was asked and what worked.

Only the command text after "Hey Claude" (and, when needed, screenshots) goes
to Claude. Audio is never saved or sent. Listens only while plugged in: Windows
reports AC power OR an external monitor is connected (docked).

Run:   .venv\\Scripts\\pythonw.exe hey_claude.py           (normal, no console)
       .venv\\Scripts\\python.exe  hey_claude.py --debug   (log every phrase heard)
Tests: --transcribe file.wav   --say "text"   --demo   --screens
Log:   hey-claude.log next to this file
"""

import collections
import contextlib
import ctypes
import ctypes.wintypes as wt
import difflib
import json
import logging
import math
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
import zlib
from pathlib import Path

import numpy as np
import sounddevice as sd

# ================================================================= settings

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"
SCREEN_DIR = WORKSPACE / "screen"
MEMORY_DIR = WORKSPACE / "memory"
MODELS = HERE / "models"
VOICE_DIR = HERE / "voice"
CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
LOG_FILE = HERE / "hey-claude.log"
DEBUG = "--debug" in sys.argv

WHISPER_MODEL = "small.en"
VOICE = "af_heart"             # Kokoro voice: af_heart, af_bella, am_michael, am_puck, bf_emma, bm_george...
VOICE_SPEED = 1.1
VOICE_LEAD_S = 0.15            # silence before speech when the speaker is already awake (see Voice.warm)
VOICE_COLD_LEAD_S = 0.8        # ...and when it isn't: a sleeping Bluetooth speaker drops ~0.5-1 s
VOICE_STAY_AWAKE_S = 6.0       # keep the speaker awake this long after a reply (follow-ups start clean)
KOKORO_RATE = 24000            # Kokoro's output sample rate
# Bluetooth speakers mute their amp on pure digital silence and take ~0.5 s to un-mute, which ate the
# first words even with the stream kept open. So "silence" (keep-awake, lead, gaps, tail) is an
# inaudible 20 Hz tone at -40 dBFS instead of zeros: too low for a small speaker to reproduce.
KEEPALIVE_HZ = 20.0
KEEPALIVE_LEVEL = 0.01
VOICE_TAIL_S = 0.6             # silence after speech (Bluetooth lag cut off the last word otherwise)

RATE = 16000
FRAME = 480                    # 30 ms
FRAME_S = FRAME / RATE
MIN_LEVEL = 0.010              # absolute RMS floor for "speech"
START_FRAMES = 5               # 150 ms of sound starts an utterance
END_SILENCE_S = 0.6            # this much quiet ends one stretch of speech, but a command keeps
                               # collecting if you talk again within settings' pause_allowed_seconds
MAX_UTTER_S = 30.0
MIN_UTTER_S = 0.5
PRE_ROLL_S = 0.4
WAKE_CHECK_S = 2.5             # utterances longer than SINGLE_PASS_S: check only the start first
SINGLE_PASS_S = 6.0
FOLLOWUP_S = 6.0               # after a bare "Hey Claude", time to START the command
                               # (answer window after a question: settings' answer_window_seconds)
FAST_WAKE_MODEL = "tiny.en"              # tiny, ~0.15 s per check: only used to SPOT "Hey Claude" quickly
EARLY_WAKE_AT_S = (0.8, 1.0, 1.3, 1.7, 2.2)   # while you talk, check for "Hey Claude" at these points
                                         # (no 0.6: most false spots came from heads that short)
EARLY_WAKE_MIN_SNR_DB = 14.0             # early spot needs a voice this far above room noise (owner at the laptop: ~23-26 dB)
EARLY_WAKE_HEAD_S = 2.4                  # so the pill appears right away (only the first ~2.4 s is checked)
BARGE_WINDOW_S = 2.5           # while it's talking: how much recent audio is checked for "Hey Claude"
BARGE_EVERY_S = 1.2            #    and how often

CONTEXT_WINDOW_S = 15 * 60     # commands this close together share context; longer gap = fresh start

# ---- settings Claude may change by voice (workspace/settings.json, re-read live)
SETTINGS_FILE = WORKSPACE / "settings.json"
MODEL_NAMES = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_SETTINGS = {
    "models": {"simple": "haiku", "normal": "sonnet", "complex": "opus"},
    "effort": {"haiku": "low", "sonnet": "low", "opus": "medium"},
    "voice": "af_heart",
    "voice_speed": 1.1,
    "hud_seconds_after_reply": 10,
    "answer_window_seconds": 8,
    "pause_allowed_seconds": 1.4,
    "listen_after_reply_seconds": 3,
    "chat_hud": True,              # false = the old status pill only (no chat window); read at startup
}
CONV_DIR = WORKSPACE / "conversations"   # one .jsonl log per conversation (append-only, never deleted)
SKILLS_DIR = WORKSPACE / "skills"        # script skills: instant commands the models can create (skills.py)
SKILLS_VERIFIED = HERE / "skills-verified.json"   # skills whose first run went through a model (kept out of
SKILL_EXAMPLES = HERE / "skills-examples"         # the workspace, so a model can't mark its own skill trusted)


class Settings:
    """Validated view of settings.json. Bad values are ignored (defaults kept), never fatal."""

    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.mtime = None
        self.reload()

    def reload(self) -> bool:
        try:
            mtime = SETTINGS_FILE.stat().st_mtime
        except FileNotFoundError:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2) + "\n", encoding="utf-8")
            mtime = SETTINGS_FILE.stat().st_mtime
        if mtime == self.mtime:
            return False
        self.mtime = mtime
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"settings.json is not valid JSON ({e}); keeping previous settings.")
            return False
        d = json.loads(json.dumps(DEFAULT_SETTINGS))
        for tier in d["models"]:
            v = str(raw.get("models", {}).get(tier, "")).lower()
            if v in MODEL_NAMES:
                d["models"][tier] = v
        for m in d["effort"]:
            v = str(raw.get("effort", {}).get(m, "")).lower()
            if v in EFFORTS:
                d["effort"][m] = v
        if isinstance(raw.get("voice"), str) and re.fullmatch(r"[a-z]{2}_[a-z]+", raw["voice"]):
            d["voice"] = raw["voice"]

        def num(key, lo, hi):
            try:
                d[key] = min(hi, max(lo, float(raw.get(key, d[key]))))
            except (TypeError, ValueError):
                pass
        num("voice_speed", 0.7, 1.6); num("hud_seconds_after_reply", 3, 120)
        num("answer_window_seconds", 3, 30); num("pause_allowed_seconds", 0.8, 4.0)
        num("listen_after_reply_seconds", 0, 10)
        if isinstance(raw.get("chat_hud"), bool):
            d["chat_hud"] = raw["chat_hud"]
        old, self.data = self.data, d
        if old != d:
            log(f"Settings loaded: {json.dumps(d)}")
        return old != d

    def __getitem__(self, key):
        return self.data[key]


SETTINGS = None


def settings() -> Settings:
    global SETTINGS
    if SETTINGS is None:
        SETTINGS = Settings()
    return SETTINGS


def continue_s() -> float:
    return max(0.2, settings()["pause_allowed_seconds"] - END_SILENCE_S)

HOTWORDS = "Hey Claude. Chrome, Spotify, Discord, TickTick, Calculator, Settings, Wi-Fi, hotspot, volume"

WAKE = re.compile(
    r"\b(?:hey|hi|hay|heh|okay|ok)[\s,.!-]*"
    r"(?:claude|claud|cloud|clod|clawed|claw|klaud|clyde)\b[\s,.!?-]*",
    re.IGNORECASE,
)

# ---- Claude sessions
CLAUDE_TIMEOUT_S = 180
MAX_TURNS = 20                 # fresh session after this many commands (keeps it fast)
MODEL_LABEL = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus"}
TOOLS = "PowerShell,Bash,Read,Glob,Grep,Edit,Write"   # Edit/Write: for its memory files in the workspace
# Connectors a voice command may use. Everything else on the account is blocked
# (so a misheard command can never send an email or Slack message).
# Email is done in the BROWSER (compose tab + pressing Send), never through the Gmail connector
# (user choice: no direct access). The Send press is guarded by the approval lock below.
BLOCKED_CONNECTORS = [
    "mcp__claude_ai_Gmail", "mcp__claude_ai_Slack", "mcp__claude_ai_Google_Drive",
    "mcp__claude_ai_Google_Docs", "mcp__claude_ai_Google_Sheets", "mcp__claude_ai_Google_Slides",
    "mcp__claude_ai_Claude_Docs", "mcp__claude_ai_Crypto_com", "mcp__claude_ai_Microsoft_365",
]

# ---- approval lock: a hook that blocks sending unless the user just said a clear "yes"
APPROVAL_TOKEN = WORKSPACE / ".approval"
SEND_TOOLS_MATCHER = "PowerShell|Bash"   # the gate itself decides: only send-compose / Ctrl+Enter presses need a yes
_GATE_CMD = (str(HERE / ".venv" / "Scripts" / "python.exe").replace("\\", "/") + " -I "
             + str(HERE / "approval_gate.py").replace("\\", "/"))
APPROVAL_HOOK_SETTINGS = json.dumps({"hooks": {"PreToolUse": [
    {"matcher": SEND_TOOLS_MATCHER, "hooks": [{"type": "command", "command": _GATE_CMD, "timeout": 20}]}]}})
YES_RE = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|do it|send it|delete it|go ahead|go for it|confirm(?:ed)?|"
                    r"please do|affirmative|that's right|correct)\b", re.I)
NO_RE = re.compile(r"\b(?:no|nope|don't|do not|not|wait|stop|cancel|hold on|never ?mind)\b", re.I)

REFLECT_PROMPT = (
    "[Automatic wrap-up, not from the user. Nothing you say here is spoken.] "
    "This conversation (spoken and typed) has ended. Review the commands in it and update memory/learned.md (Edit tool) "
    "with anything that will make future voice commands faster or more accurate. Always save: (1) anything the "
    "user had to clarify, correct or repeat, as 'when the user says X they mean Y'; (2) which app, list, device, "
    "script or file a vague phrase turned out to mean; (3) a quicker way to do something that took several "
    "steps or failed first; (4) preferences shown by how they reacted. Merge with what's already there, keep it "
    "under 40 short bullets, and remove anything outdated or contradicted. Skip things already in "
    "memory/permanent.md or memory/about-user.md, one-off details, passwords, and private content from "
    "screenshots. Only add what isn't already noted in your memory files; if nothing new was learned, change "
    "nothing. Never edit memory/permanent.md or memory/about-user.md. Reply with just 'updated' or 'no change'."
)
# Hey Claude was stopped (restart, sign-out, crash) before an earlier conversation's sessions could review it
CATCHUP_HEAD = (
    "[Automatic wrap-up, not from the user.] Hey Claude was restarted before it could review an earlier "
    "conversation, so here is its transcript instead:\n\n"
)
TYPED_NOTE = (
    "(Typed in the chat window, not spoken. Your reply is shown there as text and, by default, is not read "
    "aloud, so it can be longer than a spoken reply when that helps, and may use simple formatting: short "
    "paragraphs, '-' bullets, **bold**, `code`. If the user wants to HEAR the answer (say it, read it, speak "
    "it, out loud...), put [speak] anywhere in your reply and it is read aloud with your voice as well as "
    "shown; write it the way you'd say it. You can always do this: never say you can't speak or read "
    "something aloud. It is always meant for you: never reply [ignore].)\n"
)
# A typed prompt asking to hear the reply ("say it out loud", "read it to me", "speak it"): the reply is
# spoken too, even if Claude forgets the [speak] tag.
SPEAK_REQ_RE = re.compile(
    r"\b(?:out ?loud|aloud|speak (?:it|this|that|the (?:answer|reply)|your (?:answer|reply)|to me|up)|"
    r"(?:say|read) (?:it|this|that|the (?:answer|reply)|them)(?: (?:back|again|to me|for me))?(?=[\s,.!?]|$)|"
    r"read (?:it|this|that) (?:to|for) me|tell me verbally|use your voice|(?:with|in) your voice|"
    r"(?:voice|spoken) (?:reply|answer|response)|let me hear|i want to hear)\b", re.I)
SPEAK_TAG_RE = re.compile(r"[ \t]*\[speak\][ \t]*", re.I)
STOPPED = "\x00stopped"          # ClaudeSession.ask() result when the user pressed Stop

# ---- model routing (first match wins; instant local actions are tried before this)
OPUS_RE = re.compile(
    r"\b(research|compare|analy[sz]e|essay|draft|write (?:me|up|a|an)|explain why|"
    r"think (?:hard|carefully|it through)|figure out|investigate|debug|plan (?:out|my))\b", re.I)
SONNET_RE = re.compile(
    r"\b(tasks?|to-?dos?|tick ?tick|remind(?:er)?s?|calendar|schedule|events?|meetings?|"
    r"summari[sz]e|find|search|look up|organi[sz]e|set up|and then|after that|fix|why|how do|"
    r"configure|install|skills?|whenever i say|every time i say)\b", re.I)
# "that" only counts when it points at something ("add that to...", "close that"), not
# when it joins a sentence ("remember that the test word is...").
VISUAL_RE = re.compile(
    r"\b(this|these|those|here|screens?|page|window|tab|selected|highlighted)\b"
    r"|\bthat(?:\s*$|\s+(?:to|into|on|in|from|for|as|one|thing|email|page|tab|window|app)\b)", re.I)
MEMORY_RE = re.compile(r"\b(to (?:your|my) memory|remember (?:that|this)|forget (?:that|about|the|my))\b", re.I)

# In the open-mic window after a reply, these on their own mean "nothing more" (no command sent)
ACK_RE = re.compile(r"^(?:ok(?:ay)?|k|thanks?(?: you)?|thank you(?: so much)?|cool|nice|great|got it|alright|"
                    r"all right|perfect|awesome|sounds good|no|nope|never ?mind|that's all|that's it|"
                    r"good|wow|yeah|yep|mm-?hmm|uh-?huh|oh)(?:[ ,]+(?:thanks|thank you|claude))?[.!? ]*$", re.I)


# What Whisper typically "hears" in pure noise or silence
HALLUCINATION_RE = re.compile(
    r"^(?:thank you|thanks for watching|thank you for watching|thanks|you|bye|bye-bye|okay|ok|so|uh|um|"
    r"hmm|mm|oh|ah|the|yeah|music|applause|silence|\.+|-+|\[.*\]|\(.*\)|♪.*)(?:[.!, ]+(?:thank you|you|bye))*[.!?, ]*$",
    re.I)
# Short commands that look like instant actions skip the slower "was this meant for me?" check
LOCAL_HINT_RE = re.compile(
    r"^(?:please |can you |could you )?(?:open|close|launch|start|quit|exit|maximi[sz]e|minimi[sz]e|restore|"
    r"pause|play|resume|stop|next|skip|previous|volume|turn (?:the )?(?:volume|sound)|set (?:the )?volume|"
    r"mute|unmute|what'?s the (?:time|date)|what time)\b", re.I)


def voice_features(audio: np.ndarray):
    """(loudness in dB of the voiced parts, median pitch in Hz or None) — a cheap voice fingerprint."""
    n = len(audio) // FRAME * FRAME
    if n < FRAME * 6:
        return None
    frames = audio[:n].reshape(-1, FRAME)
    rms = np.sqrt((frames * frames).mean(axis=1)) + 1e-9
    voiced = frames[rms >= np.percentile(rms, 60)]
    if len(voiced) < 4:
        return None
    level = float(20 * np.log10(np.sqrt((voiced * voiced).mean()) + 1e-9))
    lo, hi = RATE // 400, RATE // 75                  # pitch search range 75-400 Hz
    pitches = []
    for f in voiced[:80]:
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[len(f) - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.3:
            pitches.append(RATE / lag)
    return level, (float(np.median(pitches)) if len(pitches) >= 3 else None)


def sounds_like_someone_else(ref, new) -> bool:
    """Roommates are usually farther from the mic (much quieter) and/or have a different pitch."""
    if not ref or not new:
        return False
    drop = ref[0] - new[0]                            # how much quieter than your "Hey Claude", in dB
    if drop >= 7.0:                                   # much quieter: farther from the mic
        return True
    if ref[1] and new[1]:
        ratio = new[1] / ref[1]
        if ratio < 0.78 or ratio > 1.28:              # clearly different voice pitch
            return True
        if drop >= 4.0 and (ratio < 0.87 or ratio > 1.15):   # somewhat quieter AND somewhat different
            return True
    return False


def is_echo(heard: str, spoken: str) -> bool:
    """True if what the mic heard is mostly the assistant's own last words (speaker echo)."""
    words = re.findall(r"[a-z0-9']+", heard.lower())
    if not words:
        return True
    said = set(re.findall(r"[a-z0-9']+", (spoken or "").lower()))
    return len(words) >= 2 and sum(w in said for w in words) / len(words) >= 0.8


NO_WINDOW = 0x08000000

# ---- keeping the voice smooth
# The scheduled task starts this process at BELOW-NORMAL priority (Task Scheduler's default, 7),
# so any busy app (Edge, Wallpaper Engine, the Claude app) used to take the CPU from the thread
# that feeds the speaker, and the voice broke in and out. Fix: run at normal priority, and give
# the playback thread the Windows audio scheduler (MMCSS "Pro Audio") plus a bigger buffer.
VOICE_BUFFER_S = 0.4           # output buffer: rides out short CPU stalls (stop() still cuts off instantly)
KOKORO_THREADS = 6             # same speed as all 16 cores, without pegging every core in bursts
_k32 = ctypes.WinDLL("kernel32")
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.GetCurrentThread.restype = ctypes.c_void_p
_k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]


def use_normal_priority():
    if not _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x20):     # NORMAL_PRIORITY_CLASS
        log("Couldn't raise process priority to normal.")


def boost_audio_thread():
    """Call from the thread that writes to the speaker: Windows then schedules it like audio software."""
    try:
        idx = ctypes.c_ulong(0)
        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        if not avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(idx)):
            _k32.SetThreadPriority(_k32.GetCurrentThread(), 15)          # THREAD_PRIORITY_TIME_CRITICAL
    except Exception:
        _k32.SetThreadPriority(_k32.GetCurrentThread(), 15)

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.info
for _noisy in ("faster_whisper", "httpx", "huggingface_hub"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
for _noisy in ("kokoro_onnx", "phonemizer"):                 # harmless "words count mismatch" chatter
    logging.getLogger(_noisy).setLevel(logging.ERROR)

user32, kernel32, gdi32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.gdi32
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2: crisp UI, true pixels
except Exception:
    pass


# ================================================================= Windows helpers

class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def plugged_in() -> bool:
    s = _PowerStatus()
    if kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.ACLineStatus == 1:
        return True
    return user32.GetSystemMetrics(80) > 1        # SM_CMONITORS > 1: docked to a monitor


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
user32.GetForegroundWindow.restype = wt.HWND
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(_MonitorInfo)]
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), _MONITORENUMPROC, wt.LPARAM]


def _monitor_info(hmon) -> _MonitorInfo:
    mi = _MonitorInfo(); mi.cbSize = ctypes.sizeof(_MonitorInfo)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi


def primary_work_area() -> wt.RECT:
    return _monitor_info(user32.MonitorFromPoint(wt.POINT(0, 0), 1)).rcWork   # MONITOR_DEFAULTTOPRIMARY


def all_monitors():
    """[(hmonitor, rect, is_primary)] for every connected screen."""
    found = []

    def cb(hmon, hdc, lprect, lparam):
        mi = _monitor_info(hmon)
        found.append((hmon, mi.rcMonitor, bool(mi.dwFlags & 1)))
        return True
    proc = _MONITORENUMPROC(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    return found


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def _last_input_tick() -> int:
    li = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    user32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime


def _cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def allow_foreground():
    """Let the next launched app come to the front (background processes normally can't).
    A tap of the unused F24 key makes us the 'last input' owner, then we pass that right on."""
    user32.keybd_event(0x87, 0, 0, 0); user32.keybd_event(0x87, 0, 2, 0)
    user32.AllowSetForegroundWindow(-1)


# ---- exact output volume through Core Audio (IAudioEndpointVolume), via raw COM: no extra packages

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    import uuid
    u = uuid.UUID(s)
    g = _GUID(u.fields[0], u.fields[1], u.fields[2])
    g.Data4[:] = list(u.bytes[8:])
    return g


_CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = _guid("5CDF2C82-841E-4546-9722-0CF74078229A")


def _com(obj, index, restype, *argtypes):
    """Method `index` of a COM object's vtable, callable as f(obj, ...)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _endpoint_volume():
    """IAudioEndpointVolume* of the default output device (caller must Release)."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)                     # fine if this thread already has COM
    enum, dev, vol = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDeviceEnumerator), None, 23,
                                ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enum))
    if hr != 0:
        raise OSError(f"CoCreateInstance failed 0x{hr & 0xFFFFFFFF:08X}")
    try:
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender=0, eConsole=0, &dev)
        hr = _com(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            enum, 0, 0, ctypes.byref(dev))
        # IMMDevice::Activate(IID_IAudioEndpointVolume, CLSCTX_ALL, NULL, &vol)
        _com(dev, 3, ctypes.HRESULT, ctypes.POINTER(_GUID), wt.DWORD, ctypes.c_void_p,
             ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(_IID_IAudioEndpointVolume), 23, None,
                                              ctypes.byref(vol))
        return vol
    finally:
        if dev:
            _com(dev, 2, ctypes.c_ulong)(dev)
        _com(enum, 2, ctypes.c_ulong)(enum)


def get_volume() -> int:
    vol = _endpoint_volume()
    try:
        level = ctypes.c_float()
        _com(vol, 9, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(vol, ctypes.byref(level))  # GetMasterVolumeLevelScalar
        return round(level.value * 100)
    finally:
        _com(vol, 2, ctypes.c_ulong)(vol)


def set_volume(percent: int):
    """Set the default output to exactly `percent` (0-100) in one step. Like moving the Windows
    slider, a level above 0 also unmutes."""
    vol = _endpoint_volume()
    try:
        _com(vol, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p)(vol, max(0, min(100, percent)) / 100.0, None)
        if percent > 0:
            _com(vol, 14, ctypes.HRESULT, wt.BOOL, ctypes.c_void_p)(vol, False, None)            # SetMute(FALSE)
    finally:
        _com(vol, 2, ctypes.c_ulong)(vol)


_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_NUM_WORDS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
                   "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100,
                   "max": 100, "maximum": 100, "full": 100})


def parse_percent(s: str):
    """'25', '25%', 'twenty five', 'seventy-five', 'a hundred' -> int, else None."""
    s = s.strip().lower().rstrip("%").replace(" percent", "").replace("-", " ").strip()
    if s.isdigit():
        return int(s)
    if s in _NUM_WORDS:
        return _NUM_WORDS[s]
    parts = s.split()
    if len(parts) == 2 and parts[0] in _NUM_WORDS and parts[1] in _NUM_WORDS and _NUM_WORDS[parts[0]] >= 20 \
            and _NUM_WORDS[parts[1]] < 10:
        return _NUM_WORDS[parts[0]] + _NUM_WORDS[parts[1]]
    return None


# ---- screen locked / display off: don't listen then

def is_locked_now() -> bool:
    """True while the lock screen (or any secure desktop) is showing."""
    user32.OpenInputDesktop.restype = wt.HANDLE
    h = user32.OpenInputDesktop(0, False, 0x0001)     # DESKTOP_READOBJECTS
    if not h:
        return True                                   # can't open the input desktop: it's the lock screen
    try:
        name = ctypes.create_unicode_buffer(256)
        user32.GetUserObjectInformationW(wt.HANDLE(h), 2, name, ctypes.sizeof(name), ctypes.byref(wt.DWORD()))
        return name.value.lower() != "default"
    finally:
        user32.CloseDesktop(wt.HANDLE(h))


class ScreenWatcher:
    """Hidden window that receives Windows' 'session locked/unlocked' and 'display on/off'
    events (event-driven, no polling). awake() = unlocked AND display on."""
    _GUID_CONSOLE_DISPLAY_STATE = _guid("6FE69556-704A-47A0-8F24-C28D936FDA47")

    def __init__(self):
        self.locked = is_locked_now()
        self.display_on = True
        threading.Thread(target=self._run, daemon=True).start()

    def awake(self) -> bool:
        return self.display_on and not self.locked

    def _run(self):
        try:
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                            ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                            ("lpszClassName", wt.LPCWSTR)]

            class POWERBROADCAST_SETTING(ctypes.Structure):
                _fields_ = [("PowerSetting", _GUID), ("DataLength", wt.DWORD), ("Data", wt.DWORD)]

            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

            def proc(hwnd, msg, wparam, lparam):
                if msg == 0x02B1:                                       # WM_WTSSESSION_CHANGE
                    if wparam in (7, 8):                                # SESSION_LOCK / SESSION_UNLOCK
                        self.locked = (wparam == 7)
                        log(f"Screen {'locked' if self.locked else 'unlocked'}.")
                elif msg == 0x0218 and wparam == 0x8013:                # WM_POWERBROADCAST / PBT_POWERSETTINGCHANGE
                    s = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                    on = s.Data != 0                                    # 0 = off, 1 = on, 2 = dimmed (still on)
                    if on != self.display_on:
                        self.display_on = on
                        log(f"Display turned {'on' if on else 'off'}.")
                    return 1
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._proc = WNDPROC(proc)                                  # keep a reference
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="HeyClaudeScreenWatcher")
            user32.RegisterClassW(ctypes.byref(wc))
            user32.CreateWindowExW.restype = wt.HWND
            hwnd = user32.CreateWindowExW(0, "HeyClaudeScreenWatcher", "Hey Claude watcher", 0,
                                          0, 0, 0, 0, None, None, hinst, None)
            user32.RegisterPowerSettingNotification.restype = wt.HANDLE
            user32.RegisterPowerSettingNotification(wt.HWND(hwnd), ctypes.byref(self._GUID_CONSOLE_DISPLAY_STATE), 0)
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(wt.HWND(hwnd), 0)   # this session only
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log(f"Screen watcher failed ({e!r}); falling back to lock polling only.")


def tap_key(vk: int, times: int = 1):
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0); user32.keybd_event(vk, 0, 2, 0)


# ---- screenshots of every monitor, written as PNG with no extra packages

class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


for _f, _res, _args in [
    (user32.GetDC, wt.HDC, [wt.HWND]), (user32.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
    (gdi32.CreateCompatibleDC, wt.HDC, [wt.HDC]),
    (gdi32.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
    (gdi32.BitBlt, wt.BOOL, [wt.HDC] + [ctypes.c_int] * 4 + [wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]),
    (gdi32.GetDIBits, ctypes.c_int, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                                     ctypes.POINTER(_BitmapInfoHeader), wt.UINT]),
    (gdi32.DeleteObject, wt.BOOL, [wt.HGDIOBJ]), (gdi32.DeleteDC, wt.BOOL, [wt.HDC]),
]:
    _f.restype, _f.argtypes = _res, _args


def _write_png(path: Path, rgb: np.ndarray):
    hh, ww = rgb.shape[:2]
    raw = np.concatenate([np.zeros((hh, 1), np.uint8), rgb.reshape(hh, -1)], axis=1).tobytes()

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ww, hh, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _grab_rect(r: wt.RECT) -> np.ndarray:
    """Raw pixels of one monitor (fast: tens of milliseconds)."""
    w, h = r.right - r.left, r.bottom - r.top
    sdc = user32.GetDC(None); mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h); old = gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, r.left, r.top, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
    bih = _BitmapInfoHeader(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.SelectObject(mdc, old); gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(None, sdc)
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)


def _encode_png(bgra: np.ndarray, path: Path, max_width: int = 1920):
    """Downscale + PNG-encode (the slow part, ~0.5 s for 4K) — done in the background."""
    h, w = bgra.shape[:2]
    rgb = bgra[:, :, 2::-1]
    f = math.ceil(w / max_width)
    if f > 1:                                         # average-pool so text stays legible
        hh, ww = h // f * f, w // f * f
        rgb = rgb[:hh, :ww].reshape(hh // f, f, ww // f, f, 3).mean(axis=(1, 3)).astype(np.uint8)
    _write_png(path, np.ascontiguousarray(rgb))


class ScreenGrab:
    """Screenshots of every monitor: pixels grabbed immediately (so the pill isn't in them),
    PNGs written on a background thread. get() waits for the files: [(path, description)]."""

    def __init__(self, fg=None):
        """fg: the window the user was using (when they typed in the chat, it isn't the foreground one)."""
        self.shots, self.error = [], None
        grabbed = []
        try:
            SCREEN_DIR.mkdir(parents=True, exist_ok=True)
            for old in SCREEN_DIR.glob("*.png"):
                old.unlink(missing_ok=True)
            active = user32.MonitorFromWindow(fg or user32.GetForegroundWindow(), 2)
            mons = sorted(all_monitors(), key=lambda m: (m[0] != active, not m[2]))
            for i, (hmon, r, primary) in enumerate(mons, 1):
                tags = [f"{r.right - r.left}x{r.bottom - r.top}"]
                if hmon == active:
                    tags.append("has the window the user was using")
                if primary:
                    tags.append("primary")
                grabbed.append((SCREEN_DIR / f"screen-{i}.png", ", ".join(tags), _grab_rect(r)))
        except Exception as e:
            log(f"Screenshot failed: {e!r}")
        self.thread = threading.Thread(target=self._encode, args=(grabbed,), daemon=True)
        self.thread.start()

    def _encode(self, grabbed):
        for path, desc, px in grabbed:
            try:
                _encode_png(px, path)
                self.shots.append((path, desc))
            except Exception as e:
                log(f"Screenshot encode failed: {e!r}")

    def get(self):
        self.thread.join(timeout=10)
        return list(self.shots)


def _capture_rect(r: wt.RECT, path: Path, max_width: int = 1920):
    w, h = r.right - r.left, r.bottom - r.top
    sdc = user32.GetDC(None); mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h); old = gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, r.left, r.top, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
    bih = _BitmapInfoHeader(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.SelectObject(mdc, old); gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(None, sdc)
    rgb = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, 2::-1]
    f = math.ceil(w / max_width)
    if f > 1:                                         # average-pool so text stays legible
        hh, ww = h // f * f, w // f * f
        rgb = rgb[:hh, :ww].reshape(hh // f, f, ww // f, f, 3).mean(axis=(1, 3)).astype(np.uint8)
    _write_png(path, np.ascontiguousarray(rgb))


def capture_all_screens():
    """Screenshot every monitor. Returns [(path, description)], active screen first."""
    try:
        SCREEN_DIR.mkdir(parents=True, exist_ok=True)
        for old in SCREEN_DIR.glob("*.png"):
            old.unlink(missing_ok=True)
        active = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)
        mons = sorted(all_monitors(), key=lambda m: (m[0] != active, not m[2]))
        shots = []
        for i, (hmon, r, primary) in enumerate(mons, 1):
            path = SCREEN_DIR / f"screen-{i}.png"
            _capture_rect(r, path)
            tags = [f"{r.right - r.left}x{r.bottom - r.top}"]
            if hmon == active:
                tags.append("has the window the user was using")
            if primary:
                tags.append("primary")
            shots.append((path, ", ".join(tags)))
        return shots
    except Exception as e:
        log(f"Screenshot failed: {e!r}")
        return []


# ================================================================= floating status pill

class Overlay:
    """A small rounded pill at the top of the PRIMARY screen. Never takes focus.
    Other threads call show()/hide()/hide_after(); only the Tk thread touches widgets."""
    BG, FG, SUB = "#18181b", "#fafafa", "#a1a1aa"
    COLORS = {"listen": "#5b8cff", "think": "#f5a524", "act": "#a78bfa", "done": "#34d399",
              "error": "#f87171", "ask": "#5b8cff"}
    PULSING = {"listen", "think", "act"}

    def __init__(self):
        self.root = tk.Tk(); self.root.withdraw()
        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True); w.attributes("-topmost", True); w.attributes("-alpha", 0.96)
        w.configure(bg=self.BG)
        # sizes are 10% larger than the first version (user request)
        fr = tk.Frame(w, bg=self.BG, padx=20, pady=13); fr.pack(fill="both")
        self.dot = tk.Canvas(fr, width=18, height=18, bg=self.BG, highlightthickness=0)
        self.dot.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 13), pady=(3, 0))
        self.dot_id = self.dot.create_oval(3, 3, 15, 15, fill=self.COLORS["listen"], outline="")
        self.title = tk.Label(fr, font=("Segoe UI Semibold", 12), fg=self.FG, bg=self.BG, anchor="w")
        self.title.grid(row=0, column=1, sticky="w")
        self.detail = tk.Label(fr, font=("Segoe UI", 11), fg=self.SUB, bg=self.BG, anchor="w",
                               justify="left", wraplength=600)
        self.detail.grid(row=1, column=1, sticky="w")
        fr.grid_columnconfigure(1, minsize=330)
        # ✓ / ✕ while listening: finish the command now, or cancel it (e.g. roommates start talking)
        self.btns = tk.Frame(fr, bg=self.BG)
        self.btns.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(14, 0))
        self.on_action = lambda action: None          # set by the listener
        for text, color, action in (("✓", "#34d399", "finish"), ("✕", "#f87171", "cancel")):
            b = tk.Label(self.btns, text=text, font=("Segoe UI Symbol", 13, "bold"), fg=color, bg="#27272a",
                         width=2, cursor="hand2", padx=4, pady=1)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Enter>", lambda e, b=b: b.config(bg="#3f3f46"))
            b.bind("<Leave>", lambda e, b=b: b.config(bg="#27272a"))
            b.bind("<Button-1>", lambda e, a=action: (self.on_action(a), "break")[1])
        self.btns.grid_remove()
        for widget in (w, fr, self.title, self.detail, self.dot):
            widget.bind("<Button-1>", lambda e: self._hide())
        w.geometry("+-10000+-10000"); w.update_idletasks(); w.update()
        self.hwnd = user32.GetParent(w.winfo_id())
        ex = user32.GetWindowLongW(self.hwnd, -20)
        user32.SetWindowLongW(self.hwnd, -20, ex | 0x08000000 | 0x80 | 0x8)   # NOACTIVATE|TOOLWINDOW|TOPMOST
        try:
            pref = ctypes.c_int(2)                                           # Windows 11 rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass
        user32.ShowWindow(self.hwnd, 0)
        self.q: "queue.Queue" = queue.Queue()
        self.state, self.visible, self.hide_at, self.phase = None, False, None, 0
        self.lingering, self.linger_start = False, 0.0
        self.esc_was_down = False
        self._activity_baseline()
        self.root.after(40, self._tick)

    # The chat HUD's extra API (hud.py), as no-ops: this pill is the fallback when chat_hud is off.
    prev_fg, mic_level, chat_open, visible = None, 0.0, False, False

    def chat_add(self, msg): pass
    def chat_reset(self, manual=False): pass
    def speaking(self, on): pass
    def mic_state(self, reason): pass
    def toast(self, text, seconds=2.4): pass

    def capture_hidden(self):
        return contextlib.nullcontext()

    # thread-safe API
    def show(self, state, title, detail="", hide_after=None, kind=None):
        self.q.put(("show", state, title, detail, hide_after))

    def hide(self):
        self.q.put(("hide",))

    def hide_after(self, seconds):
        self.q.put(("hide_after", seconds))

    def linger(self, seconds):
        """After a reply: stay until `seconds` pass (None = no time limit) or the user moves on
        (clicks anywhere, types, or switches to another window)."""
        self.q.put(("linger", seconds))

    # user-activity detection for linger
    def _activity_baseline(self):
        self.base_fg = user32.GetForegroundWindow()
        self.base_input = _last_input_tick()
        self.base_cursor = _cursor_pos()

    def _user_moved_on(self) -> bool:
        if (user32.GetAsyncKeyState(0x01) | user32.GetAsyncKeyState(0x02)) & 0x8000:
            return True                                              # mouse click anywhere
        if user32.GetForegroundWindow() != self.base_fg:
            return True                                              # switched windows
        cur, tick = _cursor_pos(), _last_input_tick()
        if cur != self.base_cursor:                                  # mouse moved: not "moving on" by itself
            self.base_cursor, self.base_input = cur, tick
            return False
        return tick != self.base_input                               # input without mouse movement = typing

    def quit(self):
        self.q.put(("quit",))

    # Tk thread
    def _tick(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "show":
                    self._show(*msg[1:])
                elif msg[0] == "hide":
                    self._hide()
                elif msg[0] == "hide_after":
                    if self.visible:
                        self.hide_at = time.time() + msg[1]
                elif msg[0] == "linger":
                    if self.visible:
                        self.hide_at = time.time() + msg[1] if msg[1] else None
                        self.lingering, self.linger_start = True, time.time()
                        self._activity_baseline()
                elif msg[0] == "quit":
                    self.root.quit(); return
        except queue.Empty:
            pass
        if self.visible and self.hide_at and time.time() >= self.hide_at:
            self._hide()
        if self.visible and self.lingering and time.time() - self.linger_start > 0.8 and self._user_moved_on():
            self._hide()
        # Esc = ✕ while listening (the pill never has focus, so watch the key directly)
        esc_down = bool(user32.GetAsyncKeyState(0x1B) & 0x8000)
        if esc_down and not self.esc_was_down and self.visible and self.state == "listen":
            self.on_action("cancel")
        self.esc_was_down = esc_down
        if self.visible and self.state in self.PULSING:            # breathing dot
            self.phase += 1
            r = 4.4 + 1.8 * math.sin(self.phase / 3.0)
            self.dot.coords(self.dot_id, 9 - r, 9 - r, 9 + r, 9 + r)
        self.root.after(40, self._tick)

    def _show(self, state, title, detail, hide_after):
        self.state, self.lingering = state, False
        self.dot.itemconfig(self.dot_id, fill=self.COLORS.get(state, self.SUB))
        self.dot.coords(self.dot_id, 3, 3, 15, 15)
        self.title.config(text=title)
        detail = (detail or "").strip()
        self.detail.config(text=detail)               # never shortened: it must match what's spoken
        self.detail.grid() if detail else self.detail.grid_remove()
        self.btns.grid() if state == "listen" else self.btns.grid_remove()
        self.win.update_idletasks()
        work = primary_work_area()
        ww = self.win.winfo_reqwidth()
        x = work.left + (work.right - work.left - ww) // 2
        self.win.geometry(f"+{x}+{work.top + 20}")
        self.win.update_idletasks()
        user32.ShowWindow(self.hwnd, 4)                              # SW_SHOWNOACTIVATE
        user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # stay topmost, no move/size/activate
        self.visible = True
        self.hide_at = time.time() + hide_after if hide_after else None

    def _hide(self):
        user32.ShowWindow(self.hwnd, 0)
        self.visible, self.hide_at, self.state, self.lingering = False, None, None, False


# ================================================================= voice

class Voice:
    def __init__(self):
        self.kokoro = None
        try:
            from kokoro_onnx import Kokoro
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.intra_op_num_threads, opts.inter_op_num_threads = KOKORO_THREADS, 1
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")   # don't busy-wait cores
            sess = rt.InferenceSession(str(VOICE_DIR / "kokoro-v1.0.int8.onnx"), opts,
                                       providers=["CPUExecutionProvider"])
            self.kokoro = Kokoro.from_session(sess, str(VOICE_DIR / "voices-v1.0.bin"))
            self._make("Ready.")                                     # warm-up: first call is slow
        except Exception as e:
            log(f"Kokoro unavailable, using Windows voice: {e!r}")

    def _make(self, text):
        voice = settings()["voice"]
        try:
            if voice not in self.kokoro.get_voices():
                voice = VOICE
        except Exception:
            pass
        lang = "en-gb" if voice.startswith("b") else "en-us"
        return self.kokoro.create(text, voice=voice, speed=settings()["voice_speed"], lang=lang)

    # Speech runs on its own thread so the listener can keep hearing "Hey Claude" while it talks.
    # The ONLY thing that stops it early is stop(), which the listener calls on "Hey Claude".
    _thread = None
    _stop = None

    def is_speaking(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- keeping a Bluetooth speaker awake
    # A Bluetooth speaker that has been quiet drops the first ~0.5-1 s of sound while it wakes up,
    # which cut off the first words of replies. So from "Hey Claude" until the reply plays, an
    # output stream stays open playing silence; the reply is then written into that same stream.
    _aw_lock = threading.Lock()
    _aw_state = "off"              # off | on (silence thread owns the stream)
    _aw_until = 0.0
    _aw_want = False
    _aw_given = None
    _aw_evt = None

    _ph = 0.0

    def _pad(self, seconds=None, sr=KOKORO_RATE, n=None):
        """'Silence' that keeps a Bluetooth speaker un-muted: an inaudible, phase-continuous 20 Hz tone."""
        n = int(seconds * sr) if n is None else n
        t = self._ph + 2 * np.pi * KEEPALIVE_HZ / sr * np.arange(n)
        self._ph = float((self._ph + 2 * np.pi * KEEPALIVE_HZ / sr * n) % (2 * np.pi))
        return (KEEPALIVE_LEVEL * np.sin(t)).astype(np.float32).reshape(-1, 1)

    def warm(self, seconds: float):
        """Wake the speaker now and keep it awake for up to `seconds` (or until a reply plays)."""
        if not self.kokoro:
            return
        with self._aw_lock:
            self._aw_until = max(self._aw_until, time.time() + seconds)
            if self._aw_state == "on":
                return
            self._aw_state = "on"
        threading.Thread(target=self._awake_loop, args=(None,), daemon=True).start()

    def cool(self):
        """Nothing will be spoken after all: let the speaker sleep again in a couple of seconds."""
        with self._aw_lock:
            self._aw_until = min(self._aw_until, time.time() + 2.0)

    def _keep_awake(self, out, seconds):
        with self._aw_lock:
            self._aw_until = time.time() + seconds       # the reply is done: only a short while more
            if self._aw_state == "on":                    # (can't happen: we owned the stream)
                out.close(); return
            self._aw_state = "on"
        threading.Thread(target=self._awake_loop, args=(out,), daemon=True).start()

    def _awake_loop(self, out):
        try:
            if out is None:
                out = sd.OutputStream(samplerate=KOKORO_RATE, channels=1, dtype="float32", latency=VOICE_BUFFER_S)
                out.start()
            boost_audio_thread()
            while True:
                with self._aw_lock:
                    if self._aw_want:                     # a reply is ready: hand the live stream over
                        self._aw_given, self._aw_want, self._aw_state = out, False, "off"
                        self._aw_evt.set()
                        return
                    if time.time() > self._aw_until:
                        self._aw_state = "off"
                        break
                out.write(self._pad(0.05))
        except Exception as e:
            log(f"Keeping the speaker awake failed: {e!r}")
            with self._aw_lock:
                self._aw_state = "off"
                if self._aw_want:
                    self._aw_want = False; self._aw_given = None; self._aw_evt.set()
        try:
            if out is not None:
                out.close()
        except Exception:
            pass

    def _take_stream(self, sr):
        """-> (stream, was_awake). Reuse the awake stream if there is one, else open a new one."""
        evt = None
        with self._aw_lock:
            if self._aw_state == "on" and sr == KOKORO_RATE:
                evt = self._aw_evt = threading.Event()
                self._aw_want, self._aw_given = True, None
        if evt is not None and evt.wait(timeout=1.5) and self._aw_given is not None:
            return self._aw_given, True
        out = sd.OutputStream(samplerate=sr, channels=1, dtype="float32", latency=VOICE_BUFFER_S)
        out.start()
        return out, False

    def stop(self):
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def speak(self, text: str):
        """Start speaking in the background; returns immediately."""
        self.stop()
        text = (text or "").strip()
        if not text:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(text, self._stop), daemon=True)
        self._thread.start()

    def say(self, text: str):
        """Speak and wait until it has finished (used by --say)."""
        self.speak(text)
        if self._thread:
            self._thread.join()

    def _run(self, text, stop):
        if self.kokoro:
            try:
                self._run_kokoro(text, stop)
                return
            except Exception as e:
                log(f"Kokoro failed ({e!r}); falling back to Windows voice.")
        if stop.is_set():
            return
        env = dict(os.environ, HC_SAY=text[:4000])
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($env:HC_SAY)"],
                       env=env, creationflags=NO_WINDOW, timeout=300)

    def _run_kokoro(self, text, stop):
        # Sentence by sentence: the first one starts playing while the rest are still being made,
        # and there's no length limit.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?;:])\s+", text) if s.strip()]
        chunks: "queue.Queue" = queue.Queue(maxsize=3)

        def produce():
            try:
                for s in sentences:
                    if stop.is_set():
                        break
                    audio, sr = self._make(s)
                    chunks.put((audio.astype(np.float32), sr))
            except Exception as e:
                log(f"Kokoro error on a sentence: {e!r}")
            finally:
                chunks.put(None)
        threading.Thread(target=produce, daemon=True).start()

        first = chunks.get()
        if first is None:
            return
        boost_audio_thread()
        sr = first[1]
        block = int(sr * 0.1)
        under = waited = 0
        out, was_awake = self._take_stream(sr)
        try:
            # Speaker already awake (kept fed since "Hey Claude"): a tiny lead is enough.
            # Cold Bluetooth speaker: it drops roughly the first half second while it wakes up.
            # All padding is the inaudible keep-alive tone (see KEEPALIVE_HZ), never digital zeros.
            lead = VOICE_LEAD_S if was_awake else VOICE_COLD_LEAD_S
            under += bool(out.write(self._pad(lead, sr)))
            item = first
            while item is not None and not stop.is_set():
                audio = item[0]
                for i in range(0, len(audio), block):
                    if stop.is_set():
                        break
                    blk = audio[i:i + block].reshape(-1, 1)
                    under += bool(out.write(blk + self._pad(sr=sr, n=len(blk))))   # tone under pauses too
                if stop.is_set():
                    break
                under += bool(out.write(self._pad(0.08, sr)))                     # small gap between sentences
                while True:                    # next sentence not made yet: keep the speaker fed with
                    try:                       # silence instead of letting it run dry (that glitches)
                        item = chunks.get(timeout=0.04)
                        break
                    except queue.Empty:
                        if stop.is_set():
                            item = None
                            break
                        under += bool(out.write(self._pad(0.05, sr))); waited += 1
            if not stop.is_set():
                out.write(self._pad(VOICE_TAIL_S, sr))                            # Bluetooth lag: don't clip the end
                self._keep_awake(out, VOICE_STAY_AWAKE_S)   # a quick follow-up reply starts clean too
                out = None
            else:
                out.abort()                                  # "Hey Claude" interrupted: silence now
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
        if under or waited > 5:
            log(f"Voice playback: {under} buffer underrun(s), {waited * 0.05:.2f}s waiting for speech to be made.")


def chime():
    import winsound
    winsound.MessageBeep(winsound.MB_ICONASTERISK)


# ================================================================= Claude Code sessions

class ClaudeSession:
    """One long-running `claude -p` process per model, fed commands over stdin
    (stream-json), so there's no startup delay per command. It remembers its own
    conversation; `seen`/`fresh` track which shared-history entries it already knows."""

    def __init__(self, model: str):
        self.model, self.p, self.lines, self.turns = model, None, None, 0
        self.seen, self.fresh = 0, True
        self.effort = settings()["effort"][model]

    def args(self):
        a = [str(CLAUDE), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--model", self.model, "--effort", self.effort,
             "--permission-mode", "auto",            # safety classifier approves/blocks each action
             "--permission-prompts", "none",         # nobody to ask: anything that would prompt is denied
             "--no-session-persistence",
             "--tools", TOOLS]
        a += ["--settings", APPROVAL_HOOK_SETTINGS]   # pressing Send needs a fresh spoken "yes" (all models)
        if self.model == "haiku":
            a += ["--strict-mcp-config"]             # simple commands: no connectors, faster
        else:
            a += ["--disallowedTools", *BLOCKED_CONNECTORS]
        return a

    def start(self):
        self.stop()
        self.effort = settings()["effort"][self.model]
        self.p = subprocess.Popen(self.args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=WORKSPACE, text=True, encoding="utf-8",
                                  errors="replace", bufsize=1, creationflags=NO_WINDOW)
        self.lines, self.turns, self.seen, self.fresh = queue.Queue(), 0, 0, True
        threading.Thread(target=self._read, args=(self.p, self.lines), daemon=True).start()
        log(f"Claude session started ({self.model}).")
        return self

    @staticmethod
    def _read(p, q):
        for line in p.stdout:
            q.put(line)
        q.put(None)

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if self.alive():
            try:
                self.p.kill()
            except Exception:
                pass
        self.p = None

    def ask(self, text: str, progress=lambda d: None, stop=None) -> str:
        """stop: a threading.Event; when set (Stop in the chat), the request is abandoned and this
        session restarts fresh (it then gets the conversation so far as context). Returns STOPPED."""
        if not self.alive():
            self.start()
        self.turns += 1
        try:
            self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.p.stdin.flush()
        except OSError:
            self.start(); return self.ask(text, progress, stop)
        deadline = time.time() + CLAUDE_TIMEOUT_S
        while True:
            if stop is not None and stop.is_set():
                log("  stopped by the user"); self.start()
                return STOPPED
            try:
                line = self.lines.get(timeout=max(0.05, min(0.25, deadline - time.time())))
            except queue.Empty:
                if time.time() < deadline:
                    continue
                log("  timed out"); self.start()
                return "Sorry, that took too long."
            if line is None:
                log("  Claude session ended unexpectedly"); self.p = None
                return "Sorry, something went wrong."
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        progress(describe_tool(b))
            elif ev.get("type") == "result":
                reply = (ev.get("result") or "").strip()
                if ev.get("is_error"):
                    log(f"  error: {reply}")
                    if "log in" in reply.lower() or "/login" in reply.lower():
                        return "Claude Code isn't signed in."
                    return "Sorry, that didn't work."
                return reply


def parse_reply(reply: str):
    """Claude's reply may contain display markers (see workspace CLAUDE.md):
         [keep]          keep the pill up until the user moves on (worth reading)
         [screen] ...    extra detail, appended (still shown AND spoken; implies [keep])
         [silent]        a simple, self-evident action: don't speak, just flash "Done" on the pill
       -> (spoken text, pill text, keep, silent)"""
    reply = SPEAK_TAG_RE.sub(" ", reply)              # [speak] only matters for typed prompts (see execute)
    silent = bool(re.search(r"\[silent\]", reply, re.I))
    reply = re.sub(r"\s*\[silent\]\s*", " ", reply, flags=re.I).strip()
    keep = bool(re.search(r"\[keep\]", reply, re.I))
    shown = None
    m = re.search(r"\[screen\]", reply, re.I)
    if m:
        shown = re.sub(r"\s*\[keep\]\s*", " ", reply[m.end():], flags=re.I).strip()
        reply = reply[:m.start()]
    else:
        k = re.search(r"\[keep\]", reply, re.I)       # "Spoken part. [keep] 12 × 12 + 1 = 145": treat the
        if k and reply[:k.start()].strip() and reply[k.end():].strip():   # trailing part as pill-only
            shown = reply[k.end():].strip()
            reply = reply[:k.start()]
    text = re.sub(r"\s*\[keep\]\s*", " ", reply, flags=re.I).strip()
    demark = lambda s: re.sub(r"(?m)^\s*(?:[-*•]|#+)\s+", "", re.sub(r"\*\*|__|`", "", s)).strip()
    extra = demark(shown) if shown else ""
    keep = keep or bool(extra)
    # The pill and the voice always say exactly the same thing: any extra [screen] text is
    # shown AND read, right after the main text.
    pill = "\n".join(p for p in (demark(text), extra) if p)
    spoken = re.sub(r"\s*\n\s*", " ", pill).strip()
    if silent and (keep or "?" in spoken):            # something to read or answer is never silent
        silent = False
    return spoken, pill, keep, silent


def chat_text(reply: str) -> str:
    """The reply as shown in the chat window: display markers removed, formatting kept."""
    r = re.sub(r"[ \t]*\[(?:silent|keep|ignore|speak)\][ \t]*", " ", reply or "", flags=re.I)
    r = re.sub(r"[ \t]*\[screen\][ \t]*", "\n\n", r, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", r).strip()


TRIAGE_DIR = HERE / "triage"
TRIAGE_PROMPT = (
    "You are a filter for a voice assistant called Hey Claude on a laptop in a noisy college dorm room. "
    "You get speech-to-text captured right after its wake word (or right after it finished replying). Decide if "
    "the text is a request, question, or answer DIRECTED AT THE ASSISTANT by its user, or if it is gibberish, "
    "a transcription of noise, a fragment with no request in it, or people talking to each other (roommates' "
    "conversation, a phone call, a video playing). Real commands are often short and imperfectly transcribed "
    "('open spot if I', 'whats my batery', 'add that to my tasks'), and follow-ups can depend on what the "
    "assistant just said. Answer with exactly one word: DIRECTED or IGNORE."
)


class TriageSession(ClaudeSession):
    """A tiny, separate, always-ready Haiku session with no tools and a one-line job:
    'was this speech meant for the assistant?' (~1 s). Kept apart so it never mixes into real work."""

    def __init__(self):
        super().__init__("haiku")

    def args(self):
        return [str(CLAUDE), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", "haiku", "--effort", "low", "--tools", "", "--strict-mcp-config",
                "--no-session-persistence", "--permission-mode", "auto", "--permission-prompts", "none",
                "--system-prompt", TRIAGE_PROMPT]

    def start(self):
        TRIAGE_DIR.mkdir(exist_ok=True)
        self.stop()
        self.effort = "low"
        self.p = subprocess.Popen(self.args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=TRIAGE_DIR, text=True, encoding="utf-8",
                                  errors="replace", bufsize=1, creationflags=NO_WINDOW)
        self.lines, self.turns, self.seen, self.fresh = queue.Queue(), 0, 0, True
        threading.Thread(target=self._read, args=(self.p, self.lines), daemon=True).start()
        return self

    def directed(self, text: str, assistant_said: str = "") -> bool:
        """True = treat as a real request. If the check itself fails, err on the side of answering."""
        if self.turns >= 30:
            self.start()
        msg = f"Text heard: «{text}»"
        if assistant_said:
            msg += f"\nThe assistant had just said: «{assistant_said}»"
        t = time.time()
        try:
            reply = self.ask(msg)
        except Exception as e:
            log(f"Triage failed ({e!r}); treating as a real request.")
            return True
        verdict = "IGNORE" not in reply.upper()
        log(f"Triage ({time.time() - t:.1f}s): {'DIRECTED' if verdict else 'IGNORE'} for {text[:80]!r}")
        return verdict


def describe_tool(b) -> str:
    name, inp = b.get("name", ""), b.get("input", {}) or {}
    if name in ("PowerShell", "Bash"):
        return inp.get("description") or (inp.get("command") or "")[:90]
    if name == "Read":
        p = str(inp.get("file_path", ""))
        if "screen" in p.lower():
            return "Looking at your screen"
        return "Checking its memory" if "memory" in p.lower() else f"Reading {Path(p).name}"
    if name in ("Edit", "Write"):
        p = str(inp.get("file_path", ""))
        return "Updating its memory" if "memory" in p.lower() else f"Editing {Path(p).name}"
    if name.startswith("mcp__claude_ai_"):
        server, _, tool = name[len("mcp__claude_ai_"):].partition("__")
        return f"{server.replace('_', ' ')}: {tool.replace('_', ' ')}"
    return name


CONNECTOR_RE = re.compile(
    r"\b(tasks?|to-?dos?|tick ?tick|remind(?:er)?s?|calendar|schedule|events?|meetings?|homework|assignments?|"
    r"due|quiz(?:zes)?|exams?|tests?|essays?|readings?|syllabus|mark .{1,60}? (?:as )?(?:done|complete|completed|finished)|"
    r"e-?mails?|inbox|gmail|mail|repl(?:y|ies)|forward|send|unread)\b",
    re.I)
CANT_RE = re.compile(r"\b(?:can't|cannot|can not|unable to|don't have|do not have|no)\b[^.]{0,40}?"
                     r"\b(?:reach|access|connect|connector|tools?|permission)\b", re.I)
OVERRIDE_RE = re.compile(r"\b(?:use|using|with|ask|have|via)\s+(?:claude\s+)?(opus|sonnet|haiku)\b", re.I)


def route(command: str):
    """-> (model, needs_screen). Tiers map to models via settings.json; 'use Opus ...' picks directly."""
    visual = bool(VISUAL_RE.search(command))
    words = len(command.split())
    m = OVERRIDE_RE.search(command)
    if m:                                             # "use Opus 5.5 to answer this ..."
        model = m.group(1).lower()
    elif MEMORY_RE.search(command):                   # "add X to your memory", "forget X"
        model = settings()["models"]["normal" if visual else "simple"]
    elif OPUS_RE.search(command) or words > 30:
        model = settings()["models"]["complex"]
    elif visual or SONNET_RE.search(command) or CONNECTOR_RE.search(command) or words > 14:
        model = settings()["models"]["normal"]
    else:
        model, visual = settings()["models"]["simple"], False
    if model == "haiku" and CONNECTOR_RE.search(command):
        model = "sonnet"                              # the Haiku session has no TickTick/Calendar connectors
    return model, visual


# ================================================================= instant local actions

APP_ALIASES = {  # spoken name -> process name (for closing)
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
    "spotify": "Spotify", "discord": "Discord", "word": "WINWORD", "excel": "EXCEL",
    "powerpoint": "POWERPNT", "notepad": "Notepad", "calculator": "CalculatorApp", "teams": "ms-teams",
    "outlook": "olk", "steam": "steam", "zoom": "Zoom", "obsidian": "Obsidian", "tick tick": "TickTick",
    "ticktick": "TickTick", "settings": "SystemSettings",
    "file explorer": None, "explorer": None,      # never close Explorer by voice
}
PRONOUNS = {"it", "that", "this", "that app", "this app", "that one", "this one", "them"}


class LocalActions:
    def __init__(self):
        self.apps = {}                                # Start menu name (lower) -> (name, AppID)
        self.last_opened = None                       # (display name, time) for "close it"
        threading.Thread(target=self.load_apps, daemon=True).start()

    def load_apps(self):
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=60, creationflags=NO_WINDOW).stdout
            self.apps = {a["Name"].lower(): (a["Name"], a["AppID"]) for a in json.loads(out) if a.get("Name")}
            log(f"Loaded {len(self.apps)} Start menu apps.")
        except Exception as e:
            log(f"Couldn't load Start menu apps: {e!r}")

    def find_app(self, spoken: str):
        s = spoken.lower().strip()
        if not self.apps or not s:
            return None
        squash = lambda x: re.sub(r"[^a-z0-9]", "", x)
        if s in self.apps:
            return self.apps[s]
        by_squash = {squash(n): n for n in self.apps}            # "tick tick" -> "TickTick"
        if squash(s) in by_squash:
            return self.apps[by_squash[squash(s)]]
        starts = sorted((n for n in self.apps if n.startswith(s)), key=len)
        if starts:
            return self.apps[starts[0]]
        words = sorted((n for n in self.apps if re.search(rf"\b{re.escape(s)}\b", n)), key=len)
        if words:                                                  # "chrome" -> "Google Chrome"
            return self.apps[words[0]]
        close = difflib.get_close_matches(s, list(self.apps), n=1, cutoff=0.8)
        return self.apps[close[0]] if close else None

    @staticmethod
    def _alias_for(name: str):
        n = name.lower()
        if n in APP_ALIASES:
            return n
        for key in APP_ALIASES:                       # "google chrome" -> "chrome", "microsoft edge" -> "edge"
            if re.search(rf"\b{re.escape(key)}\b", n):
                return key
        return None

    def try_handle(self, command: str):
        """Return a reply if handled instantly here, else None (send to Claude)."""
        c = re.sub(r"[.!?,]+$", "", command.lower().strip())
        c = re.sub(r"^(please |can you |could you )", "", c)

        if re.fullmatch(r"(what'?s the time|what time is it( now)?|what is the time|time)", c):
            return time.strftime("It's %I:%M %p.").replace(" 0", " ")
        if re.fullmatch(r"(what'?s|what is) (the date|today'?s date|today)|what day is (it|today)( today)?", c):
            return time.strftime("It's %A, %B %d.").replace(" 0", " ")
        # Simple, self-evident actions reply "[silent] ...": no voice, just a brief "Done" on the pill.
        m = re.fullmatch(r"(?:(?:set|turn|put|change|make|bring)\s+(?:the\s+)?(?:volume|sound|audio)\s+"
                         r"(?:up\s+|down\s+)?(?:to\s+|at\s+)?|(?:volume|sound)\s+(?:to\s+|at\s+)?)"
                         r"([a-z0-9 \-]+?)\s*(?:%|percent)?", c)
        if m and parse_percent(m.group(1)) is not None:   # "set volume to 25", "volume 40 percent"
            level = max(0, min(100, parse_percent(m.group(1))))
            set_volume(level)                          # exact level, one step, no model call
            return f"[silent] Volume {level}%."
        m = re.fullmatch(r"(?:turn (?:the )?(?:volume|it|sound|music) (up|down)|volume (up|down)|turn (up|down) the (?:volume|sound|music))( a lot| a little)?", c)
        if m:
            up = "up" in (m.group(1), m.group(2), m.group(3))
            n = 10 if m.group(4) == " a lot" else 2 if m.group(4) == " a little" else 5
            tap_key(0xAF if up else 0xAE, n)
            return "[silent] Volume up." if up else "[silent] Volume down."
        if re.fullmatch(r"(pause|play|resume|stop)( the)?( music| song| spotify)?|play music", c):
            tap_key(0xB3); return "[silent] Play / pause."
        if re.fullmatch(r"(next|skip)( song| track| this song)?|skip( this)?", c):
            tap_key(0xB0); return "[silent] Next track."
        if re.fullmatch(r"(previous|last|go back)( song| track)?", c):
            tap_key(0xB1); tap_key(0xB1); return "[silent] Previous track."
        m = re.fullmatch(r"(maximi[sz]e|minimi[sz]e|restore|un-?maximi[sz]e|make (?:this|it) (?:full ?screen|bigger|smaller))"
                         r"(?: (?:this|it|that|the|my|current))?(?: (?:window|app|one))?", c)
        if m:                                         # acts on the window you're in (the pill never takes focus)
            word = m.group(1)
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            if word.startswith("maxim") or "full" in word or "bigger" in word:
                user32.ShowWindow(hwnd, 3); return "[silent] Maximized."
            if word.startswith("minim") or "smaller" in word:
                user32.ShowWindow(hwnd, 6); return "[silent] Minimized."
            user32.ShowWindow(hwnd, 9); return "[silent] Restored."

        m = re.fullmatch(r"(?:open|launch|start|run) (?:up )?(?:the )?(.+?)(?: app)?", c)
        if m and not VISUAL_RE.search(m.group(1)) and m.group(1) not in PRONOUNS:
            app = self.find_app(m.group(1))
            if app:
                allow_foreground()
                os.startfile(f"shell:AppsFolder\\{app[1]}")
                self.last_opened = (app[0], time.time())
                return f"[silent] Opening {app[0]}."
            return None
        m = re.fullmatch(r"(?:close|quit|exit|kill) (?:the )?(.+?)(?: app)?", c)
        if m:
            name = m.group(1)
            if name in PRONOUNS:                      # "close it" -> the app opened most recently
                if not self.last_opened or time.time() - self.last_opened[1] > CONTEXT_WINDOW_S:
                    return None                       # let Claude work it out from the conversation
                name = self.last_opened[0]
            key = self._alias_for(name)
            if key is None or APP_ALIASES[key] is None:
                return None
            r = subprocess.run(["taskkill", "/IM", f"{APP_ALIASES[key]}.exe"], capture_output=True,
                               creationflags=NO_WINDOW)
            label = name if name[:1].isupper() else name.title()
            return f"[silent] Closed {label}." if r.returncode == 0 else f"{label} isn't open."
        return None


# ================================================================= listener

class Listener:
    def __init__(self, model, voice: Voice, overlay: Overlay, fast_model=None, chatlog=None):
        from chatlog import ChatLog
        from skills import SkillBook
        self.model, self.voice, self.ui = model, voice, overlay
        self.fast_model = fast_model or model         # spots the wake word; `model` transcribes commands
        self.local = LocalActions()
        self.skills = SkillBook(SKILLS_DIR, SKILLS_VERIFIED, HERE / ".venv" / "Scripts" / "python.exe", log)
        self.skills.load()
        self.sessions = {m: ClaudeSession(m) for m in ("haiku", "sonnet", "opus")}
        for m in ("haiku", "sonnet"):                 # pre-start (no cost until used); opus starts on demand
            self.sessions[m].start()
        # The conversation (until 15 quiet minutes): saved in workspace/conversations/, and picked up
        # again after a restart, so the context survives it.
        self.chatlog = chatlog or ChatLog(CONV_DIR, CONTEXT_WINDOW_S, log)
        self.history: list = self.history_from_log()  # t, handler, command, reply, source
        self.last_activity = self.history[-1]["t"] if self.history else 0.0
        self.stop_event = threading.Event()           # Stop pressed in the chat
        self.chat_question = None                      # (model, question, time): a typed reply answers it
        self.manual_listen = False                     # listening because of F20 / the mic button
        self.wake_on, self.demand_until, self.mic_reason = True, 0.0, "starting"
        self.reflect_lock = threading.Lock()
        self.q: "queue.Queue[np.ndarray]" = queue.Queue()
        self.stream, self.last_audio = None, 0.0
        self.followup_until, self.pending_shots = 0.0, []
        self.answer_until, self.answer_model = 0.0, None
        self.pending, self.pending_deadline = None, 0.0  # a command still being collected (you may keep talking)
        self.utter_start = 0.0                           # when the current stretch of speech began
        self.speaking, self.spoken_reply, self.after_speech = False, "", None
        self.barge_ring = collections.deque(maxlen=int(BARGE_WINDOW_S / FRAME_S))
        self.barge_next = 0.0
        self.settings_check = 0.0
        self.early_wake, self.early_i = None, 0          # "Hey Claude" spotted before you finished talking
        self.open_until, self.open_shown, self.open_keep = 0.0, "", False   # open mic after every spoken reply
        self.ctrl: "queue.Queue[str]" = queue.Queue()  # ✓ / ✕ clicks from the pill
        self.last_voice = None                         # loudness/pitch of the latest stretch of speech
        self.last_asr = {"logprob": 0.0, "nospeech": 0.0, "comp": 0.0}
        self.wake_at, self.wake_judged = 0.0, False    # false-wake check ~4 s after "Hey Claude"
        self.triage = TriageSession().start()          # "was that meant for me?" (no tools, ~1 s)
        try:
            self.ui.on_action = self._ui_action
        except Exception:
            pass
        self.reset_vad()
        threading.Thread(target=self._catch_up_reflections, daemon=True).start()

    # ---- the chat window / pill / F20 (actions arrive from the Tk thread)
    def _ui_action(self, action):
        if action == "stop":                           # must work while a request is running
            self.stop_event.set()
            self.ctrl.put("hush")
            return
        self.ctrl.put(action)

    def handle_ui_actions(self):
        while True:
            try:
                action = self.ctrl.get_nowait()
            except queue.Empty:
                return
            kind = action[0] if isinstance(action, tuple) else action
            try:
                if kind == "send":
                    self.run_typed(action[1])
                elif kind == "new":
                    self.new_conversation()
                elif kind == "hush":
                    self.hush()
                elif kind == "mic":
                    self.listen_now()
                elif self.speaking:
                    pass                               # ✓ / ✕ only act while listening
                elif kind == "finish":
                    self.finish_now()
                elif kind == "cancel":
                    self.cancel_listening()
            except Exception as e:
                log(f"UI action {kind!r} failed: {e!r}")
                self.ui.show("error", "Something went wrong", str(e)[:120], hide_after=6)

    def grab(self, fg=None):
        """Screenshots of every monitor, without the HUD in them."""
        with self.ui.capture_hidden():
            return ScreenGrab(fg=fg)

    def run_typed(self, text):
        text = (text or "").strip()
        if not text:
            return
        if self.speaking:
            self.hush()
        is_answer, force = False, None
        if self.answer_until:                          # it just asked something (spoken)
            is_answer, force = True, self.answer_model
        elif (self.chat_question and time.time() - self.chat_question[2] < 300
              and (YES_RE.match(text) or NO_RE.search(text) or not self.skills.match(text))):
            # a typed reply after its question answers it, unless it's clearly a new command (a skill)
            is_answer, force = True, self.chat_question[0]
        self.pending = self.early_wake = None          # typing replaces anything half-heard
        self.followup_until = self.answer_until = self.open_until = 0.0
        self.wake_at, self.wake_judged, self.manual_listen = 0.0, False, False
        self.reset_vad(keep_noise=True)
        log(f"Typed: {text!r}")
        self.execute(text, time.time(), shots=self.grab(fg=self.ui.prev_fg), force_model=force,
                     is_answer=is_answer, source="typed")

    def hush(self):
        """Stop talking now (Stop button / Stop speaking)."""
        if not self.speaking:
            return
        log("Stopped speaking (button).")
        self.voice.stop()
        self.speaking, self.after_speech = False, None
        self.reset_vad(keep_noise=True)
        self.voice.cool()
        self.ui.speaking(False)
        self.ui.linger(max(3.0, settings()["hud_seconds_after_reply"]))

    def listen_now(self):
        """F20 held / mic button: listen as if "Hey Claude" was said. Works on battery too
        (the mic opens just for this); not while the screen is locked or off."""
        why = self.why_not_listening() if hasattr(self, "screen") else "starting"
        if why and why != "on battery":
            self.ui.toast(f"Can't listen right now ({why}).")
            return
        if self.speaking:
            self.hush()
        if self.pending:
            return                                     # already collecting a command
        self.demand_until = time.time() + FOLLOWUP_S + 3
        if self.stream is None:
            try:
                self.open()
            except Exception as e:
                log(f"Couldn't open mic for push-to-talk: {e!r}")
                self.ui.toast("Couldn't open the microphone.")
                return
        self.early_wake = None
        self.answer_until = self.open_until = 0.0
        self.reset_vad(keep_noise=True)
        self.manual_listen = True
        log("Listening on request (F20 / mic button).")
        self.on_wake("", time.time())
        self.wake_judged = True                        # you asked for it: no false-wake check

    def new_conversation(self):
        if self.speaking:
            self.hush()
        self.pending = self.early_wake = None
        self.followup_until = self.answer_until = self.open_until = 0.0
        self.end_conversation("new conversation button", manual=True)

    def history_from_log(self):
        hist, user = [], None
        for m in self.chatlog.messages():
            if m.get("role") == "user":
                user = m
            elif m.get("role") == "assistant" and user:
                h = m.get("handler") if m.get("handler") in MODEL_LABEL or m.get("handler") == "skill" else "local"
                cmd = user.get("text", "")
                hist.append({"t": m.get("t", time.time()), "handler": h, "source": user.get("source", "voice"),
                             "skill": m.get("skill"),
                             "command": f"(answer) {cmd}" if user.get("answer") else cmd,
                             "reply": m.get("text", "")})
                user = None
        if hist:
            log(f"Continuing the conversation in {self.chatlog.path.name} ({len(hist)} exchanges).")
        return hist

    def _catch_up_reflections(self):
        """Conversations that ended while Hey Claude wasn't running never got their learned-memory
        review; do it now from the saved transcript (oldest first, the last few only)."""
        try:
            files = [f for f in self.chatlog.files() if f != self.chatlog.path][-3:]
            for f in files:
                if time.time() - f.stat().st_mtime < 3 * 86400:
                    self._catch_up(f)
        except Exception as e:
            log(f"Learned-memory catch-up failed: {e!r}")

    def _catch_up(self, path):
        from chatlog import ChatLog
        msgs = ChatLog.read(path)
        todo = msgs[self.chatlog.reflected_upto(path):]
        if not todo:
            return
        if not any(m.get("role") == "assistant" and m.get("handler") in MODEL_LABEL for m in todo):
            self.chatlog.mark_reflected(path, len(msgs))  # instant actions only: nothing to learn
            return
        lines = []
        for m in todo:
            who = {"user": "User", "assistant": "Assistant"}.get(m.get("role"), "Note")
            how = f" ({m['source']})" if m.get("role") == "user" and m.get("source") else ""
            lines.append(f"{who}{how}: {str(m.get('text', ''))[:1500]}")
        transcript = "\n".join(lines)[-20000:]
        model = settings()["models"]["normal"]
        with self.reflect_lock:
            s = ClaudeSession(model).start()
            try:
                result = s.ask(CATCHUP_HEAD + transcript + "\n\n" + REFLECT_PROMPT)
            finally:
                s.stop()
        log(f"Learned-memory catch-up ({path.name}, {model}): {result}")
        if not result.startswith(("Sorry", "Claude Code isn't")):
            self.chatlog.mark_reflected(path, len(msgs))

    # ---- ✓ / ✕ on the pill
    def finish_now(self):
        """✓: send what's been heard so far, right now; ignore anything said after the click."""
        log("✓ clicked: finishing the command now.")
        if self.in_speech and self.buf:
            audio = np.concatenate(self.buf)
            self.reset_vad(keep_noise=True)
            # Already have a command? Then whoever is talking now (roommates) is dropped.
            # Otherwise this IS your command, still being spoken: use it up to the click.
            if not self.pending and len(audio) / RATE >= MIN_UTTER_S:
                self.handle(audio)
        if self.pending:
            self.pending["checked"] = True            # you confirmed it yourself: skip the false-wake filter
            self.run_pending()
        else:
            self.cancel_listening(quiet=True)         # nothing to send: just stop listening

    def cancel_listening(self, quiet=False):
        """✕: drop the command (or the answer to its question) and stop listening."""
        if not quiet:
            log("✕ clicked: cancelled.")
        self.manual_listen = False
        self.pending, self.early_wake = None, None
        self.followup_until = self.answer_until = self.open_until = 0.0
        self.wake_at, self.wake_judged = 0.0, False
        APPROVAL_TOKEN.unlink(missing_ok=True)       # cancelling an approval question = no
        self.reset_vad(keep_noise=True)
        self.voice.cool()
        self.ui.hide()

    def apply_settings(self):
        """Re-read settings.json if it changed (Claude may edit it by voice). An effort change
        needs a fresh session for that model; model/tier changes simply apply to the next command."""
        if not settings().reload():
            return
        for m, s in list(self.sessions.items()):
            if s.alive() and s.effort != settings()["effort"][m]:
                log(f"Effort for {m} changed to {settings()['effort'][m]}; restarting that session.")
                self.rotate(m, "settings changed")

    # ---- audio
    def _callback(self, indata, frames, t, status):
        frame = indata[:, 0].copy()
        self.q.put(frame)
        try:
            self.ui.mic_level = float(np.sqrt(np.mean(frame * frame)))   # the chat's listening bars
        except Exception:
            pass

    def open(self):
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=FRAME,
                                     callback=self._callback)
        self.stream.start(); self.last_audio = time.time(); self.flush()
        log("Listening (mic open).")

    def close(self, why):
        if self.stream:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None
            log(f"Mic closed: {why}.")
        self.reset_vad()

    def flush(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.reset_vad(keep_noise=True)               # keep the learned background-noise level

    def reset_vad(self, keep_noise=False):
        if not keep_noise or not hasattr(self, "noise"):
            self.noise = 0.004
        self.above, self.in_speech, self.silence = 0, False, 0
        self.buf: list = []
        self.pre = collections.deque(maxlen=int(PRE_ROLL_S / FRAME_S))

    def feed(self, frame):
        rms = float(np.sqrt(np.mean(frame * frame)))
        if not self.in_speech:
            self.pre.append(frame)
            if rms > max(self.noise * 3.0, MIN_LEVEL):
                self.above += 1
                if self.above >= START_FRAMES:
                    self.in_speech, self.buf, self.silence = True, list(self.pre), 0
                    self.utter_start = time.time() - len(self.pre) * FRAME_S
                    self.early_wake, self.early_i = None, 0
            else:
                self.above = 0
                self.noise = 0.97 * self.noise + 0.03 * rms
            return None
        self.buf.append(frame)
        self.silence = 0 if rms > max(self.noise * 2.0, MIN_LEVEL * 0.6) else self.silence + 1
        if self.silence * FRAME_S >= END_SILENCE_S or len(self.buf) * FRAME_S >= MAX_UTTER_S:
            audio = np.concatenate(self.buf)
            self.in_speech, self.buf, self.above = False, [], 0
            return audio
        return None

    def transcribe(self, audio):
        segs, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                        condition_on_previous_text=False, without_timestamps=True,
                                        hotwords=HOTWORDS)
        segs = list(segs)
        # how sure the recognizer was (noise turns into low-confidence or repetitive "words")
        self.last_asr = {
            "logprob": float(np.mean([s.avg_logprob for s in segs])) if segs else -9.0,
            "nospeech": max((s.no_speech_prob for s in segs), default=1.0),
            "comp": max((s.compression_ratio for s in segs), default=0.0),
        }
        return " ".join(s.text.strip() for s in segs).strip()

    def speech_snr(self, voice) -> float:
        """How far the voice stands out above the room's background noise, in dB.
        Someone talking right at the laptop: ~20-35 dB. Noise / people across the room: much less."""
        if not voice:
            return 0.0
        return voice[0] - 20 * math.log10(max(self.noise, 1e-5))

    # ---- false-wake filter: noise, gibberish, or talk that isn't aimed at it -> silently cancel
    def judge(self, text, asr, snr, kind):
        """-> (is_real_request, reason). kind: wake | followup | open | answer."""
        t = (text or "").strip(" ,.!?\"'")
        words = re.findall(r"[A-Za-z0-9']+", t)
        if not words:
            return False, "nothing said"
        if HALLUCINATION_RE.match(t):
            return False, f"noise transcribed as {t!r}"
        lp = float(np.mean([a["logprob"] for a in asr])) if asr else 0.0
        ns = max((a["nospeech"] for a in asr), default=0.0)
        cr = max((a["comp"] for a in asr), default=0.0)
        if lp < -1.0 or (ns > 0.6 and lp < -0.6) or cr > 2.4:
            return False, f"gibberish (confidence {lp:.2f}, no-speech {ns:.2f}, repetition {cr:.1f})"
        if kind == "answer":
            return True, ""                            # yes/no logic handles answers
        snr_med = float(np.median(snr)) if snr else 30.0
        if snr_med < 10.0:
            return False, f"no one talking directly to it (voice only {snr_med:.0f} dB above the room)"
        core = re.sub(r"^(?:and|also|then|now|ok(?:ay)?|oh|actually|wait)[ ,]+", "", t, flags=re.I)
        local_like = bool(LOCAL_HINT_RE.match(core))
        clear_and_close = lp >= -0.55 and snr_med >= 16.0
        if local_like and clear_and_close:
            needs_triage = False                       # "and turn the volume down": obviously a command
        else:
            needs_triage = kind == "open" or len(words) >= 7 or not clear_and_close
        if needs_triage and not self.triage.directed(t, self.spoken_reply if kind == "open" else ""):
            return False, "sounds like conversation not directed at it"
        return True, ""

    def false_wake(self, reason, text=""):
        log(f"False wake ignored ({reason}): {text[:120]!r}")
        self.manual_listen = False
        self.voice.cool()
        self.pending, self.early_wake = None, None
        self.followup_until = self.answer_until = self.open_until = 0.0
        self.wake_at, self.wake_judged = 0.0, False
        self.reset_vad(keep_noise=True)
        self.ui.hide()                                 # no response at all

    def evaluate_partial(self):
        """~4 s after the wake word, while still listening: is real, aimed-at-it speech coming in?
        If it's noise, gibberish or roommates talking, cancel now instead of collecting more."""
        self.wake_judged = True
        p = self.pending
        parts, asr, snr = ([p["text"]] if p else []), list(p["asr"]) if p else [], list(p["snr"]) if p else []
        if self.in_speech and len(self.buf) * FRAME_S >= 1.0:
            audio = np.concatenate(self.buf)
            parts.append(self.transcribe(audio)); asr.append(self.last_asr)
            snr.append(self.speech_snr(voice_features(audio)))
        text = " ".join(x for x in parts if x).strip()
        m = WAKE.search(text)
        if m and m.start() < 3:
            text = text[m.end():]
        kind = p["kind"] if p else ("open" if self.open_until else "wake")
        if not text and (self.in_speech or p):
            return                                     # still mid-word: decide at the end instead
        ok, reason = self.judge(text, asr, snr, kind)
        if not ok:
            self.false_wake(f"4 s check: {reason}", text)
        elif p:
            p["checked"] = True

    def spot_wake(self, audio, at_start=False) -> bool:
        """Fast check (tiny model, ~0.15 s): does this audio contain 'Hey Claude'?
        The "Hey Claude." prompt is needed: without it tiny hears the owner's real voice as "a cloud" /
        "take a lot of time" and never spots him. With it, tiny also 'hears' it in "hey close…",
        "hey Kyle"…, so this is only a SENSITIVE first pass: the caller gates on loudness and
        small.en must confirm before anything is acted on. at_start: match must begin the transcript."""
        segs, _ = self.fast_model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                             condition_on_previous_text=False, without_timestamps=True,
                                             hotwords="Hey Claude.")
        m = WAKE.search(" ".join(s.text.strip() for s in segs).strip())
        return bool(m) and (not at_start or m.start() < 4)

    # ---- shared conversation context
    def context_preface(self, s: ClaudeSession) -> str:
        """Commands in this conversation that this session didn't handle itself (other models,
        instant actions), or the whole conversation so far if the session is brand new. A brand-new
        session also learns where the previous conversation is saved."""
        if s.fresh:
            entries = self.history[-40:]
        else:
            entries = [h for h in self.history[s.seen:] if h["handler"] != s.model]
        pointer = ""
        if s.fresh:
            prev = self.chatlog.previous()
            if prev is not None:
                pointer = (f"(The previous conversation is saved at {prev} (JSON lines). Read it only if the "
                           "user refers to something from before this conversation.)\n\n")
        if not entries:
            return pointer
        lines = []
        for h in entries:
            who = {"local": "done instantly on the laptop",
                   "skill": f"done instantly by the saved skill '{h.get('skill', '')}'"}.get(
                h["handler"]) or f"handled by {MODEL_LABEL[h['handler']]}"
            verb = "typed" if h.get("source") == "typed" else "said"
            reply = h["reply"] if len(h["reply"]) <= 600 else h["reply"][:600] + "…"
            lines.append(f"- {time.strftime('%H:%M', time.localtime(h['t']))} user {verb} “{h['command']}” "
                         f"→ “{reply}” ({who})")
        return pointer + ("(Earlier in this conversation — use it to understand words like 'it', 'that', "
                          "'again', 'undo':\n" + "\n".join(lines) + ")\n\n")

    def rotate(self, model, why, mark=None):
        """Swap in a fresh session for `model`; the old one writes what it learned, then exits.
        mark=(conversation file, n): record that file as reviewed once the review succeeds."""
        old = self.sessions[model]
        new = ClaudeSession(model)
        if model != "opus" or old.alive():
            new.start()
        self.sessions[model] = new
        if old.turns > 0 and old.alive():
            threading.Thread(target=self._reflect_and_stop, args=(old, why, mark), daemon=True).start()
        else:
            old.stop()

    def _reflect_and_stop(self, old: ClaudeSession, why, mark=None):
        with self.reflect_lock:                        # one writer of learned.md at a time
            try:
                result = old.ask(REFLECT_PROMPT)
                log(f"Learned-memory review ({old.model}, {why}): {result}")
                if mark and not result.startswith(("Sorry", "Claude Code isn't")):
                    self.chatlog.mark_reflected(*mark)
            except Exception as e:
                log(f"Learned-memory review failed: {e!r}")
            finally:
                old.stop()

    def end_conversation(self, why, manual=False):
        """Close this conversation: every session that took part reviews it for learned memory
        (memories, preferences and settings carry over), and the next message starts a new log."""
        log(f"Conversation ended ({why}); starting fresh.")
        conv = self.chatlog.path
        upto = len(self.chatlog.messages()) if conv else 0
        reviewing = False
        for m, s in list(self.sessions.items()):
            if s.turns > 0:
                reviewing = True
                self.rotate(m, why, mark=(conv, upto) if conv else None)
        if conv and not reviewing:                     # e.g. everything happened before a restart
            threading.Thread(target=lambda: self._safe_catch_up(conv), daemon=True).start()
        self.chatlog.new()
        self.history = []
        self.local.last_opened = None
        self.chat_question = None
        self.ui.chat_reset(manual)

    def _safe_catch_up(self, path):
        try:
            self._catch_up(path)
        except Exception as e:
            log(f"Learned-memory catch-up failed: {e!r}")

    # ---- collecting a command (it keeps going as long as you keep talking)
    def begin_pending(self, text, t0, shots=None, force_model=None, is_answer=False, kind="wake"):
        self.pending = {"text": text.strip(" ,"), "t0": t0, "shots": shots or [],
                        "force_model": force_model, "is_answer": is_answer,
                        "voice": self.last_voice,      # your voice, to spot roommates joining in
                        "kind": "answer" if is_answer else kind,
                        "asr": [self.last_asr], "snr": [self.speech_snr(self.last_voice)],
                        "checked": self.manual_listen}  # F20 / mic button: you asked for it, no filter
        self.pending_deadline = time.time() + continue_s()
        if not self.wake_at:
            self.wake_at = time.time()
        self.ui.show("listen", "Listening…", f"“{self.pending['text']}”")

    def run_pending(self):
        p, self.pending = self.pending, None
        self.wake_at, self.wake_judged = 0.0, False
        self.manual_listen = False
        text = p["text"].strip(" ,.!?")
        if not text:
            self.ui.hide(); return
        if not p.get("checked"):                       # false-wake filter before doing anything
            ok, reason = self.judge(text, p["asr"], p["snr"], p["kind"])
            if not ok:
                self.false_wake(reason, text)
                return
        self.execute(text, p["t0"], shots=p["shots"], force_model=p["force_model"], is_answer=p["is_answer"])

    def check_early_wake(self):
        """Called while you're still talking: if the start of what you've said so far is
        'Hey Claude', chime and show 'Listening…' immediately instead of waiting for you to finish."""
        if (not self.wake_on or self.early_wake is not None or self.early_i >= len(EARLY_WAKE_AT_S)
                or self.pending or self.answer_until or self.followup_until or self.open_until):
            return
        if len(self.buf) * FRAME_S < EARLY_WAKE_AT_S[self.early_i]:
            return
        self.early_i += 1
        head = np.concatenate(self.buf)[: int(EARLY_WAKE_HEAD_S * RATE)]
        if self.speech_snr(voice_features(head)) < EARLY_WAKE_MIN_SNR_DB:
            return                                    # too faint to be someone at the laptop
        if self.spot_wake(head, at_start=True):
            shots = self.grab()                      # pixels grabbed before the pill appears; PNGs in background
            self.ui.show("listen", "Listening…", "")  # visual cue now; the chime waits for small.en to confirm
            self.early_wake = {"shots": shots}
            self.wake_at, self.wake_judged = time.time(), False
            log(f"Wake word heard while still talking ({len(self.buf) * FRAME_S:.1f}s in).")

    def on_wake(self, after_wake: str, t0, shots=None):
        """'Hey Claude' heard (normally, or over its own voice). after_wake = whatever followed it.
        shots given = the chime/pill/screenshots already happened (early detection)."""
        if shots is None:
            shots = self.grab()                      # pixels grabbed before the pill appears; PNGs in background
            chime()
            self.voice.warm(20)                       # wake the Bluetooth speaker for the reply
            self.wake_at, self.wake_judged = time.time(), False
        command = after_wake.strip(" ,.!?")
        if len(command) < 2:
            log("Wake word heard, waiting for command.")
            self.ui.show("listen", "Listening…", "Go ahead")
            self.followup_until, self.pending_shots = time.time() + FOLLOWUP_S, shots
            return
        self.begin_pending(command, t0, shots=shots)

    # ---- what to do with a finished stretch of speech
    def handle(self, audio):
        if len(audio) / RATE < MIN_UTTER_S:
            return
        start, t0 = self.utter_start, time.time()
        voice = voice_features(audio)
        prev_voice, self.last_voice = self.last_voice, voice

        # 1) you kept talking: add it to the command being collected
        if self.pending:
            if start <= self.pending_deadline + 0.1:
                if sounds_like_someone_else(self.pending.get("voice"), voice):
                    log(f"Sounds like someone else talking (you {self.pending.get('voice')}, now {voice}); "
                        "not adding it, sending your command.")
                    self.run_pending()                 # stop listening; their words are dropped
                    return
                more = self.transcribe(audio).strip()
                if more:
                    self.pending["text"] += " " + more
                    self.pending["asr"].append(self.last_asr)
                    self.pending["snr"].append(self.speech_snr(voice))
                    self.ui.show("listen", "Listening…", f"“{self.pending['text']}”")
                self.pending_deadline = time.time() + continue_s()
                return
            self.run_pending()                         # that command was finished; this is new speech

        # 2) your answer to its question (no wake word needed). Counts if you STARTED in time.
        if self.answer_until and start <= self.answer_until:
            text = self.transcribe(audio)
            if is_echo(text, self.spoken_reply):       # its own voice: keep waiting for the real answer
                log(f"Answer window: ignored echo {text!r}")
                return
            self.answer_until = 0.0
            m = WAKE.search(text)
            if m and m.start() < 3:                    # they said "Hey Claude" anyway
                text = text[m.end():]
            if text.strip(" ,.!?"):
                self.begin_pending(text, t0, force_model=self.answer_model, is_answer=True)
            else:
                self.ui.hide()
            return

        # 2b) you started talking within a few seconds of its reply: a command, no wake word needed
        if self.open_until and start <= self.open_until:
            self.open_until = 0.0
            text = self.transcribe(audio)
            m = WAKE.search(text)
            if m and m.start() < 3:                    # they said "Hey Claude" anyway
                text = text[m.end():]
            text = text.strip(" ,.!?")
            if not text or is_echo(text, self.spoken_reply) or ACK_RE.match(text):
                log(f"Open-mic: ignored {text!r}")
                self.close_open_window()
                return
            log(f"Open-mic follow-up (no wake word): {text!r}")
            self.begin_pending(text, t0, shots=self.grab(), kind="open")
            return

        # 3) the command after a bare "Hey Claude"
        if self.followup_until and start <= self.followup_until:
            self.followup_until = 0.0
            text = self.transcribe(audio)
            if text.strip(" ,.!?"):
                self.begin_pending(text, t0, shots=self.pending_shots, kind="followup")
            else:
                self.ui.hide()
            return

        # 4a) "Hey Claude" was already spotted while you were talking (pill is up): read the whole thing
        if self.early_wake is not None:
            ew, self.early_wake = self.early_wake, None
            text = self.transcribe(audio)
            m = WAKE.search(text)
            if not m:                                  # the accurate model didn't hear "Hey Claude": noise
                self.false_wake("wake word not confirmed", text)
                return
            chime()                                    # confirmed
            self.voice.warm(20)                        # wake the Bluetooth speaker for the reply
            self.on_wake(text[m.end():], t0, shots=ew["shots"])
            return

        # 4) normal: is this "Hey Claude ..."? (not on battery: there the mic only opens for F20)
        if not self.wake_on:
            return
        if len(audio) / RATE <= SINGLE_PASS_S:
            text = self.transcribe(audio)
        else:
            text = self.transcribe(audio[: int(WAKE_CHECK_S * RATE)])
            if WAKE.search(text):
                text = self.transcribe(audio)
        if DEBUG:
            log(f"heard: {text!r}")
        m = WAKE.search(text)
        if m:
            self.on_wake(text[m.end():], t0)

    # ---- while it's talking, only "Hey Claude" can stop it
    def check_barge_in(self):
        audio = np.concatenate(self.barge_ring)
        if float(np.sqrt(np.mean(audio * audio))) < MIN_LEVEL:
            return
        if WAKE.search(self.spoken_reply) or not self.spot_wake(audio):   # fast check; ignore its own "Claude"
            return
        text = self.transcribe(audio)                  # accurate pass for what came after "Hey Claude"
        m = WAKE.search(text)
        if not m:
            return
        log(f"Interrupted by wake word: {text!r}")
        self.voice.stop()
        self.speaking, self.after_speech = False, None
        self.ui.speaking(False)
        self.barge_ring.clear()
        self.last_voice = None                         # mixed with its own voice: no reliable fingerprint
        self.reset_vad(keep_noise=True)
        self.on_wake(text[m.end():], time.time())

    def try_skill(self, sk, values):
        """A saved skill matched. Run it instantly when that's safe; otherwise hand the model a note.
        -> (note for the model, skill name if it ran, skill to mark verified, reply if it ran)."""
        where = f"'{sk.name}' (folder {sk.folder})"
        if sk.blocked:
            log(f"Skill {sk.name!r} matched but is disabled: {sk.blocked}")
            return (f"(A saved skill, {where}, matches this command but is disabled: {sk.blocked}. Do the "
                    "request yourself without running that script.)", None, None, None)
        cmd = sk.command_line(values, self.skills.python)
        usual = f"Its usual reply is {sk.reply!r} ({{output}} = what the script prints)."
        if sk.approval:
            why = sk.approval_reason or "it is marked as needing approval"
            log(f"Skill {sk.name!r} needs approval ({why}); the model asks first.")
            return (f"(A saved skill, {where}, matches this command. It needs the user's approval first ({why}): "
                    "ask ONE confirmation question that says exactly what it will do, and only after a clear yes "
                    f"run it with your PowerShell tool: {cmd}  {usual})", None, None, None)
        if not self.skills.is_verified(sk):
            log(f"Skill {sk.name!r} is new or changed: its first run goes through the model.")
            return (f"(A saved skill, {where}, matches this command. It is new or was changed, so run it this once "
                    f"with your PowerShell tool, exactly: {cmd}  Check it did what the user asked, then reply. {usual} "
                    "If it fails, fix the script in its folder and say so.)", None, sk, None)
        ok, out = self.skills.run(sk, values)
        if ok:
            return "", sk.name, None, out
        log(f"Skill {sk.name!r} failed: {out}")
        return (f"(A saved skill, {where}, was just tried for this command and failed: {out}. Do the request "
                "yourself, and if fixing the skill's script is simple and safe, fix it too.)", None, None, None)

    def note(self, text):
        """A small grey line in the chat (and its log), e.g. 'Stopped.'"""
        self.chatlog.append("note", text)
        self.ui.chat_add({"role": "note", "text": text, "t": time.time()})

    def execute(self, command, t0, shots=None, force_model=None, is_answer=False, source="voice"):
        """Run one command (spoken or typed) and show / speak the reply. Typed: the reply is text only."""
        typed = source == "typed"
        self.last_activity = time.time()
        self.stop_event.clear()
        self.chat_question = None
        # the chat window already shows what you typed; spoken commands are added here
        self.chatlog.append("user", command, source=source, answer=is_answer or None)
        if not typed:
            self.ui.chat_add({"role": "user", "text": command, "source": "voice", "t": time.time()})
        # Approval lock: only a clear "yes" (spoken, or typed in the chat) to its question unlocks ONE
        # send (see approval_gate.py).
        try:
            if is_answer and YES_RE.match(command) and not NO_RE.search(command):
                APPROVAL_TOKEN.write_text(f"{time.time()}\n{self.spoken_reply}\n", encoding="utf-8")
                log(f"Approval recorded for: {self.spoken_reply!r}")
            else:
                APPROVAL_TOKEN.unlink(missing_ok=True)
        except Exception as e:
            log(f"Approval token error: {e!r}")
        if isinstance(shots, ScreenGrab):
            shots = shots.get()                       # usually already finished while you were talking
        self.ui.show("think", "Got it", f"“{command}”")
        reply, handler, session = None, "local", None
        skill_note, skill_name, verify_skill = "", None, None
        if force_model is None:
            hit = None
            try:
                hit = self.skills.match(command)       # saved script skills come first...
            except Exception as e:
                log(f"Skill matching failed: {e!r}")
            if hit:
                skill_note, skill_name, verify_skill, reply = self.try_skill(*hit)
            else:
                reply = self.local.try_handle(command)  # ...then the built-in instant actions
        if reply is not None:
            if skill_name:
                handler = "skill"
                log(f"COMMAND (skill '{skill_name}'): {command} -> {reply}")
            else:
                log(f"COMMAND (local): {command} -> {reply}")
        else:
            model, visual = (force_model, False) if force_model else route(command)
            if skill_note and model == "haiku":       # running / fixing a skill: not a job for Haiku
                model = "sonnet"
            handler, label = model, MODEL_LABEL[model]
            session = self.sessions[model]
            self.ui.show("think", f"Thinking · {label}", f"“{command}”")
            if not typed or SPEAK_REQ_RE.search(command):
                self.voice.warm(180)                   # speaker stays awake while Claude works
            how = "typed" if typed else "spoken"
            body = f"(The user's {how} answer to your question:) {command}" if is_answer else command
            if typed:
                body = TYPED_NOTE + body
            if skill_note:
                body += "\n\n" + skill_note
            if visual and shots:
                listing = "\n".join(f"  - {p}  ({d})" for p, d in shots)
                body += ("\n\n(Screen context: screenshots of all the user's screens, taken when they spoke:\n"
                         f"{listing}\nRead the relevant ones first if the command refers to something on screen; "
                         "start with the one that has the window the user was using.)")
            prompt = self.context_preface(session) + body
            log(f"COMMAND ({model}{', answer' if is_answer else ''}"
                f"{f', +{len(shots)} screens' if visual and shots else ''}): {command}")
            allow_foreground()
            progress = lambda d: self.ui.show("act", f"Working · {label}", d)
            reply = session.ask(prompt, progress, stop=self.stop_event)
            log(f"  REPLY ({time.time() - t0:.1f}s): {reply}")
            if reply != STOPPED and model == "haiku" and not is_answer and CANT_RE.search(reply):
                # Haiku has no connectors; if it says it can't reach something, let Sonnet try.
                session.seen, session.fresh = len(self.history), False
                handler, label, session = "sonnet", MODEL_LABEL["sonnet"], self.sessions["sonnet"]
                log("  Haiku couldn't do it; retrying with Sonnet.")
                self.ui.show("think", f"Thinking · {label}", f"“{command}”")
                reply = session.ask(self.context_preface(session) + body,
                                    lambda d: self.ui.show("act", f"Working · {label}", d), stop=self.stop_event)
                log(f"  REPLY ({time.time() - t0:.1f}s, Sonnet): {reply}")

        if reply == STOPPED:                           # Stop pressed: that session restarted fresh
            self.history.append({"t": time.time(), "handler": handler, "source": source,
                                 "command": command, "reply": "(stopped by the user before replying)"})
            self.note("Stopped.")
            self.flush()
            self.voice.cool()
            self.ui.hide()
            self.stop_event.clear()
            return
        if session is not None and re.search(r"\[ignore\]", reply, re.I):
            if typed:                                  # typed is always meant for it
                reply = chat_text(reply) or "Okay."
            else:
                # Claude decided it wasn't meant for it (background talk / gibberish): no response at all
                log("  Claude marked it [ignore]: no response.")
                session.seen, session.fresh = len(self.history), False
                self.note("Ignored: that didn't sound like it was meant for me.")
                self.flush()
                self.voice.cool()
                self.ui.hide()
                return
        # typed prompt + "say it out loud" (or Claude tagged the reply [speak]): read the reply aloud too
        speak_typed = typed and bool(SPEAK_REQ_RE.search(command) or SPEAK_TAG_RE.search(reply))
        text = chat_text(reply)
        if verify_skill is not None and not reply.startswith(("Sorry", "Claude Code isn't")) and "?" not in reply:
            self.skills.mark_verified(verify_skill)   # its first run went through the model's checked tools
        self.history.append({"t": time.time(), "handler": handler, "source": source, "skill": skill_name,
                             "command": f"(answer) {command}" if is_answer else command,
                             "reply": text})
        if session is not None:
            session.seen, session.fresh = len(self.history), False
        self.chatlog.append("assistant", text, handler=handler, source=source, skill=skill_name)
        self.ui.chat_add({"role": "assistant", "text": text, "handler": handler, "source": source,
                          "skill": skill_name, "t": time.time()})

        spoken, shown, keep, silent = parse_reply(reply)
        ok = not spoken.startswith(("Sorry", "Claude Code isn't"))
        # Any question in the reply opens the answer window ("Delete these 2 files? Say yes to go ahead.")
        asked = session is not None and ok and "?" in spoken
        if asked:
            self.chat_question = (handler, spoken, time.time())   # a typed reply in the chat answers it
        self.spoken_reply = spoken
        self.flush()                                   # drop whatever the mic heard while it was thinking
        if speak_typed and spoken:                     # typed, but asked to hear it: speak, no open mic after
            log("  Typed prompt asked to hear the reply: speaking it.")
            self.ui.show("ask" if asked else ("done" if ok else "error"),
                         "Question" if asked else ("Done" if ok else "Couldn't do that"), shown)
            self.after_speech = ("typed", keep or asked, shown)
            self.barge_ring.clear(); self.barge_next = time.time() + BARGE_EVERY_S
            self.speaking = True
            self.ui.speaking(True)
            self.voice.speak(spoken)
            self.last_activity = time.time()
            if session is not None and session.turns >= MAX_TURNS:
                self.rotate(handler, f"{MAX_TURNS} commands")
            return
        if typed:                                      # text only: no voice, no open mic
            self.voice.cool()
            if silent and ok:
                self.ui.show("done", "Done", shown, hide_after=2.5)
            else:
                self.ui.show("ask" if asked else ("done" if ok else "error"),
                             "Question" if asked else ("Done" if ok else "Couldn't do that"), shown)
                self.ui.linger(None if keep or asked else settings()["hud_seconds_after_reply"])
            self.last_activity = time.time()
            if session is not None and session.turns >= MAX_TURNS:
                self.rotate(handler, f"{MAX_TURNS} commands")
            return
        if silent and ok:                              # simple, self-evident action: no voice, brief pill
            self.voice.cool()
            self.ui.show("done", "Done", shown, hide_after=2.5)
            self.last_activity = time.time()
            if session is not None and session.turns >= MAX_TURNS:
                self.rotate(handler, f"{MAX_TURNS} commands")
            return
        if asked:
            self.ui.show("ask", "Question", shown)
        else:
            self.ui.show("done" if ok else "error", "Done" if ok else "Couldn't do that", shown)
        # Talk in the background: the main loop keeps running so "Hey Claude" can interrupt.
        # The pill stays up until the voice finishes (see finish_speaking), then lingers.
        self.after_speech = ("ask", handler, shown) if asked else ("done", keep, shown)
        self.barge_ring.clear(); self.barge_next = time.time() + BARGE_EVERY_S
        self.speaking = True
        self.ui.speaking(True)
        self.voice.speak(spoken)
        self.last_activity = time.time()
        if session is not None and session.turns >= MAX_TURNS:
            self.rotate(handler, f"{MAX_TURNS} commands")

    def finish_speaking(self):
        """Called once the voice has finished on its own."""
        self.speaking = False
        self.ui.speaking(False)
        self.reset_vad(keep_noise=True)                # throw away the tail of our own voice
        kind, extra, shown = self.after_speech or ("done", False, "")
        self.after_speech = None
        if kind == "typed":                            # a typed prompt's reply, read aloud on request:
            self.ui.linger(None if extra else settings()["hud_seconds_after_reply"])   # no open mic after
            self.last_activity = time.time()
            return
        if kind == "ask":
            self.answer_until, self.answer_model = time.time() + settings()["answer_window_seconds"], extra
            self.ui.show("listen", "Listening for your answer…", shown, kind="answer")
            log("Asked a question; listening for the answer.")
        else:
            window = settings()["listen_after_reply_seconds"]
            self.open_shown, self.open_keep = shown, extra
            if window > 0:
                # Open mic: talk within a few seconds and it's taken as a command, no wake word.
                self.open_until = time.time() + window
                self.ui.show("listen", "Listening…", shown, kind="open")
            else:
                self.close_open_window()
        self.last_activity = time.time()

    def close_open_window(self):
        """The open-mic window ended with nothing said: back to the normal 'Done' pill, which stays
        readable for the rest of the 10 s (setting), or until you move on for [keep] replies."""
        self.open_until = 0.0
        self.wake_at, self.wake_judged = 0.0, False
        self.ui.show("done", "Done", self.open_shown)
        rest = settings()["hud_seconds_after_reply"] - settings()["listen_after_reply_seconds"]
        self.ui.linger(None if self.open_keep else max(3.0, rest))

    # ---- main loop
    def why_not_listening(self):
        """None if it should listen now, else the reason (battery, locked, display off)."""
        # Backup checks that always run, so a missed Windows event can never leave it stuck:
        if time.time() >= self.lock_poll:
            self.lock_poll = time.time() + 2
            locked = is_locked_now()                   # the lock screen is what's showing, or not
            if locked != self.screen.locked:
                log(f"Lock check: screen is {'locked' if locked else 'unlocked'}.")
                self.screen.locked = locked
            kernel32.GetTickCount.restype = ctypes.c_uint32
            idle_ms = (kernel32.GetTickCount() - _last_input_tick()) & 0xFFFFFFFF
            if not self.screen.display_on and idle_ms < 5000:
                log("Recent mouse/keyboard use: the display must be on.")
                self.screen.display_on = True          # you just used the mouse or keyboard
        if not plugged_in():
            return "on battery"
        if self.screen.locked:
            return "screen locked"
        if not self.screen.display_on:
            return "display off"
        return None

    def on_demand_active(self) -> bool:
        """On battery the mic opens only for F20 / the mic button: until that command, its reply
        and the follow-up windows after it are over."""
        return bool(time.time() < self.demand_until or self.pending is not None or self.followup_until
                    or self.answer_until or self.open_until or self.in_speech
                    or (self.speaking and not (self.after_speech and self.after_speech[0] == "typed")))

    def housekeeping(self):
        """Things that happen whether or not the mic is open (chat, F20, settings, idle end)."""
        now = time.time()
        self.handle_ui_actions()                       # ✓ / ✕, typed prompts, Stop, F20, new conversation
        if self.speaking and not self.voice.is_speaking():
            self.finish_speaking()
        if now >= self.settings_check:
            self.settings_check = now + 1.0
            self.apply_settings()
        if self.history and not self.speaking and now - self.last_activity > CONTEXT_WINDOW_S:
            self.end_conversation("15 minutes idle")

    def run(self):
        self.screen, self.lock_poll = ScreenWatcher(), 0.0
        while True:
            reason = self.why_not_listening()
            self.wake_on = reason is None              # the wake word itself: only plugged in / docked
            why = None if (reason == "on battery" and self.on_demand_active()) else reason
            if reason != self.mic_reason:
                self.mic_reason = reason
                self.ui.mic_state(reason)
            if why:
                if self.stream is not None and why != "on battery":
                    self.pending = self.early_wake = None
                    self.followup_until = self.answer_until = self.open_until = 0.0
                    self.manual_listen = False
                    self.ui.hide()
                self.close(why)
                for _ in range(5):                     # stay responsive to the chat window and F20
                    self.housekeeping()
                    if self.stream is not None:        # F20 opened the mic
                        break
                    time.sleep(0.2)
                continue
            if self.stream is None:
                try:
                    self.open()
                except Exception as e:
                    log(f"Couldn't open mic ({e}); retrying in 10 s."); time.sleep(10); continue
            power_check = time.time() + 1                 # re-check battery / lock / display every second
            while time.time() < power_check:
                now = time.time()
                self.housekeeping()
                # False-wake check ~4 s after "Hey Claude" (or after someone starts talking in the
                # open-mic window): if it's noise, gibberish or roommates, cancel silently right then.
                if self.open_until and self.in_speech and not self.wake_at and self.utter_start <= self.open_until:
                    self.wake_at, self.wake_judged = self.utter_start, False
                if self.wake_at and not self.wake_judged and not self.speaking and now - self.wake_at >= 4.0:
                    try:
                        self.evaluate_partial()
                    except Exception as e:
                        log(f"4 s check failed: {e!r}"); self.wake_judged = True
                    continue
                # Windows to start talking close only if you HAVEN'T started; never mid-sentence.
                if not self.in_speech:
                    if self.pending and now > self.pending_deadline:
                        try:
                            self.run_pending()
                        except Exception as e:
                            log(f"Error running command: {e!r}")
                            self.ui.show("error", "Something went wrong", str(e)[:120], hide_after=6)
                        continue
                    if self.followup_until and now > self.followup_until:     # nothing said after the chime
                        self.false_wake("nothing said after the wake word")
                    if self.answer_until and now > self.answer_until:         # question went unanswered
                        self.answer_until = 0.0; self.ui.hide()
                    if self.open_until and now > self.open_until:             # nothing said after the reply
                        self.close_open_window()
                try:
                    frame = self.q.get(timeout=0.3)
                except queue.Empty:
                    if time.time() - self.last_audio > 5:
                        self.close("no audio for 5 s"); break
                    continue
                self.last_audio = time.time()

                if self.speaking:                      # only listen for "Hey Claude" while talking
                    self.barge_ring.append(frame)
                    if (self.wake_on and time.time() >= self.barge_next
                            and len(self.barge_ring) >= int(1.2 / FRAME_S)):
                        self.barge_next = time.time() + BARGE_EVERY_S
                        try:
                            self.check_barge_in()
                        except Exception as e:
                            log(f"Barge-in check failed: {e!r}")
                    continue

                audio = self.feed(frame)
                if audio is None and self.in_speech and self.q.qsize() < 10:   # don't fall behind
                    try:
                        self.check_early_wake()
                    except Exception as e:
                        log(f"Early wake check failed: {e!r}")
                if audio is not None:
                    try:
                        self.handle(audio)
                    except Exception as e:
                        log(f"Error handling speech: {e!r}")
                        self.ui.show("error", "Something went wrong", str(e)[:120], hide_after=6)


# ================================================================= entry

def kill_children_with_me():
    """Put this process in a Windows job object so the Claude sessions it starts
    are closed automatically whenever the listener stops, even if it's killed."""
    class _Limits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class _ExtLimits(ctypes.Structure):
        _fields_ = [("Basic", _Limits), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    try:
        kernel32.CreateJobObjectW.restype = wt.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        info = _ExtLimits(); info.Basic.LimitFlags = 0x2000                  # KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(kernel32.GetCurrentProcess()))
        globals()["_JOB"] = job                                                # keep the handle open
    except Exception as e:
        log(f"Job object setup failed: {e!r}")


def single_instance() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\HeyClaudeListener")
    return kernel32.GetLastError() != 183


def load_wav(path):
    with wave.open(path, "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
        rate = w.getframerate()
    if rate != RATE:
        n = int(len(data) * RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(np.float32)
    return data


def load_whisper(name=WHISPER_MODEL):
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="cpu", compute_type="int8",
                        download_root=str(MODELS), local_files_only=True)


def load_fast_whisper():
    try:
        m = load_whisper(FAST_WAKE_MODEL)
        m.transcribe(np.zeros(RATE, np.float32), language="en", beam_size=1, without_timestamps=True)  # warm up
        return m
    except Exception as e:
        log(f"Fast wake model unavailable ({e!r}); using the main model for wake checks.")
        return None


MEMORY_STUBS = {
    "permanent.md": "# Permanent memory (things the user told you to remember)\n\nOnly change this file when the "
                    "user explicitly asks (\"add X to your memory\", \"remember that X\", \"forget X\"). One short "
                    "bullet per fact, newest last, with the date added.\n\n",
    "learned.md": "# Learned memory (updated automatically after each conversation)\n\nWhat the user means by "
                  "things, which apps/lists/devices they mean, quicker ways to do common requests, and mistakes "
                  "to avoid. Keep under 40 short bullets.\n\n",
    "about-user.md": "# About the user\n\n(Write a short profile here: who you are, what you do, how you like "
                     "answers. Hey Claude reads it only when a request is about you.)\n",
}


def ensure_workspace():
    """First run on a fresh copy: create the private files that are never published
    (workspace/CLAUDE.md from the template, empty memory files)."""
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in MEMORY_STUBS.items():
            p = MEMORY_DIR / name
            if not p.exists():
                p.write_text(text, encoding="utf-8")
        if not SKILLS_DIR.exists() and SKILL_EXAMPLES.exists():   # starter skills on a fresh copy
            import shutil
            shutil.copytree(SKILL_EXAMPLES, SKILLS_DIR)
        target, example = WORKSPACE / "CLAUDE.md", WORKSPACE / "CLAUDE.example.md"
        if not target.exists() and example.exists():
            target.write_text(example.read_text(encoding="utf-8").replace("{HEY_CLAUDE_DIR}", str(HERE)),
                              encoding="utf-8")
            log("Created workspace/CLAUDE.md from the template; edit it to make Hey Claude yours.")
    except Exception as e:
        log(f"Workspace setup failed: {e!r}")


def main():
    ensure_workspace()
    if "--say" in sys.argv:                          # test the voice
        Voice().say(sys.argv[sys.argv.index("--say") + 1]); return

    if "--screens" in sys.argv:                      # test multi-monitor capture
        for p, d in capture_all_screens():
            print(p, "|", d)
        return

    if "--transcribe" in sys.argv:                   # test the pipeline on a WAV
        model = load_whisper()
        audio = load_wav(sys.argv[sys.argv.index("--transcribe") + 1])
        segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                   without_timestamps=True, hotwords=HOTWORDS)
        text = " ".join(s.text.strip() for s in segs).strip()
        m = WAKE.search(text)
        cmd = text[m.end():].strip(" ,.!?") if m else ""
        print(f"transcript: {text!r}\nwake word: {bool(m)}   command: {cmd!r}")
        if cmd:
            print(f"route: {route(cmd)}")
        return

    if "--demo" in sys.argv or not settings()["chat_hud"]:
        overlay, chatlog = Overlay(), None
    else:
        from chatlog import ChatLog
        chatlog = ChatLog(CONV_DIR, CONTEXT_WINDOW_S, log)
        try:
            from hud import Hud
            overlay = Hud(log=log, chatlog=chatlog)    # status pill + chat window (hud.py)
        except Exception as e:                         # never leave it without a pill
            log(f"Chat HUD failed to start ({e!r}); using the plain status pill.")
            overlay = Overlay()

    if "--demo" in sys.argv:                         # walk through the pill's states
        steps = [("listen", "Listening…", "“add this to my tasks and”"), ("think", "Thinking · Sonnet", "“add this to my tasks”"),
                 ("act", "Working · Sonnet", "Looking at your screen"),
                 ("act", "Working · Sonnet", "TickTick: create task"),
                 ("ask", "Question", "Which list should it go in, ENGL 101 or your Inbox?"),
                 ("listen", "Listening for your answer…", "Which list should it go in, ENGL 101 or your Inbox?"),
                 ("done", "Done", "Added it to ENGL 101 for Friday.")]
        for i, s in enumerate(steps):
            overlay.root.after(1800 * i, lambda s=s: overlay._show(*s, None))
        overlay.root.after(1800 * len(steps) + 2500, overlay.root.quit)
        overlay.root.mainloop(); return

    if not single_instance():
        return
    kill_children_with_me()
    use_normal_priority()                            # the scheduled task starts us below normal (choppy voice)

    def worker():
        try:
            log(f"Hey Claude starting ({WHISPER_MODEL}, voice {settings()['voice']}).")
            model = load_whisper()
            fast = load_fast_whisper()
            voice = Voice()
            lis = Listener(model, voice, overlay, fast_model=fast, chatlog=chatlog)
            overlay.show("done", "Hey Claude is ready", "Say “Hey Claude”, or tap F20 to type", hide_after=3)
            log("Ready.")
            lis.run()
        except Exception as e:
            log(f"FATAL: {e!r}")
            overlay.quit()

    threading.Thread(target=worker, daemon=True).start()
    overlay.root.mainloop()


if __name__ == "__main__":
    main()
