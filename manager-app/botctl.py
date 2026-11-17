"""Bot on/off from the manager app (available to everyone who has the app).

Two switches, same as in avito-bot/README.md:
  - live mode (answers buyers or not): the app writes live/app_control.json, the running bot applies it within seconds
    (same effect as «▶️ Старт» / «⏸ Пауза» in Telegram; the admins get a Telegram note);
  - the process itself (manager.py, Windows task AvitoBot): start / restart via schtasks; stopped by the process id in
    live/bot.pid. Every process start begins switched OFF, so after a restart press «Старт» again.
The bot writes live/app_status.json every ~3 s; no fresh heartbeat = the process isn't running."""
import json
import os
import subprocess
import sys
import time
import uuid

from store import BOT_DIR

LIVE = BOT_DIR / "live"
CONTROL = LIVE / "app_control.json"
STATUS = LIVE / "app_status.json"
PID = LIVE / "bot.pid"
SPEND = LIVE / "gpt_spend.json"      # written by the bot: $ spent on OpenAI since the last reset
GPT_RESET = LIVE / "gpt_reset.json"  # written here when a new OpenAI key is saved; the bot zeroes its counter
TASK = "AvitoBot"
ALIVE_SECONDS = 30


def status():
    """{"running", "live_on", "dry_run", "age", "last_restart", "seen"} from the bot's heartbeat file."""
    try:
        d = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    age = time.time() - d["t"] if "t" in d else None
    return {"running": age is not None and age < ALIVE_SECONDS, "live_on": bool(d.get("live_on")),
            "dry_run": bool(d.get("dry_run")), "age": age, "last_restart": d.get("last_restart"), "seen": d.get("seen")}


def send(action):
    """'start' or 'pause' -> command id (the status's "seen" equals it once the bot applied it)."""
    assert action in ("start", "pause")
    cid = uuid.uuid4().hex
    _write(CONTROL, {"id": cid, "action": action, "by": "приложение менеджера", "t": int(time.time())})
    return cid


def _write(path, obj):
    LIVE.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    for i in range(10):  # Windows: replace fails while the bot has the file open for a moment
        try:
            os.replace(tmp, path); return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.05)


KEY_INFO = LIVE / "openai_key.json"  # {"expires": "YYYY-MM-DD" | None, "t"}: the OpenAI key's last day, typed in the app


def key_expiry():
    """The current OpenAI key's last day as "YYYY-MM-DD", or None if nobody typed one."""
    try:
        return json.loads(KEY_INFO.read_text(encoding="utf-8")).get("expires")
    except (OSError, ValueError):
        return None


def set_key_expiry(day):
    """day = "YYYY-MM-DD" or None (no date). The bot shows it on «💰 Ключ OpenAI» and reminds the admins before it."""
    _write(KEY_INFO, {"expires": day, "t": int(time.time())})


def reset_gpt_spend():
    """New OpenAI key saved: the spend counter starts again from 0 (the bot applies it within seconds, or at its next start)."""
    _write(GPT_RESET, {"id": uuid.uuid4().hex, "t": int(time.time())})


def gpt_spend():
    """{"usd", "calls", "since"} spent on OpenAI since the last reset. A reset the bot hasn't applied yet already shows 0."""
    def read(p):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    s, r = read(SPEND), read(GPT_RESET)
    if r.get("id") and r["id"] != s.get("reset"):
        return {"usd": 0.0, "calls": 0, "since": r.get("t")}
    return {"usd": float(s.get("usd", 0.0)), "calls": int(s.get("calls", 0)), "since": s.get("since")}


def _run(args):
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flashing
    return subprocess.run(args, capture_output=True, timeout=60, creationflags=flags)


def _stop_pid():
    """Kill the bot by live/bot.pid, but only if that id is still a python process (ids get reused)."""
    try:
        pid = int(PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    r = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if b"python" in r.stdout.lower():
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])


def restart():
    """Stop the bot process (if any) and start the AvitoBot task again. Windows only. It starts switched OFF."""
    if sys.platform != "win32":
        raise ValueError("Перезапуск работает только на компьютере с ботом (Windows)")
    _run(["schtasks", "/End", "/TN", TASK])
    _stop_pid()
    time.sleep(2)
    r = _run(["schtasks", "/Run", "/TN", TASK])
    if r.returncode != 0:
        raise ValueError("Не получилось запустить задачу AvitoBot — попросите администратора")
