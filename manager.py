"""BRO x BRO SHOP: Avito watcher + Telegram control bot (one process).

OFF BY DEFAULT: nothing is sent to Avito buyers until an admin presses «▶️ Старт» (state.live_on), or anyone presses «Старт»
in the manager app (live/app_control.json).
  Every start of the process switches it off again, even if it was on before the restart.
  While on, the bot answers in every chat created while it is on (its own chats, led from the very first message);
  if a person from the shop writes in such a chat, it counts as handed to the manager and the bot never writes there again. Admin can pause it in Telegram.
  * Every NEW Avito chat (created after the bot started) is recorded in live/state.json.
  * 30 s after the buyer's last message (the timer restarts on each new message) the brain decides
    what it WOULD answer. That draft is stored in the chat (marked as not sent).
  * Admins get a Telegram notification for each buyer message block, after the bot's answer.
  * 15 min of silence after the bot's answer -> the bot would send one reminder (also only recorded).
  * Every new chat created while live mode is on is the bot's (assigned); chats from before are never touched.
  * After a handoff to the manager the bot stops reading that chat.

Roles (live/roles.json): admin = everything above + chat viewer; manager = handoff notifications.
Both have a test sandbox: they write as a buyer, the bot answers as it would on Avito; «Сброс» clears it.

Run:  run_manager.cmd  or  python manager.py   (needs AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, TG_BOT_TOKEN: from the
      environment, e.g. the manager app's tokens via run_manager.cmd, else from avito-bot/.env lines NAME=value)
"""
import html
import json
import os
import re
import sys
import random
import time
import traceback
from pathlib import Path

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"   # optional fallback for tokens; never overrides what is already in the environment
if ENV_FILE.exists():
    for _line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.removeprefix("export ").split("=", 1)
            if _v.strip().strip('"\'') and not os.environ.get(_k.strip()):
                os.environ[_k.strip()] = _v.strip().strip('"\'')
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "brain")); sys.path.insert(0, str(HERE / "gpt"))
import bot as avito   # Avito HTTP helpers + token
import tg
import brain as brain_module  # module ref, to set CLOCK_OFFSET after a clock check
from brain import Brain, model_for_item, order_hello
import products as products_module  # refresh(): pick up the manager app's edits without a restart
from products import PRODUCTS
from build_new_html_style import why_ru

# ---------------------------------------------------------------- settings
DRY_RUN = False             # True: never send anything to Avito buyers (hard switch)
NOTIFY_MANAGER_REAL = False # test mode: real-chat handoffs go to admins only (Maxim answers those chats himself anyway)
REPLY_DELAY = 30            # seconds after the buyer's last message: first message, or after a pause of > ACTIVE_GAP
FAST_DELAY = 5              # live back-and-forth (buyer wrote again within ACTIVE_GAP): answer 5 s after the last message
ACTIVE_GAP = 5 * 60         # buyer messages closer than this = an active conversation
HANDBACK_AFTER = 60 * 60    # manager silent this long in a bot chat handed to him -> the chat is the bot's again (new messages only)
# handoff reasons after which the chat never goes back to the bot: «вы бот?», a photo / voice note / file, a complaint
NO_HANDBACK = re.compile(r"не бот ли|голосов|прислал фото|жалоб|претензи|недовол|брак|оскорб|груб|complaint", re.I)
# what a buyer's non-text Avito message becomes in the chat log; any of these -> straight to the manager, no GPT
MEDIA = {"image": "[фото]", "voice": "[голосовое]", "video": "[видео]", "file": "[файл]"}
MEDIA_WHY = "media from buyer: photo / voice / file, Maxim looks"


def has_media(block):
    """The buyer sent a photo, voice note, video or file in this block: the bot can't see / hear it."""
    return any(tag in x["text"] for x in block for tag in MEDIA.values())


PING_AFTER = 15 * 60        # buyer silent this long after the bot's answer -> one reminder
PING_TEXT = "Если остались вопросы, пишите, с радостью помогу 🤗"
AVITO_POLL = 10             # seconds between Avito checks
CLOCK_CHECK_EVERY = 600     # seconds between checks of this PC's clock against a trusted source (taylormomsen's clock can drift)
CLOCK_DRIFT_ALERT = 120     # seconds of drift that's worth telling the admins about
# The admin is always the same person on any PC (user, 2026-11-17): fixed by Telegram id (a username can change or be
# taken). He is admin from the first start, even with no live/roles.json; nobody else becomes admin by themselves.
ADMIN_IDS = {ADMIN_TG_ID}  # @admin
TEST_BUYER = TEST_BUYER_ID
ALLOWED_TEST_CHATS = {"TEST_CHAT_ID"}  # 2026-11-13: user asked to hand this one TEST_BUYER chat to the bot

LIVE = HERE / "live"
LIVE.mkdir(exist_ok=True)
STATE = LIVE / "state.json"
ROLES = LIVE / "roles.json"
LOG = LIVE / "manager.log"
e = html.escape
NO_ACCESS = "Нет доступа. Попросите администратора добавить вас."


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(p, obj):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(10):  # Windows: replace fails while someone (the manager app) has the file open for a moment
        try:
            tmp.replace(p); return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.05)


def msk(t):
    return time.strftime("%d.%m %H:%M", time.gmtime(t + 3 * 3600))


# Manager app (manager-app/botctl.py): it writes a start/pause command to CONTROL, the bot reads it in its main loop and
# writes its own state to STATUS every few seconds (the app shows «работает / включён / выключен» from it).
CONTROL = LIVE / "app_control.json"   # {"id": unique, "action": "start" | "pause", "by": "..."}
STATUS = LIVE / "app_status.json"     # {"t": heartbeat, "live_on", "dry_run", "last_restart", "seen": last command id}
PID = LIVE / "bot.pid"                # this process's id: the manager app stops the bot by it (botctl.restart)
SPEND = LIVE / "gpt_spend.json"       # {"usd", "calls", "since", "reset"}: $ spent on OpenAI since the last reset (app shows it)
GPT_RESET = LIVE / "gpt_reset.json"   # {"id", "t"}: the app writes it when a new OpenAI key is saved -> counter back to 0
state = load(STATE, {})
state.setdefault("started", int(time.time()))  # set once, ever: chats from before this moment are never touched
state["last_restart"] = int(time.time())       # overwritten every start, for the "ℹ️ Статус" message
state.setdefault("tg_offset", 0)
state.setdefault("chats", {})
state["live_on"] = False  # every start begins switched off; only «▶️ Старт» turns it on
state.setdefault("sandbox", {})
roles = load(ROLES, {"users": {}, "pending": {}})   # users: {tg_id: {role, username, name, mode}}
for _a in ADMIN_IDS:  # the fixed admin is always there and always admin
    _u = roles["users"].setdefault(str(_a), {"role": "admin", "username": None, "name": None, "mode": "normal"})
    _u["role"] = "admin"
    roles["pending"].pop(str(_a), None)
brain = Brain()


def ids_with(role):
    return [int(k) for k, u in roles["users"].items() if u["role"] == role]


# ---------------------------------------------------------------- shared conversation logic
def context_of(msgs):
    """Brain context: everything said before, as «buyer: / shop: / system:» lines. Unsent drafts are left out."""
    out = []
    for m in msgs:
        if m["who"] == "bot" and not m.get("sent"):
            continue
        who = "shop" if m["who"] in ("shop", "bot") else m["who"]
        out.append(f"{who}: {m['text']}")
    return "\n".join(out)


def sent_photos(conv):
    """Pictures already sent in this chat (the bot's sent messages; drafts left out)."""
    return [p for m in conv["msgs"] if m["who"] in ("bot", "shop") and (m["who"] == "shop" or m.get("sent"))
            for p in (m.get("photos") or [])]


def pending_block(conv):
    """Buyer messages not answered yet and the time of the last one. Buyer messages before the shop's last message
    (a person, or a reply the bot really sent) count as answered, even if they all arrived in one late poll."""
    msgs = conv["msgs"]
    last_shop = max((i for i, m in enumerate(msgs) if m["who"] == "shop" or (m["who"] == "bot" and m.get("sent"))), default=-1)
    block = [m for m in msgs[last_shop + 1:] if m["who"] == "buyer" and not m.get("done")]
    return block, (block[-1]["t"] if block else 0)


def reply_delay(conv, block):
    """30 s for the first message or after the buyer was away > 5 min; 5 s while the buyer writes more often than every 5 min.
    The timer always counts from the buyer's LAST message, so every new message restarts it."""
    if has_media(block):
        return 0  # a photo / voice note goes to the manager right away
    first = conv["msgs"].index(block[0])
    earlier = [m["t"] for m in conv["msgs"][:first] if m["who"] == "buyer"]
    return FAST_DELAY if earlier and block[0]["t"] - earlier[-1] <= ACTIVE_GAP else REPLY_DELAY


def order_only(conv):
    """Time of Avito's «новый заказ» if that is all the chat has (no buyer or shop messages yet), else 0."""
    if conv.get("order_hello") or any(m["who"] in ("buyer", "shop", "bot") for m in conv["msgs"]):
        return 0
    return next((m["t"] for m in conv["msgs"] if m["who"] == "system" and "новый заказ" in m["text"].lower()), 0)


def talked_before(conv, msg):
    """Buyer, shop or bot wrote anything before this message (so the size was being discussed / already chosen)."""
    return any(m["who"] in ("buyer", "shop", "bot") for m in conv["msgs"][:conv["msgs"].index(msg)])


def mark_done(conv):
    for m in conv["msgs"]:
        if m["who"] == "buyer":
            m["done"] = True


def decide(conv, model, price):
    """Run the brain on the pending block. Returns (reply, photos, escalate, why)."""
    block, _ = pending_block(conv)
    if has_media(block):
        return None, [], True, MEDIA_WHY
    first = conv["msgs"].index(block[0])
    ctx = context_of(conv["msgs"][:first])
    brain.sent_photos = sent_photos(conv)  # photos go one at a time: the next one not sent in this chat yet
    out = brain.answer("\n".join(m["text"] for m in block), model=model, context=ctx, price=price)
    reply = out.get("reply")
    if reply and not out["escalate"]:
        reply = brain_module.go_ahead_once(reply, said_before(conv))
    return reply, out.get("photos") or [], out["escalate"], out.get("why", "")


def said_before(conv):
    """Texts the bot (sent) and the shop already wrote in this chat."""
    return [m["text"] for m in conv["msgs"] if m["who"] == "shop" or (m["who"] == "bot" and m.get("sent"))]


LIVE_MODEL = "gpt-5.4"  # live Avito chats are voiced by GPT-5.4; falls back to the rules if the API errors


def decide_live(conv, model, price):
    """Live chats: GPT-5.4 writes the answer; any API error (bad key, OpenAI down, ...) falls back to the rules."""
    try:
        reply, photos, esc, why, cost_usd = decide_gpt(conv, model, price, LIVE_MODEL)
        return reply, photos, esc, why, cost_usd
    except Exception as ex:
        log("live gpt error, falling back to rules", conv.get("title"), type(ex).__name__, ex)
        reply, photos, esc, why = decide(conv, model, price)
        return reply, photos, esc, why, None


def decide_gpt(conv, model, price, gpt_model=None):
    """Same as decide(), but GPT (gpt_model, default GPT-4.1 mini) writes the answer (test chat only). Returns (reply, photos, escalate, why, cost)."""
    import gpt_reply  # lazy: the live bot never needs it
    gpt_reply._brain = brain  # reuse the loaded brain (size tree) instead of loading a second copy
    gpt_reply.on_cost = add_spend
    block, _ = pending_block(conv)
    if has_media(block):  # photo / voice / video / file: straight to Maxim, no GPT call
        return None, [], True, MEDIA_WHY, 0.0
    first = conv["msgs"].index(block[0])
    if brain_module.asks_if_bot("\n".join(m["text"] for m in block)):  # «бот» / «робот»: straight to Maxim, no GPT call
        return None, [], True, "buyer asks if this is a bot: Maxim answers", 0.0
    title = next((p["name"] for p in PRODUCTS if p["key"] == model), None)
    msgs = [m for m in conv["msgs"] if m["who"] != "bot" or m.get("sent")]
    g = gpt_reply.answer(title, price, msgs, model=gpt_model)
    why = g.get("why") or ("GPT: [МАКСИМ]" if g["escalate"] else "GPT")
    # «оформил» (order_placed) alone notifies nobody: the 🛒 note needs Avito's «новый заказ» and a known size (user's rule, 17.11)
    # safety net for a product marked «нет в наличии»: no «можете заказывать» / ship day, whatever GPT wrote
    out = brain_module.stock_guard({"reply": g["reply"], "photos": g["photos"], "escalate": g["escalate"], "why": why}, model,
                                   "\n".join(m["text"] for m in block), context_of(conv["msgs"][:first]))
    if out["reply"] and not out["escalate"]:  # «можете заказывать, … отправлю» at most once per chat
        once = brain_module.go_ahead_once(out["reply"], said_before(conv))
        if once != out["reply"]:
            out = {**out, "reply": once, "why": out["why"] + "; «можете заказывать» уже было"}
    return out["reply"], out["photos"], out["escalate"], out["why"], g["cost_usd"]


# ---------------------------------------------------------------- Avito side (read-only in test mode)
token = None


def check_clock():
    """Compares this PC's clock to the Date header of an ordinary HTTPS response from Telegram (already a hard
    dependency, so this adds no new external service). Sets brain.CLOCK_OFFSET so ships_today() ("sent today" /
    "sent tomorrow") stays right even if Windows' own clock has drifted; warns the admins once per drift episode."""
    import email.utils
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request("https://api.telegram.org", method="HEAD"), timeout=10) as resp:
            server_t = email.utils.parsedate_to_datetime(resp.headers.get("Date")).timestamp()
    except Exception as ex:
        log("clock check failed", type(ex).__name__, ex)
        return
    offset = server_t - time.time()
    brain_module.CLOCK_OFFSET = offset
    drifted = abs(offset) >= CLOCK_DRIFT_ALERT
    if drifted != state.get("clock_drifted", False):
        log("clock drift" if drifted else "clock back to normal", f"{offset:+.0f}s")
        for a in ids_with("admin"):
            tg.send(a, (f"⏰ <b>Часы на этом ПК разошлись на {abs(offset):.0f} с</b> ({'спешат' if offset < 0 else 'отстают'}). "
                        "Бот сам поправил «сегодня/завтра отправим», но лучше синхронизировать время в Windows "
                        "(Параметры → Время и язык → Дата и время → Синхронизировать сейчас)."
                        if drifted else "⏰ Часы на этом ПК снова идут нормально."))
        state["clock_drifted"] = drifted
        save(STATE, state)


def avito_poll():
    t = token.get()
    chats = avito._request("GET", f"/messenger/v2/accounts/{avito.SHOP_USER_ID}/chats?limit=50&chat_types=u2i,u2u", t).get("chats", [])
    for c in chats:
        if (c.get("created") or 0) < state["started"] or (avito.buyer_id(c) == TEST_BUYER and c["id"] not in ALLOWED_TEST_CHATS):
            continue  # only dialogs that started after the bot did; never the owner's own test chat (unless explicitly allowed)
        cid = c["id"]
        conv = state["chats"].get(cid)
        if conv is None:
            v = (c.get("context") or {}).get("value") or {}
            buyer = next((u.get("name") for u in c.get("users", []) if u.get("id") != avito.SHOP_USER_ID), "?")
            conv = state["chats"][cid] = {"title": v.get("title") or "?", "price": v.get("price_string"),
                                          "model": model_for_item(v.get("title")), "buyer": buyer,
                                          "created": c.get("created"), "updated": 0, "msgs": [], "seen": [],
                                          "assigned": bool(state.get("live_on")),
                                          "answered_upto": 0}
            log("new chat", cid, conv["title"], "assigned" if conv["assigned"] else "")
            if conv["assigned"]:
                for u in [a for a in ids_with("admin") if a not in ids_with("manager")]:  # not to managers (user's rule, 14.11)
                    tg.send(u, f"🎲 Новый чат ведёт бот: <b>{e(conv['title'][:50])}</b> · {e(buyer)}. "
                               f"Если менеджер напишет в нём сам, чат перейдёт к нему.\n<a href=\"https://www.avito.ru/profile/messenger/channel/{cid}\">Открыть на Авито</a>")
        else:  # the listing's price can change: keep the one Avito shows now
            price = ((c.get("context") or {}).get("value") or {}).get("price_string")
            if price and price != conv.get("price"):
                log("price changed", cid, conv.get("price"), "->", price)
                conv["price"] = price
        if (c.get("updated") or 0) <= conv["updated"]:
            continue
        ms = avito._request("GET", f"/messenger/v3/accounts/{avito.SHOP_USER_ID}/chats/{cid}/messages/?limit=100", t)
        ms = sorted(ms if isinstance(ms, list) else ms.get("messages", []), key=lambda m: m.get("created", 0))
        fresh = []
        for m in ms:
            if m.get("id") in conv["seen"]:
                continue
            conv["seen"].append(m["id"])
            text = ((m.get("content") or {}).get("text") or "").strip()
            if m.get("type") == "system":
                who, text = "system", text.replace("[Системное сообщение] ", "")
            else:
                who = "buyer" if m.get("direction") == "in" else "shop"
                if who == "shop" and conv["assigned"] and not conv.get("human"):
                    conv["human"] = True  # a person from the shop wrote: the chat is the manager's now (back to the bot after an hour of his silence)
                    conv["escalated"] = conv.get("escalated") or "менеджер написал в чате сам"
                    conv["handed_t"] = m.get("created") or int(time.time())
                    log("human took over", cid)
                    for u in [a for a in ids_with("admin") if a not in ids_with("manager")]:  # the manager knows he wrote: admins only
                        tg.send(u, f"🧑‍💼 <b>Чат теперь у менеджера</b>\n<b>{e(conv['title'][:50])}</b> · {e(conv.get('buyer', ''))}\n"
                                   "В чате написали сами, бот здесь не отвечает. Если менеджер промолчит час, на новые сообщения "
                                   "покупателя снова ответит бот."
                                   f"\n<a href=\"https://www.avito.ru/profile/messenger/channel/{cid}\">Открыть чат на Авито</a>")
                if m.get("type") != "text":
                    tag = MEDIA.get(m.get("type"))
                    if tag:  # a caption (if any) stays after the tag, the tag is what sends the chat to the manager
                        text = f"{tag} {text}".strip()
                    else:
                        text = text or {"link": "[ссылка]"}.get(m.get("type"), f"[{m.get('type')}]")
            conv["msgs"].append({"t": m.get("created") or int(time.time()), "who": who, "text": text})
            fresh.append(conv["msgs"][-1])
        conv["updated"] = c.get("updated") or 0
        feed_manager_chat(cid, conv, fresh)
        order_msg = next((m for m in fresh if m["who"] == "system" and "новый заказ" in m["text"].lower()), None)
        # an order after any conversation -> Maxim right away; an order with no conversation -> the bot greets
        # and asks height/weight (order_only branch in tick_real), and a later order in that chat goes to Maxim
        # a bot chat the bot already handed over (e.g. «оформил» -> escalate) still gets the 🛒 note with the size, once
        # bot chats (even ones the bot handed over): Maxim gets a 🛒 note with the size, once; the bot keeps leading its chat
        if order_msg and talked_before(conv, order_msg) and not (conv["assigned"] and conv.get("human")):
            if conv["assigned"]:
                if not order_noted(conv):
                    on_avito_order(cid, conv, int(time.time()))
            elif not conv.get("escalated"):
                hand_over_order(cid, conv, order_msg)
    save(STATE, state)


def is_bot_chat(conv):
    """The bot leads this chat (its own chat, not handed over, no person from the shop wrote). Everything else is a manager chat."""
    return conv["assigned"] and not conv.get("escalated") and not conv.get("human")


def feed_admins():
    """Admins who switched on «all messages from manager chats»."""
    return [i for i in ids_with("admin") if roles["users"][str(i)].get("mgr_feed", True)]


def feed_manager_chat(cid, conv, fresh):
    """Manager chats: shop/system messages right away; buyer messages right away only when no bot draft will follow
    (chat already handed over); otherwise they come together with the bot's draft."""
    if is_bot_chat(conv) or not fresh:
        return
    drafting = not conv.get("escalated") and not conv.get("human")
    show = [m for m in fresh if m["who"] != "buyer" or not drafting]
    if not show:
        return
    icon = {"buyer": "👤", "shop": "🏪 Менеджер:", "system": "⚙️"}
    text = (f"📨 <b>{e(conv['title'][:50])}</b> · {e(conv['buyer'])} <i>(чат менеджера)</i>\n"
            + "\n".join(f"{icon.get(m['who'], '•')} {e(m['text'])}" for m in show))
    for a in feed_admins():
        tg.send(a, text, buttons=[[("Открыть переписку", f"chat:{cid}")]])


def is_live(now, conv):
    return (not DRY_RUN and state.get("live_on") and conv["assigned"]
            and not conv.get("human"))


def avito_send_text(cid, text):
    r = avito._request("POST", f"/messenger/v1/accounts/{avito.SHOP_USER_ID}/chats/{cid}/messages", token.get(),
                       data={"message": {"text": text}, "type": "text"})
    log("SENT text", cid, text[:60])
    return r.get("id")


def avito_send_image(cid, path):
    """Upload the picture, then send it to the chat. Returns the message id."""
    import urllib.request
    body, ct = tg._multipart({}, {"uploadfile[]": Path(path)})
    req = urllib.request.Request(avito.API + f"/messenger/v1/accounts/{avito.SHOP_USER_ID}/uploadImages", data=body,
                                 headers={"Content-Type": ct, "Authorization": f"Bearer {token.get()}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        image_id = next(iter(json.loads(resp.read())))
    r = avito._request("POST", f"/messenger/v1/accounts/{avito.SHOP_USER_ID}/chats/{cid}/messages/image", token.get(),
                       data={"image_id": image_id})
    log("SENT image", cid, Path(path).name)
    return r.get("id")


def manager_t(conv):
    """When the manager last had the chat: the handoff or his own last message, whichever is later. Unknown (old chats
    marked human without a recorded shop message) -> the last message before the buyer's unanswered ones."""
    ts = [conv.get("handed_t") or 0]
    ts += [m["t"] for m in conv["msgs"] if m["who"] == "shop"]
    ts += [m["t"] for m in conv["msgs"] if m["who"] == "bot" and (m.get("text") or "").startswith("→ передано менеджеру")]
    if max(ts):
        return max(ts)
    block, _ = pending_block(conv)
    first = conv["msgs"].index(block[0]) if block else len(conv["msgs"])
    return max([m["t"] for m in conv["msgs"][:first]] or [0])


def after_handback(conv):
    """Buyer messages not answered yet that came after the hand-back moment (an hour of the manager's silence)."""
    block, _ = pending_block(conv)
    since = manager_t(conv) + HANDBACK_AFTER
    return [m for m in block if m["t"] >= since]


def may_hand_back(conv, now):
    """A bot chat now with the manager goes back to the bot when the buyer writes after the manager has been silent for
    HANDBACK_AFTER (since the handoff or his last message). Not after «вы бот?», a voice note or a complaint; only while
    live mode is on. Taken lazily, on such a message, so a quiet chat never moves and nobody gets a note for it."""
    if DRY_RUN or not state.get("live_on") or not conv["assigned"] or not (conv.get("escalated") or conv.get("human")):
        return False
    if NO_HANDBACK.search(conv.get("escalated") or ""):
        return False
    return bool(after_handback(conv))


def hand_back(cid, conv, now):
    """The chat is the bot's again. Buyer messages from before the hand-back moment stay the manager's: the bot only
    answers the ones that came after it (user's rule, 14.11)."""
    new = after_handback(conv)
    block, _ = pending_block(conv)
    for m in block:
        if m not in new:
            m["done"] = True
    conv["human"] = False
    conv.pop("escalated", None)
    conv["msgs"].append({"t": now, "who": "bot", "sent": False, "text": "→ чат снова у бота (менеджер молчал час)"})
    log("chat back to bot: manager silent an hour", cid)
    text = (f"🤖 <b>Чат снова ведёт бот</b>\n<b>{e(conv['title'][:50])}</b> · {e(conv.get('buyer', ''))}\n"
            "Менеджер не отвечал час, и покупатель написал снова — бот ответит на новые сообщения. Чтобы вести чат самому, просто напишите в нём."
            f"\n<a href=\"https://www.avito.ru/profile/messenger/channel/{cid}\">Открыть чат на Авито</a>")
    for u in set((ids_with("manager") or ids_with("admin")) + ids_with("admin")):
        tg.send(u, text)


def tick_real(now):
    for cid, conv in state["chats"].items():
        if may_hand_back(conv, now):
            hand_back(cid, conv, now)
        if conv.get("escalated") or (conv["assigned"] and conv.get("human")):
            continue  # handed to the manager (or the manager wrote himself): the bot doesn't answer until may_hand_back
        block, last_t = pending_block(conv)
        if block and now - last_t >= reply_delay(conv, block):
            reply, photos, esc, why, cost_usd = decide_live(conv, conv["model"], conv["price"])
            mark_done(conv)
            send_live = is_live(now, conv)
            if esc:
                conv["escalated"], conv["handed_t"] = why_ru(why), now
                conv["msgs"].append({"t": now, "who": "bot", "text": f"→ передано менеджеру ({why_ru(why)})", "sent": send_live})
                if send_live or NOTIFY_MANAGER_REAL:
                    notify_handoff(ids_with("manager") or ids_with("admin"), conv, cid, block, why, real=True)
            elif reply or photos:
                if send_live:
                    try:
                        if reply:
                            conv["seen"].append(avito_send_text(cid, reply))
                        for p in photos:
                            conv["seen"].append(avito_send_image(cid, p))
                    except Exception as ex:
                        send_live = False
                        log("avito send error", cid, type(ex).__name__, ex)
                        for a in ids_with("admin"):
                            tg.send(a, f"⚠️ Не удалось отправить ответ в Авито ({e(type(ex).__name__)}). Чат: {e(conv['title'][:40])}")
                conv["msgs"].append({"t": now, "who": "bot", "text": reply or "", "photos": photos, "sent": send_live})
                conv["bot_t"], conv["pinged"] = now, False
            else:
                conv["msgs"].append({"t": now, "who": "bot", "text": "(молчит: отвечать нечего)", "sent": send_live})
                conv["bot_t"], conv["pinged"] = None, True
            notify_admins_block(cid, conv, block)
            if not esc and has_avito_order(conv) and not order_noted(conv) and is_bot_chat(conv):
                size = size_now(conv, block, reply)  # the order waited for a size: confirm it once the chat names one
                if size:
                    confirm_order(cid, conv, size, now)
            save(STATE, state)
        elif not block and order_only(conv) and now - order_only(conv) >= REPLY_DELAY:
            # the buyer ordered without writing anything: greet, ask height/weight, say when it ships
            if not products_module.in_stock(conv["model"]):  # marked «нет в наличии» in the manager app: Maxim decides
                conv["order_hello"] = True
                if conv["assigned"]:
                    conv["escalated"] = "заказ товара, которого нет в наличии"
                    conv["msgs"].append({"t": now, "who": "bot", "text": "→ передано менеджеру (заказ товара, которого нет в наличии)", "sent": False})
                    if is_live(now, conv) or NOTIFY_MANAGER_REAL:
                        notify_handoff(ids_with("manager") or ids_with("admin"), conv, cid, [avito_order_msg(conv)],
                                       "заказ товара, которого нет в наличии", real=True)
                save(STATE, state)
                continue
            text = order_hello(now)
            send_live = is_live(now, conv)
            if send_live:
                try:
                    conv["seen"].append(avito_send_text(cid, text))
                except Exception as ex:
                    send_live = False; log("avito send error", cid, type(ex).__name__, ex)
            conv["order_hello"] = True
            conv["msgs"].append({"t": now, "who": "bot", "text": text, "sent": send_live})
            conv["bot_t"], conv["pinged"] = now, True  # ordered: no «клиент молчит»; the 🛒 note goes once the size is known
            notify_admins_block(cid, conv, [{"who": "system", "text": "⚙️ Новый заказ без переписки"}])
            save(STATE, state)
        elif not block and is_bot_chat(conv) and order_silent(conv, now):
            conv["order_silent_noted"] = True
            conv["msgs"].append({"t": now, "who": "bot", "sent": False, "text": "→ заказ: покупатель молчит 30 мин, размера нет — менеджер получил уведомление"})
            notify_order(cid, conv, avito_order_msg(conv), handed=False, keeps=True, note=ORDER_SILENT_NOTE)
            log("order without a size, buyer silent 30 min: manager notified", cid)
            save(STATE, state)
        elif (not block and conv.get("bot_t") and not conv.get("pinged") and now - conv["bot_t"] >= PING_AFTER
              and not any(brain_module.QUESTIONS_LEFT.search(t or "") for t in said_before(conv))  # once per chat
              and not has_order(conv)  # the item is already ordered: no «клиент молчит» reminder (user's rule, 14.11)
              and conv["msgs"] and conv["msgs"][-1]["who"] == "bot"):
            sent = is_live(now, conv)
            if sent:
                try:
                    conv["seen"].append(avito_send_text(cid, PING_TEXT))
                except Exception as ex:
                    sent = False; log("avito ping error", cid, ex)
            conv["msgs"].append({"t": now, "who": "bot", "text": PING_TEXT, "sent": sent, "ping": True})
            conv["pinged"] = True
            for a in (ids_with("admin") if sent or is_bot_chat(conv) else feed_admins()):
                tg.send(a, f"🔔 <b>{e(conv['title'][:50])}</b> · {e(conv['buyer'])}\nКлиент молчит 15 мин. Бот {'написал' if sent else 'написал бы'}: <i>{e(PING_TEXT)}</i>",
                        buttons=[[("Открыть переписку", f"chat:{cid}")]])
            save(STATE, state)


def notify_admins_block(cid, conv, block):
    bot_msg = conv["msgs"][-1]
    lines = [f"💬 <b>{e(conv['title'][:50])}</b> · {e(conv['buyer'])}" + (" · 🎲 чат бота" if conv["assigned"] else "")]
    lines += [("👤 " if m.get("who", "buyer") == "buyer" else "") + e(m["text"]) for m in block]
    mark = "" if bot_msg.get("sent") else " <i>(тест, не отправлено)</i>"
    extra = f" [+{len(bot_msg['photos'])} фото]" if bot_msg.get("photos") else ""
    lines.append(f"🤖 {e(bot_msg['text'])}{extra}{mark}")
    for a in (ids_with("admin") if bot_msg.get("sent") or is_bot_chat(conv) else feed_admins()):
        tg.send(a, "\n".join(lines), buttons=[[("Открыть переписку", f"chat:{cid}")]])


ORDER_SILENT_AFTER = 30 * 60  # an order still without a size and the buyer silent this long -> the manager is told
ORDER_SILENT_NOTE = "Покупатель 30 минут ничего не пишет (после своего последнего сообщения или заказа), размер неизвестен: уточните размер сами."


def order_silent(conv, now, wait=ORDER_SILENT_AFTER):
    """Avito's order is there, the size still isn't, and the BUYER wrote nothing for `wait` seconds since his last message
    (or since the order, if that came later); the bot's own messages and reminders don't restart the clock (user's rule,
    17.11). The manager gets a 🛒 note without a size, once. If the size comes later, confirm_order still runs."""
    if not has_avito_order(conv) or order_noted(conv) or conv.get("order_silent_noted"):
        return False
    since = max(m["t"] for m in conv["msgs"] if m["who"] == "buyer" or (m["who"] == "system" and "новый заказ" in (m["text"] or "").lower()))
    return now - since >= wait


ORDER_ASK = "Спасибо за заказ! Напишите, пожалуйста, свой рост и вес, я подберу вам размер 🤗"
SIZE_HINT = re.compile(r"\d|\b(x{0,3}[sl]|m|\d?xl)\b", re.I)


def order_confirm_text(size):
    return f"Спасибо за заказ, отправлю вам {size} размер 🤗"


def has_avito_order(conv):
    return any(m["who"] == "system" and "новый заказ" in (m["text"] or "").lower() for m in conv.get("msgs", []))


def avito_order_msg(conv):
    return next(m for m in conv["msgs"] if m["who"] == "system" and "новый заказ" in (m["text"] or "").lower())


def size_now(conv, block, reply):
    """After Avito's «новый заказ»: the size the buyer ordered, once the chat names one ("" = not yet). GPT is asked only
    when the latest messages could name a size (a number or S / M / L / XL), so a chat about delivery costs nothing."""
    if not any(SIZE_HINT.search(t or "") for t in [m["text"] for m in block] + [reply]):
        return ""
    return ordered_size(conv)


def bot_say(cid, conv, now, text):
    """The bot writes one message in a live chat (to Avito when live, else it's only recorded)."""
    send_live = is_live(now, conv)
    if send_live:
        try:
            conv["seen"].append(avito_send_text(cid, text))
        except Exception as ex:
            send_live = False
            log("avito send error", cid, type(ex).__name__, ex)
    conv["msgs"].append({"t": now, "who": "bot", "text": text, "sent": send_live})
    conv["bot_t"] = now
    return send_live


def confirm_order(cid, conv, size, now):
    """Avito's order is there and the size is known: the buyer gets «Спасибо за заказ, отправлю вам N размер», the manager
    the 🛒 note with the size; once per chat (user's rule, 17.11)."""
    conv["order_size"], conv["order_noted"], conv["pinged"] = size, True, True
    bot_say(cid, conv, now, order_confirm_text(size))
    conv["msgs"].append({"t": now, "who": "bot", "sent": False, "text": f"→ заказ: менеджер получил уведомление (размер {size}), бот продолжает вести чат"})
    notify_order(cid, conv, avito_order_msg(conv), handed=False, size=size, keeps=True)
    log("order confirmed to the buyer, size sent to the manager", cid, size)


def on_avito_order(cid, conv, now):
    """«У вас новый заказ» after a talk in a bot chat. Size known -> confirm_order. No size yet -> nobody is notified; in a
    chat the bot leads it asks height/weight, and tick_real confirms once the size is named. A chat the bot already
    handed over stays the manager's: he is in it, no 🛒 note."""
    conv["pinged"] = True  # no «клиент молчит» ping after an order
    if not is_bot_chat(conv):  # the bot handed this chat over: the manager is in it; the 🛒 note only if the size is known
        size = ordered_size(conv)
        if size:
            conv["order_size"], conv["order_noted"] = size, True
            notify_order(cid, conv, avito_order_msg(conv), handed=True, size=size)
        conv["msgs"].append({"t": now, "who": "bot", "sent": False, "text": f"→ заказ в чате менеджера: "
                             + (f"уведомление с размером {size}" if size else "размера в переписке нет, уведомления нет")})
        return
    size = ordered_size(conv)
    if size:
        confirm_order(cid, conv, size, now)
        return
    conv["msgs"].append({"t": now, "who": "bot", "sent": False, "text": "→ заказ: размера в переписке нет, менеджеру сообщим, когда он будет"})
    if not conv.get("order_asked"):
        conv["order_asked"] = True
        bot_say(cid, conv, now, ORDER_ASK)
    log("new order without a size: asked the buyer", cid)


def has_order(conv):
    """The item is already ordered in this chat: Avito's «новый заказ» arrived or the buyer wrote he ordered."""
    return order_noted(conv) or any(m["who"] == "system" and "новый заказ" in m["text"].lower() for m in conv.get("msgs", []))


def order_noted(conv):
    """Maxim already got the 🛒 note for this chat (order_size: chats noted before the order_noted flag existed)."""
    return bool(conv.get("order_noted") or conv.get("order_size"))


def hand_over_order(cid, conv, msg):
    """«У вас новый заказ» in a manager chat (not the bot's): mark it handed over and tell the admins."""
    now = int(time.time())
    send_live = is_live(now, conv)
    mark_done(conv)
    why = "new order / paid: Maxim sends it"
    conv["escalated"] = why_ru(why)
    conv["msgs"].append({"t": now, "who": "bot", "text": f"→ передано менеджеру ({why_ru(why)})", "sent": send_live})
    if conv["assigned"]:
        conv["order_size"] = ordered_size(conv)
        notify_order(cid, conv, msg, size=conv["order_size"])
    notify_admins_block(cid, conv, [msg])
    log("new order: handed to manager", cid, conv.get("order_size", ""))


def ordered_size(conv):
    """Size the buyer ordered, taken from the chat above (GPT calls order_placed with it, or without it if no size was
    named); if OpenAI fails, the size the shop last named. "" = no size in the chat. The buyer is never asked."""
    try:
        import gpt_reply
        gpt_reply._brain = brain
        gpt_reply.on_cost = add_spend
        msgs = [m for m in conv["msgs"] if m["who"] != "bot" or m.get("sent")]
        return gpt_reply.order_size(conv["title"], conv["price"], msgs, model=LIVE_MODEL)[0]
    except Exception as ex:
        log("order size: gpt error, using the last size named", type(ex).__name__, ex)
        for m in reversed(conv["msgs"]):  # the last size the bot / shop recommended in this chat
            if m["who"] in ("bot", "shop") and (m["who"] == "shop" or m.get("sent")):
                f = brain_module.REPLY_SIZE.search(m["text"] or "")
                if f:
                    return brain_module.SIZE_LABEL.get(int(f.group(1) or f.group(2)), "")
        return ""


def notify_order(cid, conv, msg, handed=True, size=None, keeps=False, note=None):
    """An order was placed in a chat the bot leads: the manager ships it (and takes the chat over when handed)."""
    link = f"https://www.avito.ru/profile/messenger/channel/{cid}"
    text = (f"🛒 <b>Новый заказ</b> {'в чате, который вёл бот' if handed else 'в чате бота'}\n<b>{e(conv['title'][:50])}</b> · {e(conv.get('price') or '')} · {e(conv['buyer'])}\n"
            + (f"📏 <b>Размер: {e(size)}</b>\n" if size else "") + (f"⏰ {e(note)}\n" if note else "") +
            f"⚙️ {e(msg['text'][:200])}\n" + ("Бот продолжает вести чат и отвечает на вопросы покупателя; отправка за вами." if keeps else
                                         "Бот передал чат вам и больше в нём не пишет." if handed else "Переписки не было: бот спросил рост и вес, отправка за вами.") + f"\n<a href=\"{link}\">Открыть чат на Авито</a>")
    for u in ids_with("manager") or ids_with("admin"):
        tg.send(u, text)
    for a in ids_with("admin"):
        if a not in ids_with("manager"):
            tg.send(a, text)
    log("order notified", cid)


def notify_handoff(to, conv, cid, block, why, real):
    link = f"https://www.avito.ru/profile/messenger/channel/{cid}" if real else None
    text = (f"🧑‍💼 <b>Нужен менеджер</b>{' (тест)' if not real else ''}\n<b>{e(conv.get('title', 'тестовый чат')[:50])}</b> · {e(conv.get('buyer', ''))}\n"
            + "\n".join(f"👤 {e(m['text'])}" for m in block) + f"\nПричина: {e(why_ru(why))}"
            + (f"\n<a href=\"{link}\">Открыть чат на Авито</a>" if link else ""))
    for u in to:
        tg.send(u, text)


# ---------------------------------------------------------------- sandbox (test mode in Telegram)
def sandbox(uid):
    return state["sandbox"].setdefault(str(uid), {"model": None, "msgs": [], "answered_upto": 0})


def tick_sandbox(now):
    for uid, sb in state["sandbox"].items():
        if sb.get("escalated"):
            continue
        block, last_t = pending_block(sb)
        if block and now - last_t >= (0 if instant(uid) else reply_delay(sb, block)):
            gpt_note = ""
            eng = ENGINES[engine(uid)]
            if eng["model"]:
                try:
                    reply, photos, esc, why, cost_usd = decide_gpt(sb, sb["model"], price_for(sb["model"]), eng["model"])
                    gpt_note = f"\n<i>({eng['name']} · ${cost_usd:.5f})</i>"
                except Exception as ex:
                    log("gpt error", uid, type(ex).__name__, ex)
                    tg.send(uid, f"⚠️ GPT не ответил ({e(type(ex).__name__)}{': нет ключа OPENAI_API_KEY' if isinstance(ex, KeyError) else ''}), "
                                 "отвечают правила.")
                    reply, photos, esc, why = decide(sb, sb["model"], price_for(sb["model"]))
            else:
                reply, photos, esc, why = decide(sb, sb["model"], price_for(sb["model"]))
            mark_done(sb)
            if esc:
                sb["escalated"] = why
                sb["msgs"].append({"t": now, "who": "bot", "text": f"→ передано менеджеру ({why_ru(why)})", "sent": True})
                tg.send(uid, "🧑‍💼 <i>Бот передал этот чат менеджеру и больше в нём не отвечает. «♻️ Сброс» — начать заново.</i>")
                notify_handoff([int(uid)], {"title": "тестовый чат", "buyer": ""}, None, block, why, real=False)
            elif reply or photos:
                sb["msgs"].append({"t": now, "who": "bot", "text": reply or "", "photos": photos, "sent": True})
                sb["bot_t"], sb["pinged"] = now, False
                if reply:
                    tg.send(uid, f"🤖 {e(reply)}{gpt_note}")
                if photos:
                    tg.send_photos(uid, photos)
            else:
                mark_done(sb)
                tg.send(uid, "<i>(бот молчит: отвечать нечего)</i>")
                sb["bot_t"], sb["pinged"] = None, True
            if not esc and has_avito_order(sb) and not order_noted(sb):
                size = size_now(sandbox_conv(sb), block, reply)
                if size:
                    sandbox_confirm(uid, sb, size, now)
            save(STATE, state)
        elif not block and order_only(sb) and now - order_only(sb) >= (0 if instant(uid) else REPLY_DELAY):
            sb["order_hello"] = True  # ordered without writing anything: greet and ask height/weight, as in a live chat
            if sb["model"] and not products_module.in_stock(sb["model"]):
                sb["escalated"] = "заказ товара, которого нет в наличии"
                sb["msgs"].append({"t": now, "who": "bot", "text": "→ передано менеджеру (заказ товара, которого нет в наличии)", "sent": False})
                tg.send(uid, "🧑‍💼 <i>Товара нет в наличии: бот отдал заказ менеджеру и ничего не написал. «🔄 Сброс» — начать заново.</i>")
                notify_handoff([int(uid)], {"title": "тестовый чат", "buyer": ""}, None, [avito_order_msg(sb)],
                               "заказ товара, которого нет в наличии", real=False)
            else:
                text = order_hello(now)
                sb["msgs"].append({"t": now, "who": "bot", "text": text, "sent": True})
                sb["bot_t"], sb["pinged"] = now, True
                tg.send(uid, f"🤖 {e(text)}\n<i>(размера ещё нет: менеджеру ничего не уходит, пока его не назовут)</i>")
            save(STATE, state)
        elif not block and not sb.get("escalated") and order_silent(sb, now, 60 if instant(uid) else ORDER_SILENT_AFTER):
            sb["order_silent_noted"] = True  # «⚡ мгновенно» on: 1 min instead of 30, to test it quickly
            tg.send(uid, f"🛒 <b>Новый заказ № {sb.get('order_no', '')}</b> (тест: так выглядит уведомление менеджеру, ему ничего не ушло)\n"
                         f"<b>{e(sandbox_conv(sb)['title'])}</b>\n⏰ {e(ORDER_SILENT_NOTE)}"
                         + ("\n<i>(в тесте с «⚡ Отвечает сразу» — через 1 минуту)</i>" if instant(uid) else ""))
            save(STATE, state)
        elif not block and sb.get("bot_t") and not sb.get("pinged") and now - sb["bot_t"] >= PING_AFTER and not has_order(sb) \
                and not any(brain_module.QUESTIONS_LEFT.search(m.get("text") or "") for m in sb["msgs"] if m["who"] == "bot"):
            sb["msgs"].append({"t": now, "who": "bot", "text": PING_TEXT, "sent": True})
            sb["pinged"] = True
            tg.send(uid, f"🤖 {e(PING_TEXT)}\n<i>(напоминание через 15 мин тишины)</i>")
            save(STATE, state)


def price_for(model):
    return None  # the sandbox has no listing card; price questions go to the manager


ORDER_BTN = "🛒 Заказ оформлен"
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def sandbox_conv(sb):
    title = next((p["name"] for p in PRODUCTS if p["key"] == sb["model"]), "тестовый чат")
    return {"title": title, "price": "", "msgs": sb["msgs"]}


def sandbox_order_note(uid, sb, size, tail):
    """Test chat: the 🛒 note the manager gets in a live chat, plus the test order number; sent only to the tester."""
    tg.send(uid, f"🛒 <b>Новый заказ № {sb.get('order_no', '')}</b> (тест: так выглядит уведомление менеджеру, ему ничего не ушло)\n"
                 f"<b>{e(sandbox_conv(sb)['title'])}</b>\n📏 <b>Размер: {e(size)}</b>\n{tail}")


def sandbox_confirm(uid, sb, size, now):
    """Test chat: order + known size -> the bot thanks and names the size, the tester sees the manager's 🛒 note."""
    sb["order_size"], sb["order_noted"], sb["pinged"] = size, True, True
    text = order_confirm_text(size)
    sb["msgs"].append({"t": now, "who": "bot", "text": text, "sent": True})
    sb["bot_t"] = now
    tg.send(uid, f"🤖 {e(text)}")
    sandbox_order_note(uid, sb, size, "Бот продолжает вести чат и отвечает на вопросы покупателя; отправка за менеджером.")


def sandbox_order(uid, sb):
    """Test chat: the «🛒 Заказ оформлен» button adds Avito's «У вас новый заказ» (+ a random test order number), and the
    bot reacts as in a live chat: size known -> «Спасибо за заказ, отправлю вам N размер» + the manager's 🛒 note (shown
    only to this tester); no size yet -> it asks height/weight and confirms once the size is named; no talk at all ->
    tick_sandbox greets and asks height/weight (order_only)."""
    now = int(time.time())
    by = time.gmtime(now + 3 * 3600 + 2 * 86400)  # Avito gives ~2 days to ship
    text = (f"[Системное сообщение] У вас новый заказ. Отправьте его через пункт Авито до {by.tm_mday} {MONTHS_RU[by.tm_mon - 1]} "
            "включительно.\n\nДеньги под защитой Авито: сможете вывести их, когда покупатель заберет товар.\n\nНа страницу заказа.")
    msg = {"t": now, "who": "system", "text": text}
    sb["msgs"].append(msg)
    sb["order_no"] = str(random.randint(10**9, 10**10 - 1))  # test only: live Avito chats carry no order number
    tg.send(uid, f"⚙️ <i>{e(text)}</i>\n<i>(тест: номер заказа {sb['order_no']})</i>")
    sb["pinged"] = True
    if talked_before(sb, msg):
        if order_noted(sb):
            tg.send(uid, "<i>(заказ в этом чате уже был: второго уведомления нет)</i>")
        else:
            size = ordered_size(sandbox_conv(sb))
            if sb.get("escalated"):
                tg.send(uid, "<i>(чат уже у менеджера: бот не пишет" + (f"; менеджер получил бы 🛒 с размером {e(size)})</i>" if size
                                                                         else "; размера нет, уведомления нет)</i>"))
            elif size:
                sandbox_confirm(uid, sb, size, now)
            elif not sb.get("order_asked"):
                sb["order_asked"] = True
                sb["msgs"].append({"t": now, "who": "bot", "text": ORDER_ASK, "sent": True})
                sb["bot_t"] = now
                tg.send(uid, f"🤖 {e(ORDER_ASK)}\n<i>(размера в переписке нет: менеджеру ничего не уходит, пока его не назовут)</i>")
    save(STATE, state)


# ---------------------------------------------------------------- Telegram side
MENU = {"admin": [["📋 Чаты", "🧪 Тест"], ["ℹ️ Статус", "⏸ Пауза", "▶️ Старт"]], "manager": [["🧪 Тест"]]}
INSTANT_ON, INSTANT_OFF = "⚡ Отвечает сразу", "⏱ Отвечает как клиентам"
# who answers in the test chat (live chats always use the rules); the button shows the current one, a tap moves to the next
ENGINES = {"rules": {"button": "🧩 Отвечают: правила", "model": None, "name": "правила",
                     "info": "🧩 В тестовом чате снова отвечает бот на правилах."},
           "gpt": {"button": "🧠 Отвечает: GPT-4.1 mini", "model": "gpt-4.1-mini", "name": "GPT-4.1 mini",
                   "info": "🧠 В тестовом чате теперь отвечает GPT-4.1 mini (промпт + инструменты: фото, сетка, день отправки)."},
           "gpt-5.4": {"button": "🧠 Отвечает: GPT-5.4", "model": "gpt-5.4", "name": "GPT-5.4",
                       "info": "🧠 В тестовом чате теперь отвечает GPT-5.4 (умнее, примерно в 6 раз дороже 4.1 mini)."}}
ENGINE_BUTTONS = {e_["button"] for e_ in ENGINES.values()} | {"🧠 Отвечает: GPT"}  # + the old label from earlier keyboards


def instant(uid):
    return bool(roles["users"].get(str(uid), {}).get("instant"))


def test_menu(uid):
    return [["♻️ Сброс", "👖 Товар"], [INSTANT_ON if instant(uid) else INSTANT_OFF],
            [ENGINES[engine(uid)]["button"]], [ORDER_BTN], ["⬅️ Выйти из теста"]]


def engine(uid):
    """Who answers in this user's test chat: a key of ENGINES («rules» = brain.py, the others = gpt/gpt_reply.py)."""
    e_ = roles["users"].get(str(uid), {}).get("engine", "rules")
    return e_ if e_ in ENGINES else "rules"


FEED_ON, FEED_OFF = "🔔 Чаты менеджера: вкл", "🔕 Чаты менеджера: выкл"


def keyboard(uid):
    u = roles["users"][str(uid)]
    if u.get("mode") == "test":
        return test_menu(uid)
    if u["role"] == "admin":
        return MENU["admin"] + [[FEED_ON if u.get("mgr_feed", True) else FEED_OFF]]
    return MENU[u["role"]]


def product_buttons():
    return [[(p["name"], f"prod:{p['key']}")] for p in PRODUCTS] + [[("Без товара", "prod:")]]


def chats_page(page, flt="all"):
    """flt: "all" (every recorded chat) or "bot" (only chats the bot currently leads, is_bot_chat())."""
    all_items = sorted(state["chats"].items(), key=lambda kv: -(kv[1]["msgs"][-1]["t"] if kv[1]["msgs"] else kv[1]["created"] or 0))
    items = [kv for kv in all_items if is_bot_chat(kv[1])] if flt == "bot" else all_items
    per = 8
    rows = []
    for cid, c in items[page * per:(page + 1) * per]:
        t = c["msgs"][-1]["t"] if c["msgs"] else c["created"] or 0
        flags = ("🎲" if c["assigned"] else "") + ("🧑‍💼" if c.get("escalated") else "")
        rows.append([(f"{msk(t)} · {c['buyer'][:12]} · {c['title'][:18]} {flags}".strip(), f"chat:{cid}")])
    nav = []
    if page > 0:
        nav.append(("◀️", f"page:{flt}:{page - 1}"))
    if (page + 1) * per < len(items):
        nav.append(("▶️", f"page:{flt}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("Все чаты" if flt == "bot" else "🤖 Только чаты бота", f"page:{'all' if flt == 'bot' else 'bot'}:0")])
    return rows, len(items)


def transcript(cid):
    c = state["chats"].get(cid)
    if not c:
        return "Чат не найден."
    icon = {"buyer": "👤", "shop": "🏪", "system": "⚙️", "bot": "🤖"}
    lines = [f"<b>{e(c['title'])}</b> · {e(c.get('price') or '')}", f"Покупатель: {e(c['buyer'])} · начат {msk(c['created'] or 0)}"
             + (" · 🎲 чат бота" if c["assigned"] else "") + (f"\n🧑‍💼 Передан менеджеру: {e(c['escalated'])}" if c.get("escalated") else ""), ""]
    for m in c["msgs"]:
        note = " <i>(тест, не отправлено)</i>" if m["who"] == "bot" and not m.get("sent") else ""
        ph = f" [+{len(m['photos'])} фото]" if m.get("photos") else ""
        lines.append(f"{msk(m['t'])} {icon.get(m['who'], '•')} {e(m['text'])}{ph}{note}")
    lines.append(f"\n<a href=\"https://www.avito.ru/profile/messenger/channel/{cid}\">Открыть на Авито</a>")
    return "\n".join(lines)


def on_message(msg):
    frm, text = msg.get("from") or {}, (msg.get("text") or "").strip()
    uid = frm.get("id")
    u = roles["users"].get(str(uid))
    if uid in ADMIN_IDS and u is not None and not u.get("name"):  # fill in the fixed admin's name on his first message
        u.update(username=frm.get("username"), name=frm.get("first_name"))
        save(ROLES, roles)
    if u is None:
        tg.send(uid, NO_ACCESS)
        if str(uid) not in roles["pending"]:
            roles["pending"][str(uid)] = {"username": frm.get("username"), "name": frm.get("first_name"), "first_text": text[:100]}
            save(ROLES, roles)
            who = f"{e(frm.get('first_name') or '')} @{e(frm.get('username') or '—')} (id {uid})"
            for a in ids_with("admin"):
                tg.send(a, f"👋 Боту написал {who}: «{e(text[:100])}». Какую роль дать?",
                        buttons=[[("Менеджер", f"role:{uid}:manager"), ("Админ", f"role:{uid}:admin"), ("Нет", f"role:{uid}:none")]])
        return
    if text in ("/start", "/menu"):
        tg.send(uid, f"Здравствуйте, {e(u.get('name') or '')}! Ваша роль: <b>{'админ' if u['role'] == 'admin' else 'менеджер'}</b>.",
                keyboard=keyboard(uid))
        return
    if text == "🧪 Тест":
        u["mode"] = "test"; save(ROLES, roles)
        tg.send(uid, "🧪 <b>Режим теста.</b> Пишите как покупатель на Авито, бот ответит так же, как ответил бы клиенту ("
                     + ("сразу" if instant(uid) else f"как клиентам: {REPLY_DELAY} с на первое сообщение, {FAST_DELAY} с, пока переписка идёт")
                     + "). Ничего никуда не отправляется. Кнопка «⚡/⏱» переключает задержку.", keyboard=keyboard(uid))
        tg.send(uid, "Какой товар открыт у «покупателя»?", buttons=product_buttons())
        return
    if text == "⬅️ Выйти из теста":
        u["mode"] = "normal"; save(ROLES, roles)
        tg.send(uid, "Вы вышли из режима теста. Тестовый чат сохранён до сброса.", keyboard=keyboard(uid))
        return
    if u.get("mode") == "test":
        if text == "♻️ Сброс":
            model = sandbox(uid)["model"]
            state["sandbox"][str(uid)] = {"model": model, "msgs": [], "answered_upto": 0}
            save(STATE, state)
            tg.send(uid, "♻️ Тестовый чат очищен. Можно писать первое сообщение.", keyboard=keyboard(uid))
            return
        if text in (INSTANT_ON, INSTANT_OFF):
            u["instant"] = not u.get("instant"); save(ROLES, roles)
            tg.send(uid, "⚡ Бот отвечает сразу после каждого сообщения." if u["instant"]
                    else f"⏱ Бот снова отвечает как клиентам: {REPLY_DELAY} с на первое сообщение и после паузы больше 5 мин, "
                         f"{FAST_DELAY} с, пока переписка идёт.",
                    keyboard=keyboard(uid))
            return
        if text in ENGINE_BUTTONS:
            keys = list(ENGINES)
            u["engine"] = keys[(keys.index(engine(uid)) + 1) % len(keys)]; save(ROLES, roles)
            tg.send(uid, ENGINES[u["engine"]]["info"], keyboard=keyboard(uid))
            return
        if text == "👖 Товар":
            tg.send(uid, "Какой товар открыт у «покупателя»?", buttons=product_buttons())
            return
        sb = sandbox(uid)
        if text == ORDER_BTN:
            sandbox_order(uid, sb)
            return
        if sb.get("escalated"):
            tg.send(uid, "<i>Этот тестовый чат уже у менеджера, бот в нём не отвечает. Нажмите «♻️ Сброс».</i>")
            return
        if msg.get("voice") or msg.get("audio") or msg.get("video_note"):
            text = ("[голосовое] " + (text or "")).strip()  # a voice note in the test chat behaves like one on Avito
        elif msg.get("photo"):
            text = ("[фото] " + (msg.get("caption") or text or "")).strip()  # and a photo too
        if text:
            sb["msgs"].append({"t": int(time.time()), "who": "buyer", "text": text})
            save(STATE, state)
        return
    if u["role"] == "admin" and text == "📋 Чаты":
        rows, n = chats_page(0, "all")
        tg.send(uid, f"Записанные чаты: {n}" + ("" if n else " (новых диалогов пока не было)"), buttons=rows or None)
        return
    if u["role"] == "admin" and text in (FEED_ON, FEED_OFF):
        u["mgr_feed"] = not u.get("mgr_feed", True); save(ROLES, roles)
        tg.send(uid, ("🔔 Включено: будут приходить все сообщения из чатов, которые ведёт менеджер (клиент, менеджер, системные)."
                      if u["mgr_feed"] else "🔕 Выключено: из чатов менеджера ничего приходить не будет. Чаты бота приходят как раньше."),
                keyboard=keyboard(uid))
        return
    if u["role"] == "admin" and text in ("⏸ Пауза", "▶️ Старт"):
        state["live_on"] = text == "▶️ Старт"; save(STATE, state)
        log("live ON" if state["live_on"] else "live OFF", "by", uid)
        tg.send(uid, "▶️ Бот включён: отвечает во всех новых чатах, пока в них не напишет менеджер." if state["live_on"]
                else "⏸ Бот выключен: клиентам ничего не пишет (чаты продолжают записываться).")
        return
    if u["role"] == "admin" and text == "ℹ️ Статус":
        cs = state["chats"].values()
        mode = ("включён: отвечает во всех новых чатах" if state.get("live_on") and not DRY_RUN
                else "выключен: клиентам ничего не отправляется (включить — «▶️ Старт»)")
        tg.send(uid, f"Работает с (первый запуск): {msk(state['started'])}\nПоследний перезапуск: {msk(state.get('last_restart') or state['started'])}\n"
                     f"Режим: <b>{mode}</b>\n"
                     f"Новых чатов: {len(state['chats'])} · чатов бота: {sum(c['assigned'] for c in cs)} · у менеджера: {sum(bool(c.get('escalated')) for c in cs)}\n"
                     f"Менеджеры: {', '.join(roles['users'][str(i)].get('name') or str(i) for i in ids_with('manager')) or 'нет'}")
        return
    tg.send(uid, "Выберите действие в меню.", keyboard=keyboard(uid))


def on_callback(cb):
    uid = cb["from"]["id"]
    data = cb.get("data", "")
    u = roles["users"].get(str(uid))
    try:
        tg.call("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    if u is None:
        return
    if data.startswith("prod:"):
        key = data[5:] or None
        sandbox(uid)["model"] = key
        save(STATE, state)
        name = next((p["name"] for p in PRODUCTS if p["key"] == key), "без товара")
        tg.send(uid, f"Товар: <b>{e(name)}</b>. Пишите как покупатель.")
    elif data.startswith("chat:") and u["role"] == "admin":
        cid = data[5:]
        c = state["chats"].get(cid)
        btn = [[("🤖 Отдать боту", f"give:{cid}")]] if c and not is_bot_chat(c) else None
        tg.send(uid, transcript(cid), buttons=btn)
    elif data.startswith("give:") and u["role"] == "admin":
        cid = data[5:]
        c = state["chats"].get(cid)
        if c is None:
            tg.send(uid, "Чат не найден.")
        else:
            c["assigned"], c["human"] = True, False
            c.pop("escalated", None)
            save(STATE, state)
            log("chat given to bot by admin", cid, uid)
            tg.send(uid, f"🤖 Чат отдан боту: <b>{e(c['title'][:50])}</b> · {e(c.get('buyer', ''))}. "
                         "Бот снова отвечает здесь на новые сообщения.")
    elif data.startswith("page:") and u["role"] == "admin":
        flt, page = data[5:].rsplit(":", 1)
        rows, n = chats_page(int(page), flt)
        label = "Чаты бота" if flt == "bot" else "Записанные чаты"
        tg.send(uid, f"{label}: {n}" + ("" if n else " (нет таких чатов)"), buttons=rows)
    elif data.startswith("role:") and u["role"] == "admin":
        _, target, role = data.split(":")
        p = roles["pending"].pop(target, {})
        if int(target) in ADMIN_IDS:
            pass  # the fixed admin's role never changes
        elif role in ("manager", "admin"):
            roles["users"][target] = {"role": role, "username": p.get("username"), "name": p.get("name"), "mode": "normal"}
            tg.send(int(target), f"Вам выдана роль: <b>{'менеджер' if role == 'manager' else 'админ'}</b>.", keyboard=MENU[role])
            tg.send(uid, f"Готово: {e(p.get('name') or target)} теперь {'менеджер' if role == 'manager' else 'админ'}.")
        else:
            roles.setdefault("blocked", {})[target] = p
            tg.send(uid, "Ок, доступа не будет.")
        save(ROLES, roles)


def tg_poll():
    ups = tg.call("getUpdates", http_timeout=15, offset=state["tg_offset"], timeout=3)
    for up in ups:
        state["tg_offset"] = up["update_id"] + 1
        try:
            if "message" in up:
                on_message(up["message"])
            elif "callback_query" in up:
                on_callback(up["callback_query"])
        except Exception:
            log("tg handler error", traceback.format_exc())
    if ups:
        save(STATE, state)


_control = {"seen": load(CONTROL, {}).get("id"), "status_t": 0}  # a command left from before this start is never replayed


spend = load(SPEND, {"usd": 0.0, "calls": 0, "since": int(time.time()), "reset": None})


def apply_spend_reset():
    r = load(GPT_RESET, {})
    if r.get("id") and r["id"] != spend.get("reset"):
        spend.update(usd=0.0, calls=0, since=r.get("t") or int(time.time()), reset=r["id"])
        save(SPEND, spend)
        log("gpt spend counter reset (new OpenAI key)")


def add_spend(usd):
    apply_spend_reset()
    spend["usd"] += usd
    spend["calls"] += 1
    save(SPEND, spend)


def app_control(now):
    """Start / pause from the manager app (anyone who has the app, same effect as «▶️ Старт» / «⏸ Пауза»)."""
    c = load(CONTROL, {})
    if c.get("id") and c["id"] != _control["seen"]:
        _control["seen"] = c["id"]
        if c.get("action") in ("start", "pause"):
            on = c["action"] == "start"
            state["live_on"] = on; save(STATE, state)
            log("live ON" if on else "live OFF", "by manager app")
            for a in ids_with("admin"):
                try:
                    tg.send(a, ("▶️ Бот включён из приложения менеджера: отвечает во всех новых чатах." if on
                                else "⏸ Бот выключен из приложения менеджера: клиентам ничего не пишет."), keyboard=keyboard(a))
                except Exception:
                    pass
        _control["status_t"] = 0  # report the new state right away
    if now - _control["status_t"] >= 3:
        _control["status_t"] = now
        save(STATUS, {"t": now, "live_on": bool(state.get("live_on")), "dry_run": DRY_RUN,
                      "last_restart": state.get("last_restart"), "seen": _control["seen"]})


def main():
    global token
    PID.write_text(str(os.getpid()), encoding="utf-8")
    if not SPEND.exists():
        save(SPEND, spend)
    token = avito.Token()
    log(f"manager started | dry_run={DRY_RUN} | live_on={bool(state.get('live_on'))}")
    for a in ids_with("admin"):
        try:
            tg.send(a, "Бот перезапущен. " + ("Он включён и отвечает во всех новых чатах." if state.get("live_on")
                       else "Он выключен: клиентам ничего не пишет, пока не нажмёте «▶️ Старт»."), keyboard=keyboard(a))
        except Exception:
            pass
    last_avito = last_clock = 0
    check_clock()  # know the drift (if any) from the first tick, not just after 10 minutes
    tray_parent = os.getppid() if os.environ.get("AVITO_TRAY") == "1" else None  # started by tray.py on a Mac
    while True:
        if tray_parent and os.getppid() != tray_parent:  # the tray icon is gone: no bot without its icon
            log("tray icon closed: stopping"); return
        try:
            tg_poll()
        except Exception as ex:
            log("tg poll error", type(ex).__name__, ex); time.sleep(3)
        now = int(time.time())
        try:
            app_control(now)
            apply_spend_reset()
        except Exception as ex:
            log("app control error", type(ex).__name__, ex)
        try:
            if products_module.refresh():
                log("products reloaded (manager app edit)", len(PRODUCTS))
        except Exception as ex:
            log("products reload error", type(ex).__name__, ex)
        if now - last_avito >= AVITO_POLL:
            last_avito = now
            try:
                avito_poll()
            except Exception as ex:
                log("avito poll error", type(ex).__name__, ex)
        if now - last_clock >= CLOCK_CHECK_EVERY:
            last_clock = now
            check_clock()
        try:
            tick_real(now)
            tick_sandbox(now)
        except Exception:
            log("tick error", traceback.format_exc())


if __name__ == "__main__":
    main()
