"""Reads and writes the bot's own files (no separate database), so the running bot picks the changes up by itself:

  products/<id>/product.txt    name, match, key (never changed for an existing product), in_stock (new: да/нет)
  products/<id>/material.txt   short answer, «состав: …», «плотность: …»
  products/<id>/size_table.txt TSV size table, cm
  products/<id>/size_chart.png picture of the size table (uploaded, or drawn here from size_table.txt)
  products/<id>/photos/        photos on a person
  shop_settings.json           shipping cutoffs (weekday / weekend), Moscow time
  prompt.txt                   GPT's prompt with {inserts}; prompt_history/ keeps the versions before each save

Every write goes to a temp file first and is then renamed, so the bot never reads a half-written file.
v1 is jeans only: sizes are the shared jeans scale SIZE_LABEL from brain/brain.py (read from its source, not copied)."""
import ast
import importlib.util
import json
import os
import re
import shutil
import time
from pathlib import Path

BOT_DIR = Path(os.environ.get("MANAGER_APP_BOT_DIR") or Path(__file__).resolve().parent.parent)
PRODUCTS_DIR = BOT_DIR / "products"
SETTINGS = BOT_DIR / "shop_settings.json"
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp")
CHART_EXT = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_CUTOFFS = {"weekday_cutoff": "19:30", "weekend_cutoff": "18:30"}  # same defaults as brain.py

# Column titles for the drawn size chart (same as make_size_charts.py).
HEAD = {"size": "Размер", "label": "", "полуобхват_талии": "Полуобхват талии", "полуталия": "Полуобхват талии",
        "полуобхват_бедер": "Полуобхват бёдер", "длина_по_внеш_шву": "Длина по внешнему шву",
        "длина_по_внутр_шву": "Длина по внутреннему шву", "полуобхват_одного_бедра": "Полуобхват одного бедра",
        "полувыход": "Полувыход", "колено": "Колено", "выход": "Выход"}
# Columns the bot's fit check understands (brain/fit.py FIELDS); others are only shown on the chart / to GPT.
FIT_COLUMNS = {"полуобхват_талии", "полуталия", "полуобхват_бедер", "полуобхват_одного_бедра"}
TABLE_HEADER = ["# Размерная сетка, см. Обхваты — в половину (изделие в разложенном виде).",
                "# Колонки разделены TAB. Одна строка = один размер."]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def size_labels():
    """{25: '2XS', ...} from brain.py's SIZE_LABEL (the shared jeans scale)."""
    src = (BOT_DIR / "brain" / "brain.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SIZE_LABEL" for t in node.targets):
            full = ast.literal_eval(node.value)
            return {int(k): (re.search(r"\((.+)\)", v).group(1) if "(" in v else "") for k, v in full.items()}
    raise RuntimeError("SIZE_LABEL not found in brain.py")


def _bot_products_module():
    """brain/products.py itself, so the app parses the files exactly like the bot does."""
    spec = importlib.util.spec_from_file_location("bot_products", BOT_DIR / "brain" / "products.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PRODUCTS_DIR = PRODUCTS_DIR
    return mod


# ---------------------------------------------------------------- products

def list_products():
    return _bot_products_module().load_products()


def get_product(pid):
    return next((p for p in list_products() if p["id"] == pid), None)


def raw_key(pid):
    """The key exactly as written in product.txt (None if there is no key line)."""
    f = _bot_products_module()._fields(PRODUCTS_DIR / pid / "product.txt")
    return f.get("key")


def valid_id(pid):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", pid or ""))


def create_product(pid, name, key):
    """New folder for a new product. Its key can be chosen once, here."""
    if not valid_id(pid):
        raise ValueError("Имя папки: латинские буквы, цифры, - и _")
    if (PRODUCTS_DIR / pid).exists():
        raise ValueError("Такая папка уже есть")
    key = (key or name).strip().lower()
    if not key:
        raise ValueError("Нужно название")
    if any(p["key"] == key for p in list_products()):
        raise ValueError("Такое внутреннее имя уже есть у другого товара")
    (PRODUCTS_DIR / pid / "photos").mkdir(parents=True)
    _write_product_txt(pid, name.strip(), [], key, True, [])
    return pid


def _write_product_txt(pid, name, match, key, in_stock, out_sizes):
    _write(PRODUCTS_DIR / pid / "product.txt", "\n".join([
        f"name: {name}",
        "# слова в названии объявления, по которым бот узнаёт товар (через запятую)",
        f"match: {', '.join(match)}",
        "# внутреннее имя модели (не менять: на него завязаны дерево размеров и правила)",
        f"key: {key}",
        "# есть ли товар в наличии (да / нет); «нет» — бот отвечает, что модели временно нет в наличии",
        f"in_stock: {'да' if in_stock else 'нет'}",
        "# размеры, которых сейчас нет (через запятую); бот говорит, что такого размера временно нет в наличии",
        f"out_sizes: {', '.join(str(n) for n in sorted(out_sizes))}",
    ]) + "\n")


def product_sizes(pid):
    """Sizes this product comes in: its size table's rows, or the whole jeans scale if it has no table yet."""
    _, rows = read_table(pid)
    return sorted(int(r["size"]) for r in rows) if rows else sorted(size_labels())


def save_product(pid, name, match_text, in_stock, out_sizes=None):
    """Name, match words, the stock flag and the sizes that are out (None = keep as they are).
    The key is re-written exactly as it was: the app never changes it."""
    key = raw_key(pid)
    if key is None:
        raise ValueError("В product.txt нет внутреннего имени (key): сохранять не буду, спросите администратора")
    name = name.strip()
    if not name:
        raise ValueError("Нужно название")
    match = [w.strip().lower() for w in match_text.split(",") if w.strip()]
    if not match:
        raise ValueError("Нужно хотя бы одно слово для узнавания объявления")
    clash = [(w, p["name"]) for p in list_products() if p["id"] != pid for w in match if w in p["match"]]
    if clash:
        raise ValueError(f"Слово «{clash[0][0]}» уже узнаёт товар «{clash[0][1]}»")
    if out_sizes is None:
        out_sizes = get_product(pid).get("out_sizes") or []
    labels = size_labels()
    out_sizes = sorted({int(n) for n in out_sizes})
    if any(n not in labels for n in out_sizes):
        raise ValueError("Такого размера нет в шкале джинсов")
    _write_product_txt(pid, name, match, key, in_stock, out_sizes)


def save_material(pid, short, composition, density):
    lines = [x for x in [short.strip(),
                         f"состав: {composition.strip()}" if composition.strip() else "",
                         f"плотность: {density.strip()}" if density.strip() else ""] if x]
    for x in lines:
        if "\n" in x:
            raise ValueError("Материал: по одной строке на поле")
    if short.strip() and ":" in short:
        raise ValueError("Короткий ответ про материал — без двоеточия (например «98% хлопок»)")
    path = PRODUCTS_DIR / pid / "material.txt"
    if lines:
        _write(path, "\n".join(lines) + "\n")
    elif path.exists():
        path.unlink()


# ---------------------------------------------------------------- size table

def read_table(pid):
    """(columns, rows) — columns without size/label; rows = [{size, label, <col>: str}]."""
    path = PRODUCTS_DIR / pid / "size_table.txt"
    if not path.is_file():
        return [], []
    cols, rows = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("size\t"):
            cols = line.split("\t")
        elif cols and line.strip() and line[0].isdigit():
            rows.append(dict(zip(cols, line.split("\t"))))
    return [c for c in (cols or []) if c not in ("size", "label")], rows


def norm_column(name):
    return re.sub(r"\s+", "_", name.strip().lower().replace("ё", "е"))


def save_table(pid, columns, rows):
    """columns: names; rows: [{size, <col>: value}] — label comes from the shared jeans scale."""
    labels = size_labels()
    columns = [norm_column(c) for c in columns if c.strip()]
    if len(set(columns)) != len(columns):
        raise ValueError("Две колонки с одинаковым названием")
    if any(c in ("size", "label") or not re.fullmatch(r"[\wа-я]+", c) for c in columns):
        raise ValueError("Название колонки: буквы, цифры и _")
    out, seen = [], set()
    for r in rows:
        size = int(r["size"])
        if size not in labels:
            raise ValueError(f"Размера {size} нет в шкале джинсов")
        if size in seen:
            raise ValueError(f"Размер {size} указан дважды")
        seen.add(size)
        vals = []
        for c in columns:
            v = str(r.get(c, "")).strip().replace(",", ".")
            if v and not re.fullmatch(r"\d+(\.\d+)?", v):
                raise ValueError(f"Размер {size}, «{c}»: нужно число в см")
            vals.append(v)
        out.append([str(size), labels[size]] + vals)
    out.sort(key=lambda x: int(x[0]))
    path = PRODUCTS_DIR / pid / "size_table.txt"
    if not out:
        if path.exists():
            path.unlink()
        return
    _write(path, "\n".join(TABLE_HEADER + ["\t".join(["size", "label"] + columns)] + ["\t".join(x) for x in out]) + "\n")


# ---------------------------------------------------------------- size by height and weight

HW_HEADER = ["# Размер по росту и весу. Строки — рост от (см), колонки — вес от (кг); 0 = «меньше следующего».",
             "# В ячейке — номер размера (25–36). Пусто = бот размер не называет, чат уходит менеджеру."]


def read_hw(pid):
    """{"heights", "weights", "cells"} from products/<pid>/size_hw.txt (parsed by the bot's own code), or None."""
    return _bot_products_module().read_hw(PRODUCTS_DIR / pid / "size_hw.txt")


def save_hw(pid, heights, weights, cells):
    """heights/weights: band starts as typed (str); cells[i][j]: size number or "" (= no size, Maxim answers)."""
    labels = size_labels()

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

    hs = starts(heights, "Рост", 0, 230)
    ws = starts(weights, "Вес", 0, 250)
    rows = []
    for h, row in zip(hs, cells):
        vals = []
        for w, c in zip(ws, row):
            c = str(c).strip()
            if c and (not c.isdigit() or int(c) not in labels):
                raise ValueError(f"Рост от {h}, вес от {w}: «{c}» — нет такого размера (есть {min(labels)}–{max(labels)})")
            vals.append(c)
        rows.append(f"{h}\t" + "\t".join(vals))
    _write(PRODUCTS_DIR / pid / "size_hw.txt",
           "\n".join(HW_HEADER + ["рост\\вес\t" + "\t".join(map(str, ws))] + rows) + "\n")


# ---------------------------------------------------------------- photos & chart

def add_photos(pid, paths):
    folder = PRODUCTS_DIR / pid / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    added = []
    for src in map(Path, paths):
        ext = src.suffix.lower()
        if ext not in PHOTO_EXT:
            raise ValueError(f"{src.name}: можно только {', '.join(PHOTO_EXT)}")
        n = 1
        while any((folder / f"{pid}_{n}{e}").exists() for e in PHOTO_EXT):
            n += 1
        dst = folder / f"{pid}_{n}{ext}"
        shutil.copyfile(src, dst.with_name(dst.name + ".tmp"))
        os.replace(dst.with_name(dst.name + ".tmp"), dst)
        added.append(dst)
    return added


def delete_photo(pid, filename):
    path = PRODUCTS_DIR / pid / "photos" / Path(filename).name
    if path.is_file():
        path.unlink()


def upload_chart(pid, src):
    """A ready screenshot of the size chart becomes size_chart.png (converted to PNG if needed)."""
    from PIL import Image
    src = Path(src)
    if src.suffix.lower() not in CHART_EXT:
        raise ValueError(f"Сетка: можно только {', '.join(CHART_EXT)}")
    dst = PRODUCTS_DIR / pid / "size_chart.png"
    tmp = dst.with_name("size_chart.tmp.png")
    with Image.open(src) as im:
        im.convert("RGB").save(tmp, "PNG")
    os.replace(tmp, dst)


def delete_chart(pid):
    path = PRODUCTS_DIR / pid / "size_chart.png"
    if path.is_file():
        path.unlink()


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
    """Draw size_chart.png from size_table.txt (Pillow; looks like make_size_charts.py's output)."""
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
    dst = PRODUCTS_DIR / pid / "size_chart.png"
    tmpf = dst.with_name("size_chart.tmp.png")
    im.save(tmpf, "PNG")
    os.replace(tmpf, dst)
    return dst


# ---------------------------------------------------------------- shipping cutoffs

def read_cutoffs():
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {k: d.get(k, v) for k, v in DEFAULT_CUTOFFS.items()}


def valid_hhmm(v):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (v or "").strip())
    return bool(m) and int(m.group(1)) < 24 and int(m.group(2)) < 60


def save_cutoffs(weekday, weekend):
    for label, v in (("Будни", weekday), ("Выходные", weekend)):
        if not valid_hhmm(v):
            raise ValueError(f"{label}: время в виде ЧЧ:ММ, например 19:30")
    norm = lambda v: "%02d:%s" % (int(v.strip().split(":")[0]), v.strip().split(":")[1])
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.update({"weekday_cutoff": norm(weekday), "weekend_cutoff": norm(weekend)})
    _write(SETTINGS, json.dumps(d, ensure_ascii=False, indent=1) + "\n")


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
