"""Tray icon for the Avito bot: Windows notification area (bottom right, or under «^») / Mac menu bar (top right).

The tray OWNS the bot: it starts manager.py as its own child process, restarts it if it dies, and «Выключить бота»
stops the bot and closes the tray, so a running bot always has a visible icon and closing the icon leaves no bot running
on this computer. One tray per computer (a second copy just exits). Only one computer should run the bot at a time:
if Telegram reports a second bot with the same token (HTTP 409 in the log), the icon turns red and says so.

Icon colour: green = live (answers buyers), orange = paused, yellow = starting, red = not running / stuck / second bot.
Menu: status line, Включить / Поставить на паузу (same as the app's «Бот» tab and «▶️ Старт» / «⏸ Пауза» in Telegram),
Открыть приложение менеджера (Windows), Открыть лог, Перезапустить бота, Выключить бота и закрыть.

Run:  Windows — the AvitoBot task starts it at logon:  manager-app\\.venv\\Scripts\\pythonw.exe tray.py
      Mac     — double-click start_tray_mac.command (tokens come from ~/.zshrc)
Test without a real bot (no Avito / Telegram):  AVITO_TRAY_TEST=1 AVITO_TRAY_LIVE=<temp dir> python tray.py
Needs: pystray + Pillow (Windows: manager-app venv; Mac: claude_trusted/.venv, plus pyobjc)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
LIVE = Path(os.environ.get("AVITO_TRAY_LIVE") or HERE / "live")
STATUS, CONTROL, PID, LOG = LIVE / "app_status.json", LIVE / "app_control.json", LIVE / "bot.pid", LIVE / "stdout.log"
TRAY_LOG = LIVE / "tray.log"
TEST = os.environ.get("AVITO_TRAY_TEST") == "1"
WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WIN else 0   # CREATE_NO_WINDOW
LOCK_PORT = 47913 if not TEST else 47914  # localhost port held while a tray runs: one tray per computer
ALIVE = 30            # heartbeat (app_status.json, every ~3 s) older than this = the bot isn't working
STUCK_START = 300     # started this long ago and still no heartbeat = stuck at start
RESPAWN_AFTER = 10    # the bot died on its own: start it again after this many seconds
COLORS = {"live": (46, 160, 67), "paused": (230, 140, 20), "starting": (225, 190, 30), "down": (210, 45, 45)}


def log(*a):
    try:
        LIVE.mkdir(exist_ok=True)
        with open(TRAY_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


def read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(p, obj):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    for i in range(10):  # Windows: replace fails while the bot reads the file for a moment
        try:
            os.replace(tmp, p); return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.05)


def pid_is_python(pid):
    try:
        if WIN:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                               timeout=30, creationflags=NO_WINDOW)
            return b"python" in r.stdout.lower()
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, timeout=10)
        return b"python" in r.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def kill_tree(pid):
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


class Tray:
    def __init__(self):
        self.child = None          # the bot process this tray started
        self.job = None            # Windows job object: the bot dies with the tray
        self.started_t = 0
        self.quitting = False
        self.died_t = None
        self.state = None
        self.icon = pystray.Icon("avito-bot", self.image("starting"), "Бот Авито: запускается…", menu=self.menu())

    # ---- the bot process -------------------------------------------------------------------------------------------
    def start_bot(self):
        self.stop_stray()
        LIVE.mkdir(exist_ok=True)
        if TEST:  # a fake bot: writes the heartbeat and applies Включить / Пауза, nothing else
            cmd = [sys.executable, "-c", FAKE_BOT, str(LIVE)]
            self.child = subprocess.Popen(cmd, creationflags=NO_WINDOW)
        elif WIN:  # run_manager.cmd: tokens from the app's store, output to live\stdout.log
            self.child = subprocess.Popen(["cmd.exe", "/c", str(HERE / "run_manager.cmd")], cwd=HERE, creationflags=NO_WINDOW)
        else:  # Mac: AVITO_TRAY=1 makes manager.py exit by itself if this tray process dies
            py = HERE.parent / ".venv" / "bin" / "python"
            out = open(LOG, "a", encoding="utf-8")
            self.child = subprocess.Popen([str(py if py.exists() else sys.executable), "-u", "manager.py"], cwd=HERE,
                                          stdout=out, stderr=subprocess.STDOUT, env=dict(os.environ, AVITO_TRAY="1"))
        self.job = die_with_tray(self.child)
        self.started_t, self.died_t = time.time(), None
        log("bot started, child pid", self.child.pid)

    def stop_bot(self):
        if self.child and self.child.poll() is None:
            kill_tree(self.child.pid)
            try:
                self.child.wait(15)
            except subprocess.TimeoutExpired:
                pass
        self.child = None
        self.stop_stray()

    def stop_stray(self):
        """Bots this tray didn't start (started by hand, or an old one stuck at start before it wrote live/bot.pid):
        stopped so there's only ever one. Called only when this tray's own bot isn't running."""
        pids = set()
        try:
            pids.add(int(PID.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            pass
        if WIN and not TEST:  # any python running manager.py on this PC
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                                    "Where-Object { $_.CommandLine -match 'manager\\.py' } | ForEach-Object { $_.ProcessId }"],
                                   capture_output=True, timeout=120, creationflags=NO_WINDOW)
                pids |= {int(x) for x in r.stdout.split() if x.strip().isdigit()}
            except (OSError, subprocess.SubprocessError):
                pass
        for pid in pids:
            if (not self.child or pid != self.child.pid) and pid != os.getpid() and pid_is_python(pid):
                log("stopping a bot this tray didn't start, pid", pid)
                kill_tree(pid)

    # ---- status ----------------------------------------------------------------------------------------------------
    def status(self):
        """-> (state, text). state: live / paused / starting / down."""
        if self.quitting:
            return "down", "Бот выключен"
        if self.child is None or self.child.poll() is not None:
            return "down", "Бот остановился — перезапускаю"
        hb = read_json(STATUS)
        age = time.time() - hb["t"] if "t" in hb else None
        fresh = age is not None and age < ALIVE and hb["t"] >= self.started_t - 5  # a heartbeat from this run only
        if fresh and self.second_bot():
            return "down", "Работает второй бот с этим же Telegram — выключите один"
        if fresh:
            return ("live", "Бот включён: отвечает покупателям") if hb.get("live_on") else \
                   ("paused", "Бот на паузе: покупателям не пишет")
        if time.time() - self.started_t < STUCK_START:
            return "starting", "Бот запускается…"
        return "down", "Бот не отвечает: нет сигнала больше 5 минут"

    def second_bot(self):
        """Telegram answered 409 Conflict in the last 2 minutes: another computer polls with the same bot token."""
        try:
            with open(LOG, "rb") as f:
                f.seek(0, 2); f.seek(max(0, f.tell() - 8000))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return False
        now = time.time()
        for line in reversed(tail):
            if "409" in line and "Conflict" in line:
                try:
                    t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                return now - t < 120
        return False

    def watch(self):
        while not self.quitting:
            if self.child is not None and self.child.poll() is not None:  # the bot exited on its own
                if self.died_t is None:
                    self.died_t = time.time()
                    log("bot exited, code", self.child.returncode)
                elif time.time() - self.died_t >= RESPAWN_AFTER:
                    self.start_bot()
            state, text = self.status()
            if state != self.state:
                if state == "down" and self.state in ("live", "paused"):
                    on_ui(lambda t=text: self.notify(t))
                self.state = state
                on_ui(lambda s=state, t=text: self.show(s, t))
            time.sleep(2)

    def show(self, state, text):
        self.icon.icon = self.image(state)
        self.icon.title = f"Бот Авито: {text}"[:127]
        self.icon.update_menu()

    # ---- UI --------------------------------------------------------------------------------------------------------
    @staticmethod
    def image(state):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, 62, 62), fill=COLORS[state])
        try:
            font = ImageFont.truetype("arialbd.ttf" if WIN else "/System/Library/Fonts/Helvetica.ttc", 38)
        except OSError:
            font = ImageFont.load_default()
        d.text((32, 33), "B", fill="white", font=font, anchor="mm")
        return img

    def notify(self, text):
        try:
            self.icon.notify(text, "Бот Авито")
        except Exception:
            pass

    def menu(self):
        st = lambda: self.status()
        return pystray.Menu(
            pystray.MenuItem(lambda item: st()[1], None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Включить: отвечать покупателям", lambda: self.control("start"),
                             visible=lambda item: st()[0] == "paused"),
            pystray.MenuItem("Поставить на паузу", lambda: self.control("pause"), visible=lambda item: st()[0] == "live"),
            pystray.MenuItem("Открыть приложение менеджера", self.open_app,
                             visible=lambda item: WIN and (HERE / "manager-app" / "start_manager.cmd").exists()),
            pystray.MenuItem("Открыть лог", self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Перезапустить бота", self.restart),
            pystray.MenuItem("Выключить бота и закрыть", self.quit))

    def control(self, action):
        write_json(CONTROL, {"id": uuid.uuid4().hex, "action": action, "by": "значок бота", "t": int(time.time())})
        log("control", action)

    def open_app(self):
        subprocess.Popen(["cmd.exe", "/c", str(HERE / "manager-app" / "start_manager.cmd")], cwd=HERE / "manager-app",
                         creationflags=NO_WINDOW)

    def open_log(self):
        if WIN:
            os.startfile(LOG)  # noqa
        else:
            subprocess.Popen(["open", "-a", "Console", str(LOG)])

    def restart(self):
        log("restart from the tray")
        self.stop_bot()
        self.start_bot()

    def quit(self):
        # The confirmation runs in its own thread: a modal dialog opened right in the menu callback runs on the icon's own
        # (pystray) thread and froze on Windows — no clicks while the watch thread kept updating the icon (2026-11-25).
        if getattr(self, "_asking", False):
            return
        self._asking = True
        threading.Thread(target=self._quit_dialog, daemon=True).start()

    def _quit_dialog(self):
        try:
            if not confirm("Выключить бота? Он перестанет отвечать покупателям и слать уведомления, пока его снова не запустят."):
                return
            log("quit from the tray: stopping the bot")
            self.quitting = True
            self.stop_bot()
            on_ui(self.icon.stop)
        finally:
            self._asking = False

    def run(self):
        self.start_bot()
        threading.Thread(target=self.watch, daemon=True).start()
        self.icon.run()


def die_with_tray(proc):
    """Windows: put the bot (cmd.exe and everything it starts) into a job object that is killed when this tray process
    ends — even if the tray crashes or is killed — so a bot never runs without its icon. Returns the job handle, which
    must stay open while the tray runs. Mac: manager.py watches its parent instead (AVITO_TRAY=1)."""
    if not WIN:
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)) and \
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    if not ok:
        log("job object failed, error", ctypes.get_last_error())
    return job


def on_ui(fn):
    """Mac: the menu bar may only be touched from the main thread (from another thread Cocoa crashes the process)."""
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn)
    else:
        fn()


def confirm(text):
    """Yes/No dialog; if no dialog is possible, act without asking."""
    try:
        if WIN:
            import ctypes
            # YES/NO, warning, topmost, set foreground (else it may open behind other windows without focus)
            return ctypes.windll.user32.MessageBoxW(None, text, "Бот Авито", 0x04 | 0x30 | 0x40000 | 0x10000) == 6
        r = subprocess.run(["osascript", "-e", f'display dialog "{text}" buttons {{"Отмена", "Выключить"}} '
                            'default button "Отмена" with title "Бот Авито" with icon caution'], capture_output=True, timeout=120)
        return "Выключить" in r.stdout.decode("utf-8", "replace")
    except Exception:
        return True


FAKE_BOT = r"""
import json, os, sys, time
live = sys.argv[1]; st = {"live_on": False}
def ctl():
    try:
        return json.load(open(os.path.join(live, "app_control.json"), encoding="utf-8"))
    except Exception:
        return {}
seen = ctl().get("id")  # like the real bot: commands from before this start are never replayed
open(os.path.join(live, "bot.pid"), "w").write(str(os.getpid()))
parent = os.getppid()
while os.getppid() == parent:  # like manager.py with AVITO_TRAY=1: stops when the tray is gone
    c = ctl()
    if c.get("id") and c["id"] != seen:
        st["live_on"] = c.get("action") == "start"; seen = c["id"]
    json.dump({"t": int(time.time()), "live_on": st["live_on"]}, open(os.path.join(live, "app_status.json"), "w"))
    time.sleep(2)
"""


def main():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log("another tray is already running here: exiting")
        return
    lock.listen(1)
    if sys.platform == "darwin" and not TEST:  # on a Mac the bot is started by hand: make sure it isn't running elsewhere
        try:
            base = time.strftime("%d.%m %H:%M", time.localtime((LIVE / "state.json").stat().st_mtime))
        except OSError:
            base = "нет"
        if not confirm(f"Запустить бота на этом Маке? Сначала выключите его на taylormomsen, иначе покупатели получат "
                       f"по два ответа. База чатов на этом Маке: {base} — если она старая, сначала перенесите "
                       f"live/state.json с taylormomsen."):
            log("start cancelled on the Mac")
            return
    log("tray started", "(test)" if TEST else "")
    Tray().run()
    log("tray closed")


if __name__ == "__main__":
    main()
