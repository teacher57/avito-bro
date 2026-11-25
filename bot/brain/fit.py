"""Fit check: compare the client's measurements with the chosen size's table row.
Table (the product's sizeTable in the catalog, see products.py) gives HALF girths. Clients usually give FULL circumference, so a value above
FULL_MIN is treated as full and halved. This is an assumption to confirm with Maxim."""
import re

from products import PRODUCTS, ON_RELOAD
FULL_MIN = {"waist": 50, "hip": 60}  # value above this = full circumference, halved (table half-waist tops at 47, half-hip at 54)

FIELDS = {  # client words -> table column
    "waist": (r"(?:полу ?талия|полуобхват талии|обхват талии|талия|талию|талии)", ("полуобхват_талии", "полуталия")),
    "hip": (r"(?:бедра|бедер|бёдра|бёдер|жопа|попа|полубедро|обхват бедер|бедро)", ("полуобхват_бедер",)),
    "thigh": (r"(?:ляжк\w*|полуобхват одного бедра|одна ляжка)", ("полуобхват_одного_бедра",)),
}


def load_tables():
    """{model key: {size number: {column: value}}} from each product's size table (catalog sizeTable)"""
    tables = {}
    for prod in PRODUCTS:
        if not prod["table"]:
            continue
        tables[prod["key"].lower()] = {n: {k: float(v) for k, v in row.items()} for n, row in prod["table"]["rows"].items()}
    return tables


TABLES = load_tables()
ON_RELOAD.append(lambda: (TABLES.clear(), TABLES.update(load_tables())))  # manager app edited a table


def client_measures(text):
    """Waist / hip / thigh numbers the client wrote (as given)."""
    t = text.lower()
    out = {}
    for key, (word, _) in FIELDS.items():
        m = re.search(word + r"\D{0,12}(\d{2,3})(?!\d)", t)
        if m:
            out[key] = int(m.group(1))
    return out


def compare(model_key, size_num, measures):
    """Returns (verdict, details). verdict: fits | tight | loose | unknown."""
    model_key = (model_key or "").lower()
    row = TABLES.get(model_key, {}).get(size_num)
    if not row or not measures:
        return "unknown", []
    details, worst = [], "fits"
    for key, value in measures.items():
        cols = [c for c in FIELDS[key][1] if c in row]
        if not cols:
            continue
        col = cols[0]
        half = value / 2 if value > FULL_MIN.get(key, 60) or (key == "thigh" and value > 45) else value
        diff = half - row[col]
        if diff > 1:
            verdict = "tight"
        elif diff < -2:
            verdict = "loose"
        else:
            verdict = "fits"
        details.append((key, value, half, row[col], verdict))
        if verdict == "tight":
            worst = "tight"
        elif verdict == "loose" and worst == "fits":
            worst = "loose"
    if not details:
        return "unknown", []  # this model has no table column for these measurements
    return worst, details


def size_by_measures(model_key, measures, tol=1.0):
    """Smallest size where every comparable client half-girth is at most table value + tol. None if no data."""
    model_key = (model_key or "").lower()
    table = TABLES.get(model_key, {})
    if not table or not measures:
        return None
    for size in sorted(table):
        row, compared, ok = table[size], 0, True
        for key, value in measures.items():
            cols = [c for c in FIELDS[key][1] if c in row]
            if not cols:
                continue
            half = value / 2 if value > FULL_MIN.get(key, 60) or (key == "thigh" and value > 45) else value
            compared += 1
            if half - row[cols[0]] > tol:
                ok = False
                break
        if compared and ok:
            return size
    return None
