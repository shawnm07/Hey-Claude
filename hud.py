"""
Hey Claude's on-screen HUD: the status pill, and the chat window it opens into.

  * The status pill at the top of the primary screen (listening / thinking / reply), as before,
    now sliding in and out.
  * Touch the top-center edge of the screen with the mouse and an idle pill drops down; move
    away and it slides back up. Hover the pill to preview the latest messages, click it to open
    the chat window. Tap F20 to open / close the chat; HOLD F20 to talk (like saying "Hey Claude").
  * The chat window: scroll back through the conversation, type prompts, start a new
    conversation (gear menu). It stays on top until X, Esc or F20 closes it. Saying "Hey Claude"
    while it's open swaps the text box for a listening panel; the spoken command and its reply
    then appear in the chat like any other message.

Threading: every Tk call happens on the Tk (main) thread. Other threads use the queue-based API
below. User actions go back to the listener through `on_action(action)`:
  "finish" / "cancel"   the check / X buttons while listening
  ("send", text)        a typed prompt
  "stop"                stop the request Claude is working on
  "hush"                stop speaking
  "mic"                 start listening without the wake word (mic button, F20 hold)
  "new"                 start a new conversation
"""
import contextlib
import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

# A private user32 handle so these signatures never clash with hey_claude.py's
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MI(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class _LII(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def _sig(f, res, *args):
    f.restype, f.argtypes = res, list(args)


_sig(_u32.GetForegroundWindow, wt.HWND)
_sig(_u32.GetParent, wt.HWND, wt.HWND)
_sig(_u32.GetWindowLongW, ctypes.c_long, wt.HWND, ctypes.c_int)
_sig(_u32.SetWindowLongW, ctypes.c_long, wt.HWND, ctypes.c_int, ctypes.c_long)
_sig(_u32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
_sig(_u32.SetWindowPos, wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
_sig(_u32.SetForegroundWindow, wt.BOOL, wt.HWND)
_sig(_u32.BringWindowToTop, wt.BOOL, wt.HWND)
_sig(_u32.IsWindow, wt.BOOL, wt.HWND)
_sig(_u32.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.c_void_p)
_sig(_u32.AttachThreadInput, wt.BOOL, wt.DWORD, wt.DWORD, wt.BOOL)
_sig(_u32.SetWindowDisplayAffinity, wt.BOOL, wt.HWND, wt.DWORD)
_sig(_u32.MonitorFromPoint, wt.HMONITOR, wt.POINT, wt.DWORD)
_sig(_u32.GetMonitorInfoW, wt.BOOL, wt.HMONITOR, ctypes.POINTER(_MI))
_sig(_u32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_sig(_u32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_sig(_u32.GetLastInputInfo, wt.BOOL, ctypes.POINTER(_LII))
_sig(_u32.RegisterHotKey, wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
_sig(_u32.GetMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
_sig(_k32.GetCurrentThreadId, wt.DWORD)

HWND_TOPMOST = wt.HWND(-1)
VK_F20 = 0x83
F20_HOLD_S = 0.35              # holding F20 this long = talk; a shorter tap opens / closes the chat
EDGE_DWELL_S = 0.10            # mouse pressed against the top-center edge this long drops the pill
EDGE_HALF_W = 170              # half-width of that edge zone (scaled)
HOVER_EXPAND_S = 0.15          # hover the pill this long to see the latest messages
RETRACT_S = 0.08               # the dropped pill slides away this soon after the mouse leaves
PEEK_CLICK_GUARD_S = 0.25      # ignore clicks right after the pill drops (aimed at a browser tab)

ICON = dict(gear="", close="", send="", mic="", copy="", stop="",
            down="", add="", folder="", check="", cancel="", up="")


def _primary():
    mi = _MI(); mi.cbSize = ctypes.sizeof(_MI)
    _u32.GetMonitorInfoW(_u32.MonitorFromPoint(wt.POINT(0, 0), 1), ctypes.byref(mi))
    return mi.rcMonitor, mi.rcWork


def _cursor():
    pt = wt.POINT(); _u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _last_input():
    li = _LII(ctypes.sizeof(_LII), 0); _u32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime


def _down(vk) -> bool:
    return bool(_u32.GetAsyncKeyState(vk) & 0x8000)


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_in(t):
    return t * t * t


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    a, b = _hex(a), _hex(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * max(0.0, min(1.0, t))) for x, y in zip(a, b))


def _rrect_pts(x0, y0, x1, y1, r):
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


def _rrect(cv, x0, y0, x1, y1, r, **kw):
    kw.setdefault("outline", "")
    return cv.create_polygon(_rrect_pts(x0, y0, x1, y1, r), smooth=True, splinesteps=10, **kw)


def _clock(t):
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


# ---------------------------------------------------------------- tiny markdown (bold, code, bullets)

_INLINE = re.compile(r"(\*\*[^*\n]+?\*\*|__[^_\n]+?__|`[^`\n]+?`)")


def _inline(s, base="n"):
    runs = []
    for part in _INLINE.split(s):
        if not part:
            continue
        if len(part) > 4 and part[:2] in ("**", "__") and part[-2:] == part[:2]:
            runs.append((part[2:-2], "b" if base == "n" else base))
        elif len(part) > 2 and part[0] == "`" and part[-1] == "`":
            runs.append((part[1:-1], "c"))
        else:
            runs.append((part, base))
    return runs


def parse_md(text):
    """-> blocks: (kind, bullet, runs, level). kind: p, li, h, code, gap. Run styles: n b c m h."""
    blocks, code = [], False
    for line in (text or "").replace("\r", "").split("\n"):
        if line.strip().startswith("```"):
            code = not code
            continue
        if code:
            blocks.append(("code", None, [(line.expandtabs(4) or " ", "m")], 0))
            continue
        if not line.strip():
            if blocks and blocks[-1][0] != "gap":
                blocks.append(("gap", None, [], 0))
            continue
        m = re.match(r"^(\s*)([-*•]|\d{1,3}[.)])\s+(.*)$", line)
        if m:
            bullet = "•" if m.group(2) in ("-", "*", "•") else m.group(2)
            blocks.append(("li", bullet, _inline(m.group(3)), min(3, len(m.group(1).expandtabs(4)) // 2)))
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            blocks.append(("h", None, _inline(m.group(1), "h"), 0))
            continue
        blocks.append(("p", None, _inline(line.strip()), 0))
    while blocks and blocks[-1][0] == "gap":
        blocks.pop()
    return blocks


def plain(text):
    """Markdown-free one-liner (pill preview)."""
    t = re.sub(r"\*\*|__|`+|^#+\s*", "", text or "", flags=re.M)
    t = re.sub(r"(?m)^\s*[-*•]\s+", "", t)
    return re.sub(r"\s+", " ", t).strip()


class _Block:
    __slots__ = ("kind", "tag", "y", "h", "off", "t0", "fades", "msg", "extra")

    def __init__(self, kind, tag, h, msg=None):
        self.kind, self.tag, self.h, self.msg = kind, tag, h, msg
        self.y, self.off, self.t0, self.fades, self.extra = 0, 0.0, None, [], {}


# ================================================================= the HUD

class Hud:
    BG, FG, SUB, MUTED = "#18181b", "#fafafa", "#a1a1aa", "#71717a"
    BORDER, PANEL, HOVER, FOCUS = "#2c2c33", "#222226", "#303038", "#4a4a55"
    USER_BG, BOT_BG, INPUT_BG = "#2b3650", "#232327", "#232327"
    ACCENT, BLUE, RED, GREEN = "#d97757", "#5b8cff", "#f87171", "#34d399"
    CODE_FG, CODE_BG = "#f2b880", "#121215"
    LISTEN_BG, LISTEN_EDGE = "#1a2238", "#2f4580"
    COLORS = {"listen": "#5b8cff", "think": "#f5a524", "act": "#a78bfa", "done": "#34d399",
              "error": "#f87171", "ask": "#5b8cff", "idle": "#d97757"}
    PULSING = {"listen", "think", "act"}
    ALPHA = 0.97

    def __init__(self, log=print, chatlog=None):
        self.log, self.chatlog = log, chatlog
        self.on_action = lambda action: None
        self.q: "queue.Queue" = queue.Queue()
        self.mic_level, self.prev_fg, self.chat_open, self.visible = 0.0, None, False, False

        self.root = tk.Tk(); self.root.withdraw()
        self.S = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        fams = set(tkfont.families(self.root))
        icons = "Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else "Segoe MDL2 Assets"
        F = lambda fam, size: tkfont.Font(self.root, family=fam, size=size)
        self.f_msg, self.f_bold = F("Segoe UI", 11), F("Segoe UI Semibold", 11)
        self.f_mono, self.f_h = F("Consolas", 10), F("Segoe UI Semibold", 12)
        self.f_meta, self.f_small, self.f_small_b = F("Segoe UI", 8), F("Segoe UI", 9), F("Segoe UI Semibold", 9)
        self.f_head, self.f_empty = F("Segoe UI Semibold", 12), F("Segoe UI Semibold", 15)
        self.f_icon, self.f_icon_s, self.f_icon_m = F(icons, 11), F(icons, 8), F(icons, 10)
        self.lh = self.f_msg.metrics("linespace")

        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True); w.attributes("-topmost", True); w.attributes("-alpha", 0.0)
        w.configure(bg=self.BG)
        w.geometry("10x10+-20000+-20000")
        self.CW, self.CH = self._chat_size()
        self._build_pill(); self._build_preview(); self._build_chat()
        w.update_idletasks(); w.update()
        self.hwnd = _u32.GetParent(w.winfo_id())
        ex = _u32.GetWindowLongW(self.hwnd, -20)
        _u32.SetWindowLongW(self.hwnd, -20, ex | 0x08000000 | 0x80 | 0x8)   # NOACTIVATE|TOOLWINDOW|TOPMOST
        try:
            pref = ctypes.c_int(2)                                           # Windows 11 rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(self.hwnd), 33, ctypes.byref(pref), 4)
        except Exception:
            pass
        _u32.ShowWindow(self.hwnd, 0)

        self.mode = "hidden"                    # hidden | pill | chat
        self.geo, self.alpha, self.anim = (-20000, -20000, 10, 10), 0.0, None
        self.status = None                      # latest (state, title, detail, kind) from the listener
        self.peek, self.peek_t0, self.expanded = False, 0.0, False
        self.hover_t0 = self.out_t0 = self.edge_t0 = self.leave_t0 = None
        self.hide_at, self.lingering, self.linger_start = None, False, 0.0
        self.esc_was_down, self.f20_poll, self.f20_was = False, False, (False, 0.0)
        self.listening, self.listen_text, self.chip_kind = False, "", None
        self.speaking_now, self.mic_reason, self.menu_open = False, None, False
        self.indicator, self.toast_until, self.inp_focused = None, 0.0, False
        self.msgs, self.blocks, self.content_h, self.view_h = [], [], 0, max(1, self.CH // 2)
        self.sy = self.sy_target = 0.0
        self.sb_seen, self.sb_drag = 0.0, None
        self.prev_loaded, self.sent, self.sent_i = None, [], None
        self.err_t = 0.0
        self._activity_baseline()
        if chatlog is not None:
            try:
                self.msgs = list(chatlog.messages())
            except Exception as e:
                self.log(f"HUD: couldn't load the conversation: {e!r}")
        self._render_all()
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        self.root.after(30, self._tick)

    def px(self, v):
        return int(round(v * self.S))

    def _chat_size(self):
        mon, work = _primary()
        ww, wh = work.right - work.left, work.bottom - work.top
        return int(min(self.px(520), ww * 0.92)), int(min(self.px(640), wh * 0.82))

    # ============================================================ thread-safe API
    def show(self, state, title, detail="", hide_after=None, kind=None):
        self.q.put(("show", state, title, detail, hide_after, kind))

    def hide(self):
        self.q.put(("hide",))

    def hide_after(self, seconds):
        self.q.put(("hide_after", seconds))

    def linger(self, seconds):
        """After a reply: stay until `seconds` pass (None = no limit) or the user moves on."""
        self.q.put(("linger", seconds))

    def chat_add(self, msg: dict):
        self.q.put(("add", dict(msg)))

    def chat_reset(self, manual=False):
        self.q.put(("reset", manual))

    def speaking(self, on: bool):
        self.q.put(("speaking", bool(on)))

    def mic_state(self, reason):
        self.q.put(("mic", reason))

    def toast(self, text, seconds=2.4):
        self.q.put(("toast", text, seconds))

    def quit(self):
        self.q.put(("quit",))

    @contextlib.contextmanager
    def capture_hidden(self):
        """Keep the HUD out of screenshots taken inside this block (it stays visible on screen)."""
        if not self.visible:
            yield
            return
        ev = threading.Event()
        self.q.put(("affinity", 0x11, ev))           # WDA_EXCLUDEFROMCAPTURE
        ev.wait(0.3); time.sleep(0.05)               # one composition frame
        try:
            yield
        finally:
            self.q.put(("affinity", 0, None))

    # ============================================================ building: pill + preview
    def _build_pill(self):
        fr = self.pill_fr = tk.Frame(self.win, bg=self.BG, padx=20, pady=13)
        self.dot = tk.Canvas(fr, width=18, height=18, bg=self.BG, highlightthickness=0)
        self.dot.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 13), pady=(3, 0))
        self.dot_id = self.dot.create_oval(3, 3, 15, 15, fill=self.COLORS["listen"], outline="")
        self.title = tk.Label(fr, font=("Segoe UI Semibold", 12), fg=self.FG, bg=self.BG, anchor="w")
        self.title.grid(row=0, column=1, sticky="w")
        self.detail = tk.Label(fr, font=("Segoe UI", 11), fg=self.SUB, bg=self.BG, anchor="w",
                               justify="left", wraplength=600)
        self.detail.grid(row=1, column=1, sticky="w")
        fr.grid_columnconfigure(1, minsize=330, weight=1)
        self.btns = tk.Frame(fr, bg=self.BG)
        self.btns.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(14, 0))
        for text, color, action in (("✓", "#34d399", "finish"), ("✕", "#f87171", "cancel")):
            b = tk.Label(self.btns, text=text, font=("Segoe UI Symbol", 13, "bold"), fg=color, bg="#27272a",
                         width=2, cursor="hand2", padx=4, pady=1)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Enter>", lambda e, b=b: b.config(bg="#3f3f46"))
            b.bind("<Leave>", lambda e, b=b: b.config(bg="#27272a"))
            b.bind("<Button-1>", lambda e, a=action: (self._act(a), "break")[1])
        self.btns.grid_remove()
        for widget in (fr, self.title, self.detail, self.dot):
            widget.bind("<Button-1>", self._pill_click)
            widget.configure(cursor="hand2")

    def _build_preview(self):
        p = self.prev_fr = tk.Frame(self.win, bg=self.BG, cursor="hand2")
        tk.Frame(p, bg=self.BORDER, height=1).pack(fill="x", padx=16)
        self.prev_rows = tk.Frame(p, bg=self.BG, cursor="hand2")
        self.prev_rows.pack(fill="x", padx=20, pady=(10, 2))
        self.prev_foot = tk.Label(p, text="Click to open the chat  ·  F20", font=self.f_meta, fg=self.MUTED,
                                  bg=self.BG, cursor="hand2")
        self.prev_foot.pack(pady=(4, 12))
        for wdg in (p, self.prev_rows, self.prev_foot):
            wdg.bind("<Button-1>", self._pill_click)

    def _fill_preview(self, width):
        for c in self.prev_rows.winfo_children():
            c.destroy()
        last = [m for m in self.msgs if m.get("role") in ("user", "assistant")][-3:]
        wrap = max(120, width - 40 - self.px(56))
        avg = max(1.0, self.f_small.measure("abcdefghijklmnopqrstuvwxyz ") / 27)
        limit = max(20, int(2 * wrap / avg) - 4)
        if not last:
            lbl = tk.Label(self.prev_rows, text="No messages yet. Click to start a chat.", font=self.f_small,
                           fg=self.SUB, bg=self.BG, cursor="hand2")
            lbl.pack(anchor="w"); lbl.bind("<Button-1>", self._pill_click)
            return
        for m in last:
            row = tk.Frame(self.prev_rows, bg=self.BG, cursor="hand2")
            row.pack(fill="x", pady=(0, self.px(5)))
            you = m.get("role") == "user"
            who = tk.Label(row, text="You" if you else "Claude", font=self.f_small_b, width=6, anchor="nw",
                           fg=self.BLUE if you else self.ACCENT, bg=self.BG, cursor="hand2")
            who.pack(side="left", anchor="n")
            s = plain(m.get("text", ""))
            if len(s) > limit:
                s = s[:limit].rsplit(" ", 1)[0] + "…"
            txt = tk.Label(row, text=s, font=self.f_small, fg=self.FG if you else self.SUB, bg=self.BG,
                           justify="left", anchor="w", wraplength=wrap, cursor="hand2")
            txt.pack(side="left", fill="x")
            for wdg in (row, who, txt):
                wdg.bind("<Button-1>", self._pill_click)

    # ============================================================ building: chat window
    def _icon_btn(self, parent, glyph, fn, fg=None, font=None):
        b = tk.Label(parent, text=glyph, font=font or self.f_icon, fg=fg or self.SUB, bg=self.BG, cursor="hand2",
                     padx=self.px(8), pady=self.px(5))
        b.bind("<Enter>", lambda e: b.config(bg=self.HOVER, fg=self.FG))
        b.bind("<Leave>", lambda e: b.config(bg=self.BG, fg=fg or self.SUB))
        b.bind("<Button-1>", lambda e: (fn(), "break")[1])
        return b

    def _build_chat(self):
        px = self.px
        c = self.chat_fr = tk.Frame(self.win, bg=self.BG)
        # header: dot, title, status · stop-speaking, settings, close
        h = self.header = tk.Frame(c, bg=self.BG, height=px(50))
        h.pack(side="top", fill="x"); h.pack_propagate(False)
        self.hdot = tk.Canvas(h, width=px(12), height=px(12), bg=self.BG, highlightthickness=0)
        self.hdot.pack(side="left", padx=(px(18), px(10)))
        self.hdot_id = self.hdot.create_oval(px(1), px(1), px(11), px(11), fill=self.ACCENT, outline="")
        tk.Label(h, text="Hey Claude", font=self.f_head, fg=self.FG, bg=self.BG).pack(side="left")
        self.hstatus = tk.Label(h, text="Ready", font=self.f_small, fg=self.SUB, bg=self.BG)
        self.hstatus.pack(side="left", padx=(px(10), 0), pady=(px(2), 0))
        self.b_close = self._icon_btn(h, ICON["close"], self.close_chat)
        self.b_close.pack(side="right", padx=(px(2), px(10)))
        self.b_gear = self._icon_btn(h, ICON["gear"], self._toggle_menu)
        self.b_gear.pack(side="right")
        self.b_hush = self._icon_btn(h, ICON["stop"], lambda: self._act("hush"), font=self.f_icon_m)
        tk.Frame(c, bg=self.BORDER, height=1).pack(side="top", fill="x")

        # composer (bottom): follow-up chip, text box / listening panel
        co = self.comp_outer = tk.Frame(c, bg=self.BG)
        co.pack(side="bottom", fill="x", padx=px(14), pady=(px(6), px(14)))
        self.chip = tk.Frame(co, bg=self.BG)
        self.chip_dot = tk.Canvas(self.chip, width=px(8), height=px(8), bg=self.BG, highlightthickness=0)
        self.chip_dot.create_oval(0, 0, px(8), px(8), fill=self.BLUE, outline="")
        self.chip_dot.pack(side="left", padx=(px(4), px(8)))
        self.chip_lbl = tk.Label(self.chip, font=self.f_small, fg=self.BLUE, bg=self.BG)
        self.chip_lbl.pack(side="left")
        self.chip_x = tk.Label(self.chip, text="Cancel", font=self.f_small, fg=self.MUTED, bg=self.BG, cursor="hand2")
        self.chip_x.pack(side="left", padx=(px(12), 0))
        self.chip_x.bind("<Button-1>", lambda e: self._act("cancel"))
        self.chip_x.bind("<Enter>", lambda e: self.chip_x.config(fg=self.FG))
        self.chip_x.bind("<Leave>", lambda e: self.chip_x.config(fg=self.MUTED))

        self.comp = tk.Canvas(co, bg=self.BG, highlightthickness=0, height=px(48))
        self.comp.pack(fill="x")
        self.inp = tk.Text(self.comp, font=self.f_msg, bg=self.INPUT_BG, fg=self.FG, insertbackground=self.FG,
                           insertwidth=max(1, px(1)), relief="flat", bd=0, highlightthickness=0, wrap="word",
                           height=1, undo=True, maxundo=-1, padx=0, pady=0, selectbackground="#3d4f7a",
                           selectforeground=self.FG, exportselection=True)
        self.comp_win = self.comp.create_window(0, 0, anchor="nw", window=self.inp)
        self.ph = tk.Label(self.comp, text="Message Claude…", font=self.f_msg, fg=self.MUTED, bg=self.INPUT_BG,
                           cursor="xterm", padx=0, pady=0, bd=0)
        self.ph.bind("<Button-1>", lambda e: self.inp.focus_set())
        self.ph_win = self.comp.create_window(0, 0, anchor="nw", window=self.ph)
        self.comp.bind("<Configure>", lambda e: self._comp_layout())
        self.comp.tag_bind("send", "<Button-1>", lambda e: self._send())
        self.comp.tag_bind("mic", "<Button-1>", lambda e: self._act("mic"))
        for t in ("send", "mic"):
            self.comp.tag_bind(t, "<Enter>", lambda e, t=t: self._comp_hover(t, True))
            self.comp.tag_bind(t, "<Leave>", lambda e, t=t: self._comp_hover(t, False))
        self.comp_hover = None
        i = self.inp
        i.bind("<Return>", lambda e: (self._send(), "break")[1])
        i.bind("<Shift-Return>", lambda e: None)
        i.bind("<KP_Enter>", lambda e: (self._send(), "break")[1])
        i.bind("<Escape>", lambda e: (self._escape(), "break")[1])
        i.bind("<Up>", self._recall_up)
        i.bind("<Down>", self._recall_down)
        i.bind("<KeyRelease>", lambda e: self._inp_changed())
        i.bind("<<Paste>>", lambda e: self.root.after(1, self._inp_changed))
        i.bind("<<Cut>>", lambda e: self.root.after(1, self._inp_changed))
        i.bind("<FocusIn>", lambda e: self._set_inp_focus(True))
        i.bind("<FocusOut>", lambda e: self._set_inp_focus(False))
        self.inp_lines = 1

        self.lpan = tk.Canvas(co, bg=self.BG, highlightthickness=0, height=px(64))
        self.lpan.bind("<Configure>", lambda e: self._lpan_build())
        self.lpan.tag_bind("ok", "<Button-1>", lambda e: self._act("finish"))
        self.lpan.tag_bind("no", "<Button-1>", lambda e: self._act("cancel"))
        for t in ("ok", "no"):
            self.lpan.tag_bind(t, "<Enter>", lambda e: self.lpan.config(cursor="hand2"))
            self.lpan.tag_bind(t, "<Leave>", lambda e: self.lpan.config(cursor=""))
        self.bars, self.bar_lvl = [], 0.0

        # transcript (fills the middle)
        b = self.body = tk.Frame(c, bg=self.BG)
        b.pack(side="top", fill="both", expand=True)
        self.tcv = tk.Canvas(b, bg=self.BG, highlightthickness=0, bd=0, yscrollincrement=1)
        self.tcv.pack(fill="both", expand=True)
        self.tcv.bind("<Configure>", self._tcv_configure)
        self.tcv.bind("<Button-1>", lambda e: self._close_menu())
        self.sb = tk.Canvas(b, width=px(6), bg=self.BG, highlightthickness=0, bd=0)
        self.sb.place(relx=1.0, x=-px(5), y=px(6), relheight=1.0, height=-px(12), anchor="ne")
        self.sb_thumb = _rrect(self.sb, 0, 0, px(6), px(30), px(3), fill=self.BG)
        self.sb.bind("<Button-1>", self._sb_press)
        self.sb.bind("<B1-Motion>", self._sb_drag)
        self.sb.bind("<ButtonRelease-1>", lambda e: setattr(self, "sb_drag", None))
        self.jump = tk.Label(b, text=ICON["down"], font=self.f_icon_m, fg=self.FG, bg=self.HOVER,
                             padx=px(9), pady=px(6), cursor="hand2")
        self.jump.bind("<Button-1>", lambda e: self._scroll_bottom())
        self.jump_shown = False
        self.toast_lbl = tk.Label(b, font=self.f_small, fg=self.FG, bg="#2a2a31", padx=px(14), pady=px(6))
        self.root.bind_all("<MouseWheel>", self._wheel, add="+")

        # settings menu (gear)
        m = self.menu = tk.Frame(c, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER,
                                 highlightcolor=self.BORDER)
        for icon, text, fn in ((ICON["add"], "New conversation", lambda: (self._close_menu(), self._act("new"))),
                               (ICON["folder"], "Open conversation logs", self._open_logs)):
            row = tk.Frame(m, bg=self.PANEL, cursor="hand2")
            row.pack(fill="x")
            a = tk.Label(row, text=icon, font=self.f_icon_m, fg=self.SUB, bg=self.PANEL, padx=px(12), pady=px(9))
            a.pack(side="left")
            t = tk.Label(row, text=text, font=self.f_small, fg=self.FG, bg=self.PANEL, padx=0, pady=px(9))
            t.pack(side="left", padx=(0, px(18)))
            parts = (row, a, t)
            for wdg in parts:
                wdg.bind("<Enter>", lambda e, ps=parts: [p.config(bg=self.HOVER) for p in ps])
                wdg.bind("<Leave>", lambda e, ps=parts: [p.config(bg=self.PANEL) for p in ps])
                wdg.bind("<Button-1>", lambda e, fn=fn: (fn(), "break")[1])
        for wdg in (self.header, self.hstatus):
            wdg.bind("<Button-1>", lambda e: self._close_menu())

    # ============================================================ composer
    def _inp_text(self):
        return self.inp.get("1.0", "end-1c")

    def _inp_changed(self):
        try:
            n = self.inp.count("1.0", "end", "displaylines")
            n = n[0] if isinstance(n, tuple) else (n or 1)
        except Exception:
            n = int(self.inp.index("end-1c").split(".")[0])
        empty = not self._inp_text()
        if empty:
            self.comp.itemconfigure(self.ph_win, state="normal"); self.ph.lift()
        else:
            self.comp.itemconfigure(self.ph_win, state="hidden")
        n = max(1, min(6, n))
        if n != self.inp_lines:
            self.inp_lines = n
            self._comp_layout()
        else:
            self._comp_paint()

    def _set_inp_focus(self, on):
        self.inp_focused = on
        self._comp_paint()

    def _comp_hover(self, tag, on):
        # only recolor here: recreating the item under the mouse would fire Leave/Enter forever
        self.comp_hover = tag if on else None
        self.comp.config(cursor="hand2" if on else "")
        self._comp_paint()

    def _comp_paint(self):
        ids = getattr(self, "comp_ids", None)
        if not ids:
            return
        cv = self.comp
        has = bool(self._inp_text().strip())
        off = self.mic_reason not in (None, "on battery")
        cv.itemconfigure(ids["box"], outline=self.FOCUS if self.inp_focused else self.BORDER)
        fill = self.ACCENT if has else self.HOVER
        if has and self.comp_hover == "send":
            fill = "#e08a6c"
        cv.itemconfigure(ids["send_bg"], fill=fill)
        cv.itemconfigure(ids["send"], fill=self.FG if has else self.MUTED)
        hov = self.comp_hover == "mic" and not off
        cv.itemconfigure(ids["mic_bg"], fill=self.HOVER if hov else self.INPUT_BG)
        cv.itemconfigure(ids["mic"], fill=self.MUTED if off else (self.FG if hov else self.SUB))

    def _comp_layout(self):
        px, cv = self.px, self.comp
        cw = cv.winfo_width()
        if cw < 50:
            cw = self.CW - 2 * px(14)
        th = self.inp_lines * self.lh
        pad_y, pad_x, btn = px(11), px(16), px(32)
        h = th + 2 * pad_y
        if int(float(cv.cget("height"))) != h:
            cv.configure(height=h)
        cv.delete("deco")
        text_w = max(50, cw - pad_x - 2 * btn - px(22))
        cv.coords(self.comp_win, pad_x, pad_y)
        cv.itemconfigure(self.comp_win, width=text_w, height=th)
        cv.coords(self.ph_win, pad_x + px(2), pad_y)
        cy = h / 2 if self.inp_lines == 1 else h - pad_y - self.lh / 2
        sx = cw - px(8) - btn / 2
        mx = sx - btn - px(2)
        r = btn / 2
        self.comp_ids = {
            "box": _rrect(cv, 1, 1, cw - 1, h - 1, px(18), fill=self.INPUT_BG, outline=self.BORDER, width=1,
                          tags="deco"),
            "send_bg": cv.create_oval(sx - r, cy - r, sx + r, cy + r, outline="", tags=("deco", "send")),
            "send": cv.create_text(sx, cy, text=ICON["send"], font=self.f_icon_m, tags=("deco", "send")),
            "mic_bg": cv.create_oval(mx - r, cy - r, mx + r, cy + r, outline="", tags=("deco", "mic")),
            "mic": cv.create_text(mx, cy, text=ICON["mic"], font=self.f_icon_m, tags=("deco", "mic")),
        }
        self._comp_paint()

    def _send(self):
        text = self._inp_text().strip()
        if not text:
            return
        self.inp.delete("1.0", "end"); self.inp.edit_reset()
        self.sent.append(text); self.sent = self.sent[-50:]; self.sent_i = None
        self._inp_changed()
        self._add_msg({"role": "user", "text": text, "source": "typed", "t": time.time()}, mine=True)
        self._act(("send", text))

    def _recall_up(self, e):
        if not self.sent or (self._inp_text() and self.sent_i is None):
            return None
        if self.inp.compare("insert", ">", "1.end"):
            return None                                # moving up inside a multi-line draft
        self.sent_i = len(self.sent) - 1 if self.sent_i is None else max(0, self.sent_i - 1)
        self.inp.delete("1.0", "end"); self.inp.insert("1.0", self.sent[self.sent_i])
        self._inp_changed()
        return "break"

    def _recall_down(self, e):
        if self.sent_i is None:
            return None
        self.sent_i += 1
        self.inp.delete("1.0", "end")
        if self.sent_i < len(self.sent):
            self.inp.insert("1.0", self.sent[self.sent_i])
        else:
            self.sent_i = None
        self._inp_changed()
        return "break"

    def _escape(self):
        if self.menu_open:
            self._close_menu()
        else:
            self.close_chat()

    # ============================================================ listening panel / chip
    def _set_listening(self, on, text=""):
        text = (text or "").strip()
        if on:
            changed = text != self.listen_text or not self.listening
            self.listen_text = text
            if not self.listening:
                self.listening = True
                self.comp.pack_forget(); self.chip.pack_forget()
                self.lpan.pack(fill="x")
            if changed:
                self._lpan_build()
        elif self.listening:
            self.listening = False
            self.lpan.pack_forget()
            self.comp.pack(fill="x")
            self._comp_layout()
            if self.chat_open and _u32.GetForegroundWindow() == self.hwnd:
                self.inp.focus_set()

    def _set_chip(self, kind):
        if kind == self.chip_kind:
            return
        self.chip_kind = kind
        if kind and not self.listening:
            self.chip_lbl.config(text="Listening for your answer…" if kind == "answer" else "Listening for a follow-up…")
            self.chip.pack(side="top", fill="x", pady=(0, self.px(8)), before=self.comp)
        else:
            self.chip.pack_forget()

    def _lpan_build(self):
        if not self.listening:
            return
        px, cv = self.px, self.lpan
        cw = cv.winfo_width()
        if cw < 50:
            cw = self.CW - 2 * px(14)
        btn = px(30)
        text_x = px(70)
        wrap = max(80, cw - text_x - 2 * btn - px(34))
        body = self.listen_text or "Go ahead, I'm listening."
        lines = max(1, min(3, math.ceil(self.f_msg.measure(body) / wrap + 0.15)))
        h = max(px(64), px(30) + px(16) + lines * self.lh)
        cv.configure(height=h)
        cv.delete("all")
        _rrect(cv, 1, 1, cw - 1, h - 1, px(18), fill=self.LISTEN_BG, outline=self.LISTEN_EDGE, width=1)
        cv.create_text(text_x, px(12), text="Listening…", font=self.f_small_b, fill=self.BLUE, anchor="nw")
        cv.create_text(text_x, px(12) + self.f_small_b.metrics("linespace") + px(2), text=body, width=wrap,
                       font=self.f_msg, fill=self.FG if self.listen_text else self.SUB, anchor="nw")
        cy = h / 2
        ox, nx = cw - px(14) - btn * 1.5 - px(6), cw - px(14) - btn / 2
        r = btn / 2
        cv.create_oval(ox - r, cy - r, ox + r, cy + r, fill="#203528", outline="", tags="ok")
        cv.create_text(ox, cy, text=ICON["check"], font=self.f_icon_m, fill=self.GREEN, tags="ok")
        cv.create_oval(nx - r, cy - r, nx + r, cy + r, fill="#3a2024", outline="", tags="no")
        cv.create_text(nx, cy, text=ICON["cancel"], font=self.f_icon_s, fill=self.RED, tags="no")
        self.bars = []
        for k in range(5):
            x = px(22) + k * px(7)
            self.bars.append(cv.create_line(x, cy - 2, x, cy + 2, width=px(4), capstyle="round", fill=self.BLUE))
        self.bar_cy = cy

    def _animate_bars(self, now):
        if not self.bars:
            return
        rms = max(1e-5, float(self.mic_level or 0.0))
        target = max(0.0, min(1.0, (20 * math.log10(rms) + 52) / 34))
        self.bar_lvl += (target - self.bar_lvl) * 0.35
        px = self.px
        for k, item in enumerate(self.bars):
            shape = (0.55, 0.85, 1.0, 0.8, 0.6)[k]
            wob = 0.55 + 0.45 * math.sin(now * 9.0 + k * 1.3)
            hh = px(3) + px(17) * shape * (0.18 + 0.82 * self.bar_lvl) * wob
            x = px(22) + k * px(7)
            self.lpan.coords(item, x, self.bar_cy - hh / 2, x, self.bar_cy + hh / 2)

    # ============================================================ transcript: layout
    def _fonts(self, style):
        return {"n": self.f_msg, "b": self.f_bold, "c": self.f_mono, "m": self.f_mono, "h": self.f_h}[style]

    def _layout(self, text, maxw, md=True):
        """Word-wrap tiny-markdown text (md=False: exactly as typed).
        -> (runs [(x, y, text, style)], used width, height, code rows)."""
        out, codes = [], []
        y, used, first = 0.0, 0, True
        sp = self.f_msg.measure(" ")
        if md:
            blocks = parse_md(text)
        else:
            blocks = [("p", None, [(ln, "n")], 0) if ln.strip() else ("gap", None, [], 0)
                      for ln in (text or "").replace("\r", "").split("\n")]
        for kind, bullet, runs, level in blocks:
            if kind == "gap":
                y += self.lh * 0.5
                continue
            line_h = self.f_h.metrics("linespace") if kind == "h" else self.lh
            if kind == "code":
                line_h = self.f_mono.metrics("linespace")
            if kind == "h" and not first:
                y += self.lh * 0.2
            first = False
            x0 = 0
            if kind == "li":
                x0 = level * self.px(14)
                out.append((x0, y, bullet, "n"))
                x0 += max(self.f_msg.measure(bullet) + sp * 2, self.px(16))
            x, cur, top = x0, None, y

            def flush():
                nonlocal cur, used
                if cur and cur[2].strip():
                    t = cur[2].rstrip() if kind != "code" else cur[2]
                    out.append((cur[0], cur[1], t, cur[3]))
                    used = max(used, cur[0] + self._fonts(cur[3]).measure(t))
                cur = None

            for text_run, style in runs:
                f = self._fonts(style)
                for tok in re.findall(r"\S+|\s+", text_run):
                    if tok.isspace():
                        if x == x0 and kind != "code":
                            continue
                        tok = tok.replace("\t", "    ")
                        wdt = f.measure(tok)
                        if cur and cur[3] == style:
                            cur[2] += tok
                        else:
                            flush(); cur = [x, y, tok, style]
                        x += wdt
                        continue
                    wdt = f.measure(tok)
                    if x + wdt > maxw and x > x0:
                        flush(); y += line_h; x = x0
                    while wdt > maxw - x0 and len(tok) > 1:    # one very long word: break it
                        n = len(tok)
                        while n > 1 and f.measure(tok[:n]) > maxw - x:
                            n -= 1
                        flush(); out.append((x, y, tok[:n], style))
                        used = max(used, x + f.measure(tok[:n]))
                        tok = tok[n:]; wdt = f.measure(tok); y += line_h; x = x0
                    if cur and cur[3] == style:
                        cur[2] += tok
                    else:
                        flush(); cur = [x, y, tok, style]
                    x += wdt
            flush()
            y += line_h
            if kind == "code":
                codes.append((top, y))
        return out, max(used, 1), max(y, self.lh), codes

    def _tag(self):
        self._tagn = getattr(self, "_tagn", 0) + 1
        return f"b{self._tagn}"

    def _side(self):
        return self.px(18)

    def _draw_msg(self, m, y):
        """Draw one message at y. -> _Block."""
        px, cv, role = self.px, self.tcv, m.get("role")
        tag = self._tag()
        if role in ("note", "divider"):
            text = m.get("text", "")
            if role == "divider":
                fh = self.f_small.metrics("linespace")
                cy = y + fh / 2 + px(6)
                tw = self.f_small.measure(text) + px(24)
                mid = self.CW / 2
                b = _Block("divider", tag, fh + px(12), m)
                ids = [cv.create_line(self._side() + px(8), cy, mid - tw / 2, cy, fill=self.BORDER, tags=tag),
                       cv.create_line(mid + tw / 2, cy, self.CW - self._side() - px(8), cy, fill=self.BORDER, tags=tag)]
                t = cv.create_text(mid, cy, text=text, font=self.f_small, fill=self.MUTED, tags=tag)
                b.fades = [(ids[0], "fill", self.BORDER), (ids[1], "fill", self.BORDER), (t, "fill", self.MUTED)]
                return b
            fh = self.f_small.metrics("linespace")
            t = cv.create_text(self.CW / 2, y + px(2), text=text, font=self.f_small, fill=self.MUTED, anchor="n",
                               width=self.CW - 4 * self._side(), justify="center", tags=tag)
            bb = cv.bbox(t) or (0, y, 0, y + fh)
            b = _Block("note", tag, (bb[3] - bb[1]) + px(4), m)
            b.fades = [(t, "fill", self.MUTED)]
            return b

        you = role == "user"
        side, padx, pady = self._side(), px(14), px(9)
        maxw = int(min(self.CW * 0.76, self.CW - 2 * side - px(40))) - 2 * padx
        runs, used, th, codes = self._layout(m.get("text", ""), maxw, md=not you)
        bw, bh = used + 2 * padx + 2, th + 2 * pady
        if you:
            x1 = self.CW - side; x0 = x1 - bw
        else:
            x0 = side; x1 = x0 + bw
        bg = self.USER_BG if you else self.BOT_BG
        b = _Block("msg", tag, 0, m)
        bub = _rrect(cv, x0, y, x1, y + bh, min(px(16), bh / 2), fill=bg, tags=(tag, tag + "bub"))
        b.fades.append((bub, "fill", bg))
        for top, bot in codes:
            r = _rrect(cv, x0 + padx - px(6), y + pady + top - px(2), x1 - padx + px(6), y + pady + bot + px(2),
                       px(6), fill=self.CODE_BG, tags=tag)
            b.fades.append((r, "fill", self.CODE_BG))
        code_dy = (self.lh - self.f_mono.metrics("linespace")) * 0.6     # inline code sits on the text's baseline
        for rx, ry, text, style in runs:
            color = self.CODE_FG if style in ("c", "m") else self.FG
            dy = code_dy if style == "c" else 0
            t = cv.create_text(x0 + padx + rx, y + pady + ry + dy, text=text, font=self._fonts(style), fill=color,
                               anchor="nw", tags=(tag, tag + "txt"))
            b.fades.append((t, "fill", color))
        # meta line: who · model · time  [mic]  [copy]
        my = y + bh + px(4)
        who = "You" if you else "Claude"
        label = MODEL_LABEL.get(m.get("handler", ""), "")
        if m.get("handler") == "skill":
            sk = str(m.get("skill") or "")
            label = "Skill" + (f": {sk[:28]}" if sk else "")
        bits = [who] + ([label] if label else []) + [_clock(m.get("t", time.time()))]
        meta = "  ·  ".join(bits)
        anchor = "ne" if you else "nw"
        mx = x1 - px(4) if you else x0 + px(4)
        t = cv.create_text(mx, my, text=meta, font=self.f_meta, fill=self.MUTED, anchor=anchor, tags=tag)
        b.fades.append((t, "fill", self.MUTED))
        mw = self.f_meta.measure(meta)
        icons_x = (mx - mw - px(8)) if you else (mx + mw + px(8))
        if m.get("source") == "voice":
            ic = cv.create_text(icons_x, my, text=ICON["mic"], font=self.f_icon_s, fill=self.MUTED,
                                anchor=anchor, tags=tag)
            b.fades.append((ic, "fill", self.MUTED))
            icons_x += (-1 if you else 1) * px(16)
        ctag = tag + "copy"
        cp = cv.create_text(icons_x, my, text=ICON["copy"], font=self.f_icon_s, fill=self.MUTED, anchor=anchor,
                            tags=(tag, ctag))
        b.fades.append((cp, "fill", self.MUTED))
        b.extra = {"copy": cp, "meta": t, "meta_text": meta}
        cv.tag_bind(ctag, "<Button-1>", lambda e, blk=b: self._copy(blk))
        cv.tag_bind(ctag, "<Enter>", lambda e, i=cp: (cv.itemconfigure(i, fill=self.FG), cv.config(cursor="hand2")))
        cv.tag_bind(ctag, "<Leave>", lambda e, i=cp: (cv.itemconfigure(i, fill=self.MUTED), cv.config(cursor="")))
        cv.tag_bind(tag + "bub", "<Button-3>", lambda e, blk=b: self._copy(blk))
        cv.tag_bind(tag + "txt", "<Button-3>", lambda e, blk=b: self._copy(blk))
        b.h = bh + px(4) + self.f_meta.metrics("linespace")
        return b

    def _copy(self, blk):
        try:
            self.root.clipboard_clear(); self.root.clipboard_append(blk.msg.get("text", ""))
            self.tcv.itemconfigure(blk.extra["meta"], text="Copied")
            self.root.after(1300, lambda: self.tcv.itemconfigure(blk.extra["meta"], text=blk.extra["meta_text"]))
        except Exception:
            pass

    def _draw_indicator(self, y):
        px, cv = self.px, self.tcv
        title, detail = self.indicator
        tag = self._tag()
        side, padx, pady = self._side(), px(14), px(10)
        maxw = int(self.CW * 0.76) - 2 * padx
        dots_w = px(34)
        detail = (detail or "").strip()
        if detail and self.f_small.measure(detail) > maxw - dots_w:
            while detail and self.f_small.measure(detail + "…") > maxw - dots_w:
                detail = detail[:-1]
            detail += "…"
        tw = max(self.f_small_b.measure(title), self.f_small.measure(detail) if detail else 0)
        lh1 = self.f_small_b.metrics("linespace")
        th = lh1 + (self.f_small.metrics("linespace") if detail else 0)
        bw, bh = dots_w + tw + 2 * padx, th + 2 * pady
        x0 = side
        b = _Block("indicator", tag, 0)
        bub = _rrect(cv, x0, y, x0 + bw, y + bh, min(px(16), bh / 2), fill=self.BOT_BG, tags=tag)
        cy = y + pady + lh1 / 2
        dots = [cv.create_oval(0, 0, 1, 1, fill=self.MUTED, outline="", tags=tag) for _ in range(3)]
        for k, d in enumerate(dots):
            cx = x0 + padx + px(4) + k * px(9)
            cv.coords(d, cx - px(3), cy - px(3), cx + px(3), cy + px(3))
        t1 = cv.create_text(x0 + padx + dots_w, y + pady, text=title, font=self.f_small_b, fill=self.SUB,
                            anchor="nw", tags=tag)
        b.fades = [(bub, "fill", self.BOT_BG), (t1, "fill", self.SUB)]
        if detail:
            t2 = cv.create_text(x0 + padx + dots_w, y + pady + lh1, text=detail, font=self.f_small,
                                fill=self.MUTED, anchor="nw", tags=tag)
            b.fades.append((t2, "fill", self.MUTED))
        # Stop
        sy = y + bh + px(5)
        stag = tag + "stop"
        si = cv.create_text(x0 + px(4), sy, text=ICON["stop"], font=self.f_icon_s, fill=self.MUTED, anchor="nw",
                            tags=(tag, stag))
        st = cv.create_text(x0 + px(20), sy - px(1), text="Stop", font=self.f_meta, fill=self.MUTED, anchor="nw",
                            tags=(tag, stag))
        b.fades += [(si, "fill", self.MUTED), (st, "fill", self.MUTED)]
        cv.tag_bind(stag, "<Button-1>", lambda e: self._act("stop"))
        cv.tag_bind(stag, "<Enter>", lambda e: ([cv.itemconfigure(i, fill=self.FG) for i in (si, st)],
                                                cv.config(cursor="hand2")))
        cv.tag_bind(stag, "<Leave>", lambda e: ([cv.itemconfigure(i, fill=self.MUTED) for i in (si, st)],
                                                cv.config(cursor="")))
        b.extra = {"dots": dots}
        b.h = bh + px(5) + self.f_meta.metrics("linespace")
        return b

    def _draw_empty(self):
        cv, px = self.tcv, self.px
        tag = self._tag()
        mid = self.CW / 2
        cy = max(px(120), self.view_h / 2 - px(30))
        b = _Block("empty", tag, 0)
        ids = [cv.create_text(mid, cy, text="How can I help?", font=self.f_empty, fill=self.FG, tags=tag),
               cv.create_text(mid, cy + px(34), text="Type a message, or say “Hey Claude”.", font=self.f_small,
                              fill=self.SUB, tags=tag),
               cv.create_text(mid, cy + px(56), text="Tap F20 to open this anytime  ·  hold F20 to talk",
                              font=self.f_small, fill=self.MUTED, tags=tag)]
        b.fades = [(ids[0], "fill", self.FG), (ids[1], "fill", self.SUB), (ids[2], "fill", self.MUTED)]
        b.h = 0                                        # doesn't take part in the layout
        return b

    def _draw_prevlink(self, y, path):
        cv, px = self.tcv, self.px
        tag = self._tag()
        when = ""
        try:
            when = time.strftime("%b %d, %I:%M %p", time.strptime(path.stem[:19], "%Y-%m-%d_%H-%M-%S"))
            when = re.sub(r" 0(\d)", r" \1", when)
        except Exception:
            pass
        label = "Show previous conversation" + (f"  ·  {when}" if when else "")
        iw, gap = self.f_icon_s.measure(ICON["up"]), px(8)
        x = (self.CW - (iw + gap + self.f_small.measure(label))) / 2
        b = _Block("prevlink", tag, 0)
        ic = cv.create_text(x, y + px(5), text=ICON["up"], font=self.f_icon_s, fill=self.MUTED,
                            anchor="nw", tags=tag)
        t = cv.create_text(x + iw + gap, y + px(2), text=label, font=self.f_small, fill=self.MUTED,
                           anchor="nw", tags=tag)
        cv.tag_bind(tag, "<Button-1>", lambda e, p=path: self._load_previous(p))
        cv.tag_bind(tag, "<Enter>", lambda e: ([cv.itemconfigure(i, fill=self.FG) for i in (ic, t)],
                                               cv.config(cursor="hand2")))
        cv.tag_bind(tag, "<Leave>", lambda e: ([cv.itemconfigure(i, fill=self.MUTED) for i in (ic, t)],
                                               cv.config(cursor="")))
        b.fades = [(ic, "fill", self.MUTED), (t, "fill", self.MUTED)]
        b.h = self.f_small.metrics("linespace") + px(8)
        return b

    # ============================================================ transcript: model
    def _gap(self, a, b):
        if a is None:
            return self.px(16)
        if a.kind == "msg" and b.kind == "msg" and a.msg.get("role") == b.msg.get("role"):
            return self.px(8)
        return self.px(14)

    def _prev_path(self):
        if self.chatlog is None:
            return None
        try:
            return self.chatlog.previous(before=self.prev_loaded or self.chatlog.path)
        except Exception:
            return None

    def _render_all(self, keep_anchor=False):
        old_h, old_sy = self.content_h, self.sy
        self.tcv.delete("all")
        self.blocks = []
        y = self.px(12)
        prev = self._prev_path()
        if prev is not None:
            b = self._draw_prevlink(y, prev); b.y = y
            self.blocks.append(b); y += b.h
        last = None
        for m in self.msgs:
            g = self._gap(last, _Block("msg", "", 0, m)) if m.get("role") in ("user", "assistant") else self.px(12)
            y += g if last is not None else self.px(4)
            b = self._draw_msg(m, y); b.y = y
            self.blocks.append(b); y += b.h; last = b
        if self.indicator:
            y += self.px(14)
            b = self._draw_indicator(y); b.y = y
            self.blocks.append(b); y += b.h
        if not self.msgs and not self.indicator:
            self.blocks.append(self._draw_empty())
        self.content_h = int(y + self.px(16))
        self._update_region()
        if keep_anchor:                                 # content added above: keep the view where it was
            self.sy = self.sy_target = old_sy + (self.content_h - old_h)
            self._apply_scroll()
        else:
            self._scroll_bottom(instant=True)

    def _drop_empty(self):
        for b in [b for b in self.blocks if b.kind == "empty"]:
            self.tcv.delete(b.tag)
        self.blocks = [b for b in self.blocks if b.kind != "empty"]

    def _append_msg_block(self, m, animate=True):
        """Add a message at the end (the 'working' indicator, if any, always stays last)."""
        ind = None
        if self.blocks and self.blocks[-1].kind == "indicator":
            ind = self.blocks.pop(); self.tcv.delete(ind.tag)
        self._drop_empty()
        last = self.blocks[-1] if self.blocks else None
        y = (last.y + last.h) if last else self.px(12)
        role = m.get("role")
        probe = _Block("msg" if role in ("user", "assistant") else role, "", 0, m)
        y += self._gap(last, probe) if last is not None and last.kind != "prevlink" else self.px(4)
        new = self._draw_msg(m, y); new.y = y
        self.blocks.append(new)
        y += new.h
        if animate:
            self._start_fade(new)
        if self.indicator:
            y += self.px(14)
            b = self._draw_indicator(y); b.y = y
            if ind is None and animate:
                self._start_fade(b)
            self.blocks.append(b); y += b.h
        self.content_h = int(y + self.px(16))
        self._update_region()

    def _start_fade(self, b):
        b.t0 = time.time()
        b.off = self.px(12)
        self.tcv.move(b.tag, 0, b.off)
        for item, opt, target in b.fades:
            self.tcv.itemconfigure(item, **{opt: self.BG})

    def _step_fades(self, now):
        busy = False
        for b in self.blocks:
            if b.t0 is None:
                continue
            t = min(1.0, (now - b.t0) / 0.24)
            e = _ease_out(t)
            off = (1 - e) * self.px(12)
            self.tcv.move(b.tag, 0, off - b.off); b.off = off
            for item, opt, target in b.fades:
                self.tcv.itemconfigure(item, **{opt: _mix(self.BG, target, e)})
            if t >= 1:
                b.t0, b.off = None, 0.0
            else:
                busy = True
        return busy

    def _add_msg(self, m, mine=False):
        if m.get("role") == "assistant" or m.get("role") == "note":
            self.indicator = None
        was_bottom = self._at_bottom()
        self.msgs.append(m)
        self._append_msg_block(m)
        if mine or was_bottom:
            self._scroll_bottom()
        if self.mode == "pill" and self.expanded:
            self._pill_refresh()

    def _set_indicator(self, title, detail):
        new = (title, detail) if title else None
        if new == self.indicator:
            return
        had = self.blocks and self.blocks[-1].kind == "indicator"
        was_bottom = self._at_bottom()
        if had:
            ind = self.blocks.pop(); self.tcv.delete(ind.tag)
        self.indicator = new
        real = [b for b in self.blocks if b.kind != "empty"]
        y = (real[-1].y + real[-1].h) if real else self.px(12)
        if new:
            for b in [b for b in self.blocks if b.kind == "empty"]:
                self.tcv.delete(b.tag)
            self.blocks = [b for b in self.blocks if b.kind != "empty"]
            y += self.px(14)
            b = self._draw_indicator(y); b.y = y
            if not had:
                self._start_fade(b)
            self.blocks.append(b); y += b.h
        elif not self.msgs:
            self.blocks.append(self._draw_empty())
        self.content_h = int(y + self.px(16))
        self._update_region()
        if was_bottom:
            self._scroll_bottom(instant=had)

    def _load_previous(self, path):
        if self.chatlog is None:
            return
        try:
            older = [m for m in self.chatlog.read(path) if m.get("role") in ("user", "assistant", "note")]
        except Exception as e:
            self.log(f"HUD: couldn't read {path}: {e!r}")
            return
        try:
            when = time.strftime("%b %d, %I:%M %p", time.strptime(path.stem[:19], "%Y-%m-%d_%H-%M-%S"))
            when = re.sub(r" 0(\d)", r" \1", when)
        except Exception:
            when = "earlier"
        head = [{"role": "divider", "text": f"Conversation from {when}"}]
        tail = [{"role": "divider", "text": "Later conversation" if self.prev_loaded else "This conversation"}]
        if not older:
            older = [{"role": "note", "text": "(nothing was said in it)"}]
        self.msgs = head + older + tail + self.msgs
        self.prev_loaded = path
        self._render_all(keep_anchor=True)

    # ============================================================ transcript: scrolling
    def _tcv_configure(self, e):
        bottom = self._at_bottom()
        self.view_h = max(1, e.height)
        if any(b.kind == "empty" for b in self.blocks):
            self._render_all()
            return
        self._update_region()
        if bottom:
            self._scroll_bottom(instant=True)

    def _max_scroll(self):
        return max(0, self.content_h - self.view_h)

    def _at_bottom(self):
        return self.sy_target >= self._max_scroll() - self.px(6)

    def _update_region(self):
        self.tcv.configure(scrollregion=(0, 0, self.CW, max(self.content_h, self.view_h)))
        self._apply_scroll()

    def _apply_scroll(self):
        self.sy = max(0.0, min(self.sy, self._max_scroll()))
        self.sy_target = max(0.0, min(self.sy_target, self._max_scroll()))
        self.tcv.yview_moveto(self.sy / max(self.content_h, self.view_h))
        self._draw_scrollbar()

    def _scroll_by(self, dy):
        self.sy_target = max(0.0, min(self.sy_target + dy, self._max_scroll()))
        self.sb_seen = time.time()

    def _scroll_bottom(self, instant=False):
        self.sy_target = self._max_scroll()
        if instant:
            self.sy = self.sy_target
            self._apply_scroll()

    def _step_scroll(self):
        d = self.sy_target - self.sy
        if abs(d) < 0.6:
            if d:
                self.sy = self.sy_target; self._apply_scroll()
            return False
        self.sy += d * 0.26 if abs(d) > 2 else d
        self._apply_scroll()
        return True

    def _wheel(self, e):
        if self.mode != "chat":
            return None
        try:
            w = self.root.winfo_containing(*self.root.winfo_pointerxy())
        except Exception:
            w = None
        if w is None or not str(w).startswith(str(self.body)):
            return None                               # the text box scrolls itself
        self._scroll_by(-e.delta / 120 * self.px(72))
        return "break"

    def _draw_scrollbar(self):
        vh, ch = self.view_h, self.content_h
        track = max(1, self.sb.winfo_height() or vh - self.px(12))
        if ch <= vh + 1:
            self.sb.itemconfigure(self.sb_thumb, fill=self.BG)
            return
        th = max(self.px(30), track * vh / ch)
        top = (track - th) * (self.sy / max(1, ch - vh))
        self.sb.coords(self.sb_thumb, _rrect_pts(0, top, self.px(6), top + th, self.px(3)))
        recent = time.time() - self.sb_seen < 1.2 or self.sb_drag is not None
        self.sb.itemconfigure(self.sb_thumb, fill="#4a4a55" if recent else "#2e2e35")

    def _sb_press(self, e):
        vh, ch = self.view_h, self.content_h
        if ch <= vh:
            return
        track = max(1, self.sb.winfo_height())
        th = max(self.px(30), track * vh / ch)
        top = (track - th) * (self.sy / max(1, ch - vh))
        if top <= e.y <= top + th:
            self.sb_drag = e.y - top
        else:                                          # click on the track: page up / down
            self._scroll_by((vh * 0.9) * (1 if e.y > top else -1))
            self.sb_drag = None

    def _sb_drag(self, e):
        if self.sb_drag is None:
            return
        vh, ch = self.view_h, self.content_h
        track = max(1, self.sb.winfo_height())
        th = max(self.px(30), track * vh / ch)
        frac = (e.y - self.sb_drag) / max(1, track - th)
        self.sy = self.sy_target = max(0.0, min(1.0, frac)) * self._max_scroll()
        self.sb_seen = time.time()
        self._apply_scroll()

    def _update_jump(self):
        show = self._max_scroll() - self.sy_target > self.px(160)
        if show and not self.jump_shown:
            self.jump.place(relx=0.5, rely=1.0, y=-self.px(12), anchor="s"); self.jump.lift()
        elif not show and self.jump_shown:
            self.jump.place_forget()
        self.jump_shown = show

    # ============================================================ menu / toast / misc
    def _toggle_menu(self):
        if self.menu_open:
            self._close_menu()
        else:
            self.menu_open = True
            self.menu.place(x=self.CW - self.px(12), y=self.px(46), anchor="ne")
            self.menu.lift()

    def _close_menu(self):
        if self.menu_open:
            self.menu_open = False
            self.menu.place_forget()

    def _open_logs(self):
        self._close_menu()
        try:
            if self.chatlog is not None:
                os.startfile(str(self.chatlog.dir))
        except Exception as e:
            self.log(f"HUD: couldn't open the logs folder: {e!r}")

    def _toast(self, text, seconds=2.4):
        self.toast_lbl.config(text=text)
        self.toast_lbl.place(relx=0.5, y=self.px(10), anchor="n"); self.toast_lbl.lift()
        self.toast_until = time.time() + seconds

    def _act(self, action):
        try:
            self.on_action(action)
        except Exception as e:
            self.log(f"HUD action {action!r} failed: {e!r}")

    def _header_status(self):
        st = self.status
        if self.speaking_now:
            text, state = "Speaking", "done"
        elif st and st[0] == "listen":
            text, state = "Listening", "listen"
        elif st and st[0] in ("think", "act"):
            text, state = st[1], st[0]
        elif self.mic_reason == "on battery":
            text, state = "Ready  ·  wake word off on battery (hold F20 to talk)", "idle"
        elif self.mic_reason:
            text, state = f"Ready  ·  mic off ({self.mic_reason})", "idle"
        else:
            text, state = "Ready", "idle"
        self.hstatus.config(text=text)
        self.hdot.itemconfigure(self.hdot_id, fill=self.COLORS.get(state, self.ACCENT))
        if self.speaking_now:
            if not self.b_hush.winfo_ismapped():
                self.b_hush.pack(side="right", before=self.b_gear)
        elif self.b_hush.winfo_ismapped():
            self.b_hush.pack_forget()
        self.hdot_state = state

    # ============================================================ geometry + animation
    def _apply_geo(self, g, alpha=None):
        x, y, w, h = (int(round(v)) for v in g)
        self.geo = (x, y, w, h)
        self.win.geometry(f"{max(1, w)}x{max(1, h)}+{x}+{y}")
        if alpha is not None and abs(alpha - self.alpha) > 0.003:
            self.alpha = alpha
            self.win.attributes("-alpha", max(0.0, min(1.0, alpha)))
        self._layout_content()

    def _layout_content(self):
        x, y, w, h = self.geo
        if self.mode == "chat":
            self.chat_fr.place(x=(w - self.CW) // 2, y=0, width=self.CW, height=self.CH)
        else:
            pw, ph, eh = getattr(self, "pill_dims", (w, h, 0))
            self.pill_fr.place(x=(w - pw) // 2, y=0, width=pw, height=ph)
            if self.expanded:
                self.prev_fr.place(x=(w - pw) // 2, y=ph, width=pw, height=eh)

    def _animate(self, to, alpha, dur, ease=_ease_out, done=None):
        self.anim = {"a": self.geo, "aa": self.alpha, "b": to, "ba": alpha, "t0": time.time(), "dur": dur,
                     "ease": ease, "done": done}

    def _step_anim(self, now):
        a = self.anim
        if not a:
            return False
        t = min(1.0, (now - a["t0"]) / a["dur"])
        e = a["ease"](t)
        g = tuple(p + (q - p) * e for p, q in zip(a["a"], a["b"]))
        self._apply_geo(g, a["aa"] + (a["ba"] - a["aa"]) * e)
        if t >= 1.0:
            self.anim = None
            if a["done"]:
                a["done"]()
        return True

    def _show_window(self):
        if not self.visible:
            _u32.ShowWindow(self.hwnd, 4)                                  # SW_SHOWNOACTIVATE
            self.visible = True
        _u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)

    def _hide_window(self):
        _u32.ShowWindow(self.hwnd, 0)
        self.visible = False
        self.mode, self.peek, self.expanded = "hidden", False, False
        self.hide_at, self.lingering = None, False
        self.pill_fr.place_forget(); self.prev_fr.place_forget(); self.chat_fr.place_forget()
        self._apply_geo((-20000, -20000, 10, 10), 0.0)

    # ============================================================ pill
    def _pill_config(self, state, title, detail):
        self.dot.itemconfig(self.dot_id, fill=self.COLORS.get(state, self.SUB))
        self.dot.coords(self.dot_id, 3, 3, 15, 15)
        self.title.config(text=title)
        detail = (detail or "").strip()
        self.detail.config(text=detail)               # never shortened: it must match what's spoken
        self.detail.grid() if detail else self.detail.grid_remove()
        self.btns.grid() if state == "listen" else self.btns.grid_remove()
        self.pill_state = state

    def _pill_target(self):
        self.pill_fr.update_idletasks()
        pw, ph = self.pill_fr.winfo_reqwidth(), self.pill_fr.winfo_reqheight()
        eh = 0
        if self.expanded:
            pw = max(pw, self.px(300))
            self._fill_preview(pw)
            self.prev_fr.update_idletasks()
            eh = self.prev_fr.winfo_reqheight()
        self.pill_dims = (pw, ph, eh)
        mon, work = _primary()
        x = work.left + (work.right - work.left - pw) // 2
        return (x, work.top + self.px(10), pw, ph + eh), (x, mon.top - ph - eh - 8, pw, ph + eh)

    def _pill_show(self):
        """Show / update the pill (slides down from the top edge when it wasn't visible)."""
        if self.mode == "chat":
            return
        on, off = self._pill_target()
        if self.mode == "hidden" or not self.visible:
            self.mode = "pill"
            self.chat_fr.place_forget()
            self._apply_geo(off, 0.0)
            self._show_window()
            self._animate(on, self.ALPHA, 0.22)
        else:
            self.mode = "pill"
            if not self.expanded:
                self.prev_fr.place_forget()
            self._show_window()
            self._animate(on, self.ALPHA, 0.16)

    def _pill_refresh(self):
        if self.mode == "pill":
            self._pill_show()

    def _pill_hide(self):
        """Slide up and out. If the mouse is on it, become the idle pill instead (it slides away
        when the mouse leaves), so it never disappears from under the cursor."""
        if self.mode != "pill":
            return
        self.hide_at, self.lingering = None, False
        if self._cursor_in(self.geo, self.px(4)):
            self.peek = True; self.peek_t0 = 0.0
            self._pill_config("idle", "Hey Claude", "Click to open the chat  ·  F20")
            self._pill_show()
            return
        on, off = self._pill_target()
        cur = self.geo
        self._animate((cur[0], off[1], cur[2], cur[3]), 0.0, 0.18, _ease_in, done=self._hide_window)

    def _peek_show(self):
        self.peek, self.peek_t0 = True, time.time()
        self._pill_config("idle", "Hey Claude", "Click to open the chat  ·  F20")
        self._pill_show()

    def _pill_click(self, e=None):
        if self.peek and time.time() - self.peek_t0 < PEEK_CLICK_GUARD_S:
            return "break"
        self.open_chat()
        return "break"

    def _cursor_in(self, g, margin=0):
        x, y = _cursor()
        return g[0] - margin <= x < g[0] + g[2] + margin and g[1] - margin <= y < g[1] + g[3] + margin

    # ============================================================ chat open / close
    def open_chat(self):
        if self.mode == "chat":
            self._focus_chat()
            return
        size = self._chat_size()
        if size != (self.CW, self.CH):                  # different screen since last time
            self.CW, self.CH = size
            self._render_all()
        mon, work = _primary()
        tx = work.left + (work.right - work.left - self.CW) // 2
        target = (tx, work.top + self.px(10), self.CW, self.CH)
        if self.mode == "pill" and self.visible:
            start = self.geo
        else:
            pw, ph = self.px(360), self.px(56)
            start = (work.left + (work.right - work.left - pw) // 2, mon.top - ph, pw, ph)
            self._apply_geo(start, 0.0)
        self.peek = self.expanded = False
        self.hide_at, self.lingering = None, False
        self.pill_fr.place_forget(); self.prev_fr.place_forget()
        self.mode, self.chat_open = "chat", True
        self._layout_content()
        self._header_status()
        st = self.status
        self._chat_status(*(st if st else ("done", "", "", None)))
        self._show_window()
        self._scroll_bottom(instant=True)
        self._animate(target, 1.0, 0.26, done=lambda: (self._scroll_bottom(instant=True), self._comp_layout()))
        self._focus_chat()

    def close_chat(self):
        if self.mode != "chat":
            return
        self._close_menu()
        self.chat_open = False
        self._release_focus()
        mon, work = _primary()
        pw, ph = self.px(360), self.px(56)
        x = work.left + (work.right - work.left - pw) // 2

        def done():
            self._hide_window()
            st = self.status
            if st and st[0] in self.PULSING:            # still listening / working: back to the pill
                self.peek = False
                self._pill_config(st[0], st[1], st[2])
                self._pill_show()

        self._animate((x, mon.top - ph - 8, pw, ph), 0.0, 0.22, _ease_in, done=done)

    def _focus_chat(self):
        fg = _u32.GetForegroundWindow()
        if fg and fg != self.hwnd:
            self.prev_fg = fg
        ex = _u32.GetWindowLongW(self.hwnd, -20)
        _u32.SetWindowLongW(self.hwnd, -20, ex & ~0x08000000)
        tid = _k32.GetCurrentThreadId()
        ftid = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(ftid and ftid != tid and _u32.AttachThreadInput(tid, ftid, True))
        try:
            _u32.BringWindowToTop(self.hwnd)
            _u32.SetForegroundWindow(self.hwnd)
        finally:
            if attached:
                _u32.AttachThreadInput(tid, ftid, False)
        try:
            self.win.focus_force()
            if not self.listening:
                self.inp.focus_set()
        except Exception:
            pass

    def _release_focus(self):
        ex = _u32.GetWindowLongW(self.hwnd, -20)
        _u32.SetWindowLongW(self.hwnd, -20, ex | 0x08000000)
        if _u32.GetForegroundWindow() == self.hwnd and self.prev_fg and _u32.IsWindow(self.prev_fg):
            _u32.SetForegroundWindow(self.prev_fg)

    def _chat_status(self, state, title, detail, kind):
        """What the listener's pill states mean inside the chat window."""
        if state == "listen":
            if kind in ("open", "answer"):
                self._set_listening(False); self._set_chip(kind)
            else:
                self._set_chip(None); self._set_listening(True, detail)
            if self.indicator:
                self._set_indicator(None, None)
        elif state in ("think", "act"):
            self._set_listening(False); self._set_chip(None)
            d = "" if (detail or "").startswith("“") else detail
            self._set_indicator(title, d)
        else:
            self._set_listening(False); self._set_chip(None)
            if self.indicator:
                self._set_indicator(None, None)
        self._header_status()

    # ============================================================ queue handling
    def _handle(self, msg):
        kind = msg[0]
        if kind == "show":
            state, title, detail, hide_after, k = msg[1:]
            self.status = (state, title, detail, k)
            if self.mode == "chat":
                self._chat_status(state, title, detail, k)
                return
            self.peek = False
            self.lingering = False
            self._pill_config(state, title, detail)
            self._pill_show()
            self.hide_at = time.time() + hide_after if hide_after else None
        elif kind == "hide":
            self.status = None
            if self.mode == "chat":
                self._chat_status("done", "", "", None)
            elif self.mode == "pill" and not self.peek:
                self._pill_hide()
        elif kind == "hide_after":
            if self.mode == "pill" and not self.peek:
                self.hide_at = time.time() + msg[1]
        elif kind == "linger":
            if self.mode == "pill" and not self.peek:
                self.hide_at = time.time() + msg[1] if msg[1] else None
                self.lingering, self.linger_start = True, time.time()
                self._activity_baseline()
            if self.status and self.status[0] not in self.PULSING:
                self.status = None
        elif kind == "add":
            self._add_msg(msg[1])
        elif kind == "reset":
            self._reset(msg[1])
        elif kind == "speaking":
            self.speaking_now = msg[1]
            if self.mode == "chat":
                self._header_status()
        elif kind == "mic":
            self.mic_reason = msg[1]
            if self.mode == "chat":
                self._header_status(); self._comp_layout()
        elif kind == "toast":
            if self.mode == "chat":
                self._toast(msg[1], msg[2])
            else:
                self._pill_config("idle", "Hey Claude", msg[1])
                self._pill_show()
                self.hide_at = time.time() + msg[2]
        elif kind == "affinity":
            try:
                _u32.SetWindowDisplayAffinity(self.hwnd, msg[1])
            finally:
                if msg[2] is not None:
                    msg[2].set()
        elif kind == "f20":
            self._f20(msg[1])
        elif kind == "quit":
            self.root.quit()
            return "quit"

    def _reset(self, manual):
        self.prev_loaded = None
        if manual or self.mode != "chat":
            self.msgs = []
            self._render_all()
            if manual:
                self._toast("New conversation started. The old one is saved.")
        else:                                          # 15 quiet minutes while you're reading: keep it visible
            self._add_msg({"role": "divider", "text": f"New conversation  ·  {_clock(time.time())}"})

    def _f20(self, how):
        if how == "hold":
            self._act("mic")
            return
        if self.mode != "chat":
            self.open_chat()
        elif _u32.GetForegroundWindow() == self.hwnd:
            self.close_chat()
        else:
            self._focus_chat()

    # ============================================================ the Tk loop
    def _activity_baseline(self):
        self.base_fg = _u32.GetForegroundWindow()
        self.base_input = _last_input()
        self.base_cursor = _cursor()

    def _user_moved_on(self) -> bool:
        if self._cursor_in(self.geo, self.px(6)):
            return False                                             # reading / about to click it
        if _down(0x01) or _down(0x02):
            return True                                              # mouse click anywhere else
        if _u32.GetForegroundWindow() != self.base_fg:
            return True                                              # switched windows
        cur, tick = _cursor(), _last_input()
        if cur != self.base_cursor:                                  # mouse moved: not "moving on" by itself
            self.base_cursor, self.base_input = cur, tick
            return False
        return tick != self.base_input                               # input without mouse movement = typing

    def _tick(self):
        now = time.time()
        busy = False
        try:
            while True:
                try:
                    msg = self.q.get_nowait()
                except queue.Empty:
                    break
                if self._handle(msg) == "quit":
                    return
            busy |= self._step_anim(now)
            if self.anim is None:
                if self.mode == "hidden":
                    self._edge_logic(now)
                elif self.mode == "pill":
                    self._pill_logic(now)
            if self.mode == "chat":
                busy |= self._chat_logic(now)
            # Esc = ✕ while the pill is listening (the pill never has focus, so watch the key directly)
            esc = _down(0x1B)
            if esc and not self.esc_was_down and self.mode == "pill" and getattr(self, "pill_state", "") == "listen":
                self._act("cancel")
            self.esc_was_down = esc
            if self.f20_poll:
                self._poll_f20(now)
            if self.mode == "pill" and getattr(self, "pill_state", "") in self.PULSING:
                r = 4.4 + 1.8 * math.sin(now * 8.3)
                self.dot.coords(self.dot_id, 9 - r, 9 - r, 9 + r, 9 + r)
                busy = True
        except Exception as e:
            if now - self.err_t > 5:
                self.err_t = now
                self.log(f"HUD error: {e!r}")
        self.root.after(15 if busy else 33, self._tick)

    def _edge_logic(self, now):
        mon, work = _primary()
        x, y = _cursor()
        cx = (mon.left + mon.right) / 2
        at_edge = y <= mon.top + 1 and abs(x - cx) <= self.px(EDGE_HALF_W) and mon.left <= x < mon.right
        if at_edge and not (_down(0x01) or _down(0x02)):
            if self.edge_t0 is None:
                self.edge_t0 = now
            elif now - self.edge_t0 >= EDGE_DWELL_S:
                self.edge_t0 = None
                self._peek_show()
        else:
            self.edge_t0 = None

    def _pill_logic(self, now):
        g = self.geo
        inside = self._cursor_in(g, self.px(2))
        mon, _ = _primary()
        # the dropped pill: stay while the mouse is on it or on the way down to it, else slide away
        if self.peek:
            x, y = _cursor()
            corridor = (g[0] - self.px(14) <= x < g[0] + g[2] + self.px(14) and mon.top - 2 <= y < g[1] + g[3] + self.px(10))
            if corridor:
                self.out_t0 = None
            else:
                self.out_t0 = self.out_t0 or now
                if now - self.out_t0 >= RETRACT_S:
                    self.out_t0 = None
                    self._pill_hide()
                    return
        # hover: show the latest messages under the pill
        if inside:
            self.leave_t0 = None
            self.hover_t0 = self.hover_t0 or now
            if not self.expanded and now - self.hover_t0 >= HOVER_EXPAND_S:
                self.expanded = True
                self._pill_show()
                return
        else:
            self.hover_t0 = None
            if self.expanded and not self._cursor_in(g, self.px(8)):
                self.leave_t0 = self.leave_t0 or now
                if now - self.leave_t0 >= 0.12:
                    self.leave_t0 = None
                    self.expanded = False
                    self.prev_fr.place_forget()
                    self._pill_show()
                    return
        if self.peek:
            return
        if inside:
            return                                     # never time out from under the cursor
        if self.hide_at and now >= self.hide_at:
            self._pill_hide()
        elif self.lingering and now - self.linger_start > 0.8 and self._user_moved_on():
            self._pill_hide()

    def _chat_logic(self, now):
        busy = self._step_scroll()
        busy |= self._step_fades(now)
        self._update_jump()
        if now - self.sb_seen < 1.4 or self.sb_drag is not None:
            self._draw_scrollbar()
        if self.listening:
            self._animate_bars(now); busy = True
        if self.indicator and self.blocks and self.blocks[-1].kind == "indicator":
            for k, d in enumerate(self.blocks[-1].extra.get("dots", [])):
                v = 0.5 + 0.5 * math.sin(now * 6.0 - k * 0.9)
                self.tcv.itemconfigure(d, fill=_mix("#4a4a52", "#c4c4cc", v))
            busy = True
        st = getattr(self, "hdot_state", "idle")
        if st in self.PULSING:
            r = self.px(3.2) + self.px(1.6) * (0.5 + 0.5 * math.sin(now * 8.3))
            c = self.px(6)
            self.hdot.coords(self.hdot_id, c - r, c - r, c + r, c + r)
            busy = True
        else:
            self.hdot.coords(self.hdot_id, self.px(1), self.px(1), self.px(11), self.px(11))
        if self.toast_until and now > self.toast_until:
            self.toast_until = 0.0
            self.toast_lbl.place_forget()
        # docked / undocked while open: keep it centered on the primary screen
        if int(now) != getattr(self, "_geo_check", 0) and self.anim is None:
            self._geo_check = int(now)
            mon, work = _primary()
            tx = work.left + (work.right - work.left - self.CW) // 2
            if (tx, work.top + self.px(10)) != self.geo[:2]:
                self._apply_geo((tx, work.top + self.px(10), self.CW, self.CH))
        return busy

    # ============================================================ F20
    def _hotkey_loop(self):
        if not _u32.RegisterHotKey(None, 0x4843, 0x4000, VK_F20):      # MOD_NOREPEAT
            self.log(f"F20 hotkey unavailable (error {ctypes.get_last_error()}); watching the key instead.")
            self.f20_poll = True
            return
        msg = wt.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message != 0x0312:                                  # WM_HOTKEY
                continue
            t0 = time.time()
            held = False
            while _down(VK_F20):
                if time.time() - t0 >= F20_HOLD_S:
                    held = True
                    break
                time.sleep(0.012)
            self.q.put(("f20", "hold" if held else "tap"))

    def _poll_f20(self, now):
        down = _down(VK_F20)
        was, t0 = self.f20_was
        if down and not was:
            self.f20_was = (True, now)
        elif down and was and t0 and now - t0 >= F20_HOLD_S:
            self.f20_was = (True, 0.0)
            self._f20("hold")
        elif not down and was:
            if t0:
                self._f20("tap")
            self.f20_was = (False, 0.0)


MODEL_LABEL = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus"}
