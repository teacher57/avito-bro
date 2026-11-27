"""Facts the GPT bot gets from code, not from the model (branch unstable-gpt-only: there is no rule bot any more).

Kept from the old brain.py, only what GPT needs:
- reading height, weight and sizes out of the buyer's messages (extract, merged_nums, sizes_in);
- the shipping day from the Moscow-time cutoffs set in the manager app (cutoffs, ships_today, order_hello);
- which sizes are in stock and the guards that stop a reply promising a size / a model that isn't there
  (size_guard, stock_guard), «можете заказывать» at most once per chat (go_ahead_once);
- «какой выход?» from the size table (leg_answer).
Size by height and weight: products.hw_size (nearest row and column of the manager app's table)."""
import json
import re
import time
from pathlib import Path

import products as _products
from products import BY_KEY, LETTER_NUMS, SIZE_LABEL, label_of, product_for_item, in_stock, in_sizes, out_sizes

HERE = Path(__file__).parent


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


def model_for_item(title):
    """Model key for a listing title, from the product's match words in the catalog (None if unknown)."""
    p = product_for_item(title)
    return p["key"] if p else None


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


# «Когда отправите?», «когда доставка?», «сможете сегодня отправить?»: the answer depends on the time in Moscow.
# Past tense («отправили?», «уже отправлено?») is a status question -> Maxim.
SEND_WHEN = re.compile(r"когда (\w+ )?(отправ(ите|ишь|ят|им|ка|ку|ки|ить|ляете|ляешь)|доставк|доставите|будет доставк|будет отправк)|"
                       r"отправ(ите|ишь|ят|ить|ляете)\b[^?]*\b(сегодня|завтра|сейчас)|(сегодня|завтра|сейчас)\b[^?]*\bотправ(ите|ишь|ят|ить|ляете)\b|"
                       r"во сколько (\w+ )?отправ|в какой день (\w+ )?отправ", re.I)


SENT_PAST = re.compile(r"отправил|отправлен|отправили|уже отправ", re.I)


WEEKDAY_CUTOFF, WEEKEND_CUTOFF = (19, 0), (18, 0)  # Moscow time: after this, it ships tomorrow (defaults)


SHOP_SETTINGS = HERE.parent / "shop_settings.json"  # old place of the cutoffs: read only when shop.json has none


_cutoff_cache = {"mtime": None, "value": (WEEKDAY_CUTOFF, WEEKEND_CUTOFF)}


def _hhmm(v, default):
    try:
        h, m = (int(x) for x in str(v).split(":"))
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else default
    except (ValueError, TypeError):
        return default


def _file_cutoffs():
    """(weekday, weekend) from the old shop_settings.json (re-read when it changes), else the defaults above."""
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


def cutoffs():
    """(weekday, weekend) cutoff as (hour, minute): shop.cutoffs on the data server (the manager app edits them; the bot
    re-reads shop.json with the products), else the old shop_settings.json, else 19:00 / 18:00."""
    c = (_products.DATA.get("shop") or {}).get("cutoffs") or {}
    if c.get("weekday") and c.get("weekend"):
        fallback = _file_cutoffs()
        return _hhmm(c["weekday"], fallback[0]), _hhmm(c["weekend"], fallback[1])
    return _file_cutoffs()


CLOCK_OFFSET = 0  # seconds added to time.time() when `now` isn't given; manager.py sets this from a trusted time source


def ships_today(now=None):
    """Weekdays before 19:00 MSK and weekends before 18:00 MSK (shop.cutoffs, editable in the manager app) -> it ships
    today, else tomorrow."""
    import time as _t
    msk = _t.gmtime((now if now is not None else _t.time() + CLOCK_OFFSET) + 3 * 3600)
    weekday, weekend = cutoffs()
    h, m = weekend if msk.tm_wday >= 5 else weekday
    return (msk.tm_hour, msk.tm_min) < (h, m)


def order_hello(now=None):
    """First message when the buyer ordered without writing anything (Maxim's three messages in one)."""
    return ("Здравствуйте! Напишите свой рост и вес, я вам помогу подобрать размер. "
            + ("Сегодня отправлю 🤗" if ships_today(now) else "Завтра отправлю 🤗"))


def asks_send_when(text):
    return bool(SEND_WHEN.search(text or "")) and not SENT_PAST.search(text or "")


# Short agreement right after an offer to order / a size recommendation = the buyer decided
AGREE = re.compile(r"^\s*(да|ага|угу|хорошо|ок|ладно|понял|понятно|давайте|договорились|супер|отлично|круто|беру|"
                   r"да,?\s*хорошо|ок,?\s*хорошо|хорошо,?\s*давайте)[\s!.,)]*$", re.I)


OFFER = re.compile(r"размер|заказыва|заказать|отправлю|оформ|можете заказ", re.I)


def closes_deal(text, context):
    if not AGREE.match(text or ""):
        return False
    shop = [l[len("shop: "):] for l in (context or "").split("\n") if l.startswith("shop: ")]
    return any(OFFER.search(x) for x in shop[-3:])


def is_decided(text):
    return bool(DECIDED.search(text or ""))


# Stock: every size is always in stock (the user's rule) -> «Да, есть.»
_SZ = r"(2?x+[sl]|xs|xl|[sml]\b|хс|хл|эс|эм|[смл]\b|\d\d\b)"


STOCK = re.compile(r"в наличи|есть ли (он|она|они|ещ|размер(?!н))|есть (ли )?(ещ[её] )?" + _SZ
                   + r"|есть (ли )?(ещ[её] )?размер(?!н)\w*\s*(" + _SZ[1:-1] + r"|\?|$|на\b)"
                   r"|\b(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл]|\d\d)( размер\w*)? (ещ[её] )?есть|остал\w* (размер|ли|ещ)"
                   r"|какие (размеры )?(есть|остал)|ещ[её] (есть|продае|актуальн)|актуальн|размеры есть", re.I)


def is_stock(text):
    t = text or ""
    # also «есть 30м?»: «есть» + a size written together with its letter
    return bool(STOCK.search(t)) or bool(re.search(r"\bесть\b|в наличи", t, re.I) and "?" in t and NAMED_SIZE.search(t))


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


JUST_IN_CASE = "На всякий случай напишите рост и вес."


NAMED_SIZE = re.compile(r"(?<![a-zа-я0-9])(" + _SZW + r")(?![a-zа-я0-9])"
                        r"|(?<![a-zа-я0-9])(2[5-9]|3[0-6])\s*[-(]?\s*(2?x+[sl]|xs|xl|[sml]|хс|хл|[смл])\)?(?![a-zа-я0-9])", re.I)  # «30м», «32l», «30-M»


# «Размер L нужен», «нужен L», «мне нужна M»: the buyer says which size they need
SIZE_NEED = re.compile(r"^\s*(\w+[,!]?\s+)?((мне|нам)\s+)?(нуж(ен|на|ны|но)\s+(размер\s+)?" + _SZP + r"(\s*размер\w*)?"
                       r"|(размер\s+)?" + _SZP + r"(\s*размер\w*)?\s+нуж(ен|на|ны|но))" + _PLEASE + r"\s*[.!)]*\s*$", re.I | re.M)


def can_order_q(text):
    """Buyer asks to order / names the size they will take (but not just a number: «31» can be anything)."""
    t = text or ""
    lines = [l for l in t.split("\n") if l.strip()]
    # a bare size line is a choice («32 м», «М»), not a question («XXS?») and not just a number («31»)
    bare = [l for l in lines if BARE_SIZE.match(l) and "?" not in l and re.search(r"[a-zа-я]", l, re.I)]
    # «могу заказать» only as a question or a «можно…» request, not «так то могу заказать, но…»
    asks = "?" in t or re.match(r"\s*(здравствуйте[\s,!.]*)?можно", t, re.I)
    return (bool(CAN_ORDER.search(t)) and bool(asks)) or bool(bare) or bool(SIZE_CHOICE.search(t)) or bool(SIZE_NEED.search(t))


def stated_size(context):
    """Size number the shop last named in this chat (e.g. «Вам подойдёт 34 L размер»)."""
    for line in reversed((context or "").split("\n")):
        if line.startswith("shop: ") and "размер" in line:
            m = re.search(rf"(?<!\d)({_NUMS})(?!\d)", line)
            if m:
                return int(m.group(1))
    return None


LEG_Q = re.compile(r"\bвыход[аеуом]?\b|полувыход|ширин\w* (низа|снизу|внизу|штанин\w*)|низ\w* штанин\w* (какой|какая|сколько)", re.I)


SIZE_NUM = SIZE_LETTER = REPLY_SIZE = None  # built from the size scale by size_patterns() below


_NUMS = ""  # «25|26|…|36»


NEVER = "(?!x)x"  # an alternation with nothing in it


LEG_COLS = ("выход", "полувыход")


def scale_letters():
    """The scale's letters in its order, each once: ["2XS", "XS", "S", "M", "L", "XL"]."""
    # letters of the shop scale in its order, then letters only some categories use (every category has its own mapping)
    base = [l.partition("(")[2].rstrip(")") for _, l in sorted(SIZE_LABEL.items()) if "(" in l]
    return list(dict.fromkeys(base + sorted(LETTER_NUMS)))


def size_patterns():
    """(Re)build the size patterns from SIZE_LABEL (the catalog's size scale); runs again when the catalog changes."""
    global _NUMS, SIZE_NUM, SIZE_LETTER, REPLY_SIZE
    _NUMS = "|".join(str(n) for n in sorted(SIZE_LABEL)) or NEVER
    letters = scale_letters()
    low = {l.lower() for l in letters} | ({"xxs"} if "2XS" in letters else set())  # «xxs» = 2XS
    low_alt = "|".join(re.escape(x) for x in sorted(low, key=lambda x: (-len(x), x))) or NEVER
    up_alt = "|".join(re.escape(x) for x in letters) or NEVER
    SIZE_NUM = re.compile(rf"(?<![\d,.])({_NUMS})(?![\d,.]|\s*(?:см|кг|cm|kg)\b)", re.I)
    SIZE_LETTER = re.compile(rf"(?<![a-z])({low_alt})(?![a-z])", re.I)
    REPLY_SIZE = re.compile(rf"(?<!\d)({_NUMS})\s*\((?:{up_alt})\)"  # «30 (M)»
                            rf"|(?:подойд\w*|подходит|берите|бери|советую|рекомендую)\D{{0,20}}?(?<!\d)({_NUMS})(?!\d)",
                            re.I)


def sizes_in(text):
    """Size numbers the buyer names in this message («у 30», «на 32 размер», «M» -> 30, 31, 32). Height/weight are not sizes."""
    t = re.sub(r"(рост|вес)\D{0,6}\d+", " ", text or "", flags=re.I)
    nums = {int(m.group(1)) for m in SIZE_NUM.finditer(t)}
    if not nums:
        for m in SIZE_LETTER.finditer(t):
            lab = m.group(1).upper().replace("XXS", "2XS")
            nums |= set(LETTER_NUMS.get(lab, ()))
    return sorted(nums)


def _cm(v):
    return (str(int(v)) if v == int(v) else str(v).replace(".", ",")) + " см"


def leg_answer(text, model):
    """«Какой выход?» (None if not asked or nothing to say):
    a concrete size named -> that size's value from the size table («выход» / «полувыход» column);
    no size -> the product's fixed answer (catalog description.legOpening, if it has one)."""
    if not LEG_Q.search(text or ""):
        return None
    rows = ((BY_KEY.get(model) or {}).get("table") or {}).get("rows") or {}
    col = next((c for c in LEG_COLS if any(c in r for r in rows.values())), None)
    vals = [(n, rows[n][col]) for n in sizes_in(text) if col and n in rows and col in rows[n]]
    if vals:
        return "Выход у " + ", у ".join(f"{n} размера — {_cm(v)}" if i == 0 else f"{n} — {_cm(v)}"
                                        for i, (n, v) in enumerate(vals)) + "."
    return BY_KEY[model].get("leg_opening") if model in BY_KEY else None


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


# a jeans size anywhere in a text: a two-digit number 20–59 that is not part of a longer number, price or height
ANY_SIZE = re.compile(r"(?<![\d.,])([2-5]\d)(?![\d.,]|\s*(?:см|кг|₽|руб|р\b|лет|года|год|%))", re.I)


def any_sizes(text):
    """Jeans sizes anywhere in a text: two-digit numbers (not prices, heights, ages…) that fall within the shop's size
    scale (its smallest to its biggest size, e.g. 25–36). Anything outside is not a jeans size — e.g. a Russian
    clothing size «50–52» (2026-11-27: «близко к 50–52» got «Но этого размера сейчас нет в наличии» appended)."""
    scale = [n for n in SIZE_LABEL if isinstance(n, int)]
    lo, hi = (min(scale), max(scale)) if scale else (20, 59)
    return [n for n in (int(x) for x in ANY_SIZE.findall(text or "")) if lo <= n <= hi]


def _sizes_ru(sizes):
    """[30] -> «30 размера», [30, 31] -> «30 и 31 размеров»."""
    s = [str(x) for x in sizes]
    return (s[0] + " размера") if len(s) == 1 else (", ".join(s[:-1]) + " и " + s[-1] + " размеров")


def size_guard(out, model, text, context=""):
    """Sizes switched off one by one in the manager app: a reply that recommends / confirms such a size gets no go-ahead
    and says the size is temporarily out; «есть 30?» / «беру 30» about such a size -> it's out; «какие размеры есть?» ->
    names the missing ones. Works on the rule bot's and GPT's replies alike."""
    gone = set(out_sizes(model))
    have = in_sizes(model)  # None = sizes unknown (old behaviour); else ONLY these are in stock
    if not out.get("reply"):
        return out
    reply = out["reply"]
    order_q = is_stock(text) or can_order_q(text) or is_decided(text) or closes_deal(text, context)
    if have is not None:
        # any two-digit size outside the in-stock list counts as not available — also one this model doesn't have
        # at all (GPT once confirmed «29 есть» for a model with 23–25 only, 2026-11-25)
        gone |= {n for n in any_sizes(text) if n not in have} if order_q else set()
        gone |= {n for n in any_sizes(reply) if n not in have}
    if not gone:
        return out
    recommended = ({int(a or b) for a, b in REPLY_SIZE.findall(reply)} |
                   (set(any_sizes(reply)) if have is not None else set())) & gone
    named = (set(sizes_in(text)) | (set(any_sizes(text)) if have is not None else set())) or \
        ({stated_size(context)} - {None} if is_decided(text) or closes_deal(text, context) else set())
    asked = (named & gone) if order_q else set()  # «есть 30?», «беру 30», or «беру» right after the shop named a missing size
    why = lambda w: (out.get("why", "") + "; " + w).lstrip("; ")
    if asked or recommended:
        rest = GO_AHEAD.sub("", reply).replace(JUST_IN_CASE, "").strip()
        bad = sorted(asked | recommended)
        # «временно» — только для размеров самой модели; размера, которого у модели нет вовсе, просто «нет»
        never = have is not None and all(n not in (BY_KEY.get(model) or {}).get("sizes", []) for n in bad)
        when = "сейчас нет" if never else "сейчас временно нет"
        if have is not None and asked and set(any_sizes(reply)) & set(bad):
            rest = ""  # GPT confirmed a size that isn't there («Да, 29 есть») — the whole reply goes, not just a note
        if asked or not rest:
            note = f"К сожалению, {_sizes_ru(bad)} {when} в наличии."
        else:
            note = ("Но этого размера" if len(bad) == 1 else "Но " + _sizes_ru(bad)) + f" {when} в наличии."
        return {**out, "reply": (note if asked and not rest else (rest + " " + note)).strip(), "escalate": False,
                "why": why(f"size {bad} out of stock")}
    listed = len(re.findall(rf"(?<!\d)(?:{_NUMS})(?!\d)", reply)) >= 3  # GPT listed sizes itself
    if is_stock(text) and not sizes_in(text) and not listed and "нет в наличии" not in reply.lower():  # «какие размеры есть?»
        if have is not None:
            tail = (" Сейчас в наличии: " + ", ".join(label_of(model, n) for n in have) + ".") if have else \
                " Сейчас этой модели нет в наличии."
            return {**out, "reply": reply.rstrip() + tail, "why": why("listed in-stock sizes")}
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


size_patterns()
_products.ON_RELOAD.append(size_patterns)
