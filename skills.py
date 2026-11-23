"""
Script skills for Hey Claude: saved scripts that run instantly (no model call) when a command matches.

Each skill is a folder in workspace/skills/ holding a script (.ps1 or .py) and a manifest, skill.json:

  {
    "name": "Screen brightness",
    "description": "Set the laptop screen brightness.",
    "triggers": ["set (?:the )?brightness to {n}(?: percent)?", "brightness {n}(?: percent)?"],
    "params": {"n": {"type": "int", "min": 0, "max": 100}},
    "script": "run.ps1",
    "args": ["{n}"],
    "reply": "[silent] Brightness {n}%.",
    "approval": false,
    "timeout": 10
  }

  triggers   regular expressions matched against the WHOLE command (lowercased, without "please" /
             "can you" in front or punctuation at the end). {name} captures a parameter.
  params     optional, per parameter: type "text" (default), "word", "int" or "number" (digits or
             number words), and for numbers "min" / "max" (the value is clamped into range).
  args       arguments passed to the script ({name} = a parameter). Passed as separate arguments,
             never through a shell, so a spoken value can't inject commands.
  reply      what to say / show afterwards. {name} = a parameter, {output} = what the script printed.
             "[silent] ..." just flashes "Done" on the pill. Default: "{output}" (or "Done.").
  approval   true = never run directly: the model asks the confirmation question first and runs it
             only after a clear yes (forced on for anything that looks destructive or outward-facing).
  timeout    seconds (default 15, at most 60); the script is stopped after that and counts as failed.
  enabled    false = ignored.
  undo       optional, for "undo that": {"script": "undo.ps1", "args": ["{undo}"], "reply": "[silent] Undone."}.
             The main script can print a line "UNDO: <value>" (never shown or spoken), e.g. the previous
             brightness; {undo} in the undo args is that value, {name} the original parameters.

Safety, enforced here and not just by the instructions:
  * A new or changed skill (manifest or script) is not trusted yet: its first run goes through the
    model's own tools (so Claude Code's permission checks see it). After that it runs instantly.
  * A static scan blocks skills that download-and-run code or install software, and forces approval
    for anything that deletes, overwrites, kills, shuts down, changes settings or sends.
  * Scripts must live inside their own skill folder; only .ps1 and .py are run.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

NO_WINDOW = 0x08000000
MAX_TIMEOUT_S = 60
DEFAULT_TIMEOUT_S = 15

# Download-and-run or install: the skill is disabled (the model handles the request instead).
BLOCK_RE = re.compile(
    r"\b(?:iex|invoke-expression|downloadfile|downloadstring|downloaddata|start-bitstransfer|winget|choco|scoop|"
    r"msiexec|install-module|install-package|install-script|add-appxpackage|set-executionpolicy|"
    r"pip3?(?:\.exe)?\s+install|python\S*\s+-m\s+pip|npm\s+(?:i|install)|urlretrieve|"
    r"invoke-webrequest[^\n]*-outfile|iwr[^\n]*-outfile|(?:curl|wget)(?:\.exe)?[^\n]*\s(?:-o|-O|--output)\b)",
    re.I)
# Destructive or outward-facing: allowed only with the confirmation question first.
APPROVAL_RE = re.compile(
    r"(?:\b(?:remove-item|rm|del|erase|rmdir|rd|ri|clear-content|clear-recyclebin|set-content|out-file|"
    r"move-item|rename-item|copy-item|stop-process|kill|taskkill|spps|stop-computer|restart-computer|"
    r"shutdown(?!(?:\.exe)?\s+[/-]h\b)|logoff|format-volume|format\.com|diskpart|send-mailmessage|send-compose|"
    r"reg(?:\.exe)?\s+(?:add|delete|import)|"
    r"set-itemproperty|new-itemproperty|remove-itemproperty|set-service|stop-service|restart-service|"
    r"disable-\w+|enable-\w+|netsh|set-net\w+|remove-\w+|uninstall-\w+|unregister-\w+|"
    r"os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|shutil\.move|subprocess|os\.system|os\.popen)\b"
    r"|\^\{enter\}|\^~)",
    re.I)
# A trigger that matches any of these is far too broad (it would swallow every command).
BROAD_PROBES = ["", "x", "hello", "okay", "do something for me", "what is this"]

_NUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_NUM.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
             "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100, "half": 50,
             "max": 100, "maximum": 100, "full": 100, "min": 0, "minimum": 0})


def parse_number(s):
    """'25', '2.5', 'twenty five', 'seventy-five', 'a hundred' -> number, else None."""
    s = str(s).strip().lower().rstrip("%").replace(" percent", "").replace("-", " ").strip()
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        pass
    if s in _NUM:
        return _NUM[s]
    parts = s.split()
    if len(parts) == 2 and all(p in _NUM for p in parts) and _NUM[parts[0]] >= 20 and _NUM[parts[1]] < 10:
        return _NUM[parts[0]] + _NUM[parts[1]]
    return None


def normalize(command: str) -> str:
    c = command.lower().strip()
    c = re.sub(r"^(?:hey,? claude[,.!]?\s*)", "", c)
    c = re.sub(r"^(?:please|can you|could you|would you|will you)\s+", "", c)
    c = re.sub(r"[\s,]+please$", "", c)
    return re.sub(r"[.!?,]+$", "", c).strip()


def _fill(template: str, values: dict) -> str:
    out = str(template)
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


class Skill:
    def __init__(self, folder: Path):
        self.folder, self.id = folder, folder.name
        self.name, self.blocked, self.approval_reason = folder.name, None, None
        self.triggers, self.params, self.args, self.reply = [], {}, [], "{output}"
        self.approval, self.timeout, self.script, self.description = False, DEFAULT_TIMEOUT_S, None, ""
        self.digest = ""
        self.undo_script, self.undo_args, self.undo_reply, self.undo_approval = None, [], "[silent] Undone.", False
        try:
            raw = (folder / "skill.json").read_bytes()
            m = json.loads(raw.decode("utf-8-sig"))
            self.name = str(m.get("name") or folder.name)[:60]
            self.description = str(m.get("description", ""))[:200]
            if m.get("enabled") is False:
                self.blocked = "turned off (enabled: false)"
                return
            script = (folder / str(m.get("script", ""))).resolve()
            if folder.resolve() not in script.parents or script.suffix.lower() not in (".ps1", ".py"):
                self.blocked = "its script must be a .ps1 or .py file inside its own folder"
                return
            if not script.exists():
                self.blocked = f"script {script.name} is missing"
                return
            self.script = script
            code = script.read_bytes()
            undo_code = b""
            u = m.get("undo")
            if isinstance(u, dict) and u.get("script"):
                us = (folder / str(u["script"])).resolve()
                if folder.resolve() in us.parents and us.suffix.lower() in (".ps1", ".py") and us.exists():
                    self.undo_script, undo_code = us, us.read_bytes()
                    self.undo_args = [str(a) for a in (u.get("args") or [])]
                    self.undo_reply = str(u.get("reply") or "[silent] Undone.")
                    utext = undo_code.decode("utf-8", errors="replace")
                    if BLOCK_RE.search(utext):
                        self.undo_script = None
                    elif APPROVAL_RE.search(utext):
                        self.undo_approval = True
            self.digest = hashlib.sha256(raw + b"\0" + code + b"\0" + undo_code).hexdigest()
            text = code.decode("utf-8", errors="replace")
            self.params = {k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (m.get("params") or {}).items()}
            self.args = [str(a) for a in (m.get("args") or [])]
            self.reply = str(m.get("reply") or "{output}")
            self.timeout = max(1, min(MAX_TIMEOUT_S, float(m.get("timeout", DEFAULT_TIMEOUT_S))))
            self.approval = bool(m.get("approval", False))
            hit = BLOCK_RE.search(text)
            if hit:
                self.blocked = f"its script downloads/runs outside code or installs software ({hit.group(0).strip()!r})"
                return
            hit = APPROVAL_RE.search(text)
            if hit and not self.approval:
                self.approval, self.approval_reason = True, f"its script can change or remove things ({hit.group(0).strip()!r})"
            trig = m.get("triggers") or []
            if isinstance(trig, str):
                trig = [trig]
            for t in trig:
                self.triggers.append(self._compile(str(t)))
            if not self.triggers:
                self.blocked = "it has no triggers"
            elif any(rx.fullmatch(p) for rx in self.triggers for p in BROAD_PROBES):
                self.blocked = "a trigger is so broad it would match almost anything"
                self.triggers = []                     # never match at all (it would swallow every command)
        except Exception as e:
            self.blocked = f"its skill.json couldn't be read ({e})"

    def _compile(self, trigger: str):
        def group(mm):
            name = mm.group(1)
            kind = self.params.get(name, {}).get("type", "text")
            body = r"[\w.%\- ]+?" if kind in ("int", "number") else (r"\S+" if kind == "word" else r".+?")
            return f"(?P<{name}>{body})"
        return re.compile(re.sub(r"\{([A-Za-z_]\w*)\}", group, trigger.strip()), re.I)

    def match(self, command: str):
        """-> dict of parameter values, or None."""
        for rx in self.triggers:
            m = rx.fullmatch(command)
            if not m:
                continue
            values, ok = {}, True
            for k, v in m.groupdict().items():
                v = (v or "").strip()[:200]
                spec = self.params.get(k, {})
                if spec.get("type") in ("int", "number"):
                    n = parse_number(v)
                    if n is None:
                        ok = False
                        break
                    if "min" in spec:
                        n = max(spec["min"], n)
                    if "max" in spec:
                        n = min(spec["max"], n)
                    v = str(int(round(n))) if spec.get("type") == "int" else str(n)
                values[k] = v
            if ok:
                return values
        return None

    def argv(self, values, python_exe, script=None, args=None):
        script = script or self.script
        args = [_fill(a, values) for a in (self.args if args is None else args)]
        if script.suffix.lower() == ".py":
            return [str(python_exe), "-I", str(script), *args]
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script), *args]

    def command_line(self, values, python_exe):
        """The same command, for the model to run with its PowerShell tool."""
        args = " ".join("'" + _fill(a, values).replace("'", "''") + "'" for a in self.args)
        if self.script.suffix.lower() == ".py":
            return f"& '{python_exe}' -I '{self.script}' {args}".strip()
        return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '{self.script}' {args}".strip()


class SkillBook:
    def __init__(self, folder: Path, verified_file: Path, python_exe: Path, log=print):
        self.dir, self.verified_file, self.python, self.log = Path(folder), Path(verified_file), python_exe, log
        self.lock = threading.Lock()
        self.skills, self.sig, self.reported = [], None, set()
        self.last_undo = None
        try:
            self.verified = json.loads(self.verified_file.read_text(encoding="utf-8"))
        except Exception:
            self.verified = {}

    def _signature(self):
        sig = []
        try:
            for d in sorted(self.dir.iterdir()):
                if d.is_dir():
                    for f in sorted(d.iterdir()):
                        st = f.stat()
                        sig.append((str(f), st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            pass
        return tuple(sig)

    def load(self):
        """(Re)load when anything in the folder changed. Cheap enough to call before every command."""
        sig = self._signature()
        if sig == self.sig:
            return self.skills
        self.sig = sig
        skills = []
        if self.dir.exists():
            for d in sorted(self.dir.iterdir()):
                if d.is_dir() and (d / "skill.json").exists():
                    s = Skill(d)
                    skills.append(s)
                    key = (s.id, s.digest, s.blocked, s.approval_reason)
                    if key not in self.reported:
                        self.reported.add(key)
                        state = (f"disabled: {s.blocked}" if s.blocked else
                                 f"needs approval: {s.approval_reason}" if s.approval_reason else
                                 "needs approval" if s.approval else
                                 "ready" if self.is_verified(s) else "new: first run goes through the model")
                        self.log(f"Skill '{s.name}' ({s.id}): {state}.")
        self.skills = skills
        return skills

    def match(self, command: str):
        """-> (skill, values) for the first skill whose trigger matches, else None."""
        c = normalize(command)
        for s in self.load():
            if s.triggers:
                values = s.match(c)
                if values is not None:
                    return s, values
        return None

    def is_verified(self, s: Skill) -> bool:
        return bool(s.digest) and self.verified.get(s.id) == s.digest

    def mark_verified(self, s: Skill):
        with self.lock:
            self.verified[s.id] = s.digest
            try:
                self.verified_file.write_text(json.dumps(self.verified, indent=1), encoding="utf-8")
            except Exception as e:
                self.log(f"Couldn't save verified skills: {e!r}")
        self.log(f"Skill '{s.name}' verified; it now runs instantly.")

    def _exec(self, s: Skill, values, script, args, reply_template):
        t = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        enc = "utf-8" if script.suffix.lower() == ".py" else "oem"
        try:
            r = subprocess.run(s.argv(values, self.python, script, args), cwd=s.folder, capture_output=True,
                               timeout=s.timeout, env=env, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False, f"it took longer than its {s.timeout:g} s timeout and was stopped", None
        except Exception as e:
            return False, f"it couldn't start ({e!r})", None
        out = r.stdout.decode(enc, errors="replace").strip()
        err = r.stderr.decode(enc, errors="replace").strip()
        undo = None
        lines = []
        for ln in out.splitlines():                    # "UNDO: <value>" lines are for "undo that", not shown
            mm = re.match(r"^\s*UNDO:\s*(.*)$", ln)
            if mm:
                undo = mm.group(1).strip()[:200]
            else:
                lines.append(ln)
        out = "\n".join(lines).strip()
        self.log(f"  skill '{s.name}'{' undo' if script != s.script else ''} exit {r.returncode} in "
                 f"{time.time() - t:.2f}s; output {out[-200:]!r}" + (f"; errors {err[-300:]!r}" if err else ""))
        if r.returncode != 0:
            return False, f"exit code {r.returncode}: {(err or out)[-600:] or 'no output'}", None
        reply = _fill(reply_template, dict(values, output=out[-800:])).strip()
        return True, reply or "Done.", undo

    def run(self, s: Skill, values):
        """Run the script. -> (ok, reply text or error description). self.last_undo = its UNDO value."""
        ok, reply, self.last_undo = self._exec(s, values, s.script, None, s.reply)
        return ok, reply

    def run_undo(self, s: Skill, values, undo_value):
        """Reverse an earlier run with the skill's undo script."""
        vals = dict(values, undo="" if undo_value is None else undo_value)
        ok, reply, _ = self._exec(s, vals, s.undo_script, s.undo_args, s.undo_reply)
        return ok, reply
