"""Avito messenger auto-reply bot (BRO x BRO SHOP).

Safety first:
  * Only chats with buyers in ALLOWED_BUYERS are ever opened, answered or marked read.
    Every other chat is left untouched.
  * DRY_RUN = True means replies are only printed, never sent.

Credentials come from the environment (set in ~/.zshrc):
  AVITO_CLIENT_ID, AVITO_CLIENT_SECRET

Run:
  python avito-bot/bot.py          # poll forever
  python avito-bot/bot.py --once   # one pass, then exit
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- settings
SHOP_USER_ID = SHOP_ACCOUNT_ID            # BRO x BRO SHOP
ALLOWED_BUYERS = {TEST_BUYER_ID}        # test buyer only (your personal account)
DRY_RUN = True                      # True = print replies, don't send
POLL_SECONDS = 20

API = "https://api.avito.ru"
STATE_FILE = Path(__file__).with_name("state.json")   # last handled message per chat


# ---------------------------------------------------------------- HTTP helpers
def _request(method, path, token=None, data=None, form=False):
    url = API + path
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class Token:
    """Fetches a client_credentials token and renews it before the 24h expiry."""

    def __init__(self):
        self.client_id = os.environ.get("AVITO_CLIENT_ID")
        self.client_secret = os.environ.get("AVITO_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            sys.exit("AVITO_CLIENT_ID / AVITO_CLIENT_SECRET are not set (check ~/.zshrc, then `source ~/.zshrc`).")
        self._value = None
        self._expires_at = 0

    def get(self):
        if not self._value or time.time() > self._expires_at - 600:
            d = _request("POST", "/token/", data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, form=True)
            if "access_token" not in d:
                raise RuntimeError(f"token request failed: {list(d.keys())}")
            self._value = d["access_token"]
            self._expires_at = time.time() + int(d.get("expires_in", 86400))
        return self._value


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- reply logic
def make_reply(chat, new_messages):
    """Decide what to answer. Placeholder until we pick the real 'brain'."""
    item = ((chat.get("context") or {}).get("value") or {}).get("title", "?")
    last_text = new_messages[-1]["content"].get("text", "")
    return f"[draft] reply about «{item}» to: {last_text!r}"


# ---------------------------------------------------------------- main loop
def buyer_id(chat):
    for u in chat.get("users", []):
        if u.get("id") != SHOP_USER_ID:
            return u.get("id")
    return None


def one_pass(token, state):
    t = token.get()
    chats = _request("GET", f"/messenger/v2/accounts/{SHOP_USER_ID}/chats?limit=100&chat_types=u2i,u2u", t)
    for chat in chats.get("chats", []):
        if buyer_id(chat) not in ALLOWED_BUYERS:
            continue                                   # never touch other customers' chats
        chat_id = chat["id"]
        last = chat.get("last_message") or {}
        if last.get("id") == state.get(chat_id) or last.get("direction") != "in":
            continue                                   # nothing new from the buyer

        msgs = _request("GET", f"/messenger/v3/accounts/{SHOP_USER_ID}/chats/{chat_id}/messages/?limit=20", t)
        msgs = sorted(msgs if isinstance(msgs, list) else msgs.get("messages", []), key=lambda m: m.get("created", 0))
        seen = state.get(chat_id)
        if seen and any(m.get("id") == seen for m in msgs):
            idx = next(i for i, m in enumerate(msgs) if m.get("id") == seen)
            msgs = msgs[idx + 1:]
        last_out = max((i for i, m in enumerate(msgs) if m.get("direction") == "out"), default=-1)
        msgs = msgs[last_out + 1:]                     # only what came after the shop's last reply
        new_in = [m for m in msgs
                  if m.get("direction") == "in" and m.get("type") == "text"]   # skips system/bot/voice/image
        if not new_in:
            state[chat_id] = last.get("id")
            continue

        reply = make_reply(chat, new_in)
        print(f"\n[{time.strftime('%H:%M:%S')}] chat {chat_id}")
        for m in new_in:
            print(f"  buyer: {m['content'].get('text', '')}")
        if DRY_RUN:
            print(f"  WOULD REPLY: {reply}")
        else:
            _request("POST", f"/messenger/v1/accounts/{SHOP_USER_ID}/chats/{chat_id}/messages", t,
                     data={"message": {"text": reply}, "type": "text"})
            _request("POST", f"/messenger/v1/accounts/{SHOP_USER_ID}/chats/{chat_id}/read", t, data={})
            print(f"  SENT: {reply}")
        state[chat_id] = last.get("id")
        save_state(state)


def main():
    once = "--once" in sys.argv
    token = Token()
    state = load_state()
    print(f"Avito bot started | dry_run={DRY_RUN} | allowed buyers={sorted(ALLOWED_BUYERS)}")
    while True:
        try:
            one_pass(token, state)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} on {e.url.split('?')[0]}")
        except Exception as e:  # keep running on network hiccups
            print(f"error: {type(e).__name__}: {e}")
        if once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
