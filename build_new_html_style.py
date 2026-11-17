"""Shared look for the render pages (colors, class names in Russian, escalation reasons)."""
CSS = """:root{--bg:#12151b;--card:#1b2029;--ink:#e7eaf0;--muted:#97a1b3;--line:#2c3440;--buyer:#252c38;--shop:#1f3a2b;--pred:#1d2a44;--sys:#2a2433}
body{max-width:820px;margin:0 auto;background:var(--bg);color:var(--ink);font:13px/1.45 "Helvetica Neue",Arial,sans-serif;padding:14px}
header{padding:0 0 10px;border-bottom:2px solid var(--line);margin-bottom:12px}
h1{margin:0 0 6px;font-size:18px} .muted{color:var(--muted);font-size:12px}
.stats{display:flex;gap:22px;margin:6px 0;font-size:14px} .stats b{font-size:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.card h3{margin:0 0 8px;font-size:14px} .card h3 small{color:var(--muted);font-weight:400}
.row{display:flex;margin:2px 0} .row.buyer{justify-content:flex-start} .row.shop{justify-content:flex-end}
.bubble{max-width:78%;padding:4px 8px;border-radius:9px;background:var(--buyer);white-space:pre-wrap;word-wrap:break-word}
.row.shop .bubble{background:var(--shop)}
.sys{margin:6px auto;max-width:90%;text-align:center;font-size:11px;color:var(--muted);background:var(--sys);border-radius:8px;padding:4px 8px}
.pred{margin:3px 0 8px 0;max-width:78%;background:var(--pred);border-radius:6px;padding:4px 8px;font-size:12px}
.pred.good{border:1px solid #2c4a37;background:#17241d} .pred.bad{border:1px solid #4a2c30;background:#261a1c}
.cls{font-weight:700;color:#93c5fd} .pics img{height:120px;margin:4px 4px 0 0;border-radius:6px} .esc{color:#fbbf24;font-weight:700}
"""

RU = {"Size advice (height/weight → size)": "Подбор размера", "Measurements / size chart": "Замеры / размерная сетка",
      "Photos on a person / fit": "Фото на человеке", "In stock? / which sizes exist": "Наличие",
      "Fit worries (too long/wide/tight)": "Сомнения по посадке", "Try-on & returns": "Примерка и возврат", "Personal meeting": "Личная встреча",
      "Delivery: method, city, cost, time": "Доставка", "Order status (sent yet? when?)": "Статус заказа",
      "Payment (on delivery, upfront)": "Оплата", "After delivery: didn't fit / complaint": "После получения: жалоба",
      "Material / density / warmth": "Материал", "How to order on Avito": "Как оформить заказ", "Reviews": "Отзывы",
      "Other": "Другое", "Price": "Цена", "Shipping (paid, ready to send)": "Shipping (оплачено)"}
WHY = [("no approved template", "нет заготовки"), ("buyer decided", "клиент решил заказать"),
       ("new order / paid", "новый заказ / оплата"), ("asks about sending", "вопрос про отправку"),
       ("return / exchange", "возврат / обмен / примерка"), ("discount", "скидка / торг"),
       ("price: discount or no card price", "нет цены в карточке"), ("height and weight already known", "рост и вес уже есть, выбор за менеджером"),
       ("measurements tight", "замеры не сходятся (тесно)"), ("measurements loose", "замеры не сходятся (свободно)"),
       ("informal wording", "неформальная формулировка"), ("no size", "размер не определён"),
       ("personal meeting", "личная встреча"), ("oversize: no bigger size", "оверсайз: больше размера нет"),
       ("asks to order", "клиент хочет заказать"),
       ("measurements and height/weight", "обхваты и рост/вес дают разные размеры"),
       ("asks if this is a bot", "клиент спрашивает, не бот ли это"), ("ships from Moscow", "отправка из Москвы"), ("GPT: [МАКСИМ]", "GPT решил отдать менеджеру"),
       ("voice message", "голосовое сообщение"), ("media from buyer", "клиент прислал фото / голосовое / файл"), ("about to order", "клиент собирается заказать"), ("no size rule", "размер не определён (проверьте рост/вес)")]
def why_ru(w):
    for k, v in WHY:
        if w.startswith(k) or k in w:
            return v
    return w

