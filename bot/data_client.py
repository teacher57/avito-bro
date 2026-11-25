"""Client of the shared avito data server (avito/data/server, format: avito/data/schema/FORMAT.md). Stdlib only.

The bot and the manager app read and write the shop's data ONLY through the server: catalog.json (products, size scale),
stock.json (the warehouse, HUB BRO writes it), shop.json (cutoffs, where stock comes from), log.json and files/ (photos,
size charts).

Config (environment first, else the bot's .env lines NAME=value):
  AVITO_DATA_URL   server address, default http://127.0.0.1:8791
  AVITO_DATA_KEY   access key, sent as the x-hubbro-key header (optional: only if the server has one)
  AVITO_DATA_CACHE folder for the local copy, default <bot>/live (data_cache/ and files/ inside it)

Local copy: the last good catalog / stock / shop are kept in live/data_cache/<key>.json and the pictures in live/files/
(same names as on the server: «klesh_1.jpg», «klesh_size_chart.png»), so the bot keeps answering when the server is down,
and Avito / Telegram uploads use local paths.

Writing (FORMAT.md, rule 8): take value + rev, change only your part, PUT {value, baseRev}. Someone was faster -> 409 with the
server's value: the caller's `mutate` is applied to that value again and sent again (a few times). Not by the format -> 422,
raised as Invalid with the server's errors."""
import copy
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
CACHED_KEYS = ("catalog", "stock", "shop")
TIMEOUT = 5  # seconds; the bot's main loop must never hang on the server


def _env_file():
    """NAME=value lines of the bot's .env (only the AVITO_DATA_* names are used here)."""
    out = {}
    path = BOT_DIR / ".env"
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.removeprefix("export ").split("=", 1)
                if k.strip().startswith("AVITO_DATA_"):
                    out[k.strip()] = v.strip().strip('"\'')
    except OSError:
        pass
    return out


def _conf(name, default=None):
    return os.environ.get(name) or _env_file().get(name) or default


URL = _conf("AVITO_DATA_URL", "http://127.0.0.1:8791").rstrip("/")
KEY = _conf("AVITO_DATA_KEY")
CACHE_ROOT = Path(_conf("AVITO_DATA_CACHE", str(BOT_DIR / "live")))
CACHE_DIR = CACHE_ROOT / "data_cache"
FILES_DIR = CACHE_ROOT / "files"


class DataError(Exception):
    """The server can't be reached or answered something unexpected."""


class Invalid(DataError):
    """422: the value is not by the format; .errors = the server's list."""

    def __init__(self, errors):
        self.errors = list(errors or [])
        super().__init__("; ".join(self.errors[:5]) or "данные не по формату")


class Conflict(DataError):
    """409 after all retries (or with no mutate function): .value / .rev = what the server has now."""

    def __init__(self, value, rev):
        self.value, self.rev = value, rev
        super().__init__("на сервере данных уже есть более новая версия")


def _request(method, path, body=None, content_type="application/json", timeout=TIMEOUT):
    """(status, bytes). Raises DataError when the server can't be reached."""
    req = urllib.request.Request(URL + path, data=body, method=method)
    if KEY:
        req.add_header("x-hubbro-key", KEY)
    if body is not None:
        req.add_header("content-type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DataError(f"сервер данных недоступен ({URL}): {type(e).__name__}") from e


def _json(status, raw, what):
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError as e:
        raise DataError(f"{what}: ответ сервера не JSON (HTTP {status})") from e


# ---------------------------------------------------------------- state (JSON files)

def revs():
    """{key: rev} of every file on the server (also «files»: changes when a picture is added / replaced / deleted)."""
    status, raw = _request("GET", "/api/state", timeout=3)
    if status != 200:
        raise DataError(f"/api/state: HTTP {status}")
    return _json(status, raw, "/api/state").get("revs") or {}


def get(key):
    """(value, rev) from the server; catalog / stock / shop are also saved to the local copy."""
    status, raw = _request("GET", "/api/state?key=" + urllib.parse.quote(key))
    if status != 200:
        raise DataError(f"{key}: HTTP {status}")
    d = _json(status, raw, key)
    value, rev = d.get("value"), d.get("rev", 0)
    if key in CACHED_KEYS:
        _save_cache(key, value, rev)
    return value, rev


def put(key, value, base_rev, mutate=None, tries=5):
    """Save `value` written on top of `base_rev`. Returns (value, rev) that ended up on the server.
    409 -> mutate(fresh) (changes the server's fresh value in place, or returns a new one) and try again, up to `tries`.
    422 -> Invalid(errors)."""
    for _ in range(max(1, tries)):
        body = json.dumps({"value": value, "baseRev": base_rev}, ensure_ascii=False).encode("utf-8")
        status, raw = _request("PUT", "/api/state?key=" + urllib.parse.quote(key), body)
        d = _json(status, raw, key)
        if status == 200:
            rev = d.get("rev", base_rev)
            if key in CACHED_KEYS:
                _save_cache(key, value, rev)
            return value, rev
        if status == 422:
            raise Invalid(d.get("errors"))
        if status == 409:
            if mutate is None:
                raise Conflict(d.get("value"), d.get("rev"))
            fresh = d.get("value")
            changed = mutate(fresh)
            value, base_rev = (fresh if changed is None else changed), d.get("rev", 0)
            continue
        raise DataError(f"{key}: HTTP {status} {d.get('error') or ''}".strip())
    raise Conflict(value, base_rev)


def update(key, mutate, tries=5):
    """Read `key`, apply mutate(value) (in place, or return a new value), save; on 409 re-apply to the server's value.
    Returns (value, rev)."""
    value, rev = get(key)
    fresh = copy.deepcopy(value)
    changed = mutate(fresh)
    return put(key, fresh if changed is None else changed, rev, mutate=mutate, tries=tries)


# ---------------------------------------------------------------- local copy

def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save_cache(key, value, rev):
    try:
        _write_atomic(CACHE_DIR / f"{key}.json",
                      json.dumps({"value": value, "rev": rev}, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # no local copy this time; the next good read writes it


def cached(key):
    """(value, rev) of the last good copy, or (None, None) when there is none."""
    try:
        d = json.loads((CACHE_DIR / f"{key}.json").read_text(encoding="utf-8"))
        return d.get("value"), d.get("rev")
    except (OSError, ValueError):
        return None, None


def load(key):
    """(value, rev, from_server): the server's value, else the local copy (value None if there is neither)."""
    try:
        value, rev = get(key)
        return value, rev, True
    except DataError:
        value, rev = cached(key)
        return value, rev, False


# ---------------------------------------------------------------- files (photos, size charts)

def _file_path(name):
    name = Path(name).name  # never outside files/
    if not name or name.startswith("."):
        raise ValueError(f"плохое имя файла: {name!r}")
    return "/api/files/" + urllib.parse.quote(name)


def file_list():
    """{name: size} of every file on the server."""
    status, raw = _request("GET", "/api/files")
    if status != 200:
        raise DataError(f"/api/files: HTTP {status}")
    return {f["name"]: f.get("size") for f in _json(status, raw, "/api/files").get("files") or []}


def file_get(name):
    """Bytes of one file (DataError if missing / unreachable)."""
    status, raw = _request("GET", _file_path(name), timeout=30)
    if status != 200:
        raise DataError(f"{name}: HTTP {status}")
    return raw


def file_put(name, data):
    """Upload bytes as files/<name> (also kept in the local copy). Returns the server's answer {name, size, rev}."""
    ext = Path(name).suffix.lower()
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif", ".heic": "image/heic", ".pdf": "application/pdf"}.get(ext, "application/octet-stream")
    status, raw = _request("PUT", _file_path(name), data, content_type=ctype, timeout=60)
    d = _json(status, raw, name)
    if status != 200:
        raise DataError(f"{name}: HTTP {status} {d.get('error') or ''}".strip())
    try:
        _write_atomic(FILES_DIR / Path(name).name, data)
    except OSError:
        pass
    return d


def file_delete(name):
    status, raw = _request("DELETE", _file_path(name))
    if status not in (200, 404):
        raise DataError(f"{name}: HTTP {status}")
    try:
        (FILES_DIR / Path(name).name).unlink()
    except OSError:
        pass


def local_file(name):
    """Local path of a downloaded file (it may not exist yet)."""
    return FILES_DIR / Path(name).name


def sync_files(names, check=False):
    """Download the files in `names` that are missing locally; with check=True (the server's files changed) also those
    whose size differs from the server's (a picture replaced under the same name, e.g. a redrawn size chart).
    Never raises: a file that can't be fetched now stays as it is (or missing). Returns the names that were downloaded."""
    names = [n for n in dict.fromkeys(names) if n]
    if not names or (not check and all(local_file(n).is_file() for n in names)):
        return []
    try:
        sizes = file_list()
    except DataError:
        return []
    got = []
    for n in names:
        p = local_file(n)
        if n not in sizes:
            continue
        if p.is_file() and p.stat().st_size == sizes[n]:
            continue
        try:
            _write_atomic(p, file_get(n))
            got.append(n)
        except (DataError, OSError):
            pass
    return got
