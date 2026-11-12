"""
AxGrab — GUI leggera per yt-dlp (pywebview + HTML/CSS).
Creato da nonsonoAxel · https://github.com/nonsonoAxel/AxGrab

Avvio in sviluppo:  python app.py      Build:  crea-exe.bat
"""
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import logging.handlers
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

import firma

APP_NAME = "AxGrab"
APP_VERSION = "1.0.6"           # da aumentare a ogni nuova release su GitHub (es. 1.0.1, 1.1.0)
GITHUB_REPO = "nonsonoAxel/AxGrab"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
# Chiave pubblica con cui si controlla la firma degli aggiornamenti (vedi firma.py). Quella
# privata sta solo sul PC di chi pubblica le release: senza, nessuno può far installare un EXE
# all'aggiornamento automatico, nemmeno entrando nell'account GitHub.
RELEASE_PUBLIC_KEY = "daf5738d9a6c0c96264d5336103ea44574e96f20a442afdc60c55365f32c3a14"
_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home()))
DATA_DIR = _LOCAL / APP_NAME


# programmi di Windows sempre con il percorso completo: chiamati solo per nome, Windows li
# cercherebbe prima nella cartella dell'EXE (es. Download), dove chiunque può metterne uno finto
_WINDIR = os.environ.get("SystemRoot") or r"C:\Windows"
POWERSHELL = os.path.join(_WINDIR, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
TASKKILL = os.path.join(_WINDIR, "System32", "taskkill.exe")
EXPLORER = os.path.join(_WINDIR, "explorer.exe")


BIN_DIR = DATA_DIR / "bin"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
CACHE_DIR = DATA_DIR / "cache"

# yt-dlp "a cartella": non deve estrarsi in una cartella temporanea a ogni avvio (analisi più rapida)
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_win.zip"
YTDLP_LATEST = "https://github.com/yt-dlp/yt-dlp/releases/latest"
# impronte SHA-256 pubblicate accanto agli zip: servono a scartare un download rovinato
YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_SUMS = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256"
DENO_SUMS = ("https://github.com/denoland/deno/releases/latest/download/"
             "deno-x86_64-pc-windows-msvc.zip.sha256sum")
# ffmpeg "shared": ffmpeg e ffprobe condividono le stesse librerie (~100 MB invece di ~390 MB)
FFMPEG_URL = ("https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl-shared.zip")
# Deno: runtime JavaScript che yt-dlp usa per le "sfide" di YouTube
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
YTDLP_DIR = BIN_DIR / "yt-dlp"
FFMPEG_DIR = BIN_DIR / "ffmpeg"
OWN_EXE = {"yt-dlp": YTDLP_DIR / "yt-dlp.exe", "ffmpeg": FFMPEG_DIR / "ffmpeg.exe",
           "deno": BIN_DIR / "deno.exe"}

# copia dei cookie dei soli siti video: evita di rileggere (e decifrare) il browser a ogni
# analisi e permette di usarli anche quando un browser Chromium è aperto e blocca il suo file.
# Sul disco è cifrata con DPAPI (la chiave è legata all'utente di Windows): non è leggibile
# copiando il file su un altro PC o da un altro utente.
COOKIE_FILE = DATA_DIR / "cookies.bin"
COOKIE_FILE_OLD = DATA_DIR / "cookies.txt"     # versioni fino alla 1.0.2: in chiaro, va eliminata
COOKIE_META = DATA_DIR / "cookies.json"
COOKIE_MAX_AGE = 30 * 60      # dopo 30 minuti si prova a rileggerli dal browser
COOKIE_KEEP = 7 * 24 * 3600   # una copia più vecchia di 7 giorni viene eliminata
NEEDS_COOKIES_FILE = DATA_DIR / "siti-con-cookie.json"
NEEDS_COOKIES_FOR = 12 * 3600  # un sito che ha chiesto i cookie li riceve subito per 12 ore
# niente google.com: quei cookie sono la sessione di tutto l'account Google (posta, Drive…),
# a YouTube bastano quelli di youtube.com
COOKIE_DOMAINS = ("youtube.com", "youtu.be", "googlevideo.com", "instagram.com",
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
LOG_FILE = DATA_DIR / "axgrab.log"
# il log non cresce all'infinito: al massimo ~500 KB più una copia precedente
_log_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=1,
                                                    encoding="utf-8")
logging.basicConfig(handlers=[_log_handler], level=logging.INFO,
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
    "skip_version": "",       # aggiornamento rimandato con "Più tardi"
}
# valori ammessi: tutto il resto (da settings.json o dall'interfaccia) viene scartato
ALLOWED_SETTINGS = {
    "mode": ("video", "audio"),
    "quality": ("best", "2160", "1440", "1080", "720", "480"),
    "audio_format": ("mp3", "m4a"),
    "cookies": ("none", "firefox", "zen", "helium", "chrome", "edge", "brave"),
    "cookie_when": ("auto", "always"),
    "parallel": (1, 2, 3, 4),
}
URL_RE = re.compile(r"https?://[^\s\"<>]+\Z", re.I)
# "Apri" avvia solo file di questo tipo, mai programmi o script
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v", ".3gp", ".gif",
             ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".weba", ".mka"}


def valid_url(url) -> bool:
    return isinstance(url, str) and len(url) < 4000 and bool(URL_RE.match(url))


def url_host(url) -> str:
    """Solo il sito, per il log: i link completi possono contenere dati privati."""
    try:
        return urllib.parse.urlsplit(url).netloc.lower() or "?"
    except (ValueError, AttributeError):
        return "?"


def log_safe(text) -> str:
    """Messaggio d'errore per il log senza link né codice del video: resta solo il sito."""
    text = re.sub(r"https?://[^\s'\"]+", lambda m: url_host(m.group()), text or "")
    return re.sub(r"\[([\w:+.-]+)\] [^\s:]+:", r"[\1]", text)[:200]


def clean_settings(data) -> dict:
    """Tiene solo le impostazioni conosciute e con un valore valido."""
    out = {}
    if not isinstance(data, dict):
        return out
    for key, allowed in ALLOWED_SETTINGS.items():
        v = data.get(key)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if type(v) in (str, int) and v in allowed:
            out[key] = v
    if isinstance(data.get("playlist"), bool):
        out["playlist"] = data["playlist"]
    folder = data.get("folder")
    if isinstance(folder, str) and 0 < len(folder) < 1000 and os.path.isabs(folder):
        out["folder"] = folder
    skip = data.get("skip_version")
    if isinstance(skip, str) and re.fullmatch(r"[0-9A-Za-z.\-]{0,30}", skip):
        out["skip_version"] = skip
    return out


def resource_path(rel: str) -> str:
    """Percorso dei file UI sia in sviluppo che dentro l'.exe di PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def load_json(path: Path, default):
    """Un file mancante, rovinato o del tipo sbagliato (lista al posto di oggetto…) vale come vuoto."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return data if isinstance(data, type(default)) else default


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Cifra/decifra con DPAPI di Windows: la chiave è dell'utente che ha fatto l'accesso."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DataBlob)]
    fn.restype = wt.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data, len(data))
    src = _DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out = _DataBlob()
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1: nessuna finestra
        raise OSError("DPAPI non riuscita")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


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


def independent_env():
    """Ambiente per avviare un'altra copia di AxGrab: senza le variabili che il bootloader di
    PyInstaller imposta per l'istanza in corso (altrimenti la nuova cerca la cartella _MEI della
    vecchia, già cancellata, e fallisce con 'Failed to load Python DLL')."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("_PYI_", "_MEIPASS", "_PYIBOOT"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def version_tuple(v: str):
    """'1.10.2' -> (1, 10, 2): confronto corretto anche con numeri a più cifre."""
    out = []
    for part in (v or "0").split("."):
        digits = "".join(ch for ch in part if ch in "0123456789")[:9]
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


def unjson(text):
    """Titoli e percorsi arrivano da yt-dlp in JSON (una riga sola anche con "a capo" dentro)."""
    try:
        v = json.loads(text)
        return v if isinstance(v, str) else text
    except ValueError:
        return text


class Api:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # analisi e cookie temporanei rimasti da sessioni precedenti (chiusura improvvisa, crash):
        # i file ck-*.txt possono contenere i cookie di tutto il browser, non devono restare
        for old in [*CACHE_DIR.glob("*.json"), *CACHE_DIR.glob("ck-*.txt")]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            COOKIE_FILE_OLD.unlink(missing_ok=True)     # vecchia copia in chiaro
        except OSError:
            pass
        self._info_cache = {}
        self._cookie_reads = set()      # file temporanei in cui yt-dlp sta leggendo dal browser
        self._procs = set()             # yt-dlp in esecuzione: vanno fermati alla chiusura
        self._needs_cookies = {k: v for k, v in load_json(NEEDS_COOKIES_FILE, {}).items()
                               if isinstance(v, (int, float))}
        self._window = None
        self._hwnd = 0
        self._maximized = False
        self._restore_rect = None
        self.settings = {**DEFAULT_SETTINGS, **clean_settings(load_json(SETTINGS_FILE, {}))}
        self.history = [h for h in load_json(HISTORY_FILE, []) if isinstance(h, dict)][:300]
        self._jobs = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(self.settings["parallel"])
        self._expire_cookies()

    # ---------- utilità ----------
    def _emit(self, event: str, payload):
        if self._window:
            try:
                self._window.evaluate_js(
                    f"window.onBackend && window.onBackend({json.dumps(event)}, {json.dumps(payload)})")
            except Exception:
                pass

    def _exe(self, name):
        """Solo il componente installato dall'app: mai uno con lo stesso nome trovato nella
        cartella corrente o nel PATH, che potrebbe non essere quello vero."""
        p = OWN_EXE[name]
        return str(p) if p.exists() else None

    @staticmethod
    def _kill(proc):
        """Ferma un processo e i suoi figli (yt-dlp avvia ffmpeg e deno)."""
        try:
            if proc and proc.poll() is None:
                subprocess.run([TASKKILL, "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW, timeout=15)
        except Exception:
            pass

    def _shutdown(self):
        """Alla chiusura della finestra: nessun yt-dlp deve restare attivo in background (alla
        fine scriverebbe un file di cookie che nessuno cancella più)."""
        for j in list(self._jobs.values()):
            j["cancel"] = True
        procs = list(self._procs) + [j.get("proc") for j in list(self._jobs.values())]
        for proc in procs:
            self._kill(proc)
        for proc in procs:
            try:
                if proc:
                    proc.wait(timeout=3)
            except Exception:
                pass
        for f in CACHE_DIR.glob("ck-*.txt"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _fetch_text(url, limit=1_000_000):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(limit)
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16", errors="replace")
        return raw.decode("utf-8", errors="replace")

    def _expected_sha256(self, sums_url, filename):
        """Impronta pubblicata per filename, o None se non si riesce a leggerla."""
        try:
            text = self._fetch_text(sums_url)
        except Exception:
            log.warning("impronte non scaricabili: %s", sums_url)
            return None
        lines = [l for l in text.splitlines() if l.strip()]
        for line in lines:          # formato "impronta  nome-del-file"
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                return parts[0].lower()
        found = re.findall(r"\b[0-9a-fA-F]{64}\b", text)     # file con una sola impronta (Deno)
        if len(found) == 1 and not any(len(l.split()) == 2 for l in lines):
            return found[0].lower()
        log.warning("impronta di %s non trovata in %s", filename, sums_url)
        return None

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

    def _install_zip(self, url, label, dest: Path, keep, sums_url=None):
        """Scarica uno zip ed estrae in dest i file per cui keep(percorso_nello_zip) restituisce
        il percorso relativo di destinazione (o None per scartarlo). Sostituisce dest solo a fine lavoro.
        Se sums_url è indicato, lo zip deve corrispondere all'impronta SHA-256 pubblicata lì."""
        zpath = BIN_DIR / f"{dest.name}.zip"
        self._download_file(url, zpath, label)
        if sums_url:
            expected = self._expected_sha256(sums_url, url.rsplit("/", 1)[-1])
            if expected:
                h = hashlib.sha256()
                with open(zpath, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != expected:
                    zpath.unlink(missing_ok=True)
                    raise ValueError(f"{label}: il file scaricato è rovinato o non è quello pubblicato. Riprova.")
        self._emit("setup", {"label": f"Estrazione {label}", "done": 1, "total": 1})
        tmp = dest.with_name(dest.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        root = tmp.resolve()
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                rel = keep(m.filename)
                if rel:
                    out = (tmp / rel).resolve()
                    if not out.is_relative_to(root):      # es. "_internal/../../altro": fuori dalla cartella
                        raise ValueError(f"{label}: percorso non valido nello zip ({m.filename})")
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
                self._install_zip(YTDLP_URL, "yt-dlp", YTDLP_DIR, self._keep_ytdlp, YTDLP_SUMS)
            if not OWN_EXE["ffmpeg"].exists():
                self._install_zip(FFMPEG_URL, "ffmpeg", FFMPEG_DIR, self._keep_ffmpeg, FFMPEG_SUMS)
            if not OWN_EXE["deno"].exists():
                self._install_zip(DENO_URL, "Deno", BIN_DIR / "deno-dir",
                                  lambda n: "deno.exe" if n.replace("\\", "/").split("/")[-1] == "deno.exe" else None,
                                  DENO_SUMS)
                os.replace(BIN_DIR / "deno-dir" / "deno.exe", OWN_EXE["deno"])
                shutil.rmtree(BIN_DIR / "deno-dir", ignore_errors=True)
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
            self._install_zip(YTDLP_URL, "yt-dlp", YTDLP_DIR, self._keep_ytdlp, YTDLP_SUMS)
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
        data = clean_settings(data)     # solo chiavi conosciute con valori ammessi
        old_parallel = self.settings.get("parallel")
        if "cookies" in data and data["cookies"] != self.settings.get("cookies"):
            self.forget_cookies()       # cambiato browser: la copia salvata non vale più
        self.settings.update(data)
        save_json(SETTINGS_FILE, self.settings)
        if self.settings["parallel"] != old_parallel:
            self._slots = threading.Semaphore(self.settings["parallel"])
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
            p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
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
            use_saved = cookies == "saved" or self._saved_cookies_fresh() or (
                self._browser_locked() and COOKIE_FILE.exists())
            if not (use_saved and self._restore_cookies(cookie_tmp)):
                spec = self._cookie_spec()
                if spec:
                    args += ["--cookies-from-browser", spec]
                    self._cookie_reads.add(str(cookie_tmp))
            args += ["--cookies", str(cookie_tmp)]
        return args

    # ---------- copia salvata dei cookie ----------
    def _cookie_age(self):
        """Secondi dall'ultima lettura dal browser della copia salvata (None se non si sa)."""
        read_at = load_json(COOKIE_META, {}).get("read_at")
        return time.time() - read_at if isinstance(read_at, (int, float)) else None

    def _expire_cookies(self):
        """Una copia vecchia, o di cui non si conosce l'età, non resta sul disco."""
        if COOKIE_FILE.exists():
            age = self._cookie_age()
            if age is None or not 0 <= age < COOKIE_KEEP:
                self.forget_cookies()

    def _restore_cookies(self, tmp: Path) -> bool:
        """Decifra la copia salvata nel file temporaneo per yt-dlp. False se non è utilizzabile."""
        self._expire_cookies()
        try:
            tmp.write_bytes(_dpapi(COOKIE_FILE.read_bytes(), False))
            return True
        except OSError:
            self.forget_cookies()       # illeggibile (es. copiata da un altro PC): si riparte dal browser
            return False

    def _saved_cookies_fresh(self):
        age = self._cookie_age()
        return (COOKIE_FILE.exists() and load_json(COOKIE_META, {}).get("browser") == self.settings["cookies"]
                and age is not None and 0 <= age < COOKIE_MAX_AGE)

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
                    COOKIE_FILE.write_bytes(_dpapi(("\n".join(keep) + "\n").encode("utf-8"), True))
                    if str(tmp) in self._cookie_reads:   # letti ora dal browser: copia "fresca"
                        save_json(COOKIE_META, {"browser": self.settings["cookies"], "read_at": time.time()})
        except OSError:
            log.exception("salvataggio cookie fallito")
        finally:
            self._cookie_reads.discard(str(tmp))
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                log.warning("cookie temporanei non eliminati subito: verranno rimossi al prossimo avvio")

    def forget_cookies(self):
        for f in (COOKIE_FILE, COOKIE_FILE_OLD, COOKIE_META):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
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
        # si guarda il dominio vero del link, non se il nome del sito compare da qualche parte
        if (self.settings.get("cookie_when") == "always" or self._site_key(url) in LOGIN_SITES
                or self._site_needs_cookies(url)):
            return [True, False]
        return [False, True]

    # siti che di recente hanno rifiutato le richieste senza cookie
    def _site_key(self, url):
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
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

    def _attempts(self, url, run, first=None, cancelled=lambda: False, on_warning=lambda w: None):
        """Esegue run(cookies, info_file) -> (ok, errore) seguendo il piano dei cookie, per l'analisi
        e per il download. first: tentativo iniziale con l'analisi salvata (cookies, file).
        Restituisce (ok, ultimo errore, cookie del tentativo riuscito/ultimo, cookie provati)."""
        attempts = ([first] if first else []) + [(c, None) for c in self._cookie_plan(url)]
        ok, err, why, i = False, "", "", 0
        while i < len(attempts):
            cookies, info_file = attempts[i]
            if i:
                if cancelled():
                    i -= 1
                    break
                prev_cookies, prev_file = attempts[i - 1]
                # dopo un tentativo dall'analisi salvata si riprova sempre; altrimenti si segue la
                # regola dei cookie
                if not prev_file and not self._should_retry(prev_cookies, cookies, why):
                    i -= 1
                    break
                if prev_cookies and not cookies:
                    on_warning(self._cookie_warning(why))
            ok, err = run(cookies, info_file)
            if ok:
                break
            # per decidere se serviva il login si guarda il messaggio senza il link: parole come
            # "login" o "account" dentro il link stesso non devono far leggere i cookie
            why = err.replace(url, "")
            if not cookies and is_auth_error(why) and self._cookie_spec():
                self._remember_needs_cookies(url)     # la prossima volta cookie subito
            # browser illeggibile (es. Helium aperto) ma c'è una copia salvata: si prova con quella
            if cookies is True and is_cookie_error(why) and COOKIE_FILE.exists() and \
                    not any(c == "saved" for c, _ in attempts):
                attempts.insert(i + 1, ("saved", None))
            i += 1
        last = attempts[min(i, len(attempts) - 1)]
        return ok, err, last[0], any(c for c, _ in attempts[:i + 1])

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
                                "unreachable", "need to log in", "logged-in")):
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
        if not valid_url(url):
            return {"ok": False, "error": "Questo non è un link valido."}
        exe = self._exe("yt-dlp")
        if not exe:
            return {"ok": False, "error": "yt-dlp non installato"}
        site, warnings, result = url_host(url), [], {}

        def run(use_cookies, _info_file):
            tmp = self._new_cookie_tmp() if use_cookies else None
            args = [exe, *self._base_args(use_cookies, tmp), "-J", "--flat-playlist",
                    "--yes-playlist" if self.settings["playlist"] else "--no-playlist",
                    "--", url]       # dopo "--" yt-dlp non legge più opzioni: il link resta un link
            t0, proc = time.time(), None
            try:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                self._procs.add(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=90)
                except subprocess.TimeoutExpired:
                    self._kill(proc)
                    proc.communicate()
                    log.warning("analisi: timeout dopo 90s (cookie=%s) %s", use_cookies, site)
                    raise
            finally:
                self._procs.discard(proc)
                if tmp:
                    self._store_cookies(tmp)
            ok = proc.returncode == 0 and bool(stdout.strip())
            err = [l for l in stderr.splitlines() if "ERROR" in l]
            err = (err[-1] if err else stderr.strip()[-300:]) or "Errore sconosciuto"
            log.info("analisi %.1fs cookie=%s ok=%s %s%s", time.time() - t0, use_cookies, ok, site,
                     "" if ok else " | " + log_safe(err.replace(url, "")))
            result["stdout"] = stdout
            return ok, err

        try:
            ok, err, use_cookies, tried = self._attempts(url, run, on_warning=warnings.append)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "L'analisi ha impiegato troppo (oltre 90 secondi). Riprova."}
        if not ok:
            return {"ok": False, "error": self._friendly(err, tried), "detail": err}
        warning, stdout = (warnings[-1] if warnings else None), result["stdout"]
        try:
            d = json.loads(stdout)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            return {"ok": False, "error": "yt-dlp ha dato una risposta che non riesco a leggere."}
        is_pl = d.get("_type") == "playlist"
        if not is_pl:
            # salva l'analisi: il download la riusa e salta 2-3 secondi di estrazione
            path = CACHE_DIR / f"{uuid.uuid4().hex}.json"
            path.write_text(stdout, encoding="utf-8")
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
        if not valid_url(url):
            return None
        opts = {**self.settings, **clean_settings(overrides)}
        info = info if isinstance(info, dict) else {}
        title, thumb, site = info.get("title"), info.get("thumbnail"), info.get("site")
        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id, "url": url, "status": "queued", "percent": 0,
            "title": title[:300] if isinstance(title, str) and title else url,
            "thumbnail": thumb if valid_url(thumb) else None,
            "site": site[:40] if isinstance(site, str) else "",
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
                # primo tentativo: l'analisi già fatta (con lo stesso uso dei cookie), poi il piano dei cookie
                cached = self._take_cache(job["url"])
                t0 = time.time()

                def run(cookies, info_file):
                    job["percent"], ta = 0, time.time()
                    code, err = self._run_proc(job, opts, cookies, info_file)
                    log.info("download (cookie=%s, analisi salvata=%s): codice %s, %.1fs%s", cookies,
                             bool(info_file), code, time.time() - ta,
                             "" if code == 0 else " | " + log_safe(err.replace(job["url"], "")))
                    return code == 0, err or f"yt-dlp ha restituito codice {code}"

                ok, err, _, tried = self._attempts(
                    job["url"], run, first=(cached[1], cached[0]) if cached else None,
                    cancelled=lambda: job["cancel"], on_warning=lambda w: job.update(warning=w))
                if ok and job.get("file") and os.path.exists(job["file"]):
                    mb = os.path.getsize(job["file"]) / 2**20
                    secs = time.time() - t0
                    log.info("completato %.1f MB in %.1fs (media %.1f MB/s, %s %s) %s", mb, secs,
                             mb / max(secs, 0.1), opts["mode"], job["quality"], url_host(job["url"]))
                if cached:
                    cached[0].unlink(missing_ok=True)
                if job["cancel"]:
                    job["status"] = "cancelled"
                elif ok:
                    job["status"], job["percent"] = "done", 100
                    self._add_history(job)
                else:
                    job.update(status="error", error=self._friendly(err, tried), detail=err)
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
        tmp = self._new_cookie_tmp() if cookies else None
        try:
            return self._download_once(job, opts, cookies, info_file, tmp)
        finally:
            if tmp:
                self._store_cookies(tmp)

    def _download_once(self, job, opts, cookies, info_file, tmp):
        # "--" chiude le opzioni: quello che segue è sempre trattato come link
        source = ["--load-info-json", str(info_file)] if info_file else ["--", job["url"]]
        args = [self._exe("yt-dlp"), *self._base_args(cookies, tmp), *self._format_args(opts),
                "-N", "4",   # frammenti in parallelo (HLS/DASH: Instagram, TikTok, Twitch, X…)
                "--yes-playlist" if opts["playlist"] else "--no-playlist",
                "-P", opts["folder"],
                "-o", "%(title).150B [%(id)s].%(ext)s",
                "-o", "pl_video:%(playlist_title).80B/%(playlist_index)03d - %(title).120B.%(ext)s",
                "--windows-filenames", "--newline", "--progress", "--no-simulate",
                "--progress-template",
                "download:@@P|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
                # titolo e percorso in formato JSON (%(...)j): stanno sempre su una riga sola, così
                # un titolo con un "a capo" non può fingere una riga di stato
                "--print", "before_dl:@@T|%(playlist_index)s|%(n_entries)s|%(title)j",
                "--print", "after_move:@@F|%(filepath)j",
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
                _, idx, n, title = (line.split("|", 3) + [""] * 4)[:4]
                title = unjson(title)[:300]
                job["percent"], job["status"] = 0, "downloading"
                if idx not in ("NA", "None") and n not in ("NA", "None"):
                    job["item"] = f"{idx}/{n}"      # playlist: titolo resta quello della playlist
                    job["current"] = title
                elif job["title"] == job["url"]:
                    job["title"] = title
                self._emit("job", self._public(job))
            elif line.startswith("@@F|"):
                job["file"] = unjson(line[4:])
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
            self._kill(proc)
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
        # anche il log racconta cosa è stato scaricato: si svuota insieme alla cronologia
        _log_handler.acquire()
        try:
            if _log_handler.stream:
                _log_handler.stream.close()
                _log_handler.stream = None
            for f in (LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1")):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            _log_handler.release()
        return True

    # ---------- file ----------
    def _is_download(self, path) -> bool:
        """Il percorso è quello di un file scaricato da AxGrab (in coda o in cronologia)."""
        if not isinstance(path, str) or not path:
            return False
        known = {j.get("file") for j in list(self._jobs.values())} | {h.get("file") for h in self.history}
        return path in known

    def open_file(self, path):
        # solo file scaricati dall'app e solo audio/video: mai programmi, script o collegamenti
        if (self._is_download(path) and Path(path).suffix.lower() in MEDIA_EXT
                and os.path.isfile(path)):
            os.startfile(path)
            return True
        return False

    def open_folder(self, path=None, file=None):
        if self._is_download(file) and os.path.exists(file):
            subprocess.Popen([EXPLORER, "/select,", os.path.normpath(file)])
        else:
            target = path if isinstance(path, str) and path else self.settings["folder"]
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
        self._lock_navigation()

    def _lock_navigation(self):
        """La finestra deve mostrare solo la pagina dell'app: qualunque altra pagina caricata lì
        dentro (es. un link trascinato sulla finestra) riceverebbe i comandi di Python."""
        try:
            from System import Action
            form = self._window.native
            view = form.webview

            def on_navigation(sender, args):
                try:
                    home = urllib.parse.urlsplit(self._window.real_url or "")
                    dest = urllib.parse.urlsplit(str(args.Uri))
                    if (dest.scheme, dest.netloc) != (home.scheme, home.netloc):
                        args.Cancel = True
                        log.warning("navigazione bloccata verso %s", dest.netloc or dest.scheme)
                except Exception:
                    args.Cancel = True

            def apply():
                view.AllowExternalDrop = False        # niente file o link trascinati dentro
                view.NavigationStarting += on_navigation

            self._nav_handler = on_navigation         # resta in vita finché esiste la finestra
            form.Invoke(Action(apply))
        except Exception:
            log.exception("blocco della navigazione non applicato")

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
        if not self._hwnd or self._maximized or not isinstance(edge, str):
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
        return {"path": str(DATA_DIR), "size": dir_size(DATA_DIR),
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
        if not isinstance(rel, dict):
            return {"ok": False, "error": "risposta di GitHub non valida"}
        latest = str(rel.get("tag_name") or "").lstrip("vV")[:30]
        newer = version_tuple(latest) > version_tuple(APP_VERSION)
        assets = rel.get("assets") if isinstance(rel.get("assets"), list) else []
        asset = next((a for a in assets if isinstance(a, dict)
                      and str(a.get("name") or "").lower().endswith(".exe")), {})
        # file di firma: stesso nome dell'EXE più ".sig"
        sig = next((a for a in assets if isinstance(a, dict) and asset
                    and a.get("name") == f"{asset.get('name')}.sig"), {})
        log.info("controllo aggiornamenti: attuale %s, ultima %s", APP_VERSION, latest or "—")
        return {"ok": True, "update": newer, "current": APP_VERSION, "latest": latest,
                "page": rel.get("html_url") or RELEASES_PAGE, "exe": asset.get("browser_download_url"),
                "size": asset.get("size"), "digest": asset.get("digest"),
                "sig": sig.get("browser_download_url"),
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
        # Prima di scaricare: la release deve avere una firma valida, fatta con la chiave di chi
        # pubblica AxGrab e per questa precisa versione. La firma dice anche quale impronta e
        # quale dimensione deve avere l'EXE. Se manca o non torna non si installa niente e non si
        # apre nemmeno la pagina: una release non firmata potrebbe non essere autentica.
        try:
            sig_url = info.get("sig") or ""
            if not sig_url.startswith(f"https://github.com/{GITHUB_REPO}/releases/download/"):
                raise ValueError("manca la firma")
            signed_sha, signed_size = firma.check_signature_file(
                self._fetch_text(sig_url, 4096), RELEASE_PUBLIC_KEY, info["latest"])
        except urllib.error.HTTPError as e:
            log.error("aggiornamento rifiutato: firma non scaricabile (%s)", e.code)
            return {"ok": False, "error": "questa release non ha una firma scaricabile. "
                                          "Non installarla e avvisa chi ti ha dato AxGrab."}
        except OSError as e:                 # rete assente o lenta: non è un problema della release
            return {"ok": False, "error": f"non riesco a scaricare la firma della release ({e}). Riprova più tardi."}
        except Exception as e:
            log.error("aggiornamento rifiutato: %s", e)
            return {"ok": False, "error": f"questa release non è firmata come dovrebbe ({e}). "
                                          "Non installarla e avvisa chi ti ha dato AxGrab."}
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
            # il file scaricato deve essere esattamente quello firmato
            if len(data) != signed_size:
                raise ValueError("download incompleto o file diverso da quello firmato")
            if hashlib.sha256(data).hexdigest() != signed_sha:
                raise ValueError("il file scaricato non è quello firmato")
        except Exception as e:
            new.unlink(missing_ok=True)
            log.exception("aggiornamento non riuscito")
            return {"ok": False, "error": str(e)}

        log.info("aggiornamento %s -> %s: riavvio", APP_VERSION, info["latest"])
        env = independent_env()          # la nuova AxGrab non deve ereditare lo stato di questa
        env.update(AXG_PIDS=f"{os.getpid()},{os.getppid()}", AXG_NEW=str(new), AXG_EXE=str(exe))
        self._detached_powershell(UPDATE_PS, env)
        threading.Timer(0.5, self._window.destroy).start()
        return {"ok": True, "restarting": True, "version": info["latest"]}

    @staticmethod
    def _detached_powershell(script, env):
        """Avvia uno script che sopravvive alla chiusura dell'app. Lo script è un testo fisso
        passato sulla riga di comando (nessun file .ps1 che qualcuno possa riscrivere) e i
        percorsi gli arrivano come variabili d'ambiente (nessun problema di apostrofi)."""
        cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
               "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
        base = NO_WINDOW | 0x00000200          # nessuna finestra, gruppo di processi separato
        try:
            # esce dal "job" dell'app, così non viene chiuso quando AxGrab termina
            subprocess.Popen(cmd, creationflags=base | 0x01000000, close_fds=True, env=env)
        except OSError:
            subprocess.Popen(cmd, creationflags=base, close_fds=True, env=env)

    def open_url(self, url):
        # solo le pagine del progetto, non qualunque indirizzo di GitHub
        home = f"https://github.com/{GITHUB_REPO}"
        if isinstance(url, str) and (url == home or url.startswith(home + "/")) and valid_url(url):
            os.startfile(url)
            return True
        return False

    def open_log(self):
        if LOG_FILE.exists():
            subprocess.Popen([EXPLORER, "/select,", str(LOG_FILE)])
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
            log.info("avvio yt-dlp: %.1fs | componenti: %.0f MB", time.time() - t0,
                     dir_size(BIN_DIR) / 2**20)

    def uninstall(self):
        """Elimina dati in AppData, l'EXE e i suoi collegamenti, dopo la chiusura dell'app."""
        # la cartella da eliminare deve essere proprio quella dei dati di AxGrab
        if not DATA_DIR.is_absolute() or DATA_DIR.name != APP_NAME or DATA_DIR.is_symlink() \
                or DATA_DIR.is_junction():
            log.error("disinstallazione annullata: cartella dati inattesa (%s)", DATA_DIR)
            return False
        # ultima conferma con una finestra di Windows: non basta che la chieda la pagina
        answer = user32.MessageBoxW(
            self._hwnd or None,
            "Disinstallare AxGrab?\n\nVerranno eliminati l'app, i suoi dati e i collegamenti.\n"
            "I video e gli audio scaricati restano dove sono.",
            "Disinstalla AxGrab", 0x4 | 0x30 | 0x100)      # Sì/No, avviso, predefinito: No
        if answer != 6:
            return False
        for j in list(self._jobs.values()):
            if j["status"] in ("queued", "downloading", "processing"):
                self.cancel(j["id"])
        # collegamenti (giunzioni) dentro la cartella dati: si toglie il collegamento, non ciò a
        # cui punta. Il PowerShell di Windows altrimenti li seguirebbe cancellando la destinazione.
        for root, dirs, _ in os.walk(DATA_DIR):
            for d in list(dirs):
                p = Path(root) / d
                if p.is_symlink() or p.is_junction():
                    dirs.remove(d)
                    try:
                        os.rmdir(p)
                    except OSError:
                        log.error("disinstallazione annullata: collegamento non rimovibile (%s)", p)
                        return False
        frozen = getattr(sys, "frozen", False)
        env = dict(os.environ)
        # in sviluppo il processo padre è il terminale: si aspetta solo la chiusura dell'app
        env.update(AXG_PIDS=f"{os.getpid()},{os.getppid()}" if frozen else str(os.getpid()),
                   AXG_DATA=str(DATA_DIR), AXG_EXE=sys.executable if frozen else "")
        log.info("disinstallazione avviata")
        logging.shutdown()
        self._detached_powershell(UNINSTALL_PS, env)
        threading.Timer(0.3, self._window.destroy).start()
        return True


# Script PowerShell eseguiti dopo la chiusura dell'app. Sono testi fissi: i valori che cambiano
# (processi da aspettare, percorsi) arrivano dalle variabili d'ambiente AXG_*, mai scritti nel testo.
_PS_WAIT = r'''
foreach ($p in ($env:AXG_PIDS -split ',')) {
  while (Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 300 } }
'''
UPDATE_PS = _PS_WAIT + r'''
$new = $env:AXG_NEW; $exe = $env:AXG_EXE
if ($new -and $exe) {
  for ($i = 0; $i -lt 40; $i++) {
    try { Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; break }
    catch { Start-Sleep -Milliseconds 500 } }
  Start-Process -FilePath $exe }
'''
UNINSTALL_PS = _PS_WAIT + r'''
Start-Sleep -Seconds 1
$data = $env:AXG_DATA; $exe = $env:AXG_EXE
if ($data -and (Split-Path -Leaf $data) -eq 'AxGrab') {
  Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue }
if ($exe) {
  Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
  $sh = New-Object -ComObject WScript.Shell
  foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    if ($d) {
      Get-ChildItem -LiteralPath $d -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
        if ($sh.CreateShortcut($_.FullName).TargetPath -ieq $exe) { Remove-Item -LiteralPath $_.FullName -Force } } } } }
'''

user32 = ctypes.windll.user32
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
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
    try:
        webview.start(debug="--debug" in sys.argv)
    finally:
        api._shutdown()      # finestra chiusa: ferma i download e toglie i cookie temporanei


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("errore fatale")
        raise
