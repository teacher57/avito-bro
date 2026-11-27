"""GPT-5.4: the Avito bot itself (branch unstable-gpt-only: no rule bot, every buyer message is answered by GPT).

The model gets: a system prompt with the shop's facts, all from the manager app / HUB BRO via code (shop name, product
from the dialog title, material, size table, in-stock sizes, size by height and weight from the app's table (nearest
height and weight), the shipping day from the Moscow-time cutoffs, placed at the very end so the rest of the prompt
stays cacheable) + the chat as user/assistant turns. No answer from GPT (no key, no internet) = no answer at all:
manager.py tells the admins in Telegram.
Tools (the product is already known from the dialog title):
  product_photo -> the next photo of this model on a person (the catalog's photos, local copies in live/files/)
  chart         -> the size chart image (the catalog's chart, <id>_size_chart.png)
  order_placed  -> the buyer ordered: manager.py sends Maxim a 🛒 note (size if named in the chat)
  escalate      -> the chat goes to the manager (or the model writes only «[МАКСИМ]»)

Key: $OPENAI_API_KEY (never printed).
Run:  python gpt/gpt_reply.py --dry          (show the request for a sample chat, no paid call)
      python gpt/gpt_reply.py                (one real call on the sample chat, prints reply + cost)
"""
import json, os, re, sys, time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "brain"))
import facts as _facts  # noqa: E402  (CLOCK_OFFSET: manager.py sets it after a clock check)
from facts import ASK_BODY, cutoffs, extract, leg_answer, merged_nums, ships_today  # noqa: E402
import products as _products  # noqa: E402
from products import SIZE_LABEL, hw_outside, hw_size, next_photo, product_for_item, shop_name, table_lines  # noqa: E402

MODEL = "gpt-5.4"
PRICES = {  # $ per 1M tokens: input, cached input, output (reasoning tokens are billed as output)
    "gpt-5.4": (2.50, 0.25, 15.00),  # input from OpenAI's model page; cached/output from third-party trackers — check
}
REASONING = lambda m: m.startswith(("gpt-5", "o"))  # reasoning models: no temperature, max_completion_tokens
HANDOFF = "[МАКСИМ]"
URL = "https://api.openai.com/v1/chat/completions"
on_cost = None  # manager.py sets it: called with $ of each API call (the app's spend counter)

TOOLS = [
    {"type": "function", "function": {
        "name": "product_photo",
        "description": "Отправить покупателю ОДНО фото этих джинсов на человеке (как сидят): каждый вызов шлёт следующее "
                       "фото, которого ещё не было в этом чате. Товар уже известен из названия диалога, аргументы не нужны. "
                       "Вызывай один раз, когда просят фото, «как сидят», «есть живые фото», «а ещё есть?» и т.п.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "chart",
        "description": "Отправить покупателю картинку с размерной сеткой этой модели (замеры по размерам). Товар уже известен "
                       "из названия диалога, аргументы не нужны. Вызывай только когда покупатель сам просит сетку, замеры, длину, ширину, обхваты.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "do_nothing",
        "description": "Ничего не отвечать: диалог окончен и помогать ничем не нужно. Покупатель благодарит, прощается, "
                       "соглашается («ок», «хорошо», «понял»), пишет, что подумает, закажет позже или напишет позже "
                       "(«минутку, я вам чуть позже напишу»), — и при этом ничего не спрашивает и ни о чём не просит. "
                       "Покупателю ничего не уходит, напоминание «Если остались вопросы…» тоже не отправляется.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "escalate",
        "description": "Передать чат менеджеру: он ответит сам, бот в этом чате больше не пишет. Покупателю ты "
                       "при этом ничего не пишешь. Вызывай, когда вопрос не покрывается фактами или правилами (см. «КОГДА "
                       "ОТДАТЬ МЕНЕДЖЕРУ»).",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Коротко для менеджера, почему отдаёшь чат (до 10 слов), "
                                                          "например «спрашивает про обмен» или «все фото уже отправлены»."}},
            "required": ["reason"], "additionalProperties": False}}},
]

# The buyer writes he ordered (normal answer), or the forced call when Avito's «У вас новый заказ» arrives (order_size).
# Not a handoff: the manager gets the order with its size, the bot keeps answering in the chat.
ORDER_TOOL = {"type": "function", "function": {
    "name": "order_placed",
    "description": "Заказ оформлен (покупатель пишет, что оформил / оплатил, или пришло «У вас новый заказ»). Менеджер "
                   "сразу получит заказ и отправит его; чат дальше ведёшь ты. Покупателя про размер не спрашивай.",
    "parameters": {"type": "object", "properties": {
        "size": {"type": "string", "description": "Указывай ТОЛЬКО если размер уже есть в переписке выше (его назвали ты / "
                                                    "магазин или покупатель): как в таблице, например «30 (M)». Если размера "
                                                    "в переписке нет — вызывай без size."}},
        "required": [], "additionalProperties": False}}}
TOOLS.append(ORDER_TOOL)

PROMPT = """Ты — {shop}, переписываешься с покупателем в чате объявления.
Пиши как живой человек-продавец, а не как бот или оператор колл-центра.

КАК ПИСАТЬ
- ГЛАВНОЕ: отвечай в том же стиле, в котором пишет покупатель. Смотри на его последние сообщения и подстраивайся:
  · длина: пишет коротко («размер?», «есть 30?») — отвечай так же коротко; пишет развёрнуто — можно чуть подробнее;
  · тон: пишет официально, с «Здравствуйте» и на «вы» — отвечай вежливо на «вы»; пишет по-простому, на «ты», со сленгом
    («здарова», «бро», «чё по размеру») — отвечай так же просто и на «ты», но без грубости и мата;
  · оформление: пишет с маленькой буквы и без точек — можно так же; ставит смайлики — можно ответить смайликом, не ставит — не ставь.
  Если стиль непонятен (первое короткое сообщение) — нейтрально и вежливо, на «вы».
- Коротко: обычно 1–2 предложения. Без списков, без канцелярита («уважаемый клиент», «благодарим за обращение», «обращайтесь» — нельзя).
- Дружелюбно и спокойно. Смайлик 🤗 можно изредка, в конце, не в каждом сообщении.
- Отвечай именно на то, что спросили, своими словами; не повторяй одну и ту же фразу из сообщения в сообщение.
- Не пиши прямыми цитатами из этой инструкции (фактов, примеров, описаний инструментов): всегда перефразируй своими словами.
- Приветствие — только в самом первом ответе в чате, если ты ещё ничего не писал, и тоже в стиле покупателя
  («Здравствуйте!» на официальное, «Привет!» на «привет», «здарова» и т.п.).
- Не задавай лишних вопросов. Рост и вес проси, если покупатель спрашивает, какой размер ему подойдёт, или про посадку,
  а роста и веса в переписке ещё нет (только недостающее: известен рост — спроси вес). Если уже просил — не повторяй.
  В остальных ответах сам не проси: если рост и вес ещё неизвестны, просьба один раз добавится к твоему ответу сама.
- Не говори, что ты бот или ИИ.
- Не заканчивай ответ фразами вроде «Если остались вопросы, пишите» — такое напоминание магазин отправляет сам, один раз.

ФАКТЫ (говори только то, что здесь есть; ничего не придумывай)
- Товар: {title}. Цена: {price}.
- Ткань: {material}. Полный состав (если спрашивают точнее): {composition}. Тянутся ли: {density}.
- Скидок и торга от магазина нет: цена окончательная. Если просят скидку или торгуются — коротко и вежливо откажи, менеджеру
  не отдавай. Исключение — скидка, которую покупателю дало само Авито (системное сообщение «Покупатель ознакомился со
  скидкой …»): она уже указана в строке «Цена» выше, называй цену с ней и не говори, что скидок нет.
{stock}
- Таблица размеров, см (обхваты — в половину, изделие в разложенном виде):
{table}
- Подсказка по размеру из таблицы магазина: {size_hint}
  Размер называй ТОЛЬКО по этой подсказке и всегда один. Не предлагай «этот или соседний», не перекладывай выбор на клиента.
  Если подсказки нет — попроси рост и вес. Сам по таблице размер не подбирай.
  Про бирку сам не говори. Только если покупатель спрашивает, что написано на бирке, или сам называет размер буквой
  (S, M, L…) — объясни, что на бирке джинсов размер написан числом, как в таблице (например, 28 или 32), букв на бирке
  нет, а буква в скобках — 30 (M) — только ориентир. Размер всегда называй числом.
  Когда называешь конкретный размер (не когда просишь рост и вес), в том же ответе добавляй:
  «Можете заказывать, … отправлю 🤗» (сегодня или завтра — по строке «ОТПРАВКА» в самом конце). Эту фразу (и любое
  «можете / можно заказывать», «оформляйте») пиши ТОЛЬКО ОДИН РАЗ за всю переписку: если она уже была в чате — от тебя
  или от магазина, — больше её не повторяй, просто отвечай на вопрос. Если заказ уже оформлен
  (видно системное сообщение «У вас новый заказ»), про заказ больше не говори.
- Отправляем из Москвы каждый день. Заказ, оформленный в будни до {weekday_cutoff}, а в выходные до {weekend_cutoff}
  по Москве, уходит в тот же день, оформленный позже — на следующий день. Когда отправим заказ, оформленный сейчас, —
  написано в строке «ОТПРАВКА» в самом конце. Если покупатель собирается заказать не сейчас (вечером, завтра, в
  выходные), посчитай день отправки по этому правилу от того времени, когда он закажет.
- Доставка — любая удобная через Авито; срок доставки покажет Авито при оформлении.
- Оплата: при оформлении на Авито деньги списываются сразу и лежат на удержании у Авито. Когда покупатель забирает посылку
  и закрывает заказ, деньги приходят магазину. Если он отказывается от посылки, Авито возвращает всю сумму.
- Примерка: только в пунктах выдачи самого Авито. Там можно осмотреть и примерить, ещё до того как забрать (оплачен заказ уже при оформлении). Не подошло —
  можно отказаться прямо там, и деньги вернутся. Если уже забрал посылку — вернуть нельзя.
  В других пунктах (СДЭК, Яндекс, Почта, 5Post и т.д.) примерить нельзя. Хочет примерить — пусть выбирает пункт выдачи Авито.
- Заказ оформляется через кнопку на Авито; когда человек решил брать, скажи, что можно заказывать, и когда отправим.
- Если в переписке уже есть системное сообщение «У вас новый заказ» — заказ уже оформлен и оплачен. Не предлагай оформить
  заказ (ни снова, ни «ещё раз»), не обещай ничего про статус заказа или списание денег — такие вопросы отдавай менеджеру.
- Про оплату, доставку, примерку и возврат говори только то, что написано здесь. Чего здесь нет — не додумывай, отдай менеджеру.

ИНСТРУМЕНТЫ
- product_photo — отправить фото этих джинсов на человеке, по одному: каждый вызов шлёт следующее фото. Если просят фото —
  вызови ОДИН раз и коротко подпиши («Вот так сидят»). Просят ещё фото — снова вызови один раз. Если инструмент ответил,
  что все фото уже отправлены, вызови escalate.
- chart — отправить картинку размерной сетки. Вызывай ТОЛЬКО если покупатель сам просит сетку, замеры, длину или обхваты;
  если он просто спрашивает размер — сетку не отправляй.
- product_photo и chart — только по прямой просьбе покупателя, ничего «для удобства» сверх того, что просили.
  Если фото или сетку уже отправляли в этом чате (в переписке видно «[отправлены фото]» / «[отправлена сетка]» или «[фото]»),
  второй раз не отправляй, пока снова не попросят.
- order_placed — покупатель пишет, что уже оформил / оплатил заказ: вызови его. Если размер уже есть в переписке выше —
  укажи size, если нет — вызови без size; покупателя про размер не переспрашивай.
  Менеджер получит заказ, а чат дальше ведёшь ты: ответь на его вопросы, а если вопросов нет — коротко поблагодари за заказ.
  Если он только собирается заказать — это не заказ, отвечай как обычно. Если в переписке уже есть «У вас новый заказ»
  или ты уже вызывал order_placed — второй раз не вызывай.
- escalate — передать чат менеджеру (см. ниже). После escalate покупателю ничего не пиши.
- do_nothing — диалог окончен и помогать ничем не нужно: покупатель благодарит, прощается, соглашается («ок», «хорошо»,
  «понял»), пишет, что подумает или напишет / закажет позже («минутку, я вам чуть позже напишу»), и ни о чём не спрашивает.
  Тогда вызови do_nothing и ничего не пиши: не отвечай «хорошо, пишите», «пожалуйста», не повторяй «можете заказывать».
  Если в сообщении есть вопрос или просьба — это не конец диалога, ответь на них. Исключение: покупатель пишет, что
  оформил заказ, — вызови order_placed и коротко поблагодари.

КОГДА ОТДАТЬ МЕНЕДЖЕРУ
Если вопрос не покрывается фактами выше (обмен или возврат уже купленного, жалоба, статус уже оформленного
заказа или трек-номер, встреча лично, другие товары, покупатель спрашивает, не бот ли ты, что-то непонятное),
вызови escalate и коротко укажи причину.
Лучше отдать менеджеру, чем угадать."""

STYLE_EXAMPLES = """
ПРИМЕРЫ ТОНА (не копируй дословно, это для понимания стиля)
Покупатель: Здравствуйте, какой размер мне взять?  →  Здравствуйте! Напишите рост и вес, подберу размер.
Покупатель: рост 180 вес 70  →  На ваш рост и вес подойдёт 32 (M). Можете заказывать, [день из «ОТПРАВКА»] отправлю 🤗
Покупатель: а они тянутся?  →  Немного тянутся, ткань почти полностью хлопок.
Покупатель: хорошо, беру  →  Отлично, жду заказ 🤗 (если «можете заказывать» в переписке ещё не было — «Отлично, можете заказывать, [день из «ОТПРАВКА»] отправлю 🤗»)
Покупатель: минутку, я вам чуть позже напишу  →  (вызови do_nothing, ничего не пиши)
Покупатель: спасибо  →  (вызови do_nothing, ничего не пиши)"""

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def ship_line(now=None):
    """Last block of the system prompt: Moscow time now and when an order placed now ships (code, not the model).
    At the very end so everything before it stays the same between answers (prompt cache)."""
    t = now if now is not None else time.time() + _facts.CLOCK_OFFSET
    msk = time.gmtime(t + 3 * 3600)
    day = "сегодня" if ships_today(now) else "завтра"
    return (f"\n\nОТПРАВКА\nСейчас {WEEKDAYS[msk.tm_wday]}, {msk.tm_hour:02d}:{msk.tm_min:02d} по Москве. "
            f"Если оформят сейчас — отправим {day}.")

def table_text(prod):
    """The product's size table as «size | label | col…» rows (catalog sizeTable; label from the size scale)."""
    if not prod or not prod["table"]:
        return "(нет таблицы)"
    return "\n".join("  " + r for r in table_lines(prod["table"], prod.get("labels") or SIZE_LABEL))


def known_hw(msgs):
    """(height, weight) the buyer gave anywhere in the chat; None for what's not known."""
    buyer = "\n".join(m["text"] or "" for m in msgs if m["who"] == "buyer")
    nums = merged_nums(buyer.split("\n")[-1] if buyer else "", buyer)
    own = extract(buyer)
    for got in (nums, own):
        if got.get("height") and got.get("weight"):
            return got["height"], got["weight"]
    return nums.get("height") or own.get("height"), nums.get("weight") or own.get("weight")


def with_hw_ask(reply, msgs, prod):
    """Add «Напишите свой рост и вес…» after the answer (or only the missing one: «…ваш вес…» when height is known),
    once per chat: only if the buyer hasn't given height and weight, nobody from the shop already asked for whatever's
    still missing (just «а вес какой?» counts when height is known), the item is in stock and not ordered yet."""
    if prod and not prod.get("in_stock", True):
        return reply
    h, w = known_hw(msgs)
    if h and w:
        return reply
    if any(m["who"] == "system" and "новый заказ" in (m["text"] or "").lower() for m in msgs):
        return reply
    missing = [word for word, have in (("рост", h), ("вес", w)) if not have]
    # «рост», «роста», «вес», «весе»… but not «весь» / «ростовка»
    asked = lambda t: all(re.search(r"\b" + word + r"(а|е|у|ом)?\b", (t or "").lower()) for word in missing)
    if asked(reply) or any(m["who"] in ("bot", "shop") and asked(m["text"]) for m in msgs):
        return reply
    ask = ASK_BODY if len(missing) == 2 else f"Напишите, пожалуйста, ваш {missing[0]}, я вам помогу подобрать размер"
    return f"{reply}\n\n{ask}" if reply else ask


def size_hint(prod, msgs):
    """Size from the manager app's height x weight table (nearest height and weight), if the buyer gave both anywhere
    in the chat."""
    h, w = known_hw(msgs)
    if not (h and w):
        return "нет (рост и вес покупатель ещё не написал)"
    if prod and prod.get("hw") and hw_outside(prod["hw"], h, w):
        return (f"рост {h}, вес {w} — за пределами таблицы размеров магазина: размер НЕ называй и не предлагай, "
                "вызови escalate (менеджер подберёт сам)")
    n = hw_size(prod["hw"], h, w) if prod and prod.get("hw") else None
    if not n:
        return f"рост {h}, вес {w} → размер не определён, отдай менеджеру"
    size = (prod.get("labels") or SIZE_LABEL).get(n) or str(n)
    gone = prod.get("out_sizes") or []
    have = prod.get("in_sizes")
    out = n in gone or (have is not None and n not in have)
    return f"рост {h}, вес {w} → размер {size}" + (" — но ЭТОГО РАЗМЕРА СЕЙЧАС НЕТ В НАЛИЧИИ" if out else "")


AVITO_DISCOUNT = re.compile(r"ознакомился со скидкой\s+([\d\s\u00a0]+)\s*₽", re.I)


def rub(n):
    return f"{n:,}".replace(",", "\u00a0") + " ₽"


def price_text(price, msgs):
    """The listing price, and the buyer's own price if Avito gave him a discount in this chat (latest one wins)."""
    if not price:
        return "см. объявление"
    found = [AVITO_DISCOUNT.search(m["text"] or "") for m in msgs if m["who"] == "system"]
    found = [f for f in found if f]
    digits = re.sub(r"\D", "", price)
    if not found or not digits:
        return price
    off = int(re.sub(r"\D", "", found[-1].group(1)) or 0)
    if not off or off >= int(digits):
        return price
    return (f"{price} в объявлении, но Авито дало этому покупателю скидку {rub(off)}: для него цена {rub(int(digits) - off)} "
            f"— называй её")


# The prompt text lives in avito-bot/prompt.txt, so a manager can change it in the manager app («Промпт» tab); it is read
# again whenever the file changes. {name} = an insert the code fills for every answer (INSERTS below); any other text is
# sent as written. No file, or an unreadable one -> the built-in DEFAULT_PROMPT.
PROMPT_FILE = HERE / "prompt.txt"
DEFAULT_PROMPT = PROMPT + STYLE_EXAMPLES + "\n\n{ship}"
INSERT = re.compile(r"\{([a-z_]+)\}")
# name -> (short title, where the value comes from); the manager app shows this list next to the prompt
INSERTS = {
    "shop": ("Кто ты (магазин)", "«продавец магазина <название> на Авито», если название указали при установке "
                                 "(deploy.sh --shop-name, shop.json); без названия — «продавец на Авито»."),
    "title": ("Название товара", "Заголовок объявления, из которого пишет покупатель."),
    "price": ("Цена", "Цена из объявления (обновляется каждые 10 секунд). Если Авито дало покупателю свою скидку, "
                      "здесь цена со скидкой и подсказка назвать её."),
    "material": ("Ткань", "Короткий ответ про материал: «Товары» → товар → «Материал»."),
    "composition": ("Состав", "Полный состав: «Товары» → товар → «Материал»."),
    "density": ("Тянутся ли", "Плотность / тянутся ли: «Товары» → товар → «Материал»."),
    "stock": ("Наличие (и выход)", "Строка про наличие: все размеры / кроме … / модели нет («Товары» → «В наличии»). "
                                   "Если покупатель спрашивает про выход штанины — ещё строка с точным ответом про выход."),
    "table": ("Таблица размеров", "Размерная сетка товара: «Товары» → товар → «Таблица размеров»."),
    "size_hint": ("Подсказка по размеру", "Размер по росту и весу из таблицы магазина, если покупатель их написал; "
                                          "иначе «нет, рост и вес неизвестны» и т.п."),
    "weekday_cutoff": ("Отправка в будни до", "Время со вкладки «Отправка»."),
    "weekend_cutoff": ("Отправка в выходные до", "Время со вкладки «Отправка»."),
    "ship": ("Когда отправим", "Блок «ОТПРАВКА»: день недели и время по Москве сейчас и «отправим сегодня / завтра». "
                               "Пусто, если товара нет в наличии. Держите его в самом конце: так OpenAI кэширует "
                               "весь текст до него, и ответы дешевле."),
}
_prompt_cache = [None, None]  # [mtime, text]


def prompt_template():
    """The manager's prompt.txt (re-read when it changes), else the built-in one."""
    try:
        mt = PROMPT_FILE.stat().st_mtime
        if _prompt_cache[0] != mt:
            text = PROMPT_FILE.read_text(encoding="utf-8")
            _prompt_cache[:] = [mt, text if text.strip() else None]
        return _prompt_cache[1] or DEFAULT_PROMPT
    except OSError:
        return DEFAULT_PROMPT


def fill(template, values):
    """Put the inserts in; {unknown} and any other braces stay as written (no str.format: a manager's text may have {})."""
    return INSERT.sub(lambda m: values.get(m.group(1), m.group(0)), template).rstrip()


def insert_values(title, price, msgs, now=None, prod=None):
    prod = prod or product_for_item(title)
    return {
        "shop": f"продавец магазина {shop_name()} на Авито" if shop_name() else "продавец на Авито", "title": title or "?", "price": price_text(price, msgs),
        "material": (prod or {}).get("material") or "нет данных",
        "composition": (prod or {}).get("composition") or "нет данных",
        "density": (prod or {}).get("density") or "нет данных", "table": table_text(prod), "size_hint": size_hint(prod, msgs),
        "stock": stock_line(prod) + leg_line(prod, msgs),
        "weekday_cutoff": "%d:%02d" % cutoffs()[0], "weekend_cutoff": "%d:%02d" % cutoffs()[1],
        "ship": "" if prod and not prod.get("in_stock", True) else ship_line(now).lstrip("\n")}


def system_prompt(title, price, msgs, now=None):
    return fill(prompt_template(), insert_values(title, price, msgs, now))


def buyer_block(msgs):
    """The buyer's latest messages (not answered yet) as one text."""
    block = []
    for m in reversed(msgs):
        if m["who"] != "buyer":
            break
        block.insert(0, m["text"] or "")
    return "\n".join(block)


def leg_line(prod, msgs):
    """«Какой выход?»: the bot's own answer (size named -> that size's value from the table; no size -> the product's
    description.legOpening), given to GPT as a fact so the numbers are always right."""
    if not prod:
        return ""
    ans = leg_answer(buyer_block(msgs), prod["key"])
    if ans:
        return ("\n- ВЫХОД (ширина низа штанины): покупатель сейчас спрашивает про выход. Ответь по смыслу ровно так: «" + ans +
                "» Цифры именно эти; сетку из-за этого вопроса не отправляй.")
    if prod.get("leg_opening"):
        return ("\n- ВЫХОД (ширина низа штанины): если спрашивают выход без размера, отвечай по смыслу так: «" + prod["leg_opening"] +
                "» Если спрашивают выход конкретного размера — назови его из таблицы.")
    return ""


IN_STOCK_LINE = "- Размеры в наличии: все из таблицы ниже."
OUT_OF_STOCK_LINE = ("- НАЛИЧИЕ: этой модели сейчас ВРЕМЕННО НЕТ В НАЛИЧИИ. Если спрашивают, есть ли она, можно ли заказать или когда "
                     "отправим — коротко скажи, что модели временно нет в наличии. Не предлагай заказывать, не пиши «можете "
                     "заказывать» и не называй день отправки. На вопросы о размере, ткани и посадке "
                     "отвечай как обычно, только без предложения заказать.")


def stock_line(prod):
    """What is in stock, for GPT. A product with sizes -> the exact in-stock list «23 (S), 24 (M)» and «no other sizes»
    (before: «all from the table EXCEPT …» — with no size table GPT took any size as available, 2026-11-25).
    No product = in stock, as before."""
    if prod and not prod.get("in_stock", True):
        return OUT_OF_STOCK_LINE
    if prod and prod.get("sizes"):
        have = prod.get("in_sizes") or []
        if not have:
            return OUT_OF_STOCK_LINE
        label = lambda n: (prod.get("labels") or {}).get(n) or _products.SIZE_LABEL.get(n, str(n))
        gone = [n for n in prod["sizes"] if n not in have]
        return ("- РАЗМЕРЫ В НАЛИЧИИ — ТОЛЬКО ЭТИ: " + ", ".join(label(n) for n in have) + ". Других размеров этой модели "
                "сейчас нет" + (f" (в том числе {', '.join(label(n) for n in gone)})" if gone else "") + ". Если покупатель "
                "спрашивает или хочет размер не из этого списка — скажи, что такого размера нет в наличии; заказывать его "
                "не предлагай и про отправку не говори. Другой размер вместо него сам не предлагай. Размер называй так же, "
                "как в списке (число и буквы в скобках).")
    gone = (prod or {}).get("out_sizes") or []
    if gone:
        return ("- Размеры в наличии: все из таблицы ниже, КРОМЕ " + ", ".join(map(str, gone)) + " — этих размеров сейчас "
                "временно нет. Если покупателю подходит или он хочет такой размер — скажи, что его временно нет в наличии; "
                "заказывать его не предлагай и про отправку не говори. Другой размер вместо него сам не предлагай.")
    return IN_STOCK_LINE


def chat_messages(msgs):
    """manager.py conv['msgs'] -> OpenAI turns: buyer = user, shop/sent bot = assistant, system lines as notes."""
    out = []
    for m in msgs:
        if m["who"] == "bot" and (not m.get("sent") or (m["text"] or "").startswith(("→", "(молчит"))):
            continue  # drafts and the bot's own notes («→ передано менеджеру…», «(молчит…)») are not messages
        if m["who"] == "buyer":
            role, text = "user", m["text"]
        elif m["who"] in ("shop", "bot"):
            ps = m.get("photos") or []
            marks = (["[отправлена сетка]"] if any("size_chart" in x for x in ps) else []) + \
                    (["[отправлены фото]"] if any("size_chart" not in x for x in ps) else [])
            role, text = "assistant", " ".join([m["text"]] + marks).strip()
        else:
            role, text = "user", f"[Системное сообщение Авито: {m['text']}]"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text  # several buyer messages in a row = one turn
        else:
            out.append({"role": role, "content": text})
    return out


def build_request(title, price, msgs, now=None, model=None):
    model = model or MODEL
    req = {"model": model, "tools": TOOLS,
           "messages": [{"role": "system", "content": system_prompt(title, price, msgs, now)}] + chat_messages(msgs)}
    if REASONING(model):
        req.update(max_completion_tokens=400, reasoning_effort="none")  # chat/completions allows tools only with reasoning off
    else:
        req.update(temperature=0.7, max_tokens=200)
    return req


def order_after_talk(msgs):
    """A «новый заказ» system message with any buyer/shop/bot message before it."""
    for i, m in enumerate(msgs):
        if m["who"] == "system" and "новый заказ" in m["text"].lower():
            if any(x["who"] in ("buyer", "shop", "bot") for x in msgs[:i]):
                return True
    return False


def cost(usage, model=None):
    p_in, p_cached, p_out = PRICES[model or MODEL]
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return ((usage["prompt_tokens"] - cached) * p_in + cached * p_cached + usage["completion_tokens"] * p_out) / 1e6


def _auth():
    return {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}


def order_size(title, price, msgs, model=None):
    """«У вас новый заказ» arrived: which size did the buyer order? One forced order_placed call over the whole chat.
    -> (size text, cost $)."""
    model = model or MODEL
    req = build_request(title, price, msgs, None, model)
    req["messages"].append({"role": "user", "content": "[Системное сообщение: покупатель оформил заказ. Вызови order_placed "
                                                       "с размером, который он заказал по этой переписке.]"})
    req["tool_choice"] = {"type": "function", "function": {"name": "order_placed"}}
    r = requests.post(URL, headers=_auth(), json=req, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI {r.status_code}: {(r.json().get('error') or {}).get('message', r.text[:300])}")
    d = r.json()
    c_usd = cost(d["usage"], model)
    if on_cost:
        on_cost(c_usd)
    calls = d["choices"][0]["message"].get("tool_calls") or []
    args = json.loads(calls[0]["function"].get("arguments") or "{}") if calls else {}
    return str(args.get("size") or "").strip(), c_usd


def answer(title, price, msgs, now=None, model=None):
    """-> {reply, photos, escalate, cost_usd, tokens}. Up to 3 calls: tools first (pictures / order), then the text."""
    prod = product_for_item(title)
    model = model or MODEL
    req = build_request(title, price, msgs, now, model)
    headers = _auth()
    photos, total, toks, text, ordered = [], 0.0, [0, 0], "", None
    sent = [p for m in msgs if m["who"] in ("bot", "shop") for p in (m.get("photos") or [])]  # photos go one at a time
    for _ in range(3):
        r = requests.post(URL, headers=headers, json=req, timeout=60)
        if r.status_code >= 400:  # show OpenAI's own explanation (it never contains the key)
            raise RuntimeError(f"OpenAI {r.status_code}: {(r.json().get('error') or {}).get('message', r.text[:300])}")
        d = r.json()
        c_usd = cost(d["usage"], model)
        if on_cost:
            on_cost(c_usd)  # every paid call counts, even if a later one in this answer fails
        total += c_usd; toks[0] += d["usage"]["prompt_tokens"]; toks[1] += d["usage"]["completion_tokens"]
        msg = d["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        req["messages"].append(msg)
        for c in calls:
            name = c["function"]["name"]
            if name == "do_nothing":  # the conversation is over: nothing is sent (manager.py: silent turn, no reminder)
                return {"reply": None, "photos": [], "escalate": False, "cost_usd": total, "tokens": toks,
                        "why": "GPT: диалог окончен, отвечать не нужно"}
            if name == "escalate":  # the chat goes to Maxim: nothing is sent to the buyer
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                reason = str(args.get("reason") or "").strip() or "без причины"
                return {"reply": None, "photos": [], "escalate": True, "cost_usd": total, "tokens": toks,
                        "why": f"GPT: {reason}"}
            if name == "order_placed":  # the buyer says he ordered: manager.py sends Maxim the size; the bot keeps the chat
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                ordered = str(args.get("size") or "").strip()
                req["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                        "content": "Менеджер получил заказ и отправит его. Чат дальше ведёшь ты: "
                                                   "ответь на вопросы покупателя, а если их нет — коротко поблагодари за заказ."})
                continue
            if name == "product_photo":
                nxt = next_photo(prod["photos"], sent + photos) if prod else None
                got = [nxt] if nxt else []
                none_left = "Все фото этой модели уже отправлены в этом чате, других нет." if prod and prod["photos"] else \
                            "Картинок для этого товара нет."
            else:
                got = [prod["chart"]] if prod and prod["chart"] else []
                none_left = "Картинок для этого товара нет."
            photos += got
            req["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                    "content": f"Отправлено картинок: {len(got)}. Они придут сразу после твоего сообщения."
                                    if got else none_left})
        if text and all(c["function"]["name"] != "order_placed" for c in calls):
            break  # text is ready and only pictures were added; after order_placed the model writes the text again
    esc = HANDOFF in text
    if not esc and ordered is None and (text or photos):
        text = with_hw_ask(text, msgs, prod)
    out = {"reply": None if esc else text, "photos": [] if esc else photos, "escalate": esc,
           "cost_usd": total, "tokens": toks}
    if ordered is not None:  # order_placed was called; "" = no size in the chat
        out["order_size"] = ordered
    return out


SAMPLE = {"title": "Джинсы клеш черные Zara bootcut LGB flared", "price": "2 990 ₽", "msgs": [
    {"who": "buyer", "text": "Здравствуйте"},
    {"who": "buyer", "text": "рост 178 вес 70 какой размер брать? и можно фото как сидят"}]}

if __name__ == "__main__":
    if "--inserts" in sys.argv:  # the manager app: example insert values for one product, as JSON
        pid = sys.argv[sys.argv.index("--inserts") + 1]
        prod = next((p for p in _products.load_products() if p["id"] == pid), None)
        msgs = [{"who": "buyer", "text": "Здравствуйте, рост 178 вес 70, какой размер?"}]
        vals = insert_values(prod["name"] if prod else "?", "2 990 ₽", msgs, prod=prod)
        sys.stdout.buffer.write(json.dumps({"values": vals, "example_chat": msgs[0]["text"]}, ensure_ascii=False).encode("utf-8"))
    elif "--dry" in sys.argv:
        req = build_request(SAMPLE["title"], SAMPLE["price"], SAMPLE["msgs"])
        print(req["messages"][0]["content"]); print("\n--- turns:", json.dumps(req["messages"][1:], ensure_ascii=False))
        print("--- tools:", [t["function"]["name"] for t in req["tools"]])
    else:
        t = time.time()
        r = answer(SAMPLE["title"], SAMPLE["price"], SAMPLE["msgs"])
        print(json.dumps(r, ensure_ascii=False, indent=1), f"\n{time.time() - t:.1f} s")
