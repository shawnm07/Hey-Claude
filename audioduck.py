"""
Ducking: turn the laptop's audio (Spotify, videos, ...) down while Hey Claude listens for a command, so
the mic hears you instead of the music, then put it back exactly where it was.

It ducks the master output volume (per-app session volume isn't available on this Windows build:
ISimpleAudioVolume answers E_NOINTERFACE for every session). Nothing of Hey Claude's own is playing
while it listens, so that's equivalent. The original level is saved to a small file while ducked, so
a crash or restart can never leave the volume low; and if you change the volume yourself while it's
ducked, your change wins.
"""
import json
import threading
import time
from pathlib import Path


class Ducker:
    def __init__(self, get_volume, set_volume, state_file: Path, level=0.3, log=print):
        self.get, self.set, self.state_file, self.level, self.log = get_volume, set_volume, state_file, level, log
        self.saved = None                               # (original %, ducked %)
        self.lock = threading.Lock()
        self.recover()

    @property
    def ducked(self):
        return self.saved is not None

    def recover(self):
        """A previous run was stopped while ducked: put the volume back."""
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        try:
            if abs(self.get() - d["ducked"]) <= 1:
                self.set(int(d["original"]))
                self.log(f"Restored the volume to {d['original']}% (left ducked by a previous run).")
        except Exception as e:
            self.log(f"Volume recovery failed: {e!r}")
        self.state_file.unlink(missing_ok=True)

    def duck(self):
        with self.lock:
            if self.saved is not None:
                return
            try:
                v = self.get()
                if v <= 5:
                    return                              # already (nearly) silent
                d = max(1, round(v * self.level))
                self.state_file.write_text(json.dumps({"original": v, "ducked": d, "t": time.time()}),
                                           encoding="utf-8")
                self.set(d)
                self.saved = (v, d)
            except Exception as e:
                self.log(f"Ducking failed: {e!r}")

    def restore(self):
        with self.lock:
            if self.saved is None:
                return
            v, d = self.saved
            self.saved = None
            try:
                if abs(self.get() - d) <= 1:            # you didn't change it meanwhile
                    self.set(v)
            except Exception as e:
                self.log(f"Restoring the volume failed: {e!r}")
            self.state_file.unlink(missing_ok=True)
