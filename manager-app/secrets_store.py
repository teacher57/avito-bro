"""Tokens and the admin PIN, stored in data/secrets.json.

Tokens are encrypted with Windows DPAPI (CryptProtectData, current-user scope): only this Windows account on this PC can
decrypt them. Copied to another PC, decryption fails and the app quietly does nothing (see SPEC.md, "Install & hardware
binding"). No token value is ever returned to the UI: only "set / not set" and the date it was changed.

On a non-Windows dev machine set MANAGER_APP_DEV=1 to use a NON-SECURE stand-in (for testing the UI only; a dev blob is
never accepted on Windows)."""
import base64
import ctypes
import datetime
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

DATA = Path(os.environ.get("MANAGER_APP_DATA") or Path(__file__).parent / "data")
FILE = DATA / "secrets.json"
CHECK = b"bro-x-bro-manager"

# Shown in the admin screen, in this order. The bot reads them as environment variables with these names.
TOKENS = {
    "AVITO_CLIENT_ID": "Avito: client_id",
    "AVITO_CLIENT_SECRET": "Avito: client_secret",
    "TG_BOT_TOKEN": "Telegram-бот: токен",
    "OPENAI_API_KEY": "OpenAI: ключ API",
}

PUBLIC_TOKENS = {"OPENAI_API_KEY"}  # anyone with the app may change these (user, 2026-11-13); the rest need the admin PIN

WINDOWS = sys.platform == "win32"
DEV = not WINDOWS and os.environ.get("MANAGER_APP_DEV") == "1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    UI_FORBIDDEN = 0x1
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> str:
    if WINDOWS:
        return "dpapi:" + base64.b64encode(_dpapi(data, True)).decode()
    if DEV:
        return "dev:" + base64.b64encode(data).decode()
    raise OSError("no secure storage on this OS")


def unprotect(blob: str) -> bytes:
    scheme, _, b64 = blob.partition(":")
    if WINDOWS and scheme == "dpapi":
        return _dpapi(base64.b64decode(b64), False)
    if DEV and scheme == "dev":
        return base64.b64decode(b64)
    raise OSError("cannot decrypt here")


def _load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(d):
    DATA.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FILE)


def state():
    """'new' = never set up here; 'ok' = set up and decrypts on this PC; 'foreign' = copied from elsewhere / unreadable."""
    d = _load()
    if not d:
        return "new"
    try:
        return "ok" if unprotect(d["check"]) == CHECK else "foreign"
    except (OSError, KeyError, ValueError):
        return "foreign"


def _pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000).hex()


def setup(pin, tokens):
    """First run (admin, on this PC): sets the PIN and the tokens. Overwrites nothing if already set up."""
    if state() != "new":
        raise RuntimeError("already set up")
    salt = os.urandom(16)
    d = {"check": protect(CHECK), "pin": {"salt": salt.hex(), "hash": _pin_hash(pin, salt)}, "tokens": {}}
    _save(d)
    set_tokens(tokens)


def check_pin(pin):
    d = _load()
    if not d or state() != "ok":
        return False
    return hmac.compare_digest(_pin_hash(pin, bytes.fromhex(d["pin"]["salt"])), d["pin"]["hash"])


def change_pin(new_pin):
    d = _load()
    salt = os.urandom(16)
    d["pin"] = {"salt": salt.hex(), "hash": _pin_hash(new_pin, salt)}
    _save(d)


def set_tokens(tokens):
    """{NAME: value}; empty values are skipped (= keep the old one)."""
    d = _load()
    today = datetime.date.today().isoformat()
    for name, value in tokens.items():
        if name in TOKENS and value and value.strip():
            d["tokens"][name] = {"blob": protect(value.strip().encode()), "changed": today}
    _save(d)


def token_status():
    """{NAME: changed-date or None}. Never the values."""
    d = _load() or {}
    t = d.get("tokens", {})
    return {name: (t[name]["changed"] if name in t else None) for name in TOKENS}


def decrypted_tokens():
    """{NAME: value} for the bot's start script only (bot_tokens.py). Empty dict if anything is wrong."""
    if state() != "ok":
        return {}
    out = {}
    for name, rec in (_load() or {}).get("tokens", {}).items():
        try:
            out[name] = unprotect(rec["blob"]).decode()
        except (OSError, KeyError, ValueError, UnicodeDecodeError):
            pass
    return out
