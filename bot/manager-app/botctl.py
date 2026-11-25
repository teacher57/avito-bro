"""Bot on/off from the manager app (available to everyone who has the app).

Three controls:
  - Avito (live mode: answers buyers or not): the app writes live/app_control.json, the running bot applies it within
    seconds (same effect as «▶️ Старт» / «⏸ Пауза» in Telegram; the admins get a Telegram note);
  - Telegram (tg_on: polling, notifications, test chat): same file, actions tg_on / tg_off; kept across restarts;
  - the process itself (manager.py): start / restart via the Windows task AvitoBot if it exists, else run_manager.cmd
    directly (detached, keeps running when the app closes); stopped by the process id in live/bot.pid. Every process
    start begins with Avito switched OFF, so after a restart press «Включить» again.
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
    """{"running", "live_on", "tg_on", "dry_run", "age", "last_restart", "seen"} from the bot's heartbeat file."""
    try:
        d = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    age = time.time() - d["t"] if "t" in d else None
    return {"running": age is not None and age < ALIVE_SECONDS, "live_on": bool(d.get("live_on")),
            "tg_on": bool(d.get("tg_on", True)),
            "license": d.get("license") or {"state": "unchecked", "until": None, "reason": "лицензия ещё не проверена"},
            "dry_run": bool(d.get("dry_run")), "age": age, "last_restart": d.get("last_restart"), "seen": d.get("seen")}


def send(action):
    """'start' / 'pause' (Avito) or 'tg_on' / 'tg_off' (Telegram) -> command id (status "seen" equals it once applied)."""
    assert action in ("start", "pause", "tg_on", "tg_off")
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


def device_code():
    """This PC's code for the BOT BRO license (bot/bot_license.py): shown in the app, sent to the admin to get a license."""
    try:
        if str(BOT_DIR) not in sys.path:
            sys.path.append(str(BOT_DIR))
        import bot_license
        return bot_license.device_code()
    except Exception:
        return "не удалось определить"


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


def _has_task():
    return _run(["schtasks", "/Query", "/TN", TASK]).returncode == 0


def stop():
    """Stop the bot process (task AvitoBot if it exists, and the pid). Windows only."""
    if sys.platform != "win32":
        raise ValueError("Останавливать можно только на компьютере с ботом (Windows)")
    if _has_task():
        _run(["schtasks", "/End", "/TN", TASK])
    _stop_pid()


def restart():
    """Stop the bot process (if any) and start it again: the AvitoBot task if it exists (tray icon + bot), else
    run_manager.cmd directly, detached from the app. Windows only. It starts with Avito switched OFF."""
    stop()
    time.sleep(2)
    if _has_task():
        r = _run(["schtasks", "/Run", "/TN", TASK])
        if r.returncode != 0:
            raise ValueError("Не получилось запустить задачу AvitoBot — попросите администратора")
        return
    # CREATE_NO_WINDOW gives cmd.exe its own hidden console, which python.exe inherits: no window ever. (DETACHED_PROCESS
    # must not be added: with it the console-less cmd.exe makes Windows open a NEW, visible console for python.exe.)
    # The bot keeps running when the app closes: it is a separate process, not tied to the app.
    NEW_GROUP, NO_WINDOW = 0x00000200, 0x08000000
    subprocess.Popen(["cmd.exe", "/c", str(BOT_DIR / "run_manager.cmd")], cwd=str(BOT_DIR), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=NEW_GROUP | NO_WINDOW)
