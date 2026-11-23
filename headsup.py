"""
Proactive heads-ups for Hey Claude (shown on the pill, never spoken):

  * Low battery: checked locally every 30 s (free) at the setting's level and again at 10 %.
  * Calendar events: 10 minutes before they start.
  * TickTick tasks: a timed task 1 hour before it's due; all-day tasks due today in the morning (after
    8 AM); all-day tasks due tomorrow in the evening (after 6 PM).

Calendar and TickTick are read by a small read-only Sonnet session every `heads_up_check_minutes`
(only while the screen is unlocked and on); each heads-up is then timed locally. What was already
shown is remembered in headsup-state.json, so nothing repeats after a restart.
"""
import datetime as dt
import json
import re
import threading
import time

AGENDA_PROMPT = (
    "You fetch the user's upcoming calendar events and due tasks for a heads-up notifier on their laptop. "
    "READ ONLY: never create, change, complete or delete anything. Use the Google Calendar and TickTick "
    "tools, then answer with one line of JSON and nothing else."
)


def _parse_dt(s):
    s = str(s or "").strip()
    if not s:
        return None, False
    for fmt, has_time in (("%Y-%m-%dT%H:%M", True), ("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
        try:
            return dt.datetime.strptime(s[:16] if has_time else s[:10], fmt), has_time
        except ValueError:
            continue
    return None, False


def _clock(t):
    return t.strftime("%I:%M %p").lstrip("0")


class HeadsUps:
    def __init__(self, notify, power, awake, settings, make_session, state_path, log=print):
        """notify(title, detail); power() -> (on_ac, percent, seconds_left or None, has_battery);
        awake() -> True when someone is at the laptop (unlocked, display on); settings() -> dict;
        make_session() -> a started ClaudeSession-like object with .ask(text) and .stop()."""
        self.notify, self.power, self.awake, self.settings = notify, power, awake, settings
        self.make_session, self.state_path, self.log = make_session, state_path, log
        self.session, self.checks = None, 0
        self.schedule, self.batt_warned = [], set()
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            self.state = {}

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    # ---------------------------------------------------------------- loop
    def _run(self):
        time.sleep(90)                                  # let startup settle first
        next_check = 0.0
        while True:
            try:
                s = self.settings()
                if s["heads_up"]:
                    self._battery(s)
                    if time.time() >= next_check and self.awake():
                        next_check = time.time() + s["heads_up_check_minutes"] * 60
                        self._agenda()
                    self._due()
            except Exception as e:
                self.log(f"Heads-up check failed: {e!r}")
            time.sleep(30)

    def _battery(self, s):
        on_ac, pct, secs, has_battery = self.power()
        if not has_battery or pct is None:
            return
        if on_ac:
            self.batt_warned = set()
            return
        for level in sorted({int(s["heads_up_battery_percent"]), 10}, reverse=True):
            if pct <= level and level not in self.batt_warned:
                self.batt_warned |= {lv for lv in (int(s["heads_up_battery_percent"]), 10) if lv >= level}
                left = f"About {round(secs / 60)} minutes left. " if secs else ""
                self.notify(f"Battery low · {pct}%", f"{left}Plug in soon.")
                break

    # ---------------------------------------------------------------- calendar + tasks
    def _agenda(self):
        now = dt.datetime.now()
        if self.session is None or self.checks >= 8:   # a fresh session now and then keeps it small
            if self.session is not None:
                self.session.stop()
            self.session, self.checks = self.make_session(), 0
        self.checks += 1
        until = now + dt.timedelta(hours=3)
        prompt = (
            f"Now: {now:%A %Y-%m-%d %H:%M} (local time).\n"
            f"1. Google Calendar: the user's events starting between now and {until:%Y-%m-%d %H:%M}.\n"
            "2. TickTick: tasks that are NOT completed and are due today or tomorrow (or overdue from today).\n"
            "Reply with ONLY this JSON on one line (local times; empty lists if nothing):\n"
            '{"events": [{"title": "...", "start": "YYYY-MM-DDTHH:MM"}], '
            '"tasks": [{"title": "...", "due": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"}]}'
        )
        t = time.time()
        reply = self.session.ask(prompt)
        m = re.search(r"\{.*\}", reply or "", re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            self.log(f"Heads-up agenda check: no usable answer ({(reply or '')[:160]!r}).")
            return
        events, tasks = data.get("events") or [], data.get("tasks") or []
        self.log(f"Heads-up agenda check ({time.time() - t:.1f}s): {len(events)} event(s), {len(tasks)} task(s).")
        self.schedule = self._plan(events, tasks, now)

    def _plan(self, events, tasks, now):
        items = []
        today, tomorrow = now.date(), now.date() + dt.timedelta(days=1)
        for ev in events:
            start, has_time = _parse_dt(ev.get("start") if isinstance(ev, dict) else None)
            title = str((ev or {}).get("title", "")).strip()[:80] if isinstance(ev, dict) else ""
            if not start or not has_time or not title or start <= now:
                continue
            items.append({"key": f"event|{title}|{start:%Y-%m-%dT%H:%M}", "when": start - dt.timedelta(minutes=10),
                          "kind": "event", "title": title, "at": start})
        for tk in tasks:
            if not isinstance(tk, dict):
                continue
            due, has_time = _parse_dt(tk.get("due"))
            title = str(tk.get("title", "")).strip()[:80]
            if not due or not title:
                continue
            if has_time:
                if due > now:
                    items.append({"key": f"task|{title}|{due:%Y-%m-%dT%H:%M}", "when": due - dt.timedelta(hours=1),
                                  "kind": "task", "title": title, "at": due})
            elif due.date() <= today:
                items.append({"key": f"today|{title}|{today}", "kind": "today", "title": title,
                              "when": dt.datetime.combine(today, dt.time(8, 0))})
            elif due.date() == tomorrow:
                items.append({"key": f"tomorrow|{title}|{tomorrow}", "kind": "tomorrow", "title": title,
                              "when": dt.datetime.combine(today, dt.time(18, 0))})
        return items

    def _due(self):
        now = dt.datetime.now()
        ready = [i for i in self.schedule if i["when"] <= now and i["key"] not in self.state]
        if not ready:
            return
        for kind in ("today", "tomorrow"):              # all-day tasks: one summary
            group = [i for i in ready if i["kind"] == kind]
            if group:
                names = [i["title"] for i in group]
                text = ", ".join(names[:3]) + (f", and {len(names) - 3} more" if len(names) > 3 else "")
                self.notify("Due today" if kind == "today" else "Due tomorrow", text + ".")
        for i in ready:
            if i["kind"] == "event":
                if i["at"] > now:                       # (skip events that already started)
                    mins = max(1, round((i["at"] - now).total_seconds() / 60))
                    self.notify(f"Coming up · {_clock(i['at'])}", f"{i['title']} starts in {mins} minutes.")
            elif i["kind"] == "task" and i["at"] > now:
                self.notify(f"Due at {_clock(i['at'])}", f"{i['title']}.")
        for i in ready:
            self.state[i["key"]] = time.time()
        cutoff = time.time() - 4 * 86400                 # forget old ones
        self.state = {k: v for k, v in self.state.items() if v >= cutoff}
        try:
            self.state_path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        except Exception as e:
            self.log(f"Couldn't save heads-up state: {e!r}")
