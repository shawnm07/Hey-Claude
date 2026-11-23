"""
Conversation logs for Hey Claude: one JSON-lines file per conversation in workspace/conversations/.

Append-only: lines are only ever added, files are never rewritten or deleted ("New conversation"
just starts the next file). Each line is one record:
  {"type": "start", "t": ...}                                         first line of a file
  {"type": "msg", "t": ..., "role": "user"|"assistant"|"note", "text": ..., "source": "voice"|"typed", ...}
  {"type": "reflected", "t": ..., "upto": N}                          learned memory reviewed through msg N
"""
import json
import threading
import time
from pathlib import Path


class ChatLog:
    def __init__(self, folder: Path, idle_s: float, log=print):
        self.dir, self.idle_s, self.log = Path(folder), idle_s, log
        self.lock = threading.Lock()
        self.path = None                               # None = the next message starts a new file
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            files = self.files()
            if files:                                  # restarted within the idle window: carry on
                last = self.read(files[-1], types=("msg", "start", "end"))
                last_t = max((r.get("t", 0) for r in last), default=0)
                ended = any(r.get("type") == "end" for r in last)
                if not ended and any(r.get("type") == "msg" for r in last) and time.time() - last_t < idle_s:
                    self.path = files[-1]
        except Exception as e:
            self.log(f"Conversation log setup failed: {e!r}")

    def files(self):
        return sorted(self.dir.glob("*.jsonl"))

    @staticmethod
    def read(path, types=("msg",)):
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue                       # a half-written line (crash) is skipped, never fatal
                    if isinstance(r, dict) and r.get("type") in types:
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def messages(self):
        return self.read(self.path) if self.path else []

    def _write(self, path, rec):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def append(self, role, text, **meta) -> dict:
        rec = {"type": "msg", "t": time.time(), "role": role, "text": text}
        rec.update({k: v for k, v in meta.items() if v is not None})
        with self.lock:
            try:
                if self.path is None:
                    name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(rec["t"]))
                    path, n = self.dir / f"{name}.jsonl", 2
                    while path.exists():
                        path, n = self.dir / f"{name}-{n}.jsonl", n + 1
                    self.path = path
                    self._write(path, {"type": "start", "t": rec["t"]})
                self._write(self.path, rec)
            except Exception as e:
                self.log(f"Couldn't write the conversation log: {e!r}")
        return rec

    def new(self):
        """Start a new conversation (the next message creates its file). Returns the old file."""
        with self.lock:
            old, self.path = self.path, None
            if old is not None:                        # so a restart doesn't pick an ended conversation back up
                try:
                    self._write(old, {"type": "end", "t": time.time()})
                except Exception as e:
                    self.log(f"Couldn't mark the conversation ended: {e!r}")
        return old

    def previous(self, before=None):
        """The conversation file before `before` (default: the current one; if there is no
        current file yet, the latest one)."""
        cur = before or self.path
        earlier = [f for f in self.files() if cur is None or f.name < Path(cur).name]
        return earlier[-1] if earlier else None

    def mark_reflected(self, path, upto):
        if not path:
            return
        with self.lock:
            try:
                self._write(path, {"type": "reflected", "t": time.time(), "upto": int(upto)})
            except Exception as e:
                self.log(f"Couldn't mark {Path(path).name} reviewed: {e!r}")

    def reflected_upto(self, path):
        return max((r.get("upto", 0) for r in self.read(path, types=("reflected",))), default=0)
