"""Tiny Telegram Bot API client (stdlib only). Token comes from $TG_BOT_TOKEN."""
import json
import mimetypes
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}/"


def call(method, http_timeout=20, **params):
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(BASE + method, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"telegram {method}: {out.get('description')}")
    return out["result"]


def _multipart(fields, files):
    """fields: {name: str}; files: {name: Path}. Returns (body, content_type)."""
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, p in files.items():
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{Path(p).name}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n".encode() + Path(p).read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


# Telegram switch (manager app «Telegram-бот»): False = the bot sends nothing to Telegram (manager.py sets it).
ENABLED = True


def send_photos(chat_id, paths, caption=None):
    """One photo -> sendPhoto, several -> one album (sendMediaGroup, up to 10). Nothing when ENABLED is False."""
    if not ENABLED:
        return None
    paths = [Path(p) for p in paths if Path(p).is_file()][:10]
    if not paths:
        return
    if len(paths) == 1:
        body, ct = _multipart({"chat_id": str(chat_id), **({"caption": caption} if caption else {})}, {"photo": paths[0]})
        method = "sendPhoto"
    else:
        media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": caption} if caption and i == 0 else {})}
                 for i in range(len(paths))]
        body, ct = _multipart({"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)},
                              {f"p{i}": p for i, p in enumerate(paths)})
        method = "sendMediaGroup"
    req = urllib.request.Request(BASE + method, data=body, headers={"Content-Type": ct})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


REMOVE_KEYBOARD = "remove"  # keyboard=REMOVE_KEYBOARD hides the reply keyboard (access taken away)


def send(chat_id, text, buttons=None, keyboard=None):
    """HTML text. buttons = [[(label, callback_data)]] inline; keyboard = [[label]] reply keyboard. Nothing when ENABLED is False."""
    if not ENABLED:
        return None
    markup = None
    if buttons:
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    elif keyboard is REMOVE_KEYBOARD:
        markup = {"remove_keyboard": True}
    elif keyboard:
        markup = {"keyboard": [[{"text": t} for t in row] for row in keyboard], "resize_keyboard": True}
    chunks, cur = [], ""
    for line in text.split("\n"):  # Telegram limit is 4096 chars; split on lines so HTML tags stay whole
        if cur and len(cur) + len(line) > 3900:
            chunks.append(cur); cur = ""
        cur += ("\n" if cur else "") + line[:3900]
    chunks.append(cur)
    out = None
    for i, c in enumerate(chunks):
        out = call("sendMessage", chat_id=chat_id, text=c or "…", parse_mode="HTML",
                   disable_web_page_preview=True, reply_markup=markup if i == len(chunks) - 1 else None)
    return out
