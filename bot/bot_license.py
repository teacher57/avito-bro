"""BOT BRO license check (user, 2026-11-25): without a confirmed license the Avito part of the bot does not work
(no replies to buyers); Telegram, the test chat and the manager app keep working, so the reason is visible.

The license key is the BOT_BRO_LICENSE token (manager app «Токены», admin PIN; or BOT_BRO_LICENSE=... in .env).
One license = one device (user, 2026-11-25): a license is issued for the key AND this PC's device code (device_code(),
from the Windows MachineGuid; the app shows it). The public list holds SHA-256 fingerprints of "key:device code",
never the keys, so the same folder copied to another PC (another device code) does not match:

    https://teacher57.github.io/avito/licenses.json
    {"format": "bot-bro-licenses", "version": 2, "licenses": {"<sha256 of key:DEVICE-CODE>": {"until": "YYYY-MM-DD"}}}

- key listed and `until` not passed  -> ok;
- list fetched, key missing / expired -> invalid right away (a revoked license stops Avito within one check);
- list unreachable (no internet, GitHub down) -> the last successful check stays good for GRACE_DAYS, then invalid.
The last successful check is kept in live/license.json. The list URL is fixed on purpose (not configurable)."""
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request
from pathlib import Path

URL = "https://teacher57.github.io/avito/licenses.json"
GRACE_DAYS = 3
CHECK_EVERY = 60 * 60          # seconds between checks while things are fine
RETRY_EVERY = 5 * 60           # seconds between checks while the list is unreachable or the license is not ok
STATE = Path(__file__).parent / "live" / "license.json"


def _machine_id():
    """Stable id of this PC: Windows MachineGuid (changes only when Windows is reinstalled); elsewhere the OS machine id."""
    if sys.platform == "win32":
        import winreg
        flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, flags) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2]
    return str(uuid.getnode())


_device = None


def device_code():
    """«XXXXX-XXXXX-XXXXX-XXXXX»: what the client sends to get a license for this PC (not secret, not the raw id)."""
    global _device
    if _device is None:
        h = hashlib.sha256(("bot-bro-device:" + _machine_id().strip().lower()).encode("utf-8")).hexdigest()[:20].upper()
        _device = "-".join(h[i:i + 5] for i in range(0, 20, 5))
    return _device


def fingerprint(key, device=None):
    return hashlib.sha256(f"{key.strip()}:{device or device_code()}".encode("utf-8")).hexdigest()


def _key():
    return (os.environ.get("BOT_BRO_LICENSE") or "").strip()


def _fetch():
    req = urllib.request.Request(URL + f"?t={int(time.time())}", headers={"Cache-Control": "no-cache",
                                                                          "User-Agent": "bot-bro-license"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    if data.get("format") != "bot-bro-licenses" or data.get("version") != 2 or not isinstance(data.get("licenses"), dict):
        raise ValueError("unexpected license list")
    return data["licenses"]


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d):
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, STATE)


def _today():
    return datetime.datetime.now(datetime.UTC).date().isoformat()


_status = {"state": "unchecked", "until": None, "checked": None, "ok_at": None, "error": None, "device": None}
_next = 0


def status():
    """{"state": ok | grace | invalid | expired | missing | unchecked, "until", "checked", "ok_at", "error", "device"}."""
    return dict(_status, device=device_code())


def ok():
    """True only when Avito may work: a confirmed license, or a recent confirmation while the list is unreachable."""
    return _status["state"] in ("ok", "grace")


def check(now=None, force=False):
    """Re-check when due (hourly; every 5 min while not ok). Returns ok()."""
    global _next
    now = now or time.time()
    if not force and now < _next:
        return ok()
    key = _key()
    saved = _load()
    if not key:
        _status.update(state="missing", until=None, checked=now, error="нет ключа лицензии BOT_BRO_LICENSE")
        _next = now + RETRY_EVERY
        return False
    fp = fingerprint(key)
    try:
        licenses = _fetch()
    except Exception as ex:  # list unreachable: the last confirmation of THIS key is good for GRACE_DAYS
        last = saved.get("ok_at") if saved.get("fp") == fp else None
        good = last is not None and now - last < GRACE_DAYS * 86400 and (saved.get("until") or "9999") >= _today()
        _status.update(state="grace" if good else "invalid", until=saved.get("until") if good else None, checked=now,
                       ok_at=last, error=f"список лицензий недоступен ({type(ex).__name__})")
        _next = now + RETRY_EVERY
        return good
    entry = licenses.get(fp)
    if not isinstance(entry, dict):
        _status.update(state="invalid", until=None, checked=now,
                       error=f"нет лицензии для этого ключа на этом устройстве (код устройства {device_code()})")
        _save({"fp": fp, "ok_at": None, "until": None})
        _next = now + RETRY_EVERY
        return False
    until = entry.get("until")
    if until and until < _today():
        _status.update(state="expired", until=until, checked=now, error=f"срок лицензии закончился {until}")
        _save({"fp": fp, "ok_at": None, "until": until})
        _next = now + RETRY_EVERY
        return False
    _status.update(state="ok", until=until, checked=now, ok_at=now, error=None)
    _save({"fp": fp, "ok_at": now, "until": until})
    _next = now + CHECK_EVERY
    return True


def reason_ru():
    """One line for Telegram / the app: why Avito is off."""
    s = _status
    if s["state"] == "missing":
        return "нет ключа лицензии (приложение менеджера → «Токены»)"
    if s["state"] == "expired":
        return f"срок лицензии закончился {s['until']}"
    if s["state"] == "invalid":
        return s.get("error") or "лицензия не подтверждена"
    if s["state"] == "unchecked":
        return "лицензия ещё не проверена"
    return ""
