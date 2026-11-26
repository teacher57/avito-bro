"""Reads and writes the shop's data on the shared data server (avito/data/server; format: avito/data/schema/FORMAT.md),
through the bot's own data_client.py, so the running bot (and HUB BRO, if the shop has it) picks the changes up by itself:

  catalog.json   products: name, match words, «нет в наличии» switches (availability), description (material,
                 composition, density), size table, size by height and weight, photos, size chart; the size scale
  shop.json      shipping cutoffs (weekday / weekend), Moscow time; stockSource (warehouse = stock comes from HUB BRO)
  log.json       one line per change made here (source "bot")
  files/         photos (<id>_<n>.<ext>) and size charts (<id>_size_chart.png)
  prompt.txt     GPT's prompt with {inserts}; prompt_history/ keeps the versions before each save (local, as before)

Every write sends the whole file with the server's revision; if someone (HUB BRO, another PC) saved in between, the
change is applied again to the fresh data. Server down: the app shows the bot's last local copy, saving says so.
Sizes are the shop's size scale from catalog.sizeScale."""
import ast
import datetime
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

BOT_DIR = Path(os.environ.get("MANAGER_APP_BOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BOT_DIR))
import data_client as dc  # noqa: E402

PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp")
CHART_EXT = (".png", ".jpg", ".jpeg", ".webp")
MAX_FILE = 15 * 1024 * 1024  # the data server's limit
DEFAULT_CUTOFFS = {"weekday_cutoff": "19:00", "weekend_cutoff": "18:00"}  # same defaults as brain.py
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")  # product id (FORMAT.md)
COLUMN_KEY = re.compile(r"[a-zа-я0-9_]+")  # size table column key (FORMAT.md)

# Column titles for the drawn size chart (same as make_size_charts.py).
HEAD = {"size": "Размер", "label": "", "полуобхват_талии": "Полуобхват талии", "полуталия": "Полуобхват талии",
        "полуобхват_бедер": "Полуобхват бёдер", "длина_по_внеш_шву": "Длина по внешнему шву",
        "длина_по_внутр_шву": "Длина по внутреннему шву", "полуобхват_одного_бедра": "Полуобхват одного бедра",
        "полувыход": "Полувыход", "колено": "Колено", "выход": "Выход"}
# Columns the bot's fit check understands (brain/fit.py FIELDS); others are only shown on the chart / to GPT.
FIT_COLUMNS = {"полуобхват_талии", "полуталия", "полуобхват_бедер", "полуобхват_одного_бедра"}



def _write(path, text):
    """Local files (prompt.txt): temp file first, then renamed, so the bot never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


_bp = None


def _bot_products_module():
    """brain/products.py itself (loaded once), so the app reads the catalog exactly like the bot does."""
    global _bp
    if _bp is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot_products", BOT_DIR / "brain" / "products.py")
        _bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_bp)
    return _bp


# ---------------------------------------------------------------- reading / writing the server

_snap = {"at": 0.0, "data": None}
SNAP_TTL = 2  # seconds: one screen asks for the same data several times


def _data(fresh=False):
    """{"catalog", "stock", "shop"} values: the server's, else the bot's last local copy (read-only then)."""
    if fresh or _snap["data"] is None or time.time() - _snap["at"] > SNAP_TTL:
        data = {}
        for k in ("catalog", "stock", "shop"):
            data[k] = dc.load(k)[0]
        data["catalog"] = data["catalog"] or {"format": "catalog", "version": 1, "sizeScale": [], "categories": [], "products": []}
        data["shop"] = data["shop"] or {}
        dc.sync_files(_bot_products_module().file_names(data["catalog"]), check=True)
        _snap.update(at=time.time(), data=data)
    return _snap["data"]


def _forget():
    _snap["data"] = None


def _server_error(ex):
    if isinstance(ex, dc.Invalid):
        return ValueError("Сервер данных не принял изменения: " + "; ".join(ex.errors[:3]))
    return ValueError("Сервер данных недоступен или ответил ошибкой — изменения не сохранены. " + str(ex))


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log(text):
    """One line in the shared change history (log.json, newest first, up to 2000). Never fails a save."""
    entry = {"id": uuid.uuid4().hex[:16], "at": _now_iso(), "source": "bot", "text": text}

    def add(log):
        log.setdefault("entries", [])
        log["entries"] = ([entry] + [e for e in log["entries"] if e.get("id") != entry["id"]])[:2000]
    try:
        dc.update("log", add)
    except dc.DataError:
        pass


def _edit_catalog(mutate, log_text=None):
    """Apply mutate(catalog) on the server (again on fresh data after a 409), then log. ValueError on any problem.
    mutate may return the log text (it sees the data it changes)."""
    said = {}

    def run(cat):
        said["text"] = mutate(cat)
    try:
        dc.update("catalog", run)
    except dc.DataError as ex:
        raise _server_error(ex) from ex
    finally:
        _forget()
    text = said.get("text") or log_text
    if text:
        _log(text)


def _prod(cat, pid):
    p = next((x for x in cat.get("products") or [] if x.get("id") == pid), None)
    if p is None:
        raise ValueError("Такого товара нет (его удалили на другом компьютере?)")
    return p


def stock_source():
    """"warehouse" (stock comes from HUB BRO's warehouse) or "manual" (the «нет в наличии» switches here)."""
    return _data()["shop"].get("stockSource") or "manual"


def size_labels():
    """{25: '2XS', ...}: the shop's size scale (catalog.sizeScale)."""
    return {n: (l.partition("(")[2].rstrip(")")) for n, l in _bot_products_module().size_labels(_data()["catalog"]).items()}


def _scale_order(cat):
    return [s for s, _ in _bot_products_module().scale_of(cat)]


def _sort_sizes(sizes, cat):
    order = {s: i for i, s in enumerate(_scale_order(cat))}
    return sorted(set(sizes), key=lambda s: (order.get(s, len(order)), int(s)))


# ---------------------------------------------------------------- products

def list_products():
    """The bot's view of the products (products.py build()); with stockSource «manual» in_stock / out_sizes are the
    product's own switches (the bot's view adds «all sizes off» = not in stock)."""
    d = _data()
    bp = _bot_products_module()
    items = bp.build(d["catalog"], d["stock"], d["shop"])
    raw = {p["id"]: p for p in d["catalog"].get("products") or []}
    manual = d["shop"].get("stockSource") != "warehouse"
    for p in items:
        r = raw.get(p["id"]) or {}
        av = r.get("availability") or {}
        p["sizes"] = [int(s) for s in r.get("sizes") or []]
        p["archived"] = bool(r.get("archived"))
        if manual:
            p["in_stock"] = av.get("inStock", True) is not False and not p["archived"]
            p["out_sizes"] = sorted(int(s) for s in av.get("outSizes") or [])
    return items


def get_product(pid):
    return next((p for p in list_products() if p["id"] == pid), None)


def raw_key(pid):
    """The bot's internal name of the product (catalog botKey; products without one use their id)."""
    p = next((x for x in _data()["catalog"].get("products") or [] if x.get("id") == pid), None)
    return (p.get("botKey") or p.get("id")) if p else None


def valid_id(pid):
    return bool(SLUG.fullmatch(pid or ""))


def create_product(pid, name, key):
    """New product in the catalog (category: the first one; sizes: that category's template). Its key can be chosen
    once, here (empty = the id)."""
    if not valid_id(pid):
        raise ValueError("Имя папки: латинские буквы, цифры, - и _ (до 40 знаков, первая — буква или цифра)")
    name = (name or "").strip()
    if not name:
        raise ValueError("Нужно название")
    key = (key or "").strip().lower()

    def add(cat):
        prods = cat.setdefault("products", [])
        if any(p.get("id") == pid for p in prods):
            raise ValueError("Такая папка уже есть")
        if any((p.get("name") or "").lower() == name.lower() for p in prods):
            raise ValueError("Товар с таким названием уже есть")
        if key and any((p.get("botKey") or p.get("id")) == key for p in prods):
            raise ValueError("Такое внутреннее имя уже есть у другого товара")
        cats = cat.setdefault("categories", [])
        if not cats:  # a shop with no categories yet: «Джинсы» with the whole scale
            cats.append({"id": "jeans", "name": "Джинсы", "sizes": _scale_order(cat), "thresholds": {"lowMax": 5, "midMax": 10}})
        if not cat.get("sizeScale"):
            cat["sizeScale"] = [{"size": s, "label": l} for s, l in _bot_products_module().scale_of(cat)]
        sizes = list(cats[0].get("sizes") or []) or _scale_order(cat)
        prods.append({
            "id": pid, "name": name, "categoryId": cats[0]["id"], "sizes": sizes, "match": [],
            **({"botKey": key} if key and key != pid else {}),
            "description": {"material": None, "composition": None, "density": None, "legOpening": None},
            "sizeTable": None, "sizeHW": None, "photos": [], "chart": None,
            "availability": {"inStock": True, "outSizes": []},
        })
        return f"Новый товар «{name}» ({pid})"
    _edit_catalog(add)
    return pid


def product_sizes(pid):
    """Sizes this product comes in (catalog sizes, in the scale's order)."""
    p = get_product(pid)
    return sorted(p["sizes"]) if p and p["sizes"] else sorted(size_labels())


def save_product(pid, name, match_text, in_stock, out_sizes=None):
    """Name, match words, the stock flag and the sizes that are out (None = keep as they are).
    With stockSource «warehouse» the stock comes from HUB BRO: the flag and the sizes are not saved."""
    name = name.strip()
    if not name:
        raise ValueError("Нужно название")
    match = list(dict.fromkeys(w.strip().lower() for w in match_text.split(",") if w.strip()))
    if not match:
        raise ValueError("Нужно хотя бы одно слово для узнавания объявления")
    labels = size_labels()
    if out_sizes is not None:
        out_sizes = sorted({int(n) for n in out_sizes})
        if any(n not in labels for n in out_sizes):
            raise ValueError("Такого размера нет в шкале размеров")
    manual = stock_source() != "warehouse"

    def edit(cat):
        p = _prod(cat, pid)
        others = [x for x in cat["products"] if x.get("id") != pid]
        clash = [(w, x["name"]) for x in others for w in match if w in (x.get("match") or [])]
        if clash:
            raise ValueError(f"Слово «{clash[0][0]}» уже узнаёт товар «{clash[0][1]}»")
        if any((x.get("name") or "").lower() == name.lower() for x in others):
            raise ValueError("Товар с таким названием уже есть")
        said = []
        if p.get("name") != name:
            said.append(f"название «{p.get('name')}» → «{name}»")
        if (p.get("match") or []) != match:
            said.append("слова: " + ", ".join(match))
        p["name"], p["match"] = name, match
        if manual:
            av = p.setdefault("availability", {"inStock": True, "outSizes": []})
            if av.get("inStock") is not bool(in_stock):
                said.append("в наличии" if in_stock else "нет в наличии")
            av["inStock"] = bool(in_stock)
            if out_sizes is not None:
                new = _sort_sizes([str(n) for n in out_sizes], cat)
                if (av.get("outSizes") or []) != new:
                    said.append("нет размеров: " + (", ".join(new) or "—"))
                av["outSizes"] = new
            av.setdefault("outSizes", [])
        return f"«{name}»: " + "; ".join(said) if said else None
    _edit_catalog(edit)


def save_material(pid, short, composition, density):
    short, composition, density = (x.strip() for x in (short or "", composition or "", density or ""))
    for x in (short, composition, density):
        if "\n" in x:
            raise ValueError("Материал: по одной строке на поле")
    if short and ":" in short:
        raise ValueError("Короткий ответ про материал — без двоеточия (например «98% хлопок»)")

    def edit(cat):
        p = _prod(cat, pid)
        d = p.setdefault("description", {})
        new = {"material": short or composition or None, "composition": composition or None, "density": density or None}
        if all(d.get(k) == v for k, v in new.items()):
            return None
        d.update(new)
        return f"«{p.get('name')}»: материал"
    _edit_catalog(edit)


# ---------------------------------------------------------------- size table

def read_table(pid):
    """(columns, rows) — columns without size/label; rows = [{size, label, <col>: str}] in the scale's order."""
    d = _data()
    p = next((x for x in d["catalog"].get("products") or [] if x.get("id") == pid), None)
    bp = _bot_products_module()
    t = bp.read_table((p or {}).get("sizeTable"), _scale_order(d["catalog"]))
    if not t:
        return [], []
    labels = size_labels()
    rows = [{"size": str(n), "label": labels.get(n, ""), **{c: bp.num_text(v) for c, v in r.items()}}
            for n, r in t["rows"].items()]
    return t["columns"], rows


def norm_column(name):
    return re.sub(r"\s+", "_", name.strip().lower().replace("ё", "е"))


def save_table(pid, columns, rows):
    """columns: names; rows: [{size, <col>: value}] — label comes from the shop's size scale.
    Only the product's own sizes (set in HUB BRO) may have a row: one size = one row."""
    allowed = set(product_sizes(pid))
    columns = [norm_column(c) for c in columns if c.strip()]
    if len(set(columns)) != len(columns):
        raise ValueError("Две колонки с одинаковым названием")
    if any(c in ("size", "label") or not COLUMN_KEY.fullmatch(c) for c in columns):
        raise ValueError("Название колонки: русские или латинские буквы, цифры и _")
    out, seen = {}, set()
    for r in rows:
        size = int(r["size"])
        if size not in allowed:
            raise ValueError(f"Размера {size} нет у этого товара (размеры товара задаются в HUB BRO)")
        if size in seen:
            raise ValueError(f"Размер {size} указан дважды")
        seen.add(size)
        vals = {}
        for c in columns:
            v = str(r.get(c, "")).strip().replace(",", ".")
            if v and not re.fullmatch(r"\d+(\.\d+)?", v):
                raise ValueError(f"Размер {size}, «{c}»: нужно число в см")
            if v:
                vals[c] = int(float(v)) if float(v) == int(float(v)) else float(v)
        out[str(size)] = vals

    def edit(cat):
        p = _prod(cat, pid)
        if not out:
            if not p.get("sizeTable"):
                return None
            p["sizeTable"] = None
            return f"«{p.get('name')}»: размерная сетка удалена"
        old_titles = {c.get("key"): c.get("title") for c in ((p.get("sizeTable") or {}).get("columns") or [])}
        cols = [{"key": c, "title": old_titles.get(c) or HEAD.get(c) or c.replace("_", " ")} for c in columns]
        p["sizeTable"] = {"columns": cols, "rows": {s: out[s] for s in _sort_sizes(out, cat)}}
        return f"«{p.get('name')}»: размерная сетка"
    _edit_catalog(edit)


# ---------------------------------------------------------------- size by height and weight

def read_hw(pid):
    """{"heights", "weights", "cells"} (sizes as numbers, None = empty cell) from the catalog's sizeHW, or None."""
    p = next((x for x in _data()["catalog"].get("products") or [] if x.get("id") == pid), None)
    return _bot_products_module().read_hw((p or {}).get("sizeHW"))


def save_hw(pid, heights, weights, cells):
    """heights/weights: points as typed (str) — the bot takes the NEAREST height row and weight column (a tie goes to the
    bigger one); cells[i][j]: size number or "" (= no size, the chat goes to the manager). Only the product's own sizes
    (set in HUB BRO) are allowed in the cells."""
    allowed = sorted(product_sizes(pid))

    def starts(values, what, lo, hi):
        out = []
        for v in values:
            v = str(v).strip()
            if not v.isdigit() or not lo <= int(v) <= hi:
                raise ValueError(f"{what}: «{v}» — нужно целое число от {lo} до {hi}")
            out.append(int(v))
        if out != sorted(set(out)):
            raise ValueError(f"{what}: значения должны идти по возрастанию и не повторяться")
        return out

    hs = starts(heights, "Рост", 100, 230)
    ws = starts(weights, "Вес", 20, 250)
    grid = []
    for h, row in zip(hs, cells):
        vals = []
        for w, c in zip(ws, row):
            c = str(c).strip()
            if c and (not c.isdigit() or int(c) not in allowed):
                raise ValueError(f"Рост {h}, вес {w}: «{c}» — такого размера нет у этого товара "
                                 f"(есть: {', '.join(map(str, allowed))}; размеры товара задаются в HUB BRO)")
            vals.append(str(int(c)) if c else None)
        grid.append(vals + [None] * (len(ws) - len(vals)))
    grid += [[None] * len(ws) for _ in range(len(hs) - len(grid))]

    def edit(cat):
        p = _prod(cat, pid)
        p["sizeHW"] = {"heights": hs, "weights": ws, "cells": grid}
        return f"«{p.get('name')}»: размер по росту и весу"
    _edit_catalog(edit)


# ---------------------------------------------------------------- photos & chart

def _put_file(name, data):
    if len(data) > MAX_FILE:
        raise ValueError(f"{name}: файл больше 15 МБ")
    try:
        dc.file_put(name, data)
    except dc.DataError as ex:
        raise _server_error(ex) from ex


def add_photos(pid, paths):
    """Upload photos as <id>_<n>.<ext> (a number no file on the server has), then add them to the product's photos."""
    srcs = list(map(Path, paths))
    for src in srcs:
        if src.suffix.lower() not in PHOTO_EXT:
            raise ValueError(f"{src.name}: можно только {', '.join(PHOTO_EXT)}")
    _prod(_data(fresh=True)["catalog"], pid)
    try:
        taken = set(dc.file_list())
    except dc.DataError as ex:
        raise _server_error(ex) from ex
    taken |= {f for p in _data()["catalog"].get("products") or [] for f in p.get("photos") or []}
    added = []
    for src in srcs:
        n = 1
        while any(f"{pid}_{n}{e}" in taken for e in PHOTO_EXT):
            n += 1
        name = f"{pid}_{n}{src.suffix.lower()}"
        _put_file(name, src.read_bytes())
        taken.add(name)
        added.append(name)

    def edit(cat):
        p = _prod(cat, pid)
        p["photos"] = list(p.get("photos") or []) + [x for x in added if x not in (p.get("photos") or [])]
        return f"«{p.get('name')}»: добавлено фото " + ", ".join(added)
    if added:
        _edit_catalog(edit)
    return [dc.local_file(x) for x in added]


def delete_photo(pid, filename):
    """Remove the photo from the product first (the bot never refers to a missing file), then the file itself."""
    name = Path(filename).name

    def edit(cat):
        p = _prod(cat, pid)
        if name not in (p.get("photos") or []):
            return None
        p["photos"] = [x for x in p["photos"] if x != name]
        return f"«{p.get('name')}»: удалено фото {name}"
    _edit_catalog(edit)
    if not any(name in (p.get("photos") or []) or p.get("chart") == name for p in _data(fresh=True)["catalog"].get("products") or []):
        try:
            dc.file_delete(name)
        except dc.DataError as ex:
            raise _server_error(ex) from ex


def chart_name(pid):
    return f"{pid}_size_chart.png"


def _set_chart(pid, png_bytes, how):
    name = chart_name(pid)
    _put_file(name, png_bytes)

    def edit(cat):
        p = _prod(cat, pid)
        p["chart"] = name
        return f"«{p.get('name')}»: картинка сетки {how}"
    _edit_catalog(edit)
    return dc.local_file(name)


def upload_chart(pid, src):
    """A ready screenshot of the size chart becomes <id>_size_chart.png on the server (converted to PNG if needed)."""
    from PIL import Image
    import io
    src = Path(src)
    if src.suffix.lower() not in CHART_EXT:
        raise ValueError(f"Сетка: можно только {', '.join(CHART_EXT)}")
    buf = io.BytesIO()
    with Image.open(src) as im:
        im.convert("RGB").save(buf, "PNG")
    return _set_chart(pid, buf.getvalue(), "загружена")


def delete_chart(pid):
    old = {}

    def edit(cat):
        p = _prod(cat, pid)
        old["name"] = p.get("chart")
        if not p.get("chart"):
            return None
        p["chart"] = None
        return f"«{p.get('name')}»: картинка сетки удалена"
    _edit_catalog(edit)
    if old.get("name"):
        try:
            dc.file_delete(old["name"])
        except dc.DataError as ex:
            raise _server_error(ex) from ex


def _font(bold, size):
    from PIL import ImageFont
    names = (["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"])
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
    for d in dirs:
        for n in names:
            if (d / n).is_file():
                return ImageFont.truetype(str(d / n), size)
    return ImageFont.load_default(size)


def draw_chart(pid):
    """Draw the size chart from the size table (Pillow; looks like make_size_charts.py's output) and upload it as
    <id>_size_chart.png. Returns the local copy's path."""
    import io
    from PIL import Image, ImageDraw
    cols, rows = read_table(pid)
    if not rows:
        raise ValueError("Сначала заполните размерную сетку")
    prod = get_product(pid)
    s = 2  # retina scale
    f, fb, fh, ft = _font(False, 16 * s), _font(True, 16 * s), _font(True, 13 * s), _font(True, 20 * s)
    fsub = _font(False, 14 * s)
    allc = ["size", "label"] + cols
    head = [HEAD.get(c, c.replace("_", " ").capitalize()) for c in allc]
    tmp = Image.new("RGB", (10, 10))
    dr = ImageDraw.Draw(tmp)

    def wrap(text, font, width):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if dr.textlength(t, font=font) <= width or not cur:
                cur = t
            else:
                lines.append(cur); cur = w
        return lines + ([cur] if cur else [])

    pad, maxw = 12 * s, 110 * s
    widths = []
    for i, c in enumerate(allc):
        cell = max([dr.textlength(r.get(c, ""), font=fb) for r in rows] + [0])
        hw = max([dr.textlength(x, font=fh) for x in wrap(head[i], fh, maxw)] + [0])
        widths.append(int(max(cell, hw)) + 2 * pad)
    head_lines = [wrap(h, fh, widths[i] - 2 * pad) for i, h in enumerate(head)]
    lh = 17 * s
    head_h = max(len(x) for x in head_lines) * lh + 14 * s
    row_h = 34 * s
    m = 24 * s
    title = f"BRO x BRO SHOP · {prod['name'] if prod else pid} — размерная сетка, см"
    sub = "Обхваты указаны в половину (замер изделия в разложенном виде)"
    tw = sum(widths)
    W = max(tw, int(dr.textlength(title, font=ft)), int(dr.textlength(sub, font=fsub))) + 2 * m
    top = m + 28 * s + 26 * s
    H = top + head_h + row_h * len(rows) + m
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.text((m, m), title, font=ft, fill="#111111")
    d.text((m, m + 30 * s), sub, font=fsub, fill="#555555")
    border = "#cccccc"
    x = m
    for i, wcol in enumerate(widths):
        d.rectangle([x, top, x + wcol, top + head_h], fill="#f2f2f2", outline=border, width=s)
        ly = top + (head_h - len(head_lines[i]) * lh) // 2
        for ln in head_lines[i]:
            d.text((x + wcol / 2, ly + lh / 2), ln, font=fh, fill="#111111", anchor="mm")
            ly += lh
        y = top + head_h
        for j, r in enumerate(rows):
            d.rectangle([x, y, x + wcol, y + row_h], fill="#fafafa" if j % 2 else "white", outline=border, width=s)
            d.text((x + wcol / 2, y + row_h / 2), r.get(allc[i], ""), font=fb if i < 2 else f, fill="#111111", anchor="mm")
            y += row_h
        x += wcol
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return _set_chart(pid, buf.getvalue(), "нарисована из таблицы")


# ---------------------------------------------------------------- shipping cutoffs

def read_cutoffs():
    """{"weekday_cutoff": "19:00", "weekend_cutoff": "18:00"} from shop.json on the data server."""
    c = _data()["shop"].get("cutoffs") or {}
    return {"weekday_cutoff": c.get("weekday") or DEFAULT_CUTOFFS["weekday_cutoff"],
            "weekend_cutoff": c.get("weekend") or DEFAULT_CUTOFFS["weekend_cutoff"]}


def valid_hhmm(v):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (v or "").strip())
    return bool(m) and int(m.group(1)) < 24 and int(m.group(2)) < 60


def save_cutoffs(weekday, weekend):
    for label, v in (("Будни", weekday), ("Выходные", weekend)):
        if not valid_hhmm(v):
            raise ValueError(f"{label}: время в виде ЧЧ:ММ, например 19:30")
    norm = lambda v: "%02d:%s" % (int(v.strip().split(":")[0]), v.strip().split(":")[1])
    new = {"weekday": norm(weekday), "weekend": norm(weekend)}
    said = {}

    def edit(shop):
        old = shop.get("cutoffs") or {}
        said["text"] = (f"Время отправки: будни {old.get('weekday', '—')} → {new['weekday']}, "
                        f"выходные {old.get('weekend', '—')} → {new['weekend']}") if old != new else None
        shop["cutoffs"] = dict(new)
    try:
        dc.update("shop", edit)
    except dc.DataError as ex:
        raise _server_error(ex) from ex
    finally:
        _forget()
    if said.get("text"):
        _log(said["text"])


# ---------------------------------------------------------------- GPT prompt

PROMPT_FILE = BOT_DIR / "prompt.txt"
PROMPT_HISTORY = BOT_DIR / "prompt_history"
HISTORY_KEEP = 30
INSERT = re.compile(r"\{([a-z_]+)\}")


def _gpt_constants():
    """INSERTS and DEFAULT_PROMPT read from gpt/gpt_reply.py's source (no import: that pulls in the whole bot)."""
    tree = ast.parse((BOT_DIR / "gpt" / "gpt_reply.py").read_text(encoding="utf-8"))
    env = {}

    def ev(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return ev(node.left) + ev(node.right)
        if isinstance(node, ast.Name):
            return env[node.id]
        return ast.literal_eval(node)

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("PROMPT", "STYLE_EXAMPLES", "DEFAULT_PROMPT", "INSERTS"):
            env[node.targets[0].id] = ev(node.value)
    return env["INSERTS"], env["DEFAULT_PROMPT"]


def prompt_inserts():
    """{name: (title, where it comes from)}, in prompt order."""
    return _gpt_constants()[0]


def default_prompt():
    return _gpt_constants()[1]


def read_prompt():
    """(text, is_default): the prompt the bot uses now."""
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8")
        if text.strip():
            return text, False
    except OSError:
        pass
    return default_prompt(), True


def prompt_problems(text):
    """(missing, unknown): inserts not in the text (GPT won't get that fact), and {names} the bot doesn't know (sent as is)."""
    names = INSERT.findall(text)
    known = prompt_inserts()
    return [n for n in known if n not in names], sorted({n for n in names if n not in known})


def save_prompt(text):
    if not (text or "").strip():
        raise ValueError("Промпт пустой. Чтобы вернуть стандартный, нажмите «Вернуть стандартный».")
    text = text.replace("\r\n", "\n")
    if PROMPT_FILE.exists():  # the version before this save, in case the new one goes wrong
        PROMPT_HISTORY.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(time.time() + 3 * 3600))  # Moscow time
        shutil.copy2(PROMPT_FILE, PROMPT_HISTORY / f"prompt_{stamp}.txt")
        for old in sorted(PROMPT_HISTORY.glob("prompt_*.txt"))[:-HISTORY_KEEP]:
            old.unlink()
    _write(PROMPT_FILE, text)


def prompt_history():
    """[(«16.11 03:49», path)] newest first (Moscow time of the save that replaced it)."""
    files = sorted(PROMPT_HISTORY.glob("prompt_*.txt"), reverse=True) if PROMPT_HISTORY.exists() else []
    out = []
    for f in files:
        d, t = f.stem[len("prompt_"):].split("_")
        out.append((f"{d[8:10]}.{d[5:7]} {t[:5].replace('-', ':')}", f))
    return out


def prompt_examples(pid):
    """Example insert values for one product, computed by the bot itself (its own Python), as {name: text}."""
    import subprocess, sys
    py = BOT_DIR / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    flags = 0x08000000 if sys.platform == "win32" else 0
    r = subprocess.run([str(py), str(BOT_DIR / "gpt" / "gpt_reply.py"), "--inserts", pid], cwd=BOT_DIR,
                       capture_output=True, timeout=120, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-500:])
    return json.loads(r.stdout.decode("utf-8"))
