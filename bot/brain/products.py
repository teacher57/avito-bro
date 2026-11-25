"""Product info from the shared data server (avito/data, format: data/schema/FORMAT.md; client: data_client.py).

catalog.json gives the products (name, match words, description, size table, size by height and weight, photos, chart) and
the size scale; availability comes from stock.json (shop.stockSource = "warehouse": HUB BRO's shelf) or from the product's
own «нет в наличии» switches in the manager app (stockSource = "manual"). The manager app and HUB BRO edit the data while
the bot runs, so `refresh()` polls the server's revisions (every 30 s; the bot's main loop calls it) and reloads when anything
changed. PRODUCTS / BY_KEY / SIZE_LABEL are updated IN PLACE, so `from products import PRODUCTS` keeps working.

Server down -> the last good copy (live/data_cache/, pictures in live/files/); no copy at all -> no products, and every
model counts as in stock (as for an unknown product), so buyers are never refused because of the server.
The old products/<id>/*.txt folders are a backup only: the bot no longer reads them."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # data_client.py lives in the bot's folder
import data_client as dc  # noqa: E402

# Used only when there is no catalog at all (no server, no local copy): the scale the bot had built in before.
DEFAULT_SCALE = [("25", "2XS"), ("26", "XS"), ("27", "XS"), ("28", "S"), ("29", "S"), ("30", "M"), ("31", "M"),
                 ("32", "M"), ("33", "L"), ("34", "L"), ("36", "XL")]


def hw_size(hw, height, weight):
    """Size number from a size-by-height-and-weight grid: the last band that starts at or below the value (None = empty cell)."""
    hi = max((i for i, h in enumerate(hw["heights"]) if h <= height), default=None)
    wi = max((i for i, w in enumerate(hw["weights"]) if w <= weight), default=None)
    return None if hi is None or wi is None else hw["cells"][hi][wi]


def next_photo(prod_photos, sent):
    """Photos go one at a time: the first of this product's photos not sent in this chat yet (None = all sent).
    `sent` = file names (or paths) of pictures already sent in the chat."""
    names = {Path(x).name for x in sent}
    return next((p for p in prod_photos if Path(p).name not in names), None)


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def scale_of(catalog):
    """[(size, label)] from catalog.sizeScale, in the shop's order (DEFAULT_SCALE if the catalog has none)."""
    scale = [(str(s.get("size")), s.get("label") or "") for s in (catalog or {}).get("sizeScale") or []
             if isinstance(s, dict) and _int(s.get("size")) is not None]
    return scale or list(DEFAULT_SCALE)


def size_labels(catalog):
    """{30: "30 (M)", …} (a size with no letter: just «35»)."""
    return {int(s): (f"{s} ({lab})" if lab else s) for s, lab in scale_of(catalog)}


def read_hw(h):
    """catalog sizeHW -> {"heights": [..], "weights": [..], "cells": [[int | None]]} or None (sizes as numbers in memory)."""
    if not isinstance(h, dict) or not h.get("heights") or not h.get("weights"):
        return None
    try:
        cells = [[_int(c) if c is not None else None for c in row] for row in h.get("cells") or []]
        return {"heights": [int(x) for x in h["heights"]], "weights": [int(x) for x in h["weights"]], "cells": cells}
    except (TypeError, ValueError):  # broken data never stops the bot: it just falls back to the old size rules
        return None


def read_table(t, scale_order=None):
    """catalog sizeTable -> {"columns": [key, …], "rows": {size number: {key: number}}} (rows in the scale's order) or None."""
    if not isinstance(t, dict) or not isinstance(t.get("rows"), dict):
        return None
    cols = [c["key"] for c in t.get("columns") or [] if isinstance(c, dict) and c.get("key")]
    order = {s: i for i, s in enumerate(scale_order or [])}
    sizes = sorted((s for s in t["rows"] if _int(s) is not None), key=lambda s: (order.get(s, len(order)), int(s)))
    rows = {int(s): {k: v for k, v in (t["rows"][s] or {}).items() if isinstance(v, (int, float))} for s in sizes}
    return {"columns": cols, "rows": rows} if rows else None


def num_text(v):
    """20 -> «20», 20.5 -> «20.5» (as the numbers were written in the old size_table.txt)."""
    return str(int(v)) if float(v) == int(v) else str(v)


def table_lines(table, labels):
    """The size table as text rows: «size | label | col…» then one row per size (what GPT gets as the raw table)."""
    if not table:
        return []
    letter = lambda n: labels.get(n, str(n)).partition("(")[2].rstrip(")")
    out = [["size", "label"] + table["columns"]]
    for n, row in table["rows"].items():
        out.append([str(n), letter(n)] + [num_text(row[c]) if c in row else "" for c in table["columns"]])
    return [" | ".join(r) for r in out]


def availability(prod, stock, shop):
    """(in_stock, out_sizes) for one catalog product (FORMAT.md «Наличие»).
    stockSource "warehouse": a size is in stock when the shelf has it (sum of stock.products[id][size].stock[].qty > 0);
    "manual": availability.inStock and the size not in availability.outSizes. Archived -> nothing in stock.
    No stock data at all (warehouse mode, no server and no copy) -> in stock, as for an unknown product."""
    sizes = [s for s in prod.get("sizes") or [] if _int(s) is not None]
    if (shop or {}).get("stockSource") == "warehouse":
        if stock is None:
            have = set(sizes)
            any_in = True
        else:
            shelf = (stock.get("products") or {}).get(prod["id"]) or {}
            have = {s for s, v in shelf.items() if sum(x.get("qty", 0) for x in (v or {}).get("stock") or []) > 0}
            any_in = bool(have)
    else:
        av = prod.get("availability") or {}
        on = av.get("inStock", True) is not False
        out = set(av.get("outSizes") or [])
        have = {s for s in sizes if s not in out} if on else set()
        any_in = bool(have) if sizes else on
    if prod.get("archived"):
        have, any_in = set(), False
    return any_in, sorted(int(s) for s in sizes if s not in have)


def build(catalog, stock=None, shop=None):
    """[{id, name, key, match, in_stock, out_sizes, material, composition, density, photos, chart, table, hw,
    leg_opening}] in id order (the old folder-name order). Photos / chart = local paths of the downloaded files."""
    scale = [s for s, _ in scale_of(catalog)]
    items = []
    for p in sorted((catalog or {}).get("products") or [], key=lambda x: str(x.get("id"))):
        if not isinstance(p, dict) or not p.get("id"):
            continue
        d = p.get("description") or {}
        in_stock, out = availability(p, stock, shop)
        photos = [str(dc.local_file(f)) for f in p.get("photos") or [] if dc.local_file(f).is_file()]
        chart = p.get("chart")
        items.append({
            "id": p["id"], "name": p.get("name") or p["id"], "key": p.get("botKey") or p["id"],
            "match": [w.strip().lower() for w in p.get("match") or [] if w and w.strip()],
            "in_stock": in_stock, "out_sizes": out,
            # the product's sizes in its own order and the ones in stock now (ints): GPT gets the in-stock list as is,
            # the size guard treats any other size (also one the product doesn't have at all) as not available
            "sizes": [n for n in (_int(s) for s in p.get("sizes") or []) if n is not None],
            "in_sizes": [n for n in (_int(s) for s in p.get("sizes") or []) if n is not None and n not in out]
                        if in_stock else [],
            "material": d.get("material") or None, "composition": d.get("composition") or None,
            "density": d.get("density") or None,
            "photos": photos,
            "chart": str(dc.local_file(chart)) if chart and dc.local_file(chart).is_file() else None,
            "table": read_table(p.get("sizeTable"), scale),
            "hw": read_hw(p.get("sizeHW")),
            "leg_opening": d.get("legOpening") or None,
        })
    return items


def file_names(catalog):
    """Every picture the catalog refers to (photos and charts)."""
    out = []
    for p in (catalog or {}).get("products") or []:
        out += list(p.get("photos") or []) + ([p["chart"]] if p.get("chart") else [])
    return out


# ---------------------------------------------------------------- live data (refreshed in place)

PRODUCTS = []
BY_KEY = {}
SIZE_LABEL = {}   # {25: "25 (2XS)", …}: the shop's size scale, from catalog.sizeScale
DATA = {"catalog": None, "stock": None, "shop": None}  # the raw JSON values the products were built from
ON_RELOAD = []  # callbacks run after a reload (fit.py re-reads its size tables, brain.py rebuilds its size patterns)
_state = {"revs": {}, "checked": 0.0, "online": False}


def _apply(check_files=False):
    dc.sync_files(file_names(DATA["catalog"]), check=check_files)
    fresh = build(DATA["catalog"], DATA["stock"], DATA["shop"])
    SIZE_LABEL.clear()
    SIZE_LABEL.update(size_labels(DATA["catalog"]))
    PRODUCTS[:] = fresh
    BY_KEY.clear()
    BY_KEY.update({p["key"]: p for p in fresh})


def load_products():
    """Read catalog / stock / shop (server, else the local copy) and return the product list (no in-place update)."""
    data = {k: dc.load(k)[0] for k in DATA}
    dc.sync_files(file_names(data["catalog"]))
    return build(data["catalog"], data["stock"], data["shop"])


def _initial():
    try:
        _state["revs"] = dc.revs()
        _state["online"] = True
    except dc.DataError:
        _state["online"] = False
    for k in DATA:
        DATA[k] = dc.load(k)[0] if _state["online"] else dc.cached(k)[0]
    _state["checked"] = time.time()
    _apply(check_files=_state["online"])


def refresh(every=30):
    """Reload when the server's catalog / stock / shop / files changed (checked at most once per `every` seconds).
    True when it reloaded. Server down -> keeps what it has (False)."""
    now = time.time()
    if now - _state["checked"] < every:
        return False
    _state["checked"] = now
    try:
        revs = dc.revs()
    except dc.DataError:
        _state["online"] = False
        return False
    old = _state["revs"]
    was_online = _state["online"]
    changed = [k for k in DATA if not was_online or revs.get(k) != old.get(k)]
    files_changed = not was_online or revs.get("files") != old.get("files")
    if not changed and not files_changed:
        return False
    try:
        for k in changed:
            DATA[k] = dc.get(k)[0]
    except dc.DataError:
        return False  # try again next time
    _state["revs"], _state["online"] = revs, True
    _apply(check_files=files_changed)
    for fn in ON_RELOAD:
        fn()
    return True


_initial()


def product_for_item(title):
    """Product whose match words appear in the listing title (None if unknown)."""
    t = (title or "").lower()
    for p in PRODUCTS:
        if any(w in t for w in p["match"]):
            return p
    return None


def in_stock(model_key):
    """False only when the product is known and out of stock (warehouse shelf empty / «нет в наличии» / archived)."""
    p = BY_KEY.get(model_key)
    return True if p is None else p["in_stock"]


def in_sizes(model_key):
    """Sizes of this product in stock now (ints, product order); None = unknown product or a product without sizes."""
    p = BY_KEY.get(model_key)
    return list(p["in_sizes"]) if p and p.get("sizes") else None


def out_sizes(model_key):
    """Sizes of this product that are out of stock now (empty for unknown products)."""
    p = BY_KEY.get(model_key)
    return list(p.get("out_sizes") or []) if p else []
