"""
Hey Claude skill: play a song, artist, album, playlist or podcast on Spotify by name, through the
Spotify Web API (no screen clicking). Needs Spotify Premium and a free Spotify developer app.

  run.py "<what to play>"         e.g. "blank space by taylor swift", "my study playlist",
                                  "the pints with aquinas podcast", "the album 1989", "some taylor swift"
  run.py --setup <client id>      save the developer app's Client ID (config.json, local only)
  run.py --login                  one-time sign-in (PKCE, no client secret); saves token.json (local only)
  run.py --status                 is it connected? which devices does Spotify see?

Only the Python standard library is used. config.json and token.json stay in this folder and are
never published (git-ignored).
"""
import base64
import difflib
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG, TOKEN = HERE / "config.json", HERE / "token.json"
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state playlist-read-private user-library-read"
API = "https://api.spotify.com/v1"


class Fail(Exception):
    """A clear, one-sentence reason (printed, exit code 1: Hey Claude then hands it to the model)."""


# ---------------------------------------------------------------- auth (PKCE, no secret)
def client_id():
    try:
        cid = json.loads(CONFIG.read_text(encoding="utf-8"))["client_id"].strip()
        if cid:
            return cid
    except Exception:
        pass
    raise Fail("Spotify isn't set up yet: it needs the Client ID of a Spotify developer app "
               "(run.py --setup <client id>), then a one-time login (run.py --login).")


def market():
    """Country for search / podcast episodes (config.json "market", default US). Using the account's own
    country would need one more permission (user-read-private) and a new sign-in."""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("market") or "US"
    except Exception:
        return "US"


def _post_token(fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise Fail(f"Spotify refused the sign-in ({e.code}: {e.read().decode(errors='replace')[:200]}). "
                   "Run the login again (run.py --login).")


def _save(tok):
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    old = {}
    try:
        old = json.loads(TOKEN.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not tok.get("refresh_token"):                   # Spotify may or may not rotate it
        tok["refresh_token"] = old.get("refresh_token")
    TOKEN.write_text(json.dumps(tok), encoding="utf-8")
    return tok


def access_token():
    try:
        tok = json.loads(TOKEN.read_text(encoding="utf-8"))
    except Exception:
        raise Fail("Spotify isn't connected yet: run the one-time login (run.py --login).")
    if tok.get("access_token") and time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    return _save(_post_token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                              "client_id": client_id()}))["access_token"]


def login():
    cid = client_id()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "scope": SCOPES, "redirect_uri": REDIRECT,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not self.path.startswith("/callback"):
                self.send_response(404); self.end_headers(); return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Hey Claude is connected to Spotify. You can close this tab." if ok else
                   "Spotify sign-in didn't complete. You can close this tab and try again.")
            self.wfile.write(f"<html><body style='font-family:Segoe UI;padding:40px'><h2>{msg}</h2></body></html>"
                             .encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    srv.timeout = 1
    print("Opening the Spotify sign-in page in your browser. Click Agree there.")
    webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in got and "error" not in got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if got.get("error") or "code" not in got or got.get("state") != state:
        raise Fail(f"Spotify sign-in didn't complete ({got.get('error', 'no answer within 5 minutes')}).")
    _save(_post_token({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
                       "client_id": cid, "code_verifier": verifier}))
    print("Connected: Hey Claude can now play things on Spotify.")


# ---------------------------------------------------------------- Web API
def api(method, path, params=None, body=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else (b"" if method == "PUT" else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + access_token(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw.decode()) if raw.strip() else None)
            except ValueError:                          # some player endpoints answer with a non-JSON body
                return r.status, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": {"message": raw[:200]}}


def get(path, **params):
    code, data = api("GET", path, params or None)
    if code == 401:
        raise Fail("Spotify needs signing in again (run.py --login).")
    if code >= 400:
        raise Fail(f"Spotify said no ({code}): {(data or {}).get('error', {}).get('message', '')}")
    return data or {}


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9 if min(len(a), len(b)) >= 3 else 0.5
    return difflib.SequenceMatcher(None, a, b).ratio()


def search(q, types, limit=5):
    return get("/search", q=q, type=",".join(types), limit=limit, market=market())


def first(items):
    items = [i for i in (items or []) if i]
    return items[0] if items else None


# ---------------------------------------------------------------- what to play
def parse(text):
    """-> (kind, name, artist, mine). kind: track | artist | album | playlist | show | liked | any"""
    t = re.sub(r"\s+", " ", text.strip().lower())
    t = re.sub(r"[.!?]+$", "", t)
    t = re.sub(r"\s+(?:on|in|from|using|with) spotify$", "", t)
    t = re.sub(r"\s+(?:for me|please|now|right now)$", "", t)
    t = re.sub(r"^(?:me |us )", "", t)
    if re.fullmatch(r"(?:my )?(?:liked|saved|favorite|favourite) (?:songs|music|tracks)", t):
        return "liked", "", "", True
    m = (re.fullmatch(r"(?P<my>my |the )?(?P<n>.+?) playlist", t)
         or re.fullmatch(r"(?P<my>my )?playlist (?:called |named )?(?P<n>.+)", t))
    if m:
        return "playlist", m.group("n").strip(), "", bool(m.group("my")) and m.group("my").strip() == "my"
    m = (re.fullmatch(r"(?:the )?(?:latest |newest |last |new )?(?:episode of )?(?:the )?(?P<n>.+?) podcast", t)
         or re.fullmatch(r"(?:the )?(?:latest |newest |new )?(?:podcast|episode of(?: the)?) (?P<n>.+)", t))
    if m:
        return "show", m.group("n").strip(), "", False
    m = (re.fullmatch(r"(?:the )?album (?P<n>.+?)(?: by (?P<a>.+))?", t)
         or re.fullmatch(r"(?:the )?(?P<n>.+?) album(?: by (?P<a>.+))?", t))
    if m:
        return "album", m.group("n").strip(), (m.group("a") or "").strip(), False
    m = re.fullmatch(r"(?:some |songs by |music by |something by |anything by |the artist |artist )(?P<a>.+)", t)
    if m:
        return "artist", "", m.group("a").strip(), False
    m = re.fullmatch(r"(?:the song |song )?(?P<n>.+?) by (?P<a>.+)", t)
    if m:
        return "track", m.group("n").strip(), m.group("a").strip(), False
    m = re.fullmatch(r"(?:the song |song )(?P<n>.+)", t)
    if m:
        return "track", m.group("n").strip(), "", False
    return "any", t, "", False


def my_playlist(name):
    best, score, offset = None, 0.0, 0
    while offset < 300:
        page = get("/me/playlists", limit=50, offset=offset)
        for p in page.get("items") or []:
            if not p:
                continue
            s = _similar(name, p.get("name", ""))
            if s > score:
                best, score = p, s
        if not page.get("next"):
            break
        offset += 50
    return best if score >= 0.75 else None


def resolve(text):
    """-> (play body, description) for PUT /me/player/play."""
    kind, name, artist, mine = parse(text)
    if kind == "liked":
        items = get("/me/tracks", limit=50).get("items") or []
        uris = [i["track"]["uri"] for i in items if i.get("track")]
        if not uris:
            raise Fail("Your Liked Songs are empty.")
        return {"uris": uris}, "your Liked Songs"
    if kind == "playlist":
        p = my_playlist(name)                           # your own playlists first, then everyone's
        if p is None:
            p = first(search(name, ["playlist"]).get("playlists", {}).get("items"))
        if p is None:
            raise Fail(f"I couldn't find a playlist called {name}.")
        return {"context_uri": p["uri"]}, f"the {p['name']} playlist"
    if kind == "show":
        show = first(search(name, ["show"]).get("shows", {}).get("items"))
        if show is None:
            raise Fail(f"I couldn't find a podcast called {name}.")
        ep = first(get(f"/shows/{show['id']}/episodes", limit=1, market=market()).get("items"))
        if ep is None:
            raise Fail(f"{show['name']} has no episodes I can play.")
        return {"uris": [ep["uri"]]}, f"the latest {show['name']} episode, {ep['name']}"
    if kind == "album":
        q = f"album:{name}" + (f" artist:{artist}" if artist else "")
        a = first(search(q, ["album"]).get("albums", {}).get("items")) or \
            first(search(f"{name} {artist}".strip(), ["album"]).get("albums", {}).get("items"))
        if a is None:
            raise Fail(f"I couldn't find the album {name}.")
        return {"context_uri": a["uri"]}, f"{a['name']} by {a['artists'][0]['name']}"
    if kind == "artist":
        a = first(search(artist, ["artist"]).get("artists", {}).get("items"))
        if a is None:
            raise Fail(f"I couldn't find {artist} on Spotify.")
        return {"context_uri": a["uri"]}, a["name"]
    if kind == "track":
        tr = None
        if artist:
            tr = first(search(f"track:{name} artist:{artist}", ["track"]).get("tracks", {}).get("items"))
        tr = tr or first(search(f"{name} {artist}".strip(), ["track"]).get("tracks", {}).get("items"))
        if tr is None:
            raise Fail(f"I couldn't find {name}" + (f" by {artist}" if artist else "") + " on Spotify.")
        return {"uris": [tr["uri"]]}, f"{tr['name']} by {tr['artists'][0]['name']}"
    # anything: an artist if the name is an exact artist, else your playlist with that name, else a song
    res = search(name, ["track", "artist", "album", "playlist"])
    art = first(res.get("artists", {}).get("items"))
    if art and _similar(name, art["name"]) >= 0.95:
        return {"context_uri": art["uri"]}, art["name"]
    p = my_playlist(name)
    if p is not None and _similar(name, p["name"]) >= 0.9:
        return {"context_uri": p["uri"]}, f"the {p['name']} playlist"
    tr = first(res.get("tracks", {}).get("items"))
    if tr is not None:
        return {"uris": [tr["uri"]]}, f"{tr['name']} by {tr['artists'][0]['name']}"
    alb = first(res.get("albums", {}).get("items"))
    if alb is not None:
        return {"context_uri": alb["uri"]}, f"{alb['name']} by {alb['artists'][0]['name']}"
    raise Fail(f"I couldn't find {name} on Spotify.")


# ---------------------------------------------------------------- where to play
def pick_device():
    """The device that's playing now; else this computer's Spotify app (it plays through the Windows
    default output, e.g. the SoundCore); else any. Starts the Spotify app if no device is around."""
    for attempt in range(2):
        devices = get("/me/player/devices").get("devices") or []
        devices = [d for d in devices if not d.get("is_restricted")]
        active = [d for d in devices if d.get("is_active")]
        if active:
            return active[0]
        host = socket.gethostname().lower()
        here = [d for d in devices if d.get("type") == "Computer" and d.get("name", "").lower() == host]
        computers = [d for d in devices if d.get("type") == "Computer"]
        if here or computers or devices:
            return (here or computers or devices)[0]
        if attempt == 0:                                # nothing running: open Spotify on this laptop
            os.startfile("spotify:")
            deadline = time.time() + 15
            while time.time() < deadline:
                time.sleep(1.0)
                if get("/me/player/devices").get("devices"):
                    break
    raise Fail("No Spotify device is available (is the Spotify app signed in?).")


def play(text):
    body, what = resolve(text)
    dev = pick_device()
    code, data = api("PUT", "/me/player/play", {"device_id": dev["id"]}, body)
    if code == 404:                                     # device went to sleep: wake it, then retry once
        api("PUT", "/me/player", None, {"device_ids": [dev["id"]], "play": False})
        time.sleep(1.0)
        code, data = api("PUT", "/me/player/play", {"device_id": dev["id"]}, body)
    if code == 403:
        raise Fail("Spotify won't start playback through the API (it needs Spotify Premium).")
    if code >= 400:
        raise Fail(f"Spotify couldn't start playing ({code}: {(data or {}).get('error', {}).get('message', '')}).")
    print(f"Playing {what}.")


def status():
    cid = client_id()
    access_token()
    me = get("/me")
    devs = get("/me/player/devices").get("devices") or []
    print(f"Connected as {me.get('display_name') or me.get('id')} ({me.get('product')}), client ID {cid[:6]}...")
    for d in devs:
        print(f"  device: {d['name']} ({d['type']}){' - active' if d.get('is_active') else ''}")


def main(argv):
    try:
        if not argv:
            raise Fail("Tell me what to play.")
        if argv[0] == "--setup":
            cid = (argv[1] if len(argv) > 1 else "").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", cid):
                raise Fail("That doesn't look like a Spotify Client ID (32 letters and digits).")
            CONFIG.write_text(json.dumps({"client_id": cid, "market": "US"}), encoding="utf-8")
            print("Saved the Client ID. Next: run.py --login")
        elif argv[0] == "--login":
            login()
        elif argv[0] == "--status":
            status()
        elif argv[0] == "--parse":                      # testing: how a phrase is understood
            print(parse(" ".join(argv[1:])))
        else:
            play(" ".join(argv))
        return 0
    except Fail as e:
        print(str(e))
        return 1
    except urllib.error.URLError as e:
        print(f"Couldn't reach Spotify ({e.reason}).")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
