"""
Self-check della logica non grafica di AxGrab (nessuna finestra, nessun download).
Uso:  .venv\\Scripts\\python.exe selfcheck.py
I dati vanno in una cartella temporanea, non in quella vera di AxGrab.
"""
import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="axgrab-check-")
os.environ["LOCALAPPDATA"] = tmp
os.environ["APPDATA"] = tmp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app      # noqa: E402
import firma    # noqa: E402

assert str(app.DATA_DIR).startswith(tmp)

# impostazioni: solo chiavi note e valori ammessi
assert app.clean_settings({"mode": "audio", "parallel": 3.0, "quality": "999", "x": 1, "playlist": "si",
                           "folder": "relativa", "skip_version": "1.2.3"}) == \
    {"mode": "audio", "parallel": 3, "skip_version": "1.2.3"}
assert app.clean_settings([]) == {}

assert app.version_tuple("1.10.2") > app.version_tuple("1.9.9")
assert app.version_tuple("v2") == (2, 0, 0)
assert app.valid_url("https://youtu.be/x") and not app.valid_url("javascript:alert(1)")
assert app.log_safe("ERROR: [youtube] abc123: https://www.youtube.com/watch?v=abc failed") == \
    "ERROR: [youtube] www.youtube.com failed"

assert app.Api._keep_ytdlp("root/yt-dlp.exe") == "yt-dlp.exe"
assert app.Api._keep_ytdlp("root/_internal/x.dll") == "_internal/x.dll"
assert app.Api._keep_ytdlp("README.md") is None
assert app.Api._keep_ffmpeg("ff/bin/ffprobe.exe") == "ffprobe.exe"
assert app.Api._keep_ffmpeg("ff/doc/ffmpeg.exe") is None

# firma: valida, manomessa, versione sbagliata
secret = bytes(range(32))
pub = firma.public_key(secret).hex()
sig = firma.make_signature_file(secret, "1.2.0", "a" * 64, 123)
assert firma.check_signature_file(sig, pub, "1.2.0") == ("a" * 64, 123)
for text, version in ((sig.replace('"size": 123', '"size": 124'), "1.2.0"), (sig, "1.3.0")):
    try:
        firma.check_signature_file(text, pub, version)
        raise AssertionError("firma non valida accettata")
    except ValueError:
        pass

# helper per cancellare: file mancanti non sono un errore
f = app.CACHE_DIR / "prova.txt"
f.parent.mkdir(parents=True, exist_ok=True)
f.write_text("x")
app.unlink_quiet(f, app.CACHE_DIR / "manca.txt")
assert not f.exists()

# lavori: creazione, chiusura, cronologia, riprova
api = app.Api()
events = []
api._emit = lambda ev, data: events.append((ev, dict(data) if isinstance(data, dict) else data))
job = api._new_job(title="file.mp3", site="File", mode="text", quality="Testo", folder=tmp)
assert job["status"] == "queued" and job["url"] == "" and events[-1][0] == "job"
job["file"] = os.path.join(tmp, "file.txt")
api._finish_job(job, True, "")
assert job["status"] == "done" and job["percent"] == 100 and api.history[0]["title"] == "file.mp3"
assert api.retry("inesistente") is None

err = api._new_job(url="https://example.com/v", title="v", site="Web", mode="video", quality="1080p", folder=tmp)
api._finish_job(err, False, "boom")
assert err["status"] == "error" and err["error"] == "boom" and err["proc"] is None
canc = api._new_job(title="c", folder=tmp)
canc["cancel"] = True
api._finish_job(canc, True, "")
assert canc["status"] == "cancelled"
new_id = api.retry(err["id"])
assert new_id and err["id"] not in api._jobs and new_id in api._jobs
deadline = time.time() + 10                     # yt-dlp non è installato qui: deve finire in errore
while api._jobs[new_id]["status"] in ("queued", "downloading") and time.time() < deadline:
    time.sleep(0.05)
assert api._jobs[new_id]["status"] == "error", api._jobs[new_id]

# lavori che finiscono insieme: tutti in cronologia, tutti notificati come completati
import threading     # noqa: E402
n0, many = len(api.history), [api._new_job(title=f"j{i}", site="Web", mode="video", quality="720p", folder=tmp)
                     for i in range(20)]
events.clear()
ts = [threading.Thread(target=api._finish_job, args=(j, True, "")) for j in many]
[t.start() for t in ts]
[t.join() for t in ts]
assert len(api.history) == n0 + 20, len(api.history)
assert sum(1 for ev, d in events if ev == "job" and d["status"] == "done") == 20
# se la cronologia non si salva, il lavoro risulta comunque completato
orig, api._add_history = api._add_history, lambda job: 1 / 0
broken = api._new_job(title="x", folder=tmp)
api._finish_job(broken, True, "")
api._add_history = orig
assert broken["status"] == "done" and events[-1][1]["status"] == "done"

# appunti: solo lettura, senza toccare quello che l'utente ha copiato
assert isinstance(api.paste(), str)

# riprova di una trascrizione da file: riparte sullo stesso file (che qui non esiste: finisce in errore)
src = os.path.join(tmp, "manca.mp3")
fid = api._transcribe_local(app.Path(src))
time.sleep(0.3)
rid = api.retry(fid)
assert rid and fid not in api._jobs and api._jobs[rid]["source"] == src

print("self-check OK")
