"""
Firma delle release di AxGrab (Ed25519).

A cosa serve: l'app installa un aggiornamento solo se è firmato con la chiave privata di chi
pubblica AxGrab. La chiave privata sta solo sul PC di chi crea le release; dentro l'app c'è solo
la chiave pubblica, che permette di controllare la firma ma non di crearne una. Così chi riuscisse
a entrare nell'account GitHub non potrebbe comunque far installare un EXE suo.

Uso (dalla cartella del progetto):
  python firma.py nuova-chiave      crea la chiave privata (una volta sola) e mostra quella pubblica
  python firma.py chiave-pubblica   mostra di nuovo la chiave pubblica
  python firma.py AxGrab.exe        crea AxGrab.exe.sig, da caricare nella release insieme all'EXE

La chiave privata è in  %USERPROFILE%\\.axgrab-firma\\chiave-privata.txt  (fuori dal progetto:
non finisce su GitHub). Va tenuta segreta e va fatta una copia di riserva: se si perde, le
versioni già installate non accettano più aggiornamenti automatici.

Ed25519 è scritto qui in Python puro seguendo l'RFC 8032, per non aggiungere librerie all'EXE.
È lento (qualche decimo di secondo) ma serve una volta per aggiornamento.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ---------- Ed25519 (RFC 8032, sezione 6) ----------
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return E * F % _P, G * H % _P, F * G % _P, E * H % _P


def _mul(s, point):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, point)
        point = _add(point, point)
        s >>= 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, -1, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point):
    zinv = pow(point[2], -1, _P)
    x, y = point[0] * zinv % _P, point[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _hash_int(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _L


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret: bytes, message: bytes) -> bytes:
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, _G))
    r = _hash_int(prefix + message)
    rs = _compress(_mul(r, _G))
    s = (r + _hash_int(rs + pub + message) * a) % _L
    return rs + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = _hash_int(signature[:32] + public + message)
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# ---------- file di firma di una release ----------
def _message(version: str, sha256: str, size: int) -> bytes:
    # la versione fa parte di ciò che viene firmato: un EXE vecchio (firmato a suo tempo) non può
    # essere ripubblicato come se fosse una versione nuova
    return f"AxGrab release v1\n{version}\n{sha256}\n{size}\n".encode("utf-8")


def make_signature_file(secret: bytes, version: str, sha256: str, size: int) -> str:
    sig = sign(secret, _message(version, sha256, size))
    return json.dumps({"v": 1, "version": version, "sha256": sha256, "size": size, "sig": sig.hex()}, indent=2) + "\n"


def check_signature_file(text: str, public_hex: str, version: str):
    """Controlla il file .sig di una release. Restituisce (sha256, dimensione) che l'EXE deve
    avere; ValueError se la firma manca, non è valida o è di un'altra versione."""
    try:
        d = json.loads(text)
        sv, sha, size, sig = d["version"], d["sha256"], d["size"], bytes.fromhex(d["sig"])
        pub = bytes.fromhex(public_hex)
    except Exception:
        raise ValueError("firma della release illeggibile") from None
    if not (isinstance(sv, str) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)
            and type(size) is int and size > 0):
        raise ValueError("firma della release illeggibile")
    if not verify(pub, _message(sv, sha, size), sig):
        raise ValueError("la firma della release non è valida")
    if sv != version:
        raise ValueError(f"la firma è della versione {sv}, non della {version}")
    return sha, size


# ---------- strumento da riga di comando ----------
KEY_FILE = Path(os.environ.get("AXGRAB_CHIAVE") or Path.home() / ".axgrab-firma" / "chiave-privata.txt")


def _load_secret() -> bytes:
    if not KEY_FILE.is_file():
        sys.exit(f"Chiave privata non trovata ({KEY_FILE}).\n"
                 "Metti la tua chiave-privata.txt sul Desktop e rilancia crea-exe.bat.\n"
                 "NON crearne una nuova: le copie di AxGrab già installate rifiuterebbero gli aggiornamenti.")
    try:
        secret = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
    except ValueError:
        secret = b""
    if len(secret) != 32:
        sys.exit(f"Il file {KEY_FILE} non contiene una chiave valida.")
    return secret


def _from_app(name: str) -> str:
    """Legge una costante da app.py senza avviarlo."""
    src = (Path(__file__).with_name("app.py")).read_text(encoding="utf-8")
    m = re.search(rf'^{name}\s*=\s*"([^"]*)"', src, re.M)
    return m.group(1) if m else ""


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "nuova-chiave":
        if KEY_FILE.exists():
            sys.exit(f"Esiste già una chiave in {KEY_FILE}: non la sovrascrivo.\n"
                     "(Cambiare chiave vuol dire che le versioni già installate non si aggiornano più da sole.)")
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        secret = os.urandom(32)
        KEY_FILE.write_text(secret.hex() + "\n", encoding="ascii")
        print(f"Chiave privata creata in {KEY_FILE}")
        print("Tienila segreta e fanne una copia di riserva (chiavetta USB, gestore di password).")
        print("Chiave pubblica (va in app.py, RELEASE_PUBLIC_KEY):")
        print(public_key(secret).hex())
    elif cmd == "chiave-pubblica":
        print(public_key(_load_secret()).hex())
    else:
        exe = Path(cmd)
        if not exe.is_file():
            sys.exit(f"File non trovato: {exe}")
        secret = _load_secret()
        version, expected_pub = _from_app("APP_VERSION"), _from_app("RELEASE_PUBLIC_KEY")
        if not version:
            sys.exit("Non trovo APP_VERSION in app.py")
        if public_key(secret).hex() != expected_pub:
            sys.exit("La chiave privata non corrisponde a RELEASE_PUBLIC_KEY in app.py: "
                     "le app installate rifiuterebbero questa firma.")
        data = exe.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        text = make_signature_file(secret, version, sha, len(data))
        check_signature_file(text, expected_pub, version)          # controprova
        out = exe.with_name(exe.name + ".sig")
        out.write_text(text, encoding="utf-8")
        print(f"Firmato {exe.name} come versione {version}")
        print(f"Creato {out.name}: caricalo nella release v{version} insieme a {exe.name}.")


if __name__ == "__main__":
    main(sys.argv)
