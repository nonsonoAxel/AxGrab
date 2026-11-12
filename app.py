"""
AxGrab — GUI leggera per yt-dlp (pywebview + HTML/CSS).
Creato da nonsonoAxel · https://github.com/nonsonoAxel/AxGrab

Avvio in sviluppo:  python app.py      Build:  crea-exe.bat
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

import webview

APP_NAME = "AxGrab"
APP_VERSION = "1.0.1"            # da aumentare a ogni nuova release su GitHub (es. 1.0.1, 1.1.0)
GITHUB_REPO = "nonsonoAxel/AxGrab"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home()))
DATA_DIR = _LOCAL / APP_NAME


def _migrate_old_data():
    """Porta i dati dal vecchio nome (Grabby): per ogni file tiene la versione più recente."""
    old = _LOCAL / "Grabby"
    if not old.is_dir():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for item in old.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(old)
            target = DATA_DIR / ("axgrab.log" if rel.name == "grabby.log" else rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or item.stat().st_mtime > target.stat().st_mtime:
                os.replace(item, target)
        shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


_migrate_old_data()
BIN_DIR = DATA_DIR / "bin"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
CACHE_DIR = DATA_DIR / "cache"

# yt-dlp "a cartella": non deve estrarsi in una cartella temporanea a ogni avvio (analisi più rapida)
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_win.zip"
YTDLP_LATEST = "https://github.com/yt-dlp/yt-dlp/releases/latest"
# ffmpeg "shared": ffmpeg e ffprobe condividono le stesse librerie (~100 MB invece di ~390 MB)
FFMPEG_URL = ("https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl-shared.zip")
# Deno: runtime JavaScript che yt-dlp usa per le "sfide" di YouTube
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
YTDLP_DIR = BIN_DIR / "yt-dlp"
FFMPEG_DIR = BIN_DIR / "ffmpeg"
OWN_EXE = {"yt-dlp": YTDLP_DIR / "yt-dlp.exe", "ffmpeg": FFMPEG_DIR / "ffmpeg.exe",
           "deno": BIN_DIR / "deno.exe"}
LEGACY_FILES = ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe")   # versioni a file singolo, più pesanti

# copia dei cookie dei soli siti video: evita di rileggere (e decifrare) il browser a ogni
# analisi e permette di usarli anche quando un browser Chromium è aperto e blocca il suo file
COOKIE_FILE = DATA_DIR / "cookies.txt"
COOKIE_META = DATA_DIR / "cookies.json"
COOKIE_MAX_AGE = 30 * 60      # dopo 30 minuti si prova a rileggerli dal browser
NEEDS_COOKIES_FILE = DATA_DIR / "siti-con-cookie.json"
NEEDS_COOKIES_FOR = 12 * 3600  # un sito che ha chiesto i cookie li riceve subito per 12 ore
COOKIE_DOMAINS = ("youtube.com", "youtu.be", "google.com", "googlevideo.com", "instagram.com",
                  "facebook.com", "fb.watch", "threads.net", "tiktok.com", "x.com", "twitter.com",
                  "twitch.tv", "reddit.com", "vimeo.com", "soundcloud.com", "dailymotion.com",
                  "bilibili.com", "patreon.com", "pinterest.com", "linkedin.com", "snapchat.com")

# siti dove quasi sempre serve il login: lì i cookie si usano subito
LOGIN_SITES = ("instagram.com", "facebook.com", "fb.watch", "threads.net", "patreon.com")
AUTH_ERRORS = ("sign in", "log in", "login", "logged-in", "cookies", "private", "age-restricted",
               "age restricted", "inappropriate", "registered users", "not a bot", "members-only",
               "members only", "rate-limit", "rate limit", "authentication", "account")

NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA_DIR / "axgrab.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(APP_NAME)

DEFAULT_SETTINGS = {
    "folder": str(Path.home() / "Downloads"),
    "mode": "video",          # video | audio
    "quality": "1080",        # best | 2160 | 1440 | 1080 | 720 | 480
    "audio_format": "mp3",    # mp3 | m4a
    "cookies": "none",        # none | firefox | zen | helium | chrome | edge | brave
    "cookie_when": "auto",    # auto (solo quando servono) | always
    "playlist": False,
    "parallel": 2,
}


def resource_path(rel: str) -> str:
    """Percorso dei file UI sia in sviluppo che dentro l'.exe di PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


APPDATA = os.environ.get("APPDATA", "")
LOCAL = os.environ.get("LOCALAPPDATA", "")
# browser non riconosciuti per nome da yt-dlp: (motore, cartella profili, file cookie nel profilo)
CUSTOM_BROWSERS = {
    "zen": ("firefox", os.path.join(APPDATA, "zen", "Profiles"), "cookies.sqlite"),
    "helium": ("chromium", os.path.join(LOCAL, "imput", "Helium", "User Data"),
               os.path.join("Network", "Cookies")),
}
BROWSER_NAMES = {"firefox": "Firefox", "zen": "Zen", "chrome": "Chrome", "edge": "Edge",
                 "brave": "Brave", "helium": "Helium"}


def newest_profile(root: Path, cookie_file: str):
    """Cartella del profilo con il file dei cookie modificato più di recente."""
    best = None
    try:
        for d in root.iterdir():
            f = d / cookie_file
            if d.is_dir() and f.exists():
                m = f.stat().st_mtime
                if not best or m > best[0]:
                    best = (m, d)
    except OSError:
        return None
    return str(best[1]) if best else None


COOKIE_ERRORS = ("dpapi", "decrypt", "cookie database", "cookies database", "could not find",
                 "failed to load cookies", "app-bound", "permission denied")


def is_cookie_error(msg: str) -> bool:
    m = (msg or "").lower()
    return ("cookie" in m or "dpapi" in m or "decrypt" in m) and any(k in m for k in COOKIE_ERRORS)


def is_auth_error(msg: str) -> bool:
    """L'errore fa pensare che servisse il login (quindi i cookie)."""
    m = (msg or "").lower()
    return any(k in m for k in AUTH_ERRORS)


def version_tuple(v: str):
    """'1.10.2' -> (1, 10, 2): confronto corretto anche con numeri a più cifre."""
    out = []
    for part in (v or "0").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Api:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for old in CACHE_DIR.glob("*.json"):     # analisi rimaste da sessioni precedenti
            old.unlink(missing_ok=True)
        self._info_cache = {}
        self._cookie_reads = set()      # file temporanei in cui yt-dlp sta leggendo dal browser
        self._needs_cookies = load_json(NEEDS_COOKIES_FILE, {})
        self._window = None
        self._hwnd = 0
        self._maximized = False
        self._restore_rect = None
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self.history = load_json(HISTORY_FILE, [])
        self._jobs = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max(1, int(self.settings["parallel"])))

    # ---------- utilità ----------
    def _emit(self, event: str, payload):
        if self._window:
            try:
                self._window.evaluate_js(
                    f"window.onBackend && window.onBackend({json.dumps(event)}, {json.dumps(payload)})")
            except Exception:
                pass

    def _exe(self, name):
        """Componente installato dall'app; in mancanza, vecchia installazione o quello di sistema."""
        for p in (OWN_EXE[name], BIN_DIR / f"{name}.exe"):
            if p.exists():
                return str(p)
        return shutil.which(name)

    def _download_file(self, url, dest: Path, label, event="setup"):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    self._emit(event, {"label": label, "done": done, "total": total})
            tmp.replace(dest)

    # ---------- dipendenze ----------
    def check_deps(self):
        """Pronti solo i componenti installati dall'app nella versione attuale (leggera)."""
        have = {k: OWN_EXE[k].exists() for k in OWN_EXE}
        log.info("check_deps: %s", have)
        return {"ytdlp": have["yt-dlp"], "ffmpeg": have["ffmpeg"], "deno": have["deno"]}

    def _install_zip(self, url, label, dest: Path, keep):
        """Scarica uno zip ed estrae in dest i file per cui keep(percorso_nello_zip) restituisce
        il percorso relativo di destinazione (o None per scartarlo). Sostituisce dest solo a fine lavoro."""
        zpath = BIN_DIR / f"{dest.name}.zip"
        self._download_file(url, zpath, label)
        self._emit("setup", {"label": f"Estrazione {label}", "done": 1, "total": 1})
        tmp = dest.with_name(dest.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                rel = keep(m.filename)
                if rel:
                    out = tmp / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        zpath.unlink(missing_ok=True)
        old = dest.with_name(dest.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.rename(old)
        tmp.rename(dest)
        shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _keep_ytdlp(name):
        # yt-dlp_win.zip: yt-dlp.exe + cartella _internal (eventualmente dentro una cartella radice)
        parts = name.replace("\\", "/").split("/")
        for i, p in enumerate(parts):
            if p == "yt-dlp.exe" or p == "_internal":
                return "/".join(parts[i:])
        return None

    @staticmethod
    def _keep_ffmpeg(name):
        # versione "shared": solo ffmpeg.exe, ffprobe.exe e le librerie .dll che condividono
        base = name.replace("\\", "/").split("/")
        if len(base) >= 2 and base[-2] == "bin" and (base[-1] in ("ffmpeg.exe", "ffprobe.exe")
                                                      or base[-1].endswith(".dll")):
            return base[-1]
        return None

    def install_deps(self):
        try:
            t0 = time.time()
            if not OWN_EXE["yt-dlp"].exists():
                self._install_zip(YTDLP_URL, "yt-dlp", YTDLP_DIR, self._keep_ytdlp)
            if not OWN_EXE["ffmpeg"].exists():
                self._install_zip(FFMPEG_URL, "ffmpeg", FFMPEG_DIR, self._keep_ffmpeg)
            if not OWN_EXE["deno"].exists():
                self._install_zip(DENO_URL, "Deno", BIN_DIR / "deno-dir",
                                  lambda n: "deno.exe" if n.endswith("deno.exe") else None)
                os.replace(BIN_DIR / "deno-dir" / "deno.exe", OWN_EXE["deno"])
                shutil.rmtree(BIN_DIR / "deno-dir", ignore_errors=True)
            # versioni precedenti, più pesanti: non servono più
            for old in LEGACY_FILES:
                (BIN_DIR / old).unlink(missing_ok=True)
            log.info("install_deps completato in %.1fs, componenti: %.0f MB",
                     time.time() - t0, dir_size(BIN_DIR) / 2**20)
            return {"ok": True, **self.check_deps()}
        except Exception as e:
            log.exception("install_deps fallito")
            return {"ok": False, "error": str(e)}

    def update_ytdlp(self):
        """La versione 'a cartella' di yt-dlp non si aggiorna da sola: se c'è una versione
        nuova su GitHub la riscarico."""
        try:
            req = urllib.request.Request(YTDLP_LATEST, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                latest = r.geturl().rstrip("/").rsplit("/", 1)[-1]
            current = self.version()
            if current and current == latest:
                return {"ok": True, "message": f"yt-dlp è già aggiornato ({current})"}
            self._install_zip(YTDLP_URL, "yt-dlp", YTDLP_DIR, self._keep_ytdlp)
            return {"ok": True, "message": f"yt-dlp aggiornato: {current or '—'} → {self.version()}"}
        except Exception as e:
            log.exception("update_ytdlp fallito")
            return {"ok": False, "error": str(e)}

    def version(self):
        exe = self._exe("yt-dlp")
        if not exe:
            return ""
        try:
            return subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  creationflags=NO_WINDOW, timeout=30).stdout.strip()
        except Exception:
            return ""

    # ---------- impostazioni ----------
    def get_settings(self):
        return self.settings

    def save_settings(self, data):
        old_parallel = self.settings.get("parallel")
        if "cookies" in data and data["cookies"] != self.settings.get("cookies"):
            self.forget_cookies()       # cambiato browser: la copia salvata non vale più
        self.settings.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        save_json(SETTINGS_FILE, self.settings)
        if self.settings["parallel"] != old_parallel:
            self._slots = threading.Semaphore(max(1, int(self.settings["parallel"])))
        return self.settings

    def choose_folder(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG,
                                             directory=self.settings["folder"])
        if res:
            folder = res[0] if isinstance(res, (list, tuple)) else res
            self.save_settings({"folder": folder})
            return folder
        return None

    def paste(self):
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW, timeout=10)
            return p.stdout.strip()
        except Exception:
            return ""

    # ---------- anteprima ----------
    def _base_args(self, cookies=False, cookie_tmp=None):
        """cookies: False = nessuno · True = copia salvata se recente, altrimenti lettura dal browser
        (e nuova copia) · "saved" = solo la copia salvata, anche se vecchia."""
        args = ["--encoding", "utf-8", "--no-warnings"]
        ff = self._exe("ffmpeg")
        if ff:
            args += ["--ffmpeg-location", os.path.dirname(ff)]
        deno = self._exe("deno")
        if deno:
            args += ["--js-runtimes", f"deno:{deno}"]
        if cookies and cookie_tmp:
            # yt-dlp legge e alla fine riscrive i cookie in un file temporaneo per questa esecuzione
            if cookies == "saved" or self._saved_cookies_fresh() or (
                    self._browser_locked() and COOKIE_FILE.exists()):
                shutil.copyfile(COOKIE_FILE, cookie_tmp)
            else:
                spec = self._cookie_spec()
                if spec:
                    args += ["--cookies-from-browser", spec]
                    self._cookie_reads.add(str(cookie_tmp))
            args += ["--cookies", str(cookie_tmp)]
        return args

    # ---------- copia salvata dei cookie ----------
    def _saved_cookies_fresh(self):
        meta = load_json(COOKIE_META, {})
        return (COOKIE_FILE.exists() and meta.get("browser") == self.settings["cookies"]
                and time.time() - meta.get("read_at", 0) < COOKIE_MAX_AGE)

    def _browser_locked(self):
        """Helium e gli altri Chromium bloccano il file dei cookie mentre sono aperti."""
        b = self.settings["cookies"]
        if b not in CUSTOM_BROWSERS or CUSTOM_BROWSERS[b][0] != "chromium":
            return False
        _, root, cookie_file = CUSTOM_BROWSERS[b]
        profile = newest_profile(Path(root), cookie_file)
        if not profile:
            return False
        try:
            with open(Path(profile) / cookie_file, "rb"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _new_cookie_tmp(self):
        return CACHE_DIR / f"ck-{uuid.uuid4().hex}.txt"

    def _store_cookies(self, tmp: Path):
        """Tiene solo i cookie dei siti video (non quelli di banche, posta, ecc.)."""
        try:
            if not tmp.exists() or tmp.stat().st_size == 0:
                return
            keep = []
            for line in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
                dom = line[10:] if line.startswith("#HttpOnly_") else line
                if line.startswith("#") and not line.startswith("#HttpOnly_"):
                    keep.append(line)
                    continue
                dom = dom.split("\t", 1)[0].lstrip(".").lower()
                if dom and any(dom == d or dom.endswith("." + d) for d in COOKIE_DOMAINS):
                    keep.append(line)
            if len(keep) > 3:
                with self._lock:
                    COOKIE_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
                    if str(tmp) in self._cookie_reads:   # letti ora dal browser: copia "fresca"
                        save_json(COOKIE_META, {"browser": self.settings["cookies"], "read_at": time.time()})
        except OSError:
            log.exception("salvataggio cookie fallito")
        finally:
            self._cookie_reads.discard(str(tmp))
            tmp.unlink(missing_ok=True)

    def forget_cookies(self):
        COOKIE_FILE.unlink(missing_ok=True)
        COOKIE_META.unlink(missing_ok=True)
        return True

    def _cookie_spec(self):
        """Valore per --cookies-from-browser. Zen e Helium non sono supportati per nome da
        yt-dlp: si passa il motore (firefox/chromium) con il percorso del profilo più recente."""
        b = self.settings["cookies"]
        if b == "none":
            return None
        if b in CUSTOM_BROWSERS:
            engine, root, cookie_file = CUSTOM_BROWSERS[b]
            profile = newest_profile(Path(root), cookie_file)
            return f"{engine}:{profile}" if profile else None
        return b

    def _cookie_warning(self, err=""):
        b = BROWSER_NAMES.get(self.settings["cookies"], self.settings["cookies"])
        e = err.lower()
        if "copy" in e or "permission" in e:
            why = f"{b} è aperto e blocca i cookie: chiudilo se ti servono"
        elif "dpapi" in e or "decrypt" in e:
            why = f"{b} cifra i cookie in modo non leggibile: usa Zen o Firefox"
        elif is_cookie_error(err):
            why = f"non riesco a leggere i cookie di {b}"
        else:
            why = f"con i cookie di {b} il sito non ha risposto bene"
        return f"{why[0].upper()}{why[1:]}. Proseguo senza cookie (per i video pubblici va bene)."

    def _cookie_plan(self, url):
        """Ordine dei tentativi (True = con cookie). In modalità 'auto' i cookie si usano subito
        sui siti che chiedono quasi sempre il login e su quelli che li hanno chiesti di recente
        (es. YouTube con la verifica anti-bot); altrove solo se il tentativo senza cookie fallisce
        per un motivo di accesso."""
        if not self._cookie_spec():
            return [False]
        host = url.lower()
        if (self.settings.get("cookie_when") == "always" or any(s in host for s in LOGIN_SITES)
                or self._site_needs_cookies(url)):
            return [True, False]
        return [False, True]

    # siti che di recente hanno rifiutato le richieste senza cookie
    def _site_key(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
        host = {"youtu.be": "youtube.com", "fb.watch": "facebook.com", "vm.tiktok.com": "tiktok.com",
                "twitter.com": "x.com"}.get(host, host)
        return ".".join(host.split(".")[-2:])

    def _site_needs_cookies(self, url):
        t = self._needs_cookies.get(self._site_key(url), 0)
        return time.time() - t < NEEDS_COOKIES_FOR

    def _remember_needs_cookies(self, url):
        self._needs_cookies[self._site_key(url)] = time.time()
        save_json(NEEDS_COOKIES_FILE, self._needs_cookies)

    def _should_retry(self, used_cookies, next_cookies, err):
        if used_cookies and not next_cookies:
            # senza cookie ha senso riprovare solo se i cookie non si leggevano: se il sito li ha
            # letti e chiede comunque l'accesso, senza cookie fallirebbe di nuovo
            return is_cookie_error(err) or not is_auth_error(err)
        if used_cookies is True and next_cookies == "saved":
            return True                       # browser illeggibile: usa la copia salvata
        return is_auth_error(err)             # senza cookie serviva il login: prova con

    def _extend_plan(self, plan, i, used, err):
        """Se la lettura dal browser fallisce (es. Helium aperto) e c'è una copia salvata,
        il tentativo successivo usa quella."""
        if used is True and is_cookie_error(err) and COOKIE_FILE.exists() and "saved" not in plan:
            plan.insert(i + 1, "saved")

    def _friendly(self, err, tried_cookies):
        """Messaggio comprensibile al posto dell'errore tecnico di yt-dlp."""
        b = BROWSER_NAMES.get(self.settings["cookies"], self.settings["cookies"])
        e = (err or "").lower()
        if "not a bot" in e or "confirm you" in e:
            if self.settings["cookies"] == "none":
                return ("YouTube chiede di confermare che non sei un bot. In Impostazioni → Cookie dal browser "
                        "scegli il browser in cui hai fatto l'accesso a YouTube.")
            return (f"YouTube chiede la verifica anti-bot anche con i cookie di {b}. Controlla di aver fatto "
                    f"l'accesso a YouTube in {b}, oppure riprova tra un po'.")
        if "could not copy" in e and "cookie" in e:
            return (f"{b} è aperto e blocca i suoi cookie. Chiudilo una volta e riprova: da quel momento AxGrab "
                    "li ricorda anche con il browser aperto.")
        if "dpapi" in e or "decrypt" in e:
            return f"{b} cifra i cookie in un modo che non si può leggere. Scegli un altro browser nelle Impostazioni."
        if "requested format is not available" in e:
            return "La qualità scelta non è disponibile per questo video: prova con Max."
        if any(k in e for k in ("private", "login required", "log in", "sign in", "requiring login",
                                "unreachable", "need to log in")):
            return ("Il contenuto richiede l'accesso. In Impostazioni → Cookie dal browser scegli il browser "
                    "in cui hai fatto l'accesso a quel sito." if not tried_cookies else
                    f"Il contenuto richiede l'accesso: controlla di averlo fatto in {b} con un account che può vederlo.")
        if "video unavailable" in e or "not available" in e:
            return "Video non disponibile: rimosso, privato o bloccato nel tuo paese."
        if "unsupported url" in e:
            return "Questo link non è supportato."
        if any(k in e for k in ("timed out", "getaddrinfo", "connection", "network is unreachable")):
            return "Problema di connessione: controlla internet e riprova."
        return err.replace("ERROR: ", "")

    def get_info(self, url):
        exe = self._exe("yt-dlp")
        if not exe:
            return {"ok": False, "error": "yt-dlp non installato"}
        warning = None
        plan = self._cookie_plan(url)
        i = 0
        while i < len(plan):
            use_cookies = plan[i]
            tmp = self._new_cookie_tmp() if use_cookies else None
            args = [exe, *self._base_args(use_cookies, tmp), "-J", "--flat-playlist"]
            args.append("--yes-playlist" if self.settings["playlist"] else "--no-playlist")
            args.append(url)
            t0 = time.time()
            try:
                p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", creationflags=NO_WINDOW, timeout=90)
            except subprocess.TimeoutExpired:
                log.warning("analisi: timeout dopo 90s (cookie=%s) %s", use_cookies, url)
                return {"ok": False, "error": "L'analisi ha impiegato troppo (oltre 90 secondi). Riprova."}
            finally:
                if tmp:
                    self._store_cookies(tmp)
            ok = p.returncode == 0 and p.stdout.strip()
            err = [l for l in p.stderr.splitlines() if "ERROR" in l]
            err = (err[-1] if err else p.stderr.strip()[-300:]) or "Errore sconosciuto"
            log.info("analisi %.1fs cookie=%s ok=%s %s%s", time.time() - t0, use_cookies, bool(ok), url,
                     "" if ok else " | " + err[:200])
            if ok:
                break
            if not use_cookies and is_auth_error(err) and self._cookie_spec():
                self._remember_needs_cookies(url)     # la prossima volta cookie subito
            self._extend_plan(plan, i, use_cookies, err)
            nxt = plan[i + 1] if i + 1 < len(plan) else None
            if nxt is not None and self._should_retry(use_cookies, nxt, err):
                if use_cookies and not nxt:
                    warning = self._cookie_warning(err)
                i += 1
                continue
            return {"ok": False, "error": self._friendly(err, any(plan[:i + 1])), "detail": err}
        d = json.loads(p.stdout)
        is_pl = d.get("_type") == "playlist"
        if not is_pl:
            # salva l'analisi: il download la riusa e salta 2-3 secondi di estrazione
            path = CACHE_DIR / f"{uuid.uuid4().hex}.json"
            path.write_text(p.stdout, encoding="utf-8")
            self._info_cache[url] = (path, time.time(), use_cookies)
        thumb = d.get("thumbnail")
        if not thumb and d.get("thumbnails"):
            thumb = d["thumbnails"][-1].get("url")
        if is_pl and not thumb:
            for e in d.get("entries") or []:
                t = e.get("thumbnails")
                if t:
                    thumb = t[-1].get("url")
                    break
        heights = sorted({f.get("height") for f in d.get("formats") or [] if f.get("height")}, reverse=True)
        return {
            "ok": True,
            "title": d.get("title") or url,
            "uploader": d.get("uploader") or d.get("channel") or d.get("extractor_key") or "",
            "duration": d.get("duration"),
            "thumbnail": thumb,
            "site": d.get("extractor_key") or "",
            "playlist": is_pl,
            "count": len(d.get("entries") or []) if is_pl else 1,
            "heights": heights[:8],
            "warning": warning,
        }

    # ---------- download ----------
    def _format_args(self, opts):
        if opts["mode"] == "audio":
            return ["-f", "ba/b", "-x", "--audio-format", opts["audio_format"], "--audio-quality", "0",
                    "--embed-thumbnail", "--embed-metadata"]
        q = opts["quality"]
        sort = "ext:mp4:m4a" if q == "best" else f"res:{q},ext:mp4:m4a"
        return ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", "mp4", "--embed-metadata"]

    def start_download(self, url, info=None, overrides=None):
        opts = {**self.settings, **(overrides or {})}
        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id, "url": url, "status": "queued", "percent": 0,
            "title": (info or {}).get("title") or url,
            "thumbnail": (info or {}).get("thumbnail"),
            "site": (info or {}).get("site", ""),
            "mode": opts["mode"],
            "quality": opts["audio_format"].upper() if opts["mode"] == "audio" else
                       ("Max" if opts["quality"] == "best" else opts["quality"] + "p"),
            "folder": opts["folder"], "file": None, "proc": None, "cancel": False,
            "item": "", "speed": None, "eta": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        self._emit("job", self._public(job))
        threading.Thread(target=self._run_job, args=(job, opts), daemon=True).start()
        return job_id

    def _public(self, job):
        return {k: v for k, v in job.items() if k not in ("proc",)}

    def _run_job(self, job, opts):
        with self._slots:
            if job["cancel"]:
                return
            try:
                Path(opts["folder"]).mkdir(parents=True, exist_ok=True)
                cached = self._take_cache(job["url"])
                # tentativi: analisi già fatta (con lo stesso uso dei cookie) -> piano dei cookie
                attempts = ([(cached[1], cached[0])] if cached else [])
                attempts += [(c, None) for c in self._cookie_plan(job["url"])]
                code, err = 1, ""
                t0 = time.time()
                i = 0
                while i < len(attempts):
                    cookies, info_file = attempts[i]
                    if i and job["cancel"]:
                        break
                    if i:
                        prev_cookies, prev_file = attempts[i - 1]
                        # dopo un tentativo dall'analisi salvata si riprova sempre; altrimenti
                        # si segue la regola dei cookie
                        if not prev_file and not self._should_retry(prev_cookies, cookies, err):
                            break
                        if prev_cookies and not cookies:
                            job["warning"] = self._cookie_warning(err)
                    job["percent"] = 0
                    ta = time.time()
                    code, err = self._run_proc(job, opts, cookies, info_file)
                    log.info("download tentativo %d (cookie=%s, analisi salvata=%s): codice %s, %.1fs%s",
                             i + 1, cookies, bool(info_file), code, time.time() - ta,
                             "" if code == 0 else " | " + err[:200])
                    if code == 0:
                        break
                    if not cookies and is_auth_error(err) and self._cookie_spec():
                        self._remember_needs_cookies(job["url"])
                    if cookies is True and is_cookie_error(err) and COOKIE_FILE.exists() and \
                            not any(c == "saved" for c, _ in attempts):
                        attempts.insert(i + 1, ("saved", None))
                    i += 1
                if code == 0 and job.get("file") and os.path.exists(job["file"]):
                    mb = os.path.getsize(job["file"]) / 2**20
                    secs = time.time() - t0
                    log.info("completato %.1f MB in %.1fs (media %.1f MB/s, %s %s) %s", mb, secs,
                             mb / max(secs, 0.1), opts["mode"], job["quality"], job["url"])
                if cached:
                    cached[0].unlink(missing_ok=True)
                if job["cancel"]:
                    job["status"] = "cancelled"
                elif code == 0:
                    job["status"], job["percent"] = "done", 100
                    self._add_history(job)
                else:
                    job["status"] = "error"
                    raw = err or f"yt-dlp ha restituito codice {code}"
                    job["error"] = self._friendly(raw, any(c for c, _ in attempts))
                    job["detail"] = raw
            except Exception as e:
                job["status"], job["error"] = "error", str(e)
            job["proc"] = None
            self._emit("job", self._public(job))

    def _take_cache(self, url):
        """Analisi salvata dall'anteprima, se recente (i link dei formati scadono)."""
        entry = self._info_cache.pop(url, None)
        if entry and time.time() - entry[1] < 20 * 60 and entry[0].exists():
            return entry[0], entry[2]          # (file dell'analisi, usava i cookie)
        return None

    def _run_proc(self, job, opts, cookies, info_file=None):
        """Esegue yt-dlp una volta; restituisce (codice di uscita, ultimo errore)."""
        exe = self._exe("yt-dlp")
        source = ["--load-info-json", str(info_file)] if info_file else [job["url"]]
        tmp = self._new_cookie_tmp() if cookies else None
        try:
            return self._run_proc_inner(job, opts, exe, source, cookies, tmp)
        finally:
            if tmp:
                self._store_cookies(tmp)

    def _run_proc_inner(self, job, opts, exe, source, cookies, tmp):
        args = [exe, *self._base_args(cookies, tmp), *self._format_args(opts),
                "-N", "4",   # frammenti in parallelo (HLS/DASH: Instagram, TikTok, Twitch, X…)
                "--yes-playlist" if opts["playlist"] else "--no-playlist",
                "-P", opts["folder"],
                "-o", "%(title).150B [%(id)s].%(ext)s",
                "-o", "pl_video:%(playlist_title).80B/%(playlist_index)03d - %(title).120B.%(ext)s",
                "--windows-filenames", "--newline", "--progress", "--no-simulate",
                "--progress-template",
                "download:@@P|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
                "--print", "before_dl:@@T|%(title)s|%(playlist_index)s|%(n_entries)s",
                "--print", "after_move:@@F|%(filepath)s",
                *source]
        job["status"] = "downloading"
        self._emit("job", self._public(job))
        last_err = ""
        last_emit = 0
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=NO_WINDOW, bufsize=1)
        job["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@P|"):
                _, done, total, est, speed, eta = (line.split("|") + [""] * 6)[:6]
                done, total = num(done), num(total) or num(est)
                if done and total:
                    job["percent"] = min(100.0, done / total * 100)
                job["speed"], job["eta"] = num(speed), num(eta)
                now = time.time()
                if now - last_emit > 0.25:
                    last_emit = now
                    self._emit("job", self._public(job))
            elif line.startswith("@@T|"):
                parts = line.split("|")
                idx, n, title = parts[-2], parts[-1], "|".join(parts[1:-2])
                job["percent"], job["status"] = 0, "downloading"
                if idx not in ("NA", "None") and n not in ("NA", "None"):
                    job["item"] = f"{idx}/{n}"      # playlist: titolo resta quello della playlist
                    job["current"] = title
                elif job["title"] == job["url"]:
                    job["title"] = title
                self._emit("job", self._public(job))
            elif line.startswith("@@F|"):
                job["file"] = line[4:]
            elif "[Merger]" in line or "[ExtractAudio]" in line:
                job["status"] = "processing"
                self._emit("job", self._public(job))
            elif line.startswith("ERROR"):
                last_err = line.replace("ERROR: ", "")
        proc.wait()
        return proc.returncode, last_err

    def cancel(self, job_id):
        job = self._jobs.get(job_id)
        if not job:
            return False
        job["cancel"] = True
        proc = job.get("proc")
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        elif job["status"] == "queued":
            job["status"] = "cancelled"
            self._emit("job", self._public(job))
        return True

    def retry(self, job_id):
        job = self._jobs.pop(job_id, None)
        if not job:
            return None
        return self.start_download(job["url"], {"title": job["title"], "thumbnail": job["thumbnail"],
                                                "site": job["site"]})

    def clear_finished(self):
        with self._lock:
            for k in [k for k, j in self._jobs.items() if j["status"] in ("done", "error", "cancelled")]:
                del self._jobs[k]
        return True

    # ---------- cronologia ----------
    def _add_history(self, job):
        entry = {"title": job["title"], "url": job["url"], "thumbnail": job["thumbnail"],
                 "site": job["site"], "file": job["file"], "folder": job["folder"],
                 "mode": job["mode"], "quality": job["quality"], "date": time.strftime("%Y-%m-%d %H:%M")}
        self.history.insert(0, entry)
        self.history = self.history[:300]
        save_json(HISTORY_FILE, self.history)
        self._emit("history", self.history)

    def get_history(self):
        return self.history

    def clear_history(self):
        self.history = []
        save_json(HISTORY_FILE, self.history)
        return True

    # ---------- file ----------
    def open_file(self, path):
        if path and os.path.exists(path):
            os.startfile(path)
            return True
        return False

    def open_folder(self, path=None, file=None):
        if file and os.path.exists(file):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(file)])
        else:
            target = path or self.settings["folder"]
            if os.path.isdir(target):
                os.startfile(target)
        return True

    # ---------- finestra senza bordo: barra integrata ----------
    def _on_shown(self):
        try:
            self._hwnd = int(self._window.native.Handle.ToInt64())
        except Exception:
            self._hwnd = user32.FindWindowW(None, APP_NAME)
        self._set_corners(rounded=True)

    def _set_corners(self, rounded):
        # Windows 11: angoli arrotondati anche senza bordo (ignorato su Windows 10)
        pref = ctypes.c_int(2 if rounded else 1)
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass

    def _rect(self):
        r = wt.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def _place(self, x, y, w, h):
        user32.SetWindowPos(self._hwnd, 0, int(x), int(y), int(w), int(h), 0x0014)

    def _follow_mouse(self, step):
        """Chiama step(dx, dy) finché il tasto sinistro resta premuto."""
        start = wt.POINT()
        user32.GetCursorPos(ctypes.byref(start))
        p = wt.POINT()
        while user32.GetAsyncKeyState(0x01) & 0x8000:
            user32.GetCursorPos(ctypes.byref(p))
            step(p.x - start.x, p.y - start.y)
            time.sleep(0.008)

    def win_drag(self):
        if not self._hwnd or self._maximized:
            return
        x, y, w, h = self._rect()
        self._follow_mouse(lambda dx, dy: self._place(x + dx, y + dy, w, h))

    def win_resize(self, edge):
        if not self._hwnd or self._maximized:
            return
        x, y, w, h = self._rect()
        scale = user32.GetDpiForWindow(self._hwnd) / 96 if hasattr(user32, "GetDpiForWindow") else 1
        mw, mh = MIN_SIZE[0] * scale, MIN_SIZE[1] * scale

        def step(dx, dy):
            nx, ny, nw, nh = x, y, w, h
            if "e" in edge:
                nw = max(mw, w + dx)
            if "s" in edge:
                nh = max(mh, h + dy)
            if "w" in edge:
                nw = max(mw, w - dx)
                nx = x + w - nw
            if "n" in edge:
                nh = max(mh, h - dy)
                ny = y + h - nh
            self._place(nx, ny, nw, nh)
        self._follow_mouse(step)

    def win_minimize(self):
        user32.ShowWindow(self._hwnd, 6)

    def win_toggle_max(self):
        if self._maximized:
            self._place(*self._restore_rect)
            self._maximized = False
        else:
            self._restore_rect = self._rect()
            mon = user32.MonitorFromWindow(self._hwnd, 2)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(mon, ctypes.byref(mi))
            a = mi.rcWork   # area di lavoro: la barra delle applicazioni resta visibile
            self._place(a.left, a.top, a.right - a.left, a.bottom - a.top)
            self._maximized = True
        self._set_corners(rounded=not self._maximized)
        return self._maximized

    def win_close(self):
        self._window.destroy()

    # ---------- dati e disinstallazione ----------
    def data_info(self):
        size = 0
        for root, _, files in os.walk(DATA_DIR):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return {"path": str(DATA_DIR), "size": size,
                "exe": sys.executable if getattr(sys, "frozen", False) else None}

    def open_data_folder(self):
        os.startfile(DATA_DIR)
        return True

    # ---------- versione e aggiornamenti ----------
    def app_info(self):
        return {"version": APP_VERSION, "repo": f"https://github.com/{GITHUB_REPO}"}

    def check_update(self):
        """Confronta la versione con l'ultima release pubblicata su GitHub."""
        try:
            req = urllib.request.Request(RELEASES_API, headers={
                "User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rel = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:                 # nessuna release ancora pubblicata
                return {"ok": True, "update": False, "current": APP_VERSION}
            return {"ok": False, "error": f"GitHub ha risposto {e.code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        latest = (rel.get("tag_name") or "").lstrip("vV")
        newer = version_tuple(latest) > version_tuple(APP_VERSION)
        asset = next((a for a in rel.get("assets") or []
                      if a.get("name", "").lower().endswith(".exe")), {})
        log.info("controllo aggiornamenti: attuale %s, ultima %s", APP_VERSION, latest or "—")
        return {"ok": True, "update": newer, "current": APP_VERSION, "latest": latest,
                "page": rel.get("html_url") or RELEASES_PAGE, "exe": asset.get("browser_download_url"),
                "size": asset.get("size"), "digest": asset.get("digest"),
                "auto": bool(getattr(sys, "frozen", False) and asset),
                "notes": (rel.get("body") or "")[:600]}

    def apply_update(self):
        """Scarica il nuovo AxGrab.exe dalla release, lo verifica, sostituisce quello in uso
        dopo la chiusura e riavvia l'app."""
        info = self.check_update()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error", "controllo non riuscito")}
        if not info.get("update"):
            return {"ok": False, "error": "Hai già l'ultima versione."}
        url = info.get("exe") or ""
        if not getattr(sys, "frozen", False) or \
                not url.startswith(f"https://github.com/{GITHUB_REPO}/releases/download/"):
            self.open_url(info.get("page") or RELEASES_PAGE)
            return {"ok": True, "manual": True}
        exe = Path(sys.executable)
        new = exe.with_name(exe.stem + ".new.exe")
        try:
            try:
                self._download_file(url, new, f"AxGrab {info['latest']}", event="update")
            except PermissionError:          # cartella dell'EXE non scrivibile: uso la cartella temporanea
                new = Path(os.environ.get("TEMP", str(DATA_DIR))) / "AxGrab.new.exe"
                self._download_file(url, new, f"AxGrab {info['latest']}", event="update")
            data = new.read_bytes()
            if len(data) < 1_000_000 or data[:2] != b"MZ":
                raise ValueError("il file scaricato non è un EXE valido")
            if info.get("size") and len(data) != info["size"]:
                raise ValueError("download incompleto")
            dig = info.get("digest") or ""
            if dig.startswith("sha256:") and hashlib.sha256(data).hexdigest() != dig[7:].lower():
                raise ValueError("il file scaricato non corrisponde a quello della release")
        except Exception as e:
            new.unlink(missing_ok=True)
            log.exception("aggiornamento non riuscito")
            return {"ok": False, "error": str(e)}

        q = lambda s: "'" + str(s).replace("'", "''") + "'"
        script = "\n".join([
            f"foreach ($p in @({os.getpid()}, {os.getppid()})) {{",
            "  while (Get-Process -Id $p -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 300 } }",
            f"$new = {q(new)}; $exe = {q(exe)}",
            "for ($i = 0; $i -lt 40; $i++) {",       # l'EXE può restare bloccato qualche istante
            "  try { Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; break }",
            "  catch { Start-Sleep -Milliseconds 500 } }",
            "Start-Process -FilePath $exe",
            "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force",
        ])
        path = Path(os.environ.get("TEMP", str(_LOCAL))) / "axgrab-update.ps1"
        path.write_text(script, encoding="utf-8-sig")
        log.info("aggiornamento %s -> %s: riavvio", APP_VERSION, info["latest"])
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(path)]
        base = NO_WINDOW | 0x00000200
        try:
            subprocess.Popen(cmd, creationflags=base | 0x01000000, close_fds=True)
        except OSError:
            subprocess.Popen(cmd, creationflags=base, close_fds=True)
        threading.Timer(0.5, self._window.destroy).start()
        return {"ok": True, "restarting": True, "version": info["latest"]}

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://github.com/"):
            os.startfile(url)
            return True
        return False

    def open_log(self):
        logf = DATA_DIR / "axgrab.log"
        if logf.exists():
            subprocess.Popen(["explorer", "/select,", str(logf)])
        return True

    def _diagnostics(self):
        """Nel log: dati del PC e quanto ci mette yt-dlp solo ad avviarsi (antivirus, disco lento…)."""
        import platform
        log.info("sistema: Windows %s, %s core, AxGrab %s", platform.version(), os.cpu_count(),
                 "exe" if getattr(sys, "frozen", False) else "sviluppo")
        exe = self._exe("yt-dlp")
        if exe:
            t0 = time.time()
            subprocess.run([exe, "--version"], capture_output=True, creationflags=NO_WINDOW, timeout=60)
            log.info("avvio yt-dlp: %.1fs (%s) | componenti: %.0f MB", time.time() - t0, exe,
                     dir_size(BIN_DIR) / 2**20)

    def uninstall(self):
        """Elimina dati in AppData, l'EXE e i suoi collegamenti, dopo la chiusura dell'app."""
        for j in list(self._jobs.values()):
            if j["status"] in ("queued", "downloading", "processing"):
                self.cancel(j["id"])
        exe = sys.executable if getattr(sys, "frozen", False) else ""
        q = lambda s: "'" + str(s).replace("'", "''") + "'"
        script = "\n".join([
            f"foreach ($p in @({os.getpid()}, {os.getppid()})) {{",
            "  while (Get-Process -Id $p -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 300 } }",
            "Start-Sleep -Seconds 1",
            f"Remove-Item -LiteralPath {q(DATA_DIR)} -Recurse -Force -ErrorAction SilentlyContinue",
            f"$exe = {q(exe)}",
            "if ($exe) {",
            "  Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue",
            "  $sh = New-Object -ComObject WScript.Shell",
            "  foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {",
            "    Get-ChildItem -LiteralPath $d -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {",
            "      if ($sh.CreateShortcut($_.FullName).TargetPath -ieq $exe) { Remove-Item -LiteralPath $_.FullName -Force } } } }",
            "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force",
        ])
        path = Path(os.environ.get("TEMP", str(_LOCAL))) / "axgrab-uninstall.ps1"
        path.write_text(script, encoding="utf-8-sig")
        log.info("disinstallazione avviata")
        logging.shutdown()
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(path)]
        base = NO_WINDOW | 0x00000200          # nessuna finestra, gruppo di processi separato
        try:
            # esce dal "job" dell'app, così non viene chiuso quando AxGrab termina
            subprocess.Popen(cmd, creationflags=base | 0x01000000, close_fds=True)
        except OSError:
            subprocess.Popen(cmd, creationflags=base, close_fds=True)
        threading.Timer(0.3, self._window.destroy).start()
        return True


user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HMONITOR
MIN_SIZE = (880, 600)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def main():
    log.info("avvio %s %s (python %s)", APP_NAME, APP_VERSION, sys.version.split()[0])
    api = Api()
    window = webview.create_window(
        APP_NAME, resource_path(os.path.join("ui", "index.html")), js_api=api,
        width=1120, height=760, min_size=MIN_SIZE, background_color="#0e0e10",
        frameless=True, easy_drag=False,
    )
    api._window = window
    window.events.shown += api._on_shown
    threading.Thread(target=lambda: (time.sleep(5), api._diagnostics()), daemon=True).start()
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("errore fatale")
        raise
