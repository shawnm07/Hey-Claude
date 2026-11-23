"""
Voice match for Hey Claude: is this the owner talking?

A WeSpeaker ResNet34 model (voice/speaker/voxceleb_resnet34_LM.onnx, run with onnxruntime) turns a few
seconds of speech into a 256-number "voiceprint". Same speaker -> similar vectors (cosine similarity
roughly 0.5-0.85); different people -> roughly 0-0.35.

The profile (voiceprint.json, next to hey_claude.py) holds only these vectors, never audio:
  enroll   from the one-time setup ("Hey Claude, learn my voice": 5 phrases)
  adapt    from recent commands that matched confidently (keeps up with mic / room changes)
"""
import json
import math
import threading
import time

import numpy as np

RATE = 16000
MIN_SPEECH_S = 0.8          # less voiced audio than this: too little to judge (no decision)
MAX_ADAPT = 30
_EPS = np.float32(1.1920929e-07)


def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


def _mel_banks(n_mels=80, n_fft=512, rate=RATE, low=20.0, high=None):
    """Kaldi-style triangular mel filters over the FFT bins (Kaldi ignores the Nyquist bin)."""
    high = high or rate / 2
    nbins = n_fft // 2
    freqs = np.arange(nbins) * rate / n_fft
    mels = _mel(freqs)
    lo, hi = _mel(low), _mel(high)
    delta = (hi - lo) / (n_mels + 1)
    w = np.zeros((n_mels, nbins), np.float64)
    for m in range(n_mels):
        left, center, right = lo + m * delta, lo + (m + 1) * delta, lo + (m + 2) * delta
        up = (mels - left) / (center - left)
        down = (right - mels) / (right - center)
        w[m] = np.clip(np.minimum(up, down), 0, None) * ((mels > left) & (mels < right))
    return w


_BANKS = _mel_banks()
_WINDOW = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(400) / 399)   # Hamming, as WeSpeaker uses


def fbank(audio: np.ndarray) -> np.ndarray:
    """80-dim log mel filterbank (Kaldi compatible: 25 ms frames, 10 ms shift, no dither), mean-normalized."""
    x = audio.astype(np.float64) * 32768.0
    n = 1 + (len(x) - 400) // 160
    if n < 10:
        return None
    idx = np.arange(400)[None, :] + 160 * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    pre = np.empty_like(fr)
    pre[:, 1:] = fr[:, 1:] - 0.97 * fr[:, :-1]
    pre[:, 0] = fr[:, 0] * (1 - 0.97)
    spec = np.abs(np.fft.rfft(pre * _WINDOW, 512)) ** 2
    feats = np.log(np.maximum(spec[:, :256] @ _BANKS.T, _EPS)).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def voiced(audio: np.ndarray) -> np.ndarray:
    """Drop the quiet parts (pauses, room noise) so the voiceprint is of the voice."""
    f = 480
    n = len(audio) // f
    if n < 4:
        return audio
    frames = audio[: n * f].reshape(n, f)
    rms = np.sqrt((frames * frames).mean(axis=1)) + 1e-9
    keep = rms >= max(0.004, 0.12 * np.percentile(rms, 90))
    return frames[keep].reshape(-1) if keep.sum() >= 4 else audio


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


class VoiceID:
    def __init__(self, model_path, profile_path, log=print):
        import onnxruntime as rt
        opts = rt.SessionOptions()
        opts.intra_op_num_threads, opts.inter_op_num_threads = 2, 1
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.sess = rt.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self.path, self.log, self.lock = profile_path, log, threading.Lock()
        self.enroll, self.adapt, self.calibrated = [], [], None
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.enroll = [_unit(v) for v in d.get("enroll", [])]
            self.adapt = [_unit(v) for v in d.get("adapt", [])]
            self.calibrated = d.get("calibrated")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Voiceprint unreadable ({e!r}); voice match is off until you set it up again.")
        self._centroid = None
        log(f"Voice match: {'set up' if self.enrolled else 'not set up yet'}"
            + (f" (calibrated threshold {self.calibrated:.2f})" if self.enrolled and self.calibrated else "") + ".")

    @property
    def enrolled(self) -> bool:
        return len(self.enroll) >= 3

    def embed(self, audio: np.ndarray):
        """-> unit voiceprint of this audio, or None if there's too little speech in it."""
        v = voiced(audio)
        if len(v) < MIN_SPEECH_S * RATE:
            return None
        feats = fbank(v[: 12 * RATE])
        if feats is None:
            return None
        out = self.sess.run(None, {"feats": feats[None]})[0][0]
        return _unit(out)

    def centroid(self):
        if self._centroid is None and self.enrolled:
            vecs = self.enroll + self.enroll + self.adapt   # setup phrases count double
            self._centroid = _unit(np.mean(vecs, axis=0))
        return self._centroid

    def score(self, emb) -> float:
        c = self.centroid()
        return float(np.dot(c, emb)) if c is not None and emb is not None else 0.0

    def threshold(self, setting: float) -> float:
        """The setting, lowered if this voice varies more than usual between its own phrases."""
        t = setting
        if self.calibrated is not None:
            t = min(t, self.calibrated)
        return max(0.2, t)

    def set_enrollment(self, embs):
        """Save the setup phrases; calibrate from how similar they are to each other."""
        with self.lock:
            self.enroll, self.adapt, self._centroid = [_unit(e) for e in embs], [], None
            loo = []
            for i, e in enumerate(self.enroll):            # each phrase vs. the average of the others
                rest = _unit(np.mean([x for j, x in enumerate(self.enroll) if j != i], axis=0))
                loo.append(float(np.dot(rest, e)))
            self.calibrated = round(min(loo) - 0.12, 3) if loo else None
            self._save()
        return loo

    def add_adapt(self, emb):
        with self.lock:
            self.adapt = (self.adapt + [_unit(emb)])[-MAX_ADAPT:]
            self._centroid = None
            self._save()

    def forget(self):
        with self.lock:
            self.enroll, self.adapt, self.calibrated, self._centroid = [], [], None, None
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass

    def _save(self):
        d = {"version": 1, "saved": time.strftime("%Y-%m-%d %H:%M"), "calibrated": self.calibrated,
             "enroll": [[round(float(x), 5) for x in v] for v in self.enroll],
             "adapt": [[round(float(x), 5) for x in v] for v in self.adapt]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(self.path)
