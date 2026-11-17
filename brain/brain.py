"""Reply brain (no LLM):
1. TF-IDF + logistic regression -> question type (13 types)
2. regex -> height, weight, half-waist (handles "178,70", "150/41", "рост 186 вес 76")
3. code -> size from sizes.json (YOUR tables; empty until you send them)
4. reply:
   - height/weight missing -> Maxim's own wording asking for them
   - sizing with all numbers but no table row -> escalate
   - other questions -> Maxim's similar short, generic reply (no digits, no prices, no names)
If nothing safe is found, the brain escalates to Maxim."""

import json
import re
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import hstack
from fit import TABLES, compare, client_measures, size_by_measures
from products import PRODUCTS, BY_KEY, product_for_item, in_stock, out_sizes, hw_size, next_photo
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import NearestNeighbors

HERE = Path(__file__).parent
DATA = HERE.parent / "data" / "train"
SIZES = HERE.parent / "sizes.json"

SIZE_WORD = re.compile(r"(?<![a-zа-я0-9])(xxl|xxs|2xs|2xl|xs|xl|xxxl|хс|хл|хм|[sml]|[сми]|\d{2}\s?[lsml]?)(?![a-zа-я0-9])", re.I)
SIZING_TYPES = {"Size advice (height/weight → size)", "Measurements / size chart"}
ASK_BODY = "Напишите свой рост и вес, я вам помогу подобрать размер"


def extract(text):
    """Height (cm), weight (kg), half-waist (cm). None for what is missing."""
    t = text.lower()
    out = {"height": None, "weight": None, "half_waist": None}
    # «рост/вес 165/60», «рост и вес: 180 75», «вес/рост 60/165»: both labels first, then both numbers
    pair = r"\s*(?:[,./\\-]|и)?\s*"
    m = re.search(r"рост" + pair + r"вес\D{0,6}(1[4-9]\d|2[01]\d)\s*(?:[,./\\\s]|на)\s*(\d{2,3})(?!\d)", t)
    r = re.search(r"вес" + pair + r"рост\D{0,6}(\d{2,3})\s*(?:[,./\\\s]|на)\s*(1[4-9]\d|2[01]\d)(?!\d)", t)
    if m or r:
        out["height"], out["weight"] = (int(m.group(1)), int(m.group(2))) if m else (int(r.group(2)), int(r.group(1)))
        w = re.search(r"(полуобхват|полу ?талия|пол талии|обхват талии|талия)\D{0,15}(\d{2,3})", t)
        out["half_waist"] = int(w.group(2)) if w else None
        return out
    m = re.search(r"рост\D{0,6}(\d{3})", t) or re.search(r"(\d{3})\s*(?:см|рост)", t) or re.search(r"(?<!\d)(1[4-9]\d|2[01]\d)\s+на\s+\d{2,3}", t)
    if m:
        out["height"] = int(m.group(1))
    m = re.search(r"вес\D{0,6}(\d{2,3})", t) or re.search(r"(\d{2,3})\s*(?:кг|вес)", t)
    if m:
        out["weight"] = int(m.group(1))
    if out["height"] is None or out["weight"] is None:  # "178,70", "185/75", "150/41", "185 75"
        m = re.search(r"(?<!\d)(1[4-9]\d|2[01]\d)\s*(?:[,./\s-]|на)\s*(\d{2,3})(?!\d)", t)
        if m and abs(int(m.group(2)) - int(m.group(1))) <= 10:  # «182/183», «182-183»: two heights (not sure), no weight
            out["height"] = out["height"] or int(m.group(1))
        elif m:
            out["height"] = out["height"] or int(m.group(1))
            out["weight"] = out["weight"] or int(m.group(2))
    if out["height"] is None:  # a lone 140-219 number is the height ("170 вес 55")
        cands = [int(x) for x in re.findall(r"(?<!\d)(1[4-9]\d|2[01]\d)(?!\d)", t)]
        if cands:
            out["height"] = cands[0]
    m = re.search(r"(полуобхват|полу ?талия|пол талии|обхват талии|талия)\D{0,15}(\d{2,3})", t)
    if m:
        out["half_waist"] = int(m.group(2))
    return out


# Spelling fixes for words Maxim writes wrong (the chats teach the typo, so fix it on output)
FIXES = {"будите": "будете", "Будите": "Будете"}


def fix_spelling(reply):
    for wrong, right in FIXES.items():
        reply = re.sub(rf"(?<![а-яёa-z]){wrong}(?![а-яёa-z])", right, reply)
    return reply


# buyer says the order is already placed: never answer with "how to order" wording
ALREADY_ORDERED = re.compile(r"\b(заказал|заказала|заказали|оформил|оформила|оформили|оплатил|оплатила|оплатили)\b", re.I)
ORDER_TALK = re.compile(r"заказ|оформ|\?", re.I)
STATUS_TYPE = "Order status (sent yet? when?)"


WIDE = "широкие трубы прямые zara rick owens"
FLARED = "клеш lgb flared bootcut zara"


SIZE_LABEL = {25: "25 (2XS)", 26: "26 (XS)", 27: "27 (XS)", 28: "28 (S)", 29: "29 (S)", 30: "30 (M)",
              31: "31 (M)", 32: "32 (M)", 33: "33 (L)", 34: "34 (L)", 36: "36 (XL)"}
SIZE_TREE = HERE.parent / "data" / "size_tree.joblib"
OVERSIZE = re.compile(r"на вырост|навырост|с запасом|овер ?[сз]ай[зс]|oversi[sz]e|over ?size|на размер больше|размером больше|посвободнее|свободн\w* посадк|мешковат", re.I)


def wants_oversize(text, context=""):
    """Buyer wants an oversize fit (in this message or earlier buyer lines)."""
    buyer = [l[len("buyer: "):] for l in (context or "").split("\n") if l.startswith("buyer: ")]
    return any(OVERSIZE.search(t) for t in [text] + buyer)


def size_up(size):
    """One size bigger for an oversize fit: «30 (M)» -> «31 (M)», 34 -> 36. None if already the biggest."""
    m = re.match(r"\s*(\d{2})", str(size))
    if not m:
        return None
    order = sorted(SIZE_LABEL)
    n = int(m.group(1))
    bigger = [x for x in order if x > n]
    return SIZE_LABEL[bigger[0]] if bigger else None


def size_reply(size, text, context, prefix="Вам подойдёт"):
    """Size answer; oversize request -> one size bigger (None = no bigger size, Maxim answers)."""
    label = SIZE_LABEL.get(size, size)
    if wants_oversize(text, context):
        up = size_up(label)
        if not up:
            return None
        grow = re.search(r"на ?вырост|с запасом", text + "\n" + (context or ""), re.I)  # use the buyer's own words
        return f"На вырост подойдёт {up} размер." if grow else f"Для оверсайз посадки подойдёт {up} размер."
    return f"{prefix} {label} размер."


def model_for_item(title):
    """Model key for a listing title, from products/<id>/product.txt match words (None if unknown)."""
    p = product_for_item(title)
    return p["key"] if p else None


GREETING = re.compile(r"^\s*(здравствуй|добрый\s+(день|вечер|утро)|здаров)", re.I)


def polish(out):
    """Final check on every reply: formal address, no 'привет', and never send informal wording."""
    if out.get("reply"):
        out["reply"] = formal(out["reply"])
        if not is_formal(out["reply"]):
            return {**out, "reply": None, "escalate": True, "why": "informal wording: Maxim answers"}
    return out


def add_greeting(out, context):
    """First message in a chat: greet first. Never duplicates a greeting that is already there."""
    talked = any(l.startswith(("buyer: ", "shop: ")) for l in (context or "").split("\n"))
    if talked or not out.get("reply"):  # only Avito system lines so far (e.g. «новый заказ») still means the first message
        return out
    if not GREETING.match(out["reply"]):
        out["reply"] = "Здравствуйте! " + out["reply"]
    return out


def merged_nums(text, context=""):
    """Height/weight from this message; anything missing is taken from the buyer's earlier messages (newest first)."""
    nums = extract(text)
    if nums["height"] and nums["weight"]:
        return nums
    lines = (context or "").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.startswith("buyer: "):
            continue
        # «30» right after «А вес какой?» is the weight, also when it is an older message in the chat
        old = extract(label_bare_number(line[len("buyer: "):], "\n".join(lines[:i])))
        if nums["height"] is None and old["height"]:
            nums["height"] = old["height"]
        if nums["weight"] is None and old["weight"]:
            nums["weight"] = old["weight"]
        if nums["height"] and nums["weight"]:
            break
    return nums


# Buyer has decided and is ordering / has ordered -> Maxim completes the order (bot never confirms it)
DECIDED = re.compile(r"\b(заказал|заказала|заказали|заказываю|закажу|закажем|беру|возьму|возьмём|оформил|оформила|"
                     r"оформлю|оплатил|оплатила|оплатили|оплачу|заказать хочу|хочу заказать|буду заказывать|давайте заказ|"
                     r"определил|определилась|определился|решил|решила|решили|покупаю|куплю|выкупаю|выкуплю|давайте попробуем|"
                     r"давайте (тогда )?(размер )?(\d\d|2?xx?s|xs|s|m|l|xl|2xl|xxl|хс|м|л|хл)(?![\w]))", re.I)
# Shipping / sending talk: the bot never promises or confirms it
SHIPPING = re.compile(r"отправ|сегодня|завтра|посылк|заказ|доставк|сдэк|сдек|5 ?post|почт|пвз|оплат|отзыв", re.I)


# Payment went through: the order is ready to ship -> class "shipping", Maxim ships it
PAID = re.compile(r"\b(оплатил|оплатила|оплатили|оплата прошла|оплачено|заказ оплачен|оплата прошла|перевел|перевела|перевод сделал|перевод отправил)\b", re.I)
SHIP_TYPE = "Shipping (paid, ready to send)"


# Questions about sending or arrival: only Maxim knows the real status
ASKS_SENDING = re.compile(r"отправ\w*|когда (придет|придёт|приедет|будет|пришл)|сегодня (отправ|будет)|завтра (отправ|будет)", re.I)


# «За сколько придёт?», «сколько идёт доставка?», «какие сроки доставки?» (before any order): Avito shows it at checkout
DELIVERY_TIME = re.compile(r"за сколько (\w+ )?(дн|придут|придет|придёт|дойд|доход|приход|доставя|доставит|привезут)|"
                           r"сколько (\w+ )?(ид[уё]т|ехать|едут|везут|дней|по времени)|срок\w* (доставк|прихода)|"
                           r"как (долго|быстро) (\w+ )?(придут|придет|придёт|доставка|идут|ид[её]т|доставят)|долго (идти|ид[ёе]т|ехать)", re.I)
DELIVERY_TIME_REPLY = "При оформлении покажет срок доставки."


def label_bare_number(text, context):
    """A bare number right after the shop asked «А вес какой?» / «А рост какой?» is that value: «60» -> «вес 60»."""
    m = re.fullmatch(r"\s*(?:где-?то|примерно|около|~)?\s*(\d{2,3}(?:[.,]\d)?)\s*(кг|см)?\s*[.!]?\s*", text or "", re.I)
    shop = [l[len("shop: "):] for l in (context or "").split("\n") if l.startswith("shop: ")]
    if not m or not shop:
        return text
    n = float(m.group(1).replace(",", "."))
    if "вес какой" in shop[-1].lower() and 30 <= n <= 150:
        return f"вес {m.group(1)}"
    if "рост какой" in shop[-1].lower() and 130 <= n <= 215:
        return f"рост {m.group(1)}"
    return text


def half_body_q(own, nums):
    """The buyer gave only one of height/weight in this message and the chat doesn't have the other one yet."""
    if own["height"] and not own["weight"] and nums["weight"] is None:
        return "А вес какой?"
    if own["weight"] and not own["height"] and nums["height"] is None:
        return "А рост какой?"
    return None


def asks_delivery_time(text):
    return bool(DELIVERY_TIME.search(text or ""))


# «Когда отправите?», «когда доставка?», «сможете сегодня отправить?»: the answer depends on the time in Moscow.
# Past tense («отправили?», «уже отправлено?») is a status question -> Maxim.
SEND_WHEN = re.compile(r"когда (\w+ )?(отправ(ите|ишь|ят|им|ка|ку|ки|ить|ляете|ляешь)|доставк|доставите|будет доставк|будет отправк)|"
                       r"отправ(ите|ишь|ят|ить|ляете)\b[^?]*\b(сегодня|завтра|сейчас)|(сегодня|завтра|сейчас)\b[^?]*\bотправ(ите|ишь|ят|ить|ляете)\b|"
                       r"во сколько (\w+ )?отправ|в какой день (\w+ )?отправ", re.I)
SENT_PAST = re.compile(r"отправил|отправлен|отправили|уже отправ", re.I)
WEEKDAY_CUTOFF, WEEKEND_CUTOFF = (19, 30), (18, 30)  # Moscow time: after this, it ships tomorrow (defaults)
SHOP_SETTINGS = HERE.parent / "shop_settings.json"  # the manager app edits the cutoffs here; re-read when it changes
_cutoff_cache = {"mtime": None, "value": (WEEKDAY_CUTOFF, WEEKEND_CUTOFF)}


def _hhmm(v, default):
    try:
        h, m = (int(x) for x in str(v).split(":"))
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else default
    except (ValueError, TypeError):
        return default


def cutoffs():
    """(weekday, weekend) cutoff as (hour, minute), from shop_settings.json if present, else the defaults above."""
    try:
        mtime = SHOP_SETTINGS.stat().st_mtime_ns
    except OSError:
        return WEEKDAY_CUTOFF, WEEKEND_CUTOFF
    if mtime != _cutoff_cache["mtime"]:
        try:
            d = json.loads(SHOP_SETTINGS.read_text(encoding="utf-8"))
            _cutoff_cache["value"] = (_hhmm(d.get("weekday_cutoff"), WEEKDAY_CUTOFF), _hhmm(d.get("weekend_cutoff"), WEEKEND_CUTOFF))
            _cutoff_cache["mtime"] = mtime
        except (OSError, ValueError):
            pass  # half-written file: keep the last good value
    return _cutoff_cache["value"]
CLOCK_OFFSET = 0  # seconds added to time.time() when `now` isn't given; manager.py sets this from a trusted time source
                  # (the PC's own clock can drift) so "today"/"tomorrow" stays right even if the system clock is wrong


def ships_today(now=None):
    """Weekdays before 19:30 MSK and weekends before 18:30 MSK (editable in the manager app) -> it ships today, else tomorrow."""
    import time as _t
    msk = _t.gmtime((now if now is not None else _t.time() + CLOCK_OFFSET) + 3 * 3600)
    weekday, weekend = cutoffs()
    h, m = weekend if msk.tm_wday >= 5 else weekday
    return (msk.tm_hour, msk.tm_min) < (h, m)


def send_when_reply(now=None):
    return "Отправим сегодня." if ships_today(now) else "Отправим завтра."


def will_order(text, context):
    """Intent to order but no order yet: no «новый заказ» in the chat and not «заказал / оформил / оплатил»."""
    has_order = any(l.startswith("system: ") and "новый заказ" in l.lower() for l in (context or "").split("\n"))
    return not has_order and not ALREADY_ORDERED.search(text or "") and not is_paid(text)


LATER = re.compile(r"\b(завтра|послезавтра|вечером|вечерком|позже|попозже|потом|через (\w+ )?(час|часик|пару|день|дн|недел)|"
                   r"на выходных|после работы|после учёбы|после учебы|как (получу|приду|придут|будут)|утром)\b", re.I)


def go_ahead_reply(now=None):
    return "Можете заказывать, " + ("сегодня" if ships_today(now) else "завтра") + " отправлю 🤗"


def order_hello(now=None):
    """First message when the buyer ordered without writing anything (Maxim's three messages in one)."""
    return ("Здравствуйте! Напишите свой рост и вес, я вам помогу подобрать размер. "
            + ("Сегодня отправлю 🤗" if ships_today(now) else "Завтра отправлю 🤗"))


# Which delivery service / do you ship to my city / via СДЭК? -> any service the buyer likes (chosen at checkout)
DELIVERY_HOW = re.compile(r"как(ой|ая|ую|ие|ими) (\w+ )?(доставк|служб|почт|способ)|(доставк|служб)\w* (\w+ )?(как|какие|какая|есть)|"
                          r"(сдэк|сдек|cdek|5 ?post|пятёрочк|пятерочк|почт\w* росс|почтой|яндекс|boxberry|боксберри|озон|ozon|авито доставк|"
                          r"пункт\w* выдач|пвз|курьер)|(доставля|отправля)ете (\w+ )?(в|до|по)\b|доставка (в|до|по)\b|"
                          r"(можно|есть|будет) (\w+ )?доставк|доставк\w* (\w+ )?(можно|есть)", re.I)
DELIVERY_HOW_REPLY = "Можно заказать любой удобной доставкой."


def asks_delivery_how(text):
    t = text or ""
    question = "?" in t or re.search(r"\b(можно|как|какой|какая|какую|какие|есть ли|доставляете|отправляете|сможете|получится)\b", t, re.I)
    ordering = re.search(r"оформля|оформлю|заказыва|закажу|заказал|оформил", t, re.I)
    return bool(DELIVERY_HOW.search(t)) and bool(question) and not ordering


FROM_WHERE = re.compile(r"откуда (\w+ )?(отправ|высыла|шл[её]т|ед[её]т|прид[её]т|доставк|посылк|вы\b)|из как\w+ (город|регион)|"
                        r"(вы|магазин) (из|в) как\w+ город|в каком город\w* (вы|наход|магазин)", re.I)
FROM_WHERE_REPLY = "Из Москвы."


def asks_from_where(text):
    return bool(FROM_WHERE.search(text or ""))


def asks_send_when(text):
    return bool(SEND_WHEN.search(text or "")) and not SENT_PAST.search(text or "")


def asks_sending(text):
    return bool(ASKS_SENDING.search(text or "")) and not asks_chart(text)  # «можете сетку отправить?» is a chart request


# Any return / exchange / refusal request: always Maxim
RETURN = re.compile(r"возврат|вернуть|верните|вернул|верну\b|вернутся деньги|вернулись деньги|обмен|обменять|обменяйте|"
                    r"поменять (размер|на)|отказаться|отказ\b|сделать отказ|перезаказ|заберу назад|отправить назад|"
                    r"примерк|примерить|примерка", re.I)
RETURN_TYPE = "Try-on & returns"


def is_return(text):
    return bool(RETURN.search(text or "")) and not asks_try_on(text)


# «Можно примерить?», «примерка есть?» (a question, no return/exchange words): only with Avito Delivery
TRY_ON = re.compile(r"примерк|примерить|примерять|померить|померять|примерочн", re.I)
RETURN_ONLY = re.compile(r"возврат|вернуть|верните|вернул|верну\b|обмен|обменять|поменять|отказ|не подош|не подход|малы|велики|"
                         r"заберу назад|отправить назад|перезаказ", re.I)
TRY_ON_REPLY = "Примерить можно, если заказать Авито Доставкой."


def asks_try_on(text):
    t = text or ""
    if asks_return_after_pickup(t):
        return False
    return bool(TRY_ON.search(t)) and not RETURN_ONLY.search(t) and ("?" in t or re.search(r"\b(можно|есть|будет|получится)\b", t, re.I))


# «Есть ли возврат после того, как заберу из ПВЗ?» (a question about the rules, not a return request): no
RETURN_AFTER_PICKUP = re.compile(r"(возврат|вернуть|верну|обмен)\w*[^.!]*\b(после|если|когда|как)\b[^.!]*\b(забер|забра|получ|выкуп)|"
                                 r"(после|если|когда|как)\b[^.!]*\b(забер|забра|получ|выкуп)\w*[^.!]*(возврат|вернуть|верну|обмен)", re.I)
RETURN_AFTER_PICKUP_REPLY = "Нет."


def asks_return_after_pickup(text):
    t = text or ""
    picked = re.search(r"\b(забрал|забрала|получил|получила|выкупил|выкупила)\b", t, re.I)
    hypothetical = re.search(r"(после того,? как|если|когда)\s+(\w+\s+)?(забрал|получил|выкупил)", t, re.I)
    return bool(RETURN_AFTER_PICKUP.search(t)) and "?" in t and (not picked or bool(hypothetical))


# «А можно будет вернуть, если не подойдёт?», «возврат есть?» (asked before buying): the policy, no handoff
RETURN_POLICY = re.compile(r"(можно|можем|смогу|могу|получится)( ли)?( будет)? (\w+ )?(вернуть|сделать возврат|оформить возврат|обменять)|"
                           r"возврат\w* (\w+ )?(есть|будет|возможен|можно|принимаете|делаете)|(есть|будет)( ли)? (\w+ )?возврат|"
                           r"(а )?если не (подойд|подош|сяд|налез)\w*", re.I)
RETURN_POLICY_REPLY = "Возврат есть, но после того, как забрали из пункта выдачи, вернуть нельзя."


def asks_return_policy(text):
    """A question about the return rules, not a return of something the buyer already has."""
    t = text or ""
    if not RETURN_POLICY.search(t) or asks_return_after_pickup(t):
        return False
    question = "?" in t or re.search(r"\b(можно|есть|будет|если)\b", t, re.I)
    real = re.search(r"\b(хочу|хотим|хотела|хотел|давайте|оформите|оформляю)\b[^.?!]*(верн|возврат|обмен)|"
                     r"\b(не подошли|не подошёл|не подошел|не сели|малы|велики|забрал|забрала|получил|получила|пришли|пришёл|пришел|пришла)\b", t, re.I)
    return bool(question) and not real


# «Ты бот?», «вы робот?», «это автоответчик?», «я с нейросетью общаюсь?»: straight to Maxim, no reply
ASKS_IF_BOT = re.compile(r"\b(бот|ботом|бота|боту|боте|боты|ботов|робот|робота|роботом|роботу|роботе|роботы|роботов|автоответ\w*|нейросет\w*|нейронк\w*|искусственн\w* интеллект\w*|"
                         r"ии|chatgpt|чат ?гпт|гпт|gpt|ai)\b|живой (человек|продавец)|(с )?человеком (общаюсь|говорю|переписываюсь)|"
                         r"вы (живой|человек)|ты (живой|человек)|это (программа|скрипт)", re.I)


def asks_if_bot(text):
    return bool(ASKS_IF_BOT.search(text or ""))


def order_just_created(context):
    """The newest event in the chat is Avito's «У вас новый заказ»: the order exists and must be shipped."""
    lines = [l for l in (context or "").split("\n") if l.strip()]
    return bool(lines) and lines[-1].startswith("system: ") and "новый заказ" in lines[-1].lower()


def order_without_talk(context):
    """The chat so far is only Avito's «новый заказ» (the buyer ordered without writing; nobody has talked yet)."""
    lines = [l for l in (context or "").split("\n") if l.strip()]
    return order_just_created(context) and not any(l.startswith(("buyer: ", "shop: ")) for l in lines)


def is_paid(text):
    return bool(PAID.search(text or ""))


# Short agreement right after an offer to order / a size recommendation = the buyer decided
AGREE = re.compile(r"^\s*(да|ага|угу|хорошо|ок|ладно|понял|понятно|давайте|договорились|супер|отлично|круто|беру|"
                   r"да,?\s*хорошо|ок,?\s*хорошо|хорошо,?\s*давайте)[\s!.,)]*$", re.I)
OFFER = re.compile(r"размер|заказыва|заказать|отправлю|оформ|можете заказ", re.I)


# Buyer postpones ("подумаю", "напишу позже", "вечером закажу"): just say "Хорошо"
POSTPONE = re.compile(r"\b(подумаю|подумать|подумаем|позже|попозже|потом|вечером|на выходных|после зарплаты|"
                      r"когда (получу|придут|будут|появятся) деньги|как (получу|придут|будут)|напишу|посмотрю|"
                      r"посоветуюсь|решу|определюсь|в течени[ие])\b", re.I)
POSTPONE_REPLY = "Хорошо"


def postpones(text):
    """Postponing, but not a past-tense order/payment ('заказал', 'оплатил' are handled elsewhere)."""
    return bool(POSTPONE.search(text or "")) and not ALREADY_ORDERED.search(text or "") and not is_paid(text)


def closes_deal(text, context):
    if not AGREE.match(text or ""):
        return False
    shop = [l[len("shop: "):] for l in (context or "").split("\n") if l.startswith("shop: ")]
    return any(OFFER.search(x) for x in shop[-3:])


def is_decided(text):
    return bool(DECIDED.search(text or ""))


# Formal address only: "вы", never "ты"; "привет" becomes "здравствуйте"
INFORMAL = re.compile(r"\b(ты|тебе|тебя|тобой|тобою|твой|твоя|твоё|твое|твои|бро|братан|братух|брат|чувак|блин|сап|"
                      r"даров|здарова|здаров|привет|чел)\b", re.I)


def formal(text):
    """Replace 'привет' with 'здравствуйте' (keeps capital letter)."""
    if not text:
        return text
    return re.sub(r"\bпривет\b", lambda m: "Здравствуйте" if m.group(0)[0].isupper() else "здравствуйте",
                  text, flags=re.I)


def is_formal(text):
    return not INFORMAL.search(text or "")


# Price: take it from the listing card of THIS chat; discounts and bargaining go to Maxim
PRICE_Q = re.compile(r"сколько (стоит|стоят|будет|будут|это|эт)|цен[аыуе]|почём|почем|стоимост|сколько денег|какая цена", re.I)
DISCOUNT = re.compile(r"скидк|торг|дешевле|дешевл|уступ|со скидкой|дешевле|сбавь|подешевле", re.I)


def is_price_q(text):
    return bool(PRICE_Q.search(text or ""))


NO_DISCOUNT = "К сожалению, нет"


def is_discount(text):
    return bool(DISCOUNT.search(text or ""))


def price_reply(price):
    return f"Стоимость {price}."


# Approved replies, written for this shop. Only these question types get an answer from the bot;
# everything else is handed to Maxim. Replies are formal ("вы"), promise nothing about sending or payment.
TEMPLATES = {
    "Measurements / size chart": "Размерная сетка и замеры указаны в объявлении. Если напишете рост и вес, помогу подобрать размер.",
    "Fit worries (too long/wide/tight)": "Посадка зависит от роста, веса и размера. Напишите, пожалуйста, рост и вес, и я подскажу размер.",
    "In stock? / which sizes exist": "Напишите, пожалуйста, рост и вес, и я подскажу размер, который вам подойдёт.",
    "Material / density / warmth": "Характеристики и состав материала указаны в объявлении.",
    "How to order on Avito": "Заказ оформляется через объявление на Авито: выберите размер и оформите заказ.",
}


# Stock: every size is always in stock (the user's rule) -> «Да, есть.»
_SZ = r"(2?x+[sl]|xs|xl|[sml]\b|хс|хл|эс|эм|[смл]\b|\d\d\b)"
STOCK = re.compile(r"в наличи|есть ли (он|она|они|ещ|размер(?!н))|есть (ли )?(ещ[её] )?" + _SZ
                   + r"|есть (ли )?(ещ[её] )?размер(?!н)\w*\s*(" + _SZ[1:-1] + r"|\?|$|на\b)"
                   r"|\b(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл]|\d\d)( размер\w*)? (ещ[её] )?есть|остал\w* (размер|ли|ещ)"
                   r"|какие (размеры )?(есть|остал)|ещ[её] (есть|продае|актуальн)|актуальн|размеры есть", re.I)
STOCK_REPLY = "Да, есть."
SIZE_HELP = re.compile(r"подскаж|подбер|посовет|какой (мне )?размер|подойд", re.I)


def is_stock(text):
    t = text or ""
    # also «есть 30м?»: «есть» + a size written together with its letter
    return bool(STOCK.search(t)) or bool(re.search(r"\bесть\b|в наличи", t, re.I) and "?" in t and NAMED_SIZE.search(t))


# Pure acknowledgement («Спасибо», «Хорошо, буду ждать», «👍»): nothing to answer, no handoff.
# Checked AFTER the order rules, so «Да, хорошо» right after «Можете заказывать» still goes to Maxim.
ACK_WORDS = set("""спасибо спасибочки спс пасиба благодарю большое огромное вам тоже хорошо ок окей ok okay понял поняла
понятно ясно отлично супер класс круто с кайфом договорились буду будем ждать жду ждём ждем до свидания всего доброго
хорошего дня вечера а и да-да подождите подожди секунду минуту минутку щас сейчас это ещё еще лучше всё все ага угу ладно""".split())
GREETING_ONLY = re.compile(r"^\s*(ещ[её] раз )?(здравствуйте|здравствуй|привет|приветствую|добрый (день|вечер|утро)|доброе утро|"
                           r"здарова|здаров|салам)[\s!.,)]*$", re.I)


def is_ack(text):
    if re.search(r"\d|\?", text or ""):  # numbers or a question are never just «ok»
        return False
    words = re.findall(r"[а-яёa-z]+", (text or "").lower())
    return all(w in ACK_WORDS for w in words) and (bool(words) or bool((text or "").strip()))


# «Можно заказать размер S?», «L могу заказать?», «Можно 28 размер», «32 м»: yes, go ahead (Maxim's wording)
_SZW = r"(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл]|s-m|с-м|m-l|2[5-9]|3[0-6])"
_SZP = _SZW + r"(\s*\(?" + _SZW + r"\)?)?"  # «30», «M», «30 M», «30 (M)»
_PLEASE = r"(\s*,?\s*(пожалуйста|пожалуй|пж|плиз|плз))?"
CAN_ORDER = re.compile(r"(можно|могу|смогу|можем)\b[^?]{0,30}\b(заказ|оформ|взять|брать)"
                       r"|\b(можно|могу|возьму|беру|давайте|хочу)\s+(размер\s+)?(" + _SZP + r"|(2[5-9]|3[0-6])\s*[-(]?\s*(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл])\)?)"
                       r"(\s*размер\w*)?" + _PLEASE + r"\s*\??\s*$", re.I | re.M)
# a statement of the size the buyer picked: «мне 30 M пожалуйста», «давайте 32 (M)», «нужен L», «хочу M размер»
SIZE_CHOICE = re.compile(r"^\s*(\w+[,!]?\s+)?(мне|давайте|давай|хочу|хотел\w* бы|нужен|нужна|нужны|нужно|буду брать|будем брать)\s+"
                         r"((джинсы|размер)\s+)?" + _SZP + r"(\s*размер\w*)?" + _PLEASE + r"\s*[.!)]*\s*$", re.I | re.M)
BARE_SIZE = re.compile(r"^\s*(размер\s+)?(" + _SZW + r"\s*(размер\w*)?|(2[5-9]|3[0-6])\s*[a-zа-я]{1,3})\s*[?.!]*\s*$", re.I)
CAN_ORDER_REPLY = "Да, можете заказывать."
JUST_IN_CASE = "На всякий случай напишите рост и вес."
NAMED_SIZE = re.compile(r"(?<![a-zа-я0-9])(" + _SZW + r")(?![a-zа-я0-9])"
                        r"|(?<![a-zа-я0-9])(2[5-9]|3[0-6])\s*[-(]?\s*(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл])\)?(?![a-zа-я0-9])", re.I)  # «30м», «32l», «30-M»


# «Размер L нужен», «нужен L», «мне нужна M»: the buyer says which size they need
SIZE_NEED = re.compile(r"^\s*(\w+[,!]?\s+)?((мне|нам)\s+)?(нуж(ен|на|ны|но)\s+(размер\s+)?" + _SZP + r"(\s*размер\w*)?"
                       r"|(размер\s+)?" + _SZP + r"(\s*размер\w*)?\s+нуж(ен|на|ны|но))" + _PLEASE + r"\s*[.!)]*\s*$", re.I | re.M)


def names_size(text):
    """The buyer names a concrete size («30 размер», «M», «xs», «s-m»)."""
    return bool(NAMED_SIZE.search(text or ""))


def can_order_q(text):
    """Buyer asks to order / names the size they will take (but not just a number: «31» can be anything)."""
    t = text or ""
    lines = [l for l in t.split("\n") if l.strip()]
    # a bare size line is a choice («32 м», «М»), not a question («XXS?») and not just a number («31»)
    bare = [l for l in lines if BARE_SIZE.match(l) and "?" not in l and re.search(r"[a-zа-я]", l, re.I)]
    # «могу заказать» only as a question or a «можно…» request, not «так то могу заказать, но…»
    asks = "?" in t or re.match(r"\s*(здравствуйте[\s,!.]*)?можно", t, re.I)
    return (bool(CAN_ORDER.search(t)) and bool(asks)) or bool(bare) or bool(SIZE_CHOICE.search(t)) or bool(SIZE_NEED.search(t))


# Personal meeting / pickup: Maxim arranges place and time himself
MEETING_TYPE = "Personal meeting"
MEETING = re.compile(r"встреч|встрет|самовывоз|\bлично\b|подъех|подъед|\bприеду\b|\bприехать|заехать|\bзаеду|"
                     r"забрать сам|сам заберу|могу забрать|где (вы )?находит|\bадрес|\bу метро|\bметро\b", re.I)


def is_meeting(text, context=""):
    """Buyer arranges a personal meeting now, or one was arranged earlier in this chat."""
    return bool(MEETING.search(text)) or any(
        MEETING.search(l.split(": ", 1)[-1]) for l in (context or "").split("\n") if l.startswith(("buyer: ", "shop: ")))


ASK_BODY_TYPES = {"Fit worries (too long/wide/tight)", "In stock? / which sizes exist"}


FIT_WORDS = re.compile(r"свобод|тесн|обтяж|облип|посад|сядут|сидеть|сидят|подойд|длинн|широк|узк|большеват|"
                       r"маловат|по больше|по меньше|оверсайз", re.I)


def size_number(tree, nums):
    """Size number (25..36) from the height/weight tree, or None."""
    if tree is None or not (nums["height"] and nums["weight"]):
        return None
    return int(tree.predict(np.array([[nums["height"], nums["weight"]]]))[0])


def stated_size(context):
    """Size number the shop last named in this chat (e.g. «Вам подойдёт 34 L размер»)."""
    for line in reversed((context or "").split("\n")):
        if line.startswith("shop: ") and "размер" in line:
            m = re.search(r"(?<!\d)(25|26|27|28|29|30|31|32|33|34|36)(?!\d)", line)
            if m:
                return int(m.group(1))
    return None


PHOTO_REPLY = "Вот так сидят:"
CHART = re.compile(r"замер|сетк|(скин|пришл|кин)\w* размеры|размеры (скин|пришл|кин)|таблиц\w* размер|размерн\w* таблиц|(?<!мало)(?<!больше)мерк|параметр\w* (джинс|изделия|штанов|брюк)|какая длин|длина по|длинн?у\b|ширин\w* штанин|полуобхват|обхват\w* (у|какой|пояса у)", re.I)
CHART_REPLY = "Вот размерная сетка. Если напишете рост и вес, помогу подобрать размер."


def charts_for(model):
    """Size chart image(s): the model's one, or all of them when the model is unknown."""
    prods = [BY_KEY[model]] if model in BY_KEY else PRODUCTS
    return [p["chart"] for p in prods if p["chart"]]


def asks_chart(text):
    """Buyer asks for measurements / the size chart (not giving own girths with numbers)."""
    return bool(CHART.search(text)) and not client_measures(text)


def photos_for(model, sent=()):
    """Photos of this model on a person (products/<id>/photos/), ONE at a time: the next one not sent in this chat yet.
    Empty list if the model has none or all of them were already sent."""
    p = next_photo(BY_KEY[model]["photos"], sent) if model in BY_KEY else None
    return [p] if p else []


def all_photos_sent(model, sent=()):
    """The model has photos, and every one of them was already sent in this chat (a new request -> Maxim)."""
    return model in BY_KEY and bool(BY_KEY[model]["photos"]) and not photos_for(model, sent)


LEG_Q = re.compile(r"\bвыход[аеуом]?\b|полувыход|ширин\w* (низа|снизу|внизу|штанин\w*)|низ\w* штанин\w* (какой|какая|сколько)", re.I)


SIZE_NUM = re.compile(r"(?<![\d,.])(25|26|27|28|29|30|31|32|33|34|36)(?![\d,.]|\s*(?:см|кг|cm|kg)\b)", re.I)
SIZE_LETTER = re.compile(r"(?<![a-z])(2xs|xxs|xs|s|m|l|xl)(?![a-z])", re.I)
LEG_COLS = ("выход", "полувыход")


def sizes_in(text):
    """Size numbers the buyer names in this message («у 30», «на 32 размер», «M» -> 30, 31, 32). Height/weight are not sizes."""
    t = re.sub(r"(рост|вес)\D{0,6}\d+", " ", text or "", flags=re.I)
    nums = {int(m.group(1)) for m in SIZE_NUM.finditer(t)}
    if not nums:
        for m in SIZE_LETTER.finditer(t):
            lab = m.group(1).upper().replace("XXS", "2XS")
            nums |= {n for n, l in SIZE_LABEL.items() if l.endswith(f"({lab})")}
    return sorted(nums)


def _cm(v):
    return (str(int(v)) if v == int(v) else str(v).replace(".", ",")) + " см"


def leg_answer(text, model):
    """«Какой выход?» (None if not asked or nothing to say):
    a concrete size named -> that size's value from the size table («выход» / «полувыход» column);
    no size -> the product's fixed answer from products/<id>/leg_opening.txt (if it has one)."""
    if not LEG_Q.search(text or ""):
        return None
    rows = TABLES.get((model or "").lower(), {})
    col = next((c for c in LEG_COLS if any(c in r for r in rows.values())), None)
    vals = [(n, rows[n][col]) for n in sizes_in(text) if col and n in rows and col in rows[n]]
    if vals:
        return "Выход у " + ", у ".join(f"{n} размера — {_cm(v)}" if i == 0 else f"{n} — {_cm(v)}"
                                        for i, (n, v) in enumerate(vals)) + "."
    return BY_KEY[model].get("leg_opening") if model in BY_KEY else None


def chart_only(text, model):
    """Measurements of a concrete size («замеры 30 размера», «какая длина у 32») -> just the size chart, no text."""
    return bool(sizes_in(text)) and asks_chart(text) and not LEG_Q.search(text or "") and bool(charts_for(model))


DENSITY_Q = re.compile(r"плотн|тянут|тянуч|эласти|стрейч|stretch", re.I)
# always a composition question (cotton named explicitly)
COMPOSITION_Q = re.compile(r"100\s*%|сто процент|чист\w* хлопок|только хлопок|полностью хлопок|натуральн", re.I)
# a composition question only right after the bot already said «Материал: …»
COMPOSITION_FOLLOWUP = re.compile(r"остальн|(а|что|чего) ещ[её]|ещ[её] что|кроме хлопка|какой процент|сколько процент|"
                                  r"точн|полностью|материал|состав|ткан|из чего", re.I)


def composition_for(model):
    if model in BY_KEY:
        return BY_KEY[model]["composition"]
    comps = {p["composition"] for p in PRODUCTS}
    return comps.pop() if len(comps) == 1 else None


def material_part(text, model, context=""):
    """Follow-up about the material («а 2% что?», «а остальное?», «100% хлопок?», second material question):
    name what the default answer left out, e.g. «Полиэстер 2%.». None if it is not a follow-up."""
    comp = composition_for(model)
    if not comp:
        return None
    parts = [(int(n), f.strip()) for n, f in re.findall(r"(\d{1,3})\s*%\s*([а-яёa-z\- ]+)", comp, re.I)]
    say = lambda ps: " ".join(f"{f.capitalize() if i == 0 else f} {n}%," for i, (n, f) in enumerate(ps)).rstrip(",") + "."
    asked = re.search(r"(\d{1,3})\s*(%|процент)", text or "", re.I)
    if asked and any(n == int(asked.group(1)) for n, _ in parts):
        return say([(n, f) for n, f in parts if n == int(asked.group(1))])
    said_before = "материал:" in (context or "").lower()
    if COMPOSITION_Q.search(text or "") or (said_before and COMPOSITION_FOLLOWUP.search(text or "")):
        default = (material_for(model) or "").lower()
        rest = [(n, f) for n, f in parts if f.lower() not in default]
        return say(rest or parts)
    return None


def material_for(model):
    """Material from products/<id>/material.txt; with no model, only if every product has the same one."""
    if model in BY_KEY:
        return BY_KEY[model]["material"]
    mats = {p["material"] for p in PRODUCTS}
    return mats.pop() if len(mats) == 1 else None


def is_generic(reply):
    """Only reuse short replies with no digits, prices, currency or sizes."""
    if len(reply) > 60 or re.search(r"\d|руб|₽|%", reply, re.I):
        return False
    if SHIPPING.search(reply):  # never promise or confirm sending, delivery, payment
        return False
    if not is_formal(reply):  # never informal wording in replies reused from chats
        return False
    return not SIZE_WORD.search(reply)


# ---------------------------------------------------------------- buyer typos
VOCAB_FILE = HERE.parent / "data" / "vocab.json"
VOCAB = json.loads(VOCAB_FILE.read_text(encoding="utf-8")) if VOCAB_FILE.exists() else {}
_ABC = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
# a typo may only be fixed INTO a keyword of the rules that answer or hand over safely; never into «потом», «хорошо»,
# «заказал» etc. (those make the bot say «Хорошо» or close a deal)
TYPO_TARGET = re.compile(r"замер|сетк|таблиц|мерк|длин|ширин|выход|талии|бедр|фото|фотк|сидят|сидит|сидеть|человек|налич|"
                         r"доставк|сдэк|почт|яндекс|пункт|выдач|отправ|примерк|примерит|примерят|померит|заказать|оформить|"
                         r"плотн|тянут|материал|состав|хлоп|оверсайз|размер|вернуть|возврат|обмен|встреч|самовывоз|"
                         r"приехать|подъехать|адрес|метро|стоит|стоят|цена|скидк|подобрать|подойд", re.I)


def _edits1(w):
    sp = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    return set([a + b[1:] for a, b in sp if b] + [a + b[1] + b[0] + b[2:] for a, b in sp if len(b) > 1]
               + [a + c + b[1:] for a, b in sp if b for c in _ABC] + [a + c + b for a, b in sp for c in _ABC])


def fix_typos(text):
    """«замееры можно» -> «замеры можно»: words of 5+ letters not in the chats' word list and one edit away from a
    listed word become the most frequent such word. Used only when no rule fired on the original text."""
    def one(m):
        w = m.group(0); lw = w.lower()
        if len(lw) < 5 or lw in VOCAB:
            return w
        cands = [c for c in _edits1(lw) if c in VOCAB and TYPO_TARGET.search(c)]
        return max(cands, key=VOCAB.get) if cands else w
    return re.sub(r"[А-Яа-яЁё]+", one, text or "")


CLASSIFIER_WHYS = {"approved template", "no approved template: Maxim answers", "asked height and weight (Maxim's wording)",
                   "height and weight already known; fit/stock choice is Maxim's", "asked height and weight"}


def from_classifier(out, text):
    """True when no hand-written rule decided (the answer came from the TF-IDF question type)."""
    why = out.get("why", "")
    return why in CLASSIFIER_WHYS or (why.startswith("return / exchange") and not is_return(text))


def _keyword_rules():
    import contextlib, io, sys
    sys.path.insert(0, str(HERE.parent))
    with contextlib.redirect_stdout(io.StringIO()):
        from classify import CATS
    return CATS


OUT_OF_STOCK_REPLY = "К сожалению, этой модели сейчас временно нет в наличии."
GO_AHEAD = re.compile(r"[^.!?🤗]*\b(можете заказывать|можно заказывать|заказывайте|отправлю|отправим|да, есть)\b[^.!?🤗]*[.!?]*\s*🤗?\s*", re.I)


# «Можете заказывать, сегодня отправлю 🤗» and its variants: said at most once per chat (user's rule, 15.11)
ORDER_GO = re.compile(r"[^.!?🤗\n]*\b(?:можете|можно)\s+(?:заказывать|оформлять|заказать|оформить)\b[^.!?🤗\n]*[.!?]*\s*🤗?"
                      r"|[^.!?🤗\n]*\b(?:заказывайте|оформляйте)\b[^.!?🤗\n]*[.!?]*\s*🤗?", re.I)


# «Если остались вопросы, пишите…» (the 15-minute reminder, or GPT's own variant): also once per chat (user, 15.11)
QUESTIONS_LEFT = re.compile(r"[^.!?🤗\n]*\bесли (?:остались|останутся|будут|появятся)(?: ещё| еще)? вопросы\b[^.!?🤗\n]*[.!?]*\s*🤗?", re.I)
ONCE_PER_CHAT = (ORDER_GO, QUESTIONS_LEFT)


def go_ahead_once(reply, earlier):
    """Phrases allowed once per chat («можете заказывать, … отправлю», «если остались вопросы…»): drop them from the
    reply if the bot or the shop already said them (earlier = texts of their messages). -> the reply, or None when
    nothing worth sending is left."""
    for pat in ONCE_PER_CHAT:
        if reply and pat.search(reply) and any(pat.search(t or "") for t in earlier):
            reply = pat.sub("", reply).strip(" ,—-\n")
            reply = reply if re.search(r"[а-яёa-z0-9]", reply, re.I) else None
    return reply


REPLY_SIZE = re.compile(r"(?<!\d)(25|26|27|28|29|30|31|32|33|34|36)\s*\((?:2XS|XS|S|M|L|XL)\)"  # «30 (M)»
                        r"|(?:подойд\w*|подходит|берите|бери|советую|рекомендую)\D{0,20}?(?<!\d)(25|26|27|28|29|30|31|32|33|34|36)(?!\d)",
                        re.I)


def _sizes_ru(sizes):
    """[30] -> «30 размера», [30, 31] -> «30 и 31 размеров»."""
    s = [str(x) for x in sizes]
    return (s[0] + " размера") if len(s) == 1 else (", ".join(s[:-1]) + " и " + s[-1] + " размеров")


def size_guard(out, model, text, context=""):
    """Sizes switched off one by one in the manager app: a reply that recommends / confirms such a size gets no go-ahead
    and says the size is temporarily out; «есть 30?» / «беру 30» about such a size -> it's out; «какие размеры есть?» ->
    names the missing ones. Works on the rule bot's and GPT's replies alike."""
    gone = set(out_sizes(model))
    if not gone or not out.get("reply"):
        return out
    reply = out["reply"]
    recommended = {int(a or b) for a, b in REPLY_SIZE.findall(reply)} & gone
    order_q = is_stock(text) or can_order_q(text) or is_decided(text) or closes_deal(text, context)
    named = set(sizes_in(text)) or ({stated_size(context)} - {None} if is_decided(text) or closes_deal(text, context) else set())
    asked = (named & gone) if order_q else set()  # «есть 30?», «беру 30», or «беру» right after the shop named a missing size
    why = lambda w: (out.get("why", "") + "; " + w).lstrip("; ")
    if asked or recommended:
        rest = GO_AHEAD.sub("", reply).replace(JUST_IN_CASE, "").strip()
        bad = sorted(asked | recommended)
        if asked or not rest:
            note = f"К сожалению, {_sizes_ru(bad)} сейчас временно нет в наличии."
        else:
            note = ("Но этого размера" if len(bad) == 1 else "Но " + _sizes_ru(bad)) + " сейчас временно нет в наличии."
        return {**out, "reply": (rest + " " + note).strip(), "escalate": False, "why": why(f"size {bad} out of stock")}
    listed = len(re.findall(r"(?<!\d)(?:25|26|27|28|29|30|31|32|33|34|36)(?!\d)", reply)) >= 3  # GPT listed sizes itself
    if is_stock(text) and not sizes_in(text) and not listed and "нет в наличии" not in reply.lower():  # «какие размеры есть?»
        return {**out, "reply": reply.rstrip() + f" Сейчас нет в наличии только {_sizes_ru(sorted(gone))}.",
                "why": why("listed missing sizes")}
    return out


def stock_guard(out, model, text, context=""):
    """Product marked «нет в наличии» in the manager app: never say it's in stock / go ahead and order / when it ships.
    Stock and order questions get OUT_OF_STOCK_REPLY; other answers (size, material, photos) stay, minus go-ahead sentences.
    Handoffs (reply None) are left alone: Maxim answers those himself. Sizes switched off one by one: see size_guard."""
    if not out.get("reply"):
        return out
    if in_stock(model):
        return size_guard(out, model, text, context)
    asks_order = is_stock(text) or can_order_q(text) or is_decided(text) or closes_deal(text, context) or asks_send_when(text)
    rest = GO_AHEAD.sub("", out["reply"]).strip()
    if asks_order or not rest:
        return {**out, "reply": OUT_OF_STOCK_REPLY, "photos": [], "escalate": False,
                "why": (out.get("why", "") + "; out of stock").lstrip("; ")}
    if rest != out["reply"].strip():  # a go-ahead / «есть» sentence was cut: say why instead
        rest = rest + " " + OUT_OF_STOCK_REPLY
    return {**out, "reply": rest, "why": (out.get("why", "") + "; out of stock: no go-ahead").lstrip("; ")}


class Brain:
    sent_photos = ()  # manager.py sets it before each answer: file names of pictures already sent in this chat

    def __init__(self):
        rows = [json.loads(l)["messages"] for l in (DATA / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        self.inputs = [r[0]["content"] for r in rows]
        self.replies = [r[1]["content"] for r in rows]
        recs = json.loads((DATA / "labels.json").read_text(encoding="utf-8"))
        labels = {(p["input"], p["reply"]): p["category"] for p in recs}
        ctx = {(p["input"], p["reply"]): p.get("context", "") for p in recs}
        self.types = [labels.get((i, r), "Other") for i, r in zip(self.inputs, self.replies)]
        # retrieval on the request text
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
        X = self.vec.fit_transform(self.inputs)
        # classifier: request features + the WHOLE earlier chat as separate features (chat weighted 0.5)
        self.cvec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True, max_features=200000)
        Xc = self.cvec.fit_transform([ctx.get((i, r), "") for i, r in zip(self.inputs, self.replies)])
        self.clf = LogisticRegression(max_iter=2000, class_weight="balanced").fit(hstack([X, 0.5 * Xc]).tocsr(), self.types)
        # multi-label: a message can carry several question types (e.g. size + photo)
        from sklearn.multiclass import OneVsRestClassifier
        from sklearn.preprocessing import MultiLabelBinarizer
        rules = _keyword_rules()
        multi = [[n for n, rx in rules.items() if re.search(rx, i.lower())] or ["Other"] for i in self.inputs]
        self.mlb = MultiLabelBinarizer()
        Y = self.mlb.fit_transform(multi)
        self.ml = OneVsRestClassifier(LogisticRegression(max_iter=2000, class_weight="balanced")).fit(
            hstack([X, 0.5 * Xc]).tocsr(), Y)
        self.nn = NearestNeighbors(n_neighbors=10, metric="cosine").fit(X)
        self.sizes = json.loads(SIZES.read_text(encoding="utf-8")) if SIZES.exists() else {}
        # the size tree learned from Maxim's answers is the default; manual rules are the fallback
        self.tree = joblib.load(SIZE_TREE) if SIZE_TREE.exists() else None

    def size_for(self, model, height, weight):
        if height and weight and not (13 <= weight / (height / 100) ** 2 <= 45):
            return None  # implausible pair (e.g. 180 cm, 30 kg): no size, Maxim checks
        hw = (BY_KEY.get(model) or {}).get("hw")
        if hw and height and weight:  # the product's own height x weight table (manager app) wins; empty cell = Maxim
            n = hw_size(hw, height, weight)
            return SIZE_LABEL.get(n, str(n)) if n else None
        if self.tree is not None and height and weight:
            return SIZE_LABEL.get(int(self.tree.predict(np.array([[height, weight]]))[0]))
        rows = self.sizes.get(model)
        if not rows or height is None or weight is None:
            return None
        for r in rows:
            if r["min_h"] <= height <= r["max_h"] and r["min_w"] <= weight <= r["max_w"]:
                return r["size"]
        return None

    def classify(self, text, context=""):
        """Question type from the whole earlier chat + the request."""
        feats = hstack([self.vec.transform([text]), 0.5 * self.cvec.transform([context or ""])]).tocsr()
        return str(self.clf.predict(feats)[0])

    def answer(self, text, model=None, context="", price=None):
        """Single-question answer. Formal wording, greeting on the first message."""
        out = polish(self._typo_retry(self._answer, text, model, context, price))
        return add_greeting(stock_guard(out, model, text, context), context)

    def answer_multi(self, text, model=None, context="", price=None):
        """Multi-question answer. Formal wording, greeting on the first message."""
        out = polish(self._typo_retry(self._answer_multi, text, model, context, price))
        return add_greeting(stock_guard(out, model, text, context), context)

    def _typo_retry(self, fn, text, model, context, price):
        """Rules first on the buyer's own text; only if none fired, try again with typos fixed."""
        out = fn(text, model, context, price)
        if from_classifier(out, text):
            fixed = fix_typos(text)
            if fixed != text:
                again = fn(fixed, model, context, price)
                if not from_classifier(again, fixed):
                    again["why"] = again.get("why", "") + " (typos fixed)"
                    return again
        return out

    def _answer(self, text, model=None, context="", price=None):
        if "[голосовое]" in (text or ""):  # a voice note: the bot can't hear it, straight to Maxim
            return {"type": "Other", "reply": None, "escalate": True, "why": "voice message: Maxim listens"}
        if asks_if_bot(text):
            return {"type": "Other", "reply": None, "escalate": True, "why": "buyer asks if this is a bot: Maxim answers"}
        text = label_bare_number(text, context)
        kind = self.classify(text, context)
        if is_meeting(text, context):
            return {"type": MEETING_TYPE, "reply": None, "escalate": True, "why": "personal meeting: Maxim arranges it"}
        if is_discount(text):  # no discounts: polite no, no escalation
            return {"type": "Price", "reply": NO_DISCOUNT, "escalate": False, "why": "no discounts"}
        if is_price_q(text):
            if is_discount(text) or not price:
                return {"type": "Payment (on delivery, upfront)", "reply": None, "escalate": True,
                        "why": "price: discount or no card price -> Maxim"}
            return {"type": "Price", "reply": price_reply(price), "escalate": False, "why": "price from listing card"}
        if asks_return_policy(text):
            return {"type": RETURN_TYPE, "reply": (self._size_prefix(text, model, context) + RETURN_POLICY_REPLY).strip(),
                    "escalate": False, "why": "return policy"}
        if asks_return_after_pickup(text):
            return {"type": RETURN_TYPE, "reply": RETURN_AFTER_PICKUP_REPLY, "escalate": False,
                    "why": "no returns after pickup"}
        if asks_try_on(text):
            return {"type": RETURN_TYPE, "reply": (self._size_prefix(text, model, context) + TRY_ON_REPLY).strip(),
                    "escalate": False, "why": "try-on: only with Avito Delivery"}
        if is_return(text):
            return {"type": RETURN_TYPE, "reply": None, "escalate": True, "why": "return / exchange: Maxim decides"}
        leg = leg_answer(text, model)
        if leg:  # «какой выход (у 30)?» -> the size's value from the table, or the product's fixed answer; no size chart
            return {"type": "Measurements / size chart", "reply": leg, "escalate": False, "why": "leg opening"}
        if chart_only(text, model):  # measurements of a concrete size: just the chart, no text
            return {"type": "Measurements / size chart", "reply": "", "photos": charts_for(model), "escalate": False,
                    "why": "size chart for a concrete size (no text)"}
        # Client wrote girths (waist / hips / thigh): pick the size from the measurement table
        meas = client_measures(text)
        if model and (meas.get("waist") or meas.get("hip")):
            sz = size_by_measures(model, meas)
            if sz and self._disagrees(sz, model, text, context):
                return {"type": "Fit worries (too long/wide/tight)", "reply": None, "escalate": True,
                        "why": "measurements and height/weight give different sizes"}
            if sz and size_reply(sz, text, context, "По вашим замерам подойдёт"):
                return {"type": "Size advice (height/weight → size)", "escalate": False, "why": "size from measurement table",
                        "reply": size_reply(sz, text, context, "По вашим замерам подойдёт")}
        if asks_delivery_time(text) and not order_just_created(context) and not is_paid(text) and not ALREADY_ORDERED.search(text):
            own_hw, size_part = extract(text), ""
            if own_hw["height"] and own_hw["weight"]:  # «рост 158 вес 38, за сколько приходят?» -> size + delivery time
                size = self.size_for(model, own_hw["height"], own_hw["weight"])
                size_part = (size_reply(size, text, context) or "") if size else ""
            return {"type": "Delivery: method, city, cost, time", "reply": (size_part + " " + DELIVERY_TIME_REPLY).strip(),
                    "escalate": False, "why": "delivery time: shown at checkout"}
        if asks_from_where(text) and not order_just_created(context) and not is_paid(text):
            return {"type": "Delivery: method, city, cost, time", "reply": (self._size_prefix(text, model, context) + FROM_WHERE_REPLY).strip(),
                    "escalate": False, "why": "ships from Moscow"}
        if asks_send_when(text):
            return {"type": "Order status (sent yet? when?)", "reply": (self._size_prefix(text, model, context) + send_when_reply()).strip(), "escalate": False,
                    "why": "when we ship: by Moscow time"}
        if asks_delivery_how(text) and not order_just_created(context) and not is_paid(text) and not ALREADY_ORDERED.search(text):
            return {"type": "Delivery: method, city, cost, time", "reply": (self._size_prefix(text, model, context) + DELIVERY_HOW_REPLY).strip(),
                    "escalate": False, "why": "delivery method: any"}
        if kind == RETURN_TYPE:
            return {"type": RETURN_TYPE, "reply": None, "escalate": True, "why": "return / exchange: Maxim decides"}
        early = self._order_first_words(text, model, context)
        if early:
            return early
        if order_just_created(context) or is_paid(text):
            return {"type": SHIP_TYPE, "reply": None, "escalate": True, "why": "new order / paid: Maxim ships"}
        if asks_sending(text):
            return {"type": "Order status (sent yet? when?)", "reply": None, "escalate": True,
                    "why": "asks about sending: Maxim knows the real status"}
        if postpones(text):
            return {"type": "Other", "reply": POSTPONE_REPLY, "escalate": False, "why": "buyer postpones"}
        if is_decided(text) or closes_deal(text, context):
            if will_order(text, context):  # «Хорошо», «закажу», «сейчас оформлю» and no order yet
                if LATER.search(text):  # «завтра утром оформлю», «вечерком закажу»: just agree
                    return {"type": "Other", "reply": (self._size_prefix(text, model, context) + POSTPONE_REPLY).strip(),
                            "escalate": False, "why": "buyer will order later"}
                return {"type": "How to order on Avito", "reply": (self._size_prefix(text, model, context) + go_ahead_reply()).strip(),
                        "escalate": False, "why": "buyer is about to order: go ahead"}
            return {"type": "Order status (sent yet? when?)", "reply": None, "escalate": True,
                    "why": "buyer decided / ordering: Maxim completes the order"}
        if is_stock(text):  # any size is in stock; add the size (or ask height/weight) when they want help
            return self._stock_answer(text, model, context)
        if GREETING_ONLY.match(text.replace("\n", " ")):
            known = merged_nums(text, context)
            if known["height"] is not None and known["weight"] is not None:  # sizes already sorted out in this chat
                return {"type": "Other", "reply": "Здравствуйте!", "escalate": False, "why": "greeting only"}
            return {"type": "Other", "reply": "Здравствуйте! " + ASK_BODY, "escalate": False,
                    "why": "greeting only: offer to pick a size"}
        if is_ack(text):
            return {"type": "Other", "reply": None, "escalate": False, "why": "acknowledgement: nothing to answer"}
        if can_order_q(text) and not client_measures(text):
            own_hw = extract(text)
            extra = None
            if own_hw["height"] and own_hw["weight"]:
                size = self.size_for(model, own_hw["height"], own_hw["weight"])
                extra = size_reply(size, text, context) if size else None
            elif SIZE_HELP.search(text):
                extra = ASK_BODY
            elif names_size(text) and None in (merged_nums(text, context)["height"], merged_nums(text, context)["weight"]):
                extra = JUST_IN_CASE  # a size of their own choice and no height/weight yet
            # a statement («мне 30 M пожалуйста», a bare «30м»): no «Да», one sentence
            statement = "?" not in text and not CAN_ORDER.search(text)
            if SIZE_NEED.search(text) and "?" not in text:  # «Размер L нужен»
                return {"type": "Size advice (height/weight → size)", "escalate": False, "why": "buyer needs a size",
                        "reply": JUST_IN_CASE if extra == JUST_IN_CASE else "Можете заказывать."}
            if extra == JUST_IN_CASE:
                reply = ("Можете заказывать" if statement else "Да, можете заказывать") + ", на всякий случай напишите рост и вес."
            else:
                reply = ("Можете заказывать." if statement else CAN_ORDER_REPLY) + (" " + extra if extra else "")
            return {"type": "How to order on Avito", "reply": reply, "escalate": False, "why": "asks to order a size: yes"}
        nums = merged_nums(text, context)
        own = extract(text)  # only numbers in THIS message make it a sizing question
        if own["height"] and own["weight"]:
            kind = next(iter(SIZING_TYPES))

        # Fit question with the client's own measurements: compare with the table row of the size
        if model and (kind == "Fit worries (too long/wide/tight)" or FIT_WORDS.search(text)):
            meas = client_measures(text)
            for line in (context or "").split("\n"):
                if line.startswith("buyer: ") and not meas:
                    meas = client_measures(line[len("buyer: "):])
            hw_label = self.size_for(model, nums["height"], nums["weight"]) if nums["height"] and nums["weight"] else None
            size_num = (int(hw_label.split()[0]) if hw_label else size_number(self.tree, nums)) or stated_size(context)
            if meas and size_num:
                verdict, det = compare(model, size_num, meas)
                if verdict == "fits":
                    return {"type": "Fit worries (too long/wide/tight)", "reply": "По вашим замерам размер подходит.",
                            "escalate": False, "why": "measurements fit the size table"}
                if verdict in ("tight", "loose"):
                    return {"type": "Fit worries (too long/wide/tight)", "reply": None, "escalate": True,
                            "why": f"measurements {verdict} for size {size_num}: {det}"}
        if asks_chart(text) and not (own["height"] and own["weight"]):
            pics = charts_for(model)
            if pics:
                return {"type": "Measurements / size chart", "reply": CHART_REPLY, "photos": pics,
                        "escalate": False, "why": "size chart screenshot"}
        half = half_body_q(own, nums)
        if half:  # only the height (or only the weight) so far: ask for the missing one
            return {"type": "Size advice (height/weight → size)", "reply": half, "escalate": False, "why": "asked the missing height/weight"}
        if kind in SIZING_TYPES:
            if nums["height"] is None or nums["weight"] is None:
                return {"type": kind, "reply": ASK_BODY, "escalate": False, "why": "asked height and weight (Maxim's wording)"}
            size = self.size_for(model, nums["height"], nums["weight"])
            if size is None:
                return {"type": kind, "reply": None, "escalate": True, "why": "no size rule/table row yet"}
            reply = size_reply(size, text, context)
            if reply is None:
                return {"type": kind, "reply": None, "escalate": True, "why": "oversize: no bigger size"}
            return {"type": kind, "reply": reply, "escalate": False,
                    "why": "size from table" + (" +1 oversize" if wants_oversize(text, context) else "")}

        ordered = bool(ALREADY_ORDERED.search(text))
        if ordered:
            kind = STATUS_TYPE
        # Templates that ask for height and weight must not ask again when the chat already has them
        known = nums["height"] is not None and nums["weight"] is not None
        if known and kind in ASK_BODY_TYPES:
            return {"type": kind, "reply": None, "escalate": True,
                    "why": "height and weight already known; fit/stock choice is Maxim's"}
        if kind == "Photos on a person / fit":
            pics = photos_for(model, self.sent_photos)
            if pics:
                return {"type": kind, "reply": PHOTO_REPLY, "photos": pics, "escalate": False, "why": "model photo (next one)"}
            if all_photos_sent(model, self.sent_photos):
                return {"type": kind, "reply": None, "escalate": True, "why": "all photos already sent: Maxim has more?"}
        dens = BY_KEY[model]["density"] if model in BY_KEY else (
            next(iter({p["density"] for p in PRODUCTS})) if len({p["density"] for p in PRODUCTS}) == 1 else None)
        if dens and DENSITY_Q.search(text):  # «плотные?», «тянутся?», «тонкие?»
            return {"type": "Material / density / warmth", "reply": dens[0].upper() + dens[1:] + ".", "escalate": False,
                    "why": "density from products/<id>/material.txt"}
        part = material_part(text, model, context)
        if part:  # «а 2% что?» -> the fibre behind that percentage in material.txt
            return {"type": "Material / density / warmth", "reply": part, "escalate": False,
                    "why": "material share from products/<id>/material.txt"}
        if kind == "Material / density / warmth" and material_for(model):
            return {"type": kind, "reply": f"Материал: {material_for(model).lower()}.", "escalate": False,
                    "why": "material from products/<id>/material.txt"}
        if kind in TEMPLATES:
            return {"type": kind, "reply": TEMPLATES[kind], "escalate": False, "why": "approved template"}
        return {"type": kind, "reply": None, "escalate": True, "why": "no approved template: Maxim answers"}

    def _order_first_words(self, text, model, context):
        """The buyer ordered without talking and now writes first (before the bot's greeting): height+weight -> size and
        when it ships; a bare greeting -> the order greeting. Anything else stays with Maxim (None)."""
        if not order_without_talk(context) or is_paid(text) or ALREADY_ORDERED.search(text or ""):
            return None
        nums = merged_nums(text, context)
        if nums["height"] and nums["weight"]:
            size = self.size_for(model, nums["height"], nums["weight"])
            said = size_reply(size, text, context) if size else None
            if said:
                return {"type": "Size advice (height/weight → size)", "escalate": False, "why": "order without talk: size",
                        "reply": said + " " + ("Сегодня" if ships_today() else "Завтра") + " отправлю 🤗"}
        half = half_body_q(extract(text), nums)
        if half:
            return {"type": "Size advice (height/weight → size)", "reply": half, "escalate": False, "why": "asked the missing height/weight"}
        if GREETING_ONLY.match((text or "").replace("\n", " ")):
            return {"type": "Other", "reply": order_hello(), "escalate": False, "why": "order without talk: greeting"}
        return None

    def _disagrees(self, size_num, model, text, context):
        """Size from the girths vs size from height/weight already in the chat: more than one size apart -> True."""
        nums = merged_nums(text, context)
        if nums["height"] is None or nums["weight"] is None:
            return False
        hw = self.size_for(model, nums["height"], nums["weight"])
        m = re.match(r"\s*(\d{2})", str(hw or ""))
        order = sorted(SIZE_LABEL)
        if not m or int(m.group(1)) not in order or size_num not in order:
            return False
        return abs(order.index(int(m.group(1))) - order.index(size_num)) > 1

    def _size_prefix(self, text, model, context):
        """«Рост 180 вес 70, сдэком можно?»: the size goes first, then the quick answer."""
        own_hw = extract(text)
        if own_hw["height"] and own_hw["weight"]:
            size = self.size_for(model, own_hw["height"], own_hw["weight"])
            return ((size_reply(size, text, context) or "") + " ") if size else ""
        return ""

    def _stock_answer(self, text, model, context):
        nums, own = merged_nums(text, context), extract(text)
        extra, why = None, "always in stock"
        if nums["height"] is not None and nums["weight"] is not None and (
                (own["height"] and own["weight"]) or SIZE_HELP.search(text)):
            size = self.size_for(model, nums["height"], nums["weight"])
            extra = size_reply(size, text, context) if size else None
            why += " + size"
        elif asks_chart(text) and charts_for(model):
            extra, why = CHART_REPLY, why + " + size chart"
        elif SIZE_HELP.search(text):
            extra, why = ASK_BODY, why + " + asked height and weight"
        elif names_size(text) and (nums["height"] is None or nums["weight"] is None):
            extra, why = JUST_IN_CASE, why + " + just in case: height and weight"
        reply = STOCK_REPLY + (" " + extra if extra else "")
        photos = charts_for(model) if extra == CHART_REPLY else []
        return {"type": "In stock? / which sizes exist", "reply": reply, "photos": photos, "escalate": False, "why": why}

    def features(self, text, context=""):
        return hstack([self.vec.transform([text]), 0.5 * self.cvec.transform([context or ""])]).tocsr()

    def intents(self, text, context="", threshold=0.35):
        probs = self.ml.predict_proba(self.features(text, context))[0]
        chosen = [c for c, pr in zip(self.mlb.classes_, probs) if pr >= threshold]
        if not chosen:
            chosen = [self.mlb.classes_[int(probs.argmax())]]
        if len(chosen) > 1 and "Other" in chosen:
            chosen.remove("Other")
        return chosen

    def _answer_multi(self, text, model=None, context="", price=None):
        """Answer every intent in the message. Sizes from the tree; photos and anything unsafe go to Maxim."""
        if "[голосовое]" in (text or ""):  # a voice note: the bot can't hear it, straight to Maxim
            return {"intents": ["Other"], "reply": None, "escalate": True, "why": "voice message: Maxim listens"}
        if asks_if_bot(text):
            return {"intents": ["Other"], "reply": None, "escalate": True, "why": "buyer asks if this is a bot: Maxim answers"}
        text = label_bare_number(text, context)
        if is_meeting(text, context):
            return {"intents": [MEETING_TYPE], "reply": None, "escalate": True, "why": "personal meeting: Maxim arranges it"}
        if is_discount(text):  # no discounts: polite no, no escalation
            return {"intents": ["Price"], "reply": NO_DISCOUNT, "escalate": False, "why": "no discounts"}
        if is_price_q(text):
            if is_discount(text) or not price:
                return {"intents": ["Price"], "reply": None, "escalate": True, "why": "price: discount or no card price -> Maxim"}
            return {"intents": ["Price"], "reply": price_reply(price), "escalate": False, "why": "price from listing card"}
        meas_m = client_measures(text)
        if not is_return(text) and model and (meas_m.get("waist") or meas_m.get("hip")):
            sz = size_by_measures(model, meas_m)
            if sz and self._disagrees(sz, model, text, context):
                return {"intents": ["Fit worries (too long/wide/tight)"], "reply": None, "escalate": True,
                        "why": "measurements and height/weight give different sizes"}
            if sz and size_reply(sz, text, context, "По вашим замерам подойдёт"):
                return {"intents": ["Size advice (height/weight → size)"], "escalate": False, "why": "size from measurement table",
                        "reply": size_reply(sz, text, context, "По вашим замерам подойдёт")}
        delivery_q = asks_delivery_time(text) and not order_just_created(context) and not is_paid(text) and not ALREADY_ORDERED.search(text)
        if (is_return(text) and not asks_return_after_pickup(text) and not asks_return_policy(text)) or (RETURN_TYPE in self.intents(text, context) and not delivery_q
                                                                         and not asks_try_on(text) and not asks_return_after_pickup(text)
                                                                         and not asks_return_policy(text)):
            return {"intents": [RETURN_TYPE], "reply": None, "escalate": True, "why": "return / exchange: Maxim decides"}
        if (asks_try_on(text) or asks_return_after_pickup(text) or asks_return_policy(text) or asks_send_when(text) or (asks_from_where(text) and not order_just_created(context) and not is_paid(text)) or (asks_delivery_how(text) and not order_just_created(context)
                                      and not is_paid(text) and not ALREADY_ORDERED.search(text))) and not (is_return(text) and not asks_return_policy(text)
                                                             and not asks_return_after_pickup(text)) \
                and not is_meeting(text, context):
            one = self._answer(text, model=model, context=context, price=price)
            return {"intents": [one["type"]], "reply": one["reply"], "photos": one.get("photos", []),
                    "escalate": one["escalate"], "why": one["why"]}
        early = self._order_first_words(text, model, context)
        if early:
            return dict(early, intents=[early["type"]])
        if order_just_created(context) or is_paid(text):
            return {"intents": [SHIP_TYPE], "reply": None, "escalate": True, "why": "new order / paid: Maxim ships"}
        if postpones(text):
            return {"intents": ["Other"], "reply": POSTPONE_REPLY, "escalate": False, "why": "buyer postpones"}
        if is_decided(text) or closes_deal(text, context):
            one = self._answer(text, model=model, context=context, price=price)
            return {"intents": [one["type"]], "reply": one["reply"], "photos": one.get("photos", []),
                    "escalate": one["escalate"], "why": one["why"]}
        if GREETING_ONLY.match(text.replace("\n", " ")) or is_ack(text) or (can_order_q(text) and not client_measures(text)):
            one = self._answer(text, model=model, context=context, price=price)
            return {"intents": [one["type"]], "reply": one["reply"], "photos": one.get("photos", []),
                    "escalate": one["escalate"], "why": one["why"]}
        if is_stock(text):
            one = self._stock_answer(text, model, context)
            return {"intents": [one["type"]], "reply": one["reply"], "photos": one["photos"], "escalate": False, "why": one["why"]}
        parts, why, escalate, photos = [], [], False, []
        if delivery_q:
            parts.append(DELIVERY_TIME_REPLY); why.append("delivery time: shown at checkout")
        nums = merged_nums(text, context)
        own = extract(text)
        leg = leg_answer(text, model)
        if leg:  # «какой выход (у 30)?» -> table value or the product's fixed answer; the chart only if asked in other words
            parts.append(leg); why.append("leg opening")
        chart = asks_chart(LEG_Q.sub(" ", text) if leg else text) and not (own["height"] and own["weight"]) and charts_for(model)
        if chart:
            photos += chart; why.append("size chart screenshot")
            if not chart_only(text, model):  # measurements of a concrete size: the chart alone, no text
                parts.append(CHART_REPLY)
        for kind in self.intents(text, context):
            if kind in SIZING_TYPES and chart:
                continue
            if leg and (kind == "Measurements / size chart" or (kind in SIZING_TYPES and (nums["height"] is None or nums["weight"] is None))):
                continue  # the leg-opening answer already covers it; no «напишите рост и вес» tacked on
            if delivery_q and kind in (RETURN_TYPE, "Delivery: method, city, cost, time", "Other"):
                continue  # already answered with the delivery time
            if delivery_q and kind in SIZING_TYPES and (nums["height"] is None or nums["weight"] is None):
                continue  # a delivery question is not a request for size help  # the chart (with the height/weight offer) already answers it
            if kind in SIZING_TYPES:
                half = half_body_q(extract(text), nums)
                if half or nums["height"] is None or nums["weight"] is None:
                    parts.append(half or ASK_BODY)
                    why.append("asked the missing height/weight" if half else "asked height and weight")
                    continue
                size = self.size_for(model, nums["height"], nums["weight"])
                if size is None:
                    escalate = True; why.append("no size for these numbers")
                else:
                    reply = size_reply(size, text, context)
                    if reply is None:
                        escalate = True; why.append("oversize: no bigger size")
                    else:
                        parts.append(reply); why.append("size from tree")
            elif kind == "Photos on a person / fit":
                pics = photos_for(model, self.sent_photos)
                if pics:
                    photos += pics; parts.append(PHOTO_REPLY); why.append("model photo (next one)")
                else:
                    escalate = True
                    why.append("all photos already sent" if all_photos_sent(model, self.sent_photos) else "no photos for this product")
            else:
                one = self._answer(text, model=model, context=context)
                if one["escalate"]:
                    escalate = True; why.append(one["why"])
                else:
                    parts.append(one["reply"]); why.append(one["why"])
        if PHOTO_REPLY in parts:  # photos go last: answer first, then «Вот так сидят:» + photos
            parts = [x for x in parts if x != PHOTO_REPLY] + [PHOTO_REPLY]
        parts = list(dict.fromkeys(parts))  # two sizing intents give the same sentence: say it once
        reply = " ".join(parts) or None
        if reply is None and not photos:
            escalate = True
        return {"intents": self.intents(text, context), "reply": reply, "photos": photos,
                "escalate": escalate, "why": "; ".join(why)}


if __name__ == "__main__":
    import sys
    b = Brain()
    text = " ".join(sys.argv[1:]) or "здравствуйте на рост 186 вес 76 какой размер"
    print(json.dumps(b.answer(text, model=None), ensure_ascii=False, indent=1))
