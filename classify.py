"""Rough keyword-based tally of buyer question types in data/chats.json (categories picked by reading the chats)."""
import json, re
from collections import Counter

CATS = {
    "Size advice (height/weight → size)": r"рост|\b1[5-9]\d\s*[/,\- ]\s*\d{2}\b|какой размер|размер подойд|подбер|подобра|помогите с размер|сайз",
    "Measurements / size chart":          r"замер|сетк|таблиц|мерк|полуобхват|обхват|длин|выход|ширин",
    "Photos on a person / fit":           r"фото|фотк|сидят|сидит|силят|на человеке|на модели|посадк|фит|fit",
    "Fit worries (too long/wide/tight)":  r"широк|длинн|по полу|пола касаются|облип|обтяг|оверсайз|свободн|висел|мешок|подши|складк",
    "Material / density / warmth":        r"материал|состав|плотн|тонк|тянут|тянуч|эласти|тепл|холодн|градус|деним|унци",
    "In stock? / which sizes exist":      r"в наличи|еще продае|ещё продае|ещё продаются|еще в наличии|есть размер|есть \w* ?размер|есть [xsmlх]+\b|есть м\b|есть s\b|есть 3\d",
    "Delivery: method, city, cost, time": r"доставк|сдэк|сдек|5 ?post|почт|яндекс|озон|отправ\w* в|в [А-Я]\w+ (сможете|отправ)|придут|придет|приедут|идти будет|срок",
    "Try-on & returns":                   r"пример|возврат|вернуть|верну",
    "Payment (on delivery, upfront)":     r"оплат|постоплат|при получени|перевод",
    "How to order on Avito":              r"как заказ|как оформ|как выбрать размер|через что|по этому объявлению|это объявление|не да[её]т заказ|не могу заказ|можно заказ|могу оформлять|тут заказать",
    "Order status (sent yet? when?)":     r"отправил|отправили|отправишь|отправите|когда отправ|отменил",
    "After delivery: didn't fit / complaint": r"не подош|малы|маленьк\w* они|велики|большие|не так|обманы|не сели|узкие очень|не подходят",
    "Reviews":                            r"отзыв",
}

if __name__ == "__main__":  # report only when run directly; importing just loads CATS
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "avito-bot/data/chats.json"
    d = [c for c in json.load(open(path)) if c["buyer_id"] != TEST_BUYER_ID]   # skip the test chat
    tally, examples = Counter(), {}
    for c in d:
        text = " | ".join(m["text"] for m in c["messages"] if m["from"] == "buyer" and m["type"] == "text").lower()
        for name, rx in CATS.items():
            if re.search(rx, text):
                tally[name] += 1
    print(f"chats analysed: {len(d)} (latest {sum(c['group']=='latest' for c in d)}, random {sum(c['group']=='random' for c in d)})")
    for name, n in tally.most_common():
        print(f"{n:4d}  {100*n/len(d):3.0f}%  {name}")
