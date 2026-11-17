"""Product info from avito-bot/products/<id>/ (one folder per product, see products/README.md).

The manager app edits these files while the bot runs, so `refresh()` re-reads them when anything in products/ changed
(the bot's main loop calls it). PRODUCTS / BY_KEY are updated IN PLACE, so `from products import PRODUCTS` keeps working."""
import time
from pathlib import Path

PRODUCTS_DIR = Path(__file__).parent.parent / "products"
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp")
NO = {"нет", "no", "false", "0", "-"}


def _fields(path):
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    return out


def _material(path):
    """material.txt: first line = what the bot says by default («хлопок»);
    optional line «состав: 98% хлопок, 2% полиэстер» = full composition, said only when the buyer asks again;
    optional line «плотность: немного тянутся» = the answer to «плотные?», «тянутся?»."""
    if not path.is_file():
        return {"material": None, "composition": None, "density": None}
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    field = lambda name: next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith(name + ":")), None)
    comp = field("состав")
    short = next((l for l in lines if ":" not in l), comp)
    return {"material": short, "composition": comp, "density": field("плотность")}


def read_hw(path):
    """size_hw.txt: size by height and weight, a grid (TAB-separated). Header row: «рост\\вес» + weight band starts (kg);
    each next row: height band start (cm) + one size number per weight band; empty cell = no size (Maxim answers).
    -> {"heights": [..], "weights": [..], "cells": [[int | None]]} or None."""
    if not path.is_file():
        return None
    heights, weights, cells = [], None, []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [x.strip() for x in line.split("\t")]
            if weights is None:
                weights = [int(x) for x in parts[1:] if x]
                continue
            heights.append(int(parts[0]))
            row = [int(x) if x.isdigit() else None for x in parts[1:1 + len(weights)]]
            cells.append(row + [None] * (len(weights) - len(row)))
    except ValueError:  # a broken file never stops the bot: it just falls back to the old size rules
        return None
    return {"heights": heights, "weights": weights, "cells": cells} if heights and weights else None


def hw_size(hw, height, weight):
    """Size number from a size_hw grid: the last band that starts at or below the value (None = empty cell)."""
    hi = max((i for i, h in enumerate(hw["heights"]) if h <= height), default=None)
    wi = max((i for i, w in enumerate(hw["weights"]) if w <= weight), default=None)
    return None if hi is None or wi is None else hw["cells"][hi][wi]


def _sizes(value):
    """«30, 31» -> [30, 31] (anything that is not a number is ignored)."""
    return sorted({int(x) for x in value.replace(";", ",").split(",") if x.strip().isdigit()})


def _text(path):
    """Whole file as one line of text (None if missing/empty), e.g. leg_opening.txt: the bot's answer to «какой выход?»."""
    if not path.is_file():
        return None
    return " ".join(l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")) or None


def next_photo(prod_photos, sent):
    """Photos go one at a time: the first of this product's photos not sent in this chat yet (None = all sent).
    `sent` = file names (or paths) of pictures already sent in the chat."""
    names = {Path(x).name for x in sent}
    return next((p for p in prod_photos if Path(p).name not in names), None)


def load_products():
    """[{id, dir, name, key, match, in_stock, out_sizes, material, photos, chart, table, hw, leg_opening}] in folder-name order."""
    items = []
    for d in sorted(p for p in PRODUCTS_DIR.iterdir() if p.is_dir()) if PRODUCTS_DIR.is_dir() else []:
        f = _fields(d / "product.txt")
        mat = d / "material.txt"
        photos = d / "photos"
        items.append({
            "id": d.name, "dir": d, "name": f.get("name", d.name), "key": f.get("key", d.name),
            "match": [w.strip().lower() for w in f.get("match", "").split(",") if w.strip()],
            "in_stock": f.get("in_stock", "да").strip().lower() not in NO,  # no line = in stock (old files)
            "out_sizes": _sizes(f.get("out_sizes", "")),  # sizes switched off in the manager app («нет в наличии»)
            **_material(mat),
            "photos": sorted(str(x) for x in photos.iterdir() if x.suffix.lower() in IMAGE_EXT) if photos.is_dir() else [],
            "chart": str(d / "size_chart.png") if (d / "size_chart.png").is_file() else None,
            "table": d / "size_table.txt" if (d / "size_table.txt").is_file() else None,
            "hw": read_hw(d / "size_hw.txt"),
            "leg_opening": _text(d / "leg_opening.txt"),
        })
    return items


def _signature():
    """Changes whenever a file in products/ is added, removed or edited."""
    if not PRODUCTS_DIR.is_dir():
        return ()
    return tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in PRODUCTS_DIR.rglob("*") if p.is_file()))


PRODUCTS = load_products()
BY_KEY = {p["key"]: p for p in PRODUCTS}
ON_RELOAD = []  # callbacks run after a reload (fit.py re-reads its size tables)
_state = {"sig": _signature(), "checked": time.time()}


def refresh(every=5):
    """Re-read products/ if it changed (checked at most once per `every` seconds). True when it reloaded."""
    now = time.time()
    if now - _state["checked"] < every:
        return False
    _state["checked"] = now
    try:
        sig = _signature()
        if sig == _state["sig"]:
            return False
        fresh = load_products()
    except OSError:  # a file is being written right now: try again next time
        return False
    _state["sig"] = sig
    PRODUCTS[:] = fresh
    BY_KEY.clear()
    BY_KEY.update({p["key"]: p for p in fresh})
    for fn in ON_RELOAD:
        fn()
    return True


def product_for_item(title):
    """Product whose match words appear in the listing title (None if unknown)."""
    t = (title or "").lower()
    for p in PRODUCTS:
        if any(w in t for w in p["match"]):
            return p
    return None


def in_stock(model_key):
    """False only when the product is known and marked «нет в наличии» in the manager app."""
    p = BY_KEY.get(model_key)
    return True if p is None else p["in_stock"]


def out_sizes(model_key):
    """Sizes of this product switched off one by one in the manager app (empty for unknown products)."""
    p = BY_KEY.get(model_key)
    return list(p.get("out_sizes") or []) if p else []
