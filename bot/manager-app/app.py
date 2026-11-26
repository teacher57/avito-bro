"""BOT BRO — manager app (Flet) for the BRO x BRO SHOP bot. See SPEC.md and README.md.

Run:  python app.py   one start for everything (desktop shortcut «BOT BRO»): on a PC where the app was never set up it
                      opens the first-run form (admin PIN + tokens), afterwards the normal app (user, 2026-11-25).

Managers (Даня, Макс) switch the bot on/off, edit products, stock, the shipping cutoffs and GPT's prompt. Tokens are behind the admin PIN.
On a PC where the stored tokens don't decrypt (folder copied elsewhere, with data/secrets.json) the window opens and
quietly shows nothing useful."""
import datetime
import sys
import time
import traceback
from pathlib import Path

import flet as ft

import botctl
import secrets_store as sec
import store
import ui_theme as ui

TITLE = "BOT BRO — менеджер"
ICON = Path(__file__).resolve().parent / "assets" / "icon.ico"  # window + taskbar icon (BB)
PHOTO_TYPES = [e.lstrip(".") for e in store.PHOTO_EXT]
CHART_TYPES = [e.lstrip(".") for e in store.CHART_EXT]
GREY = ft.Colors.ON_SURFACE_VARIANT  # muted text; all colors are theme roles, see ui_theme.py


def msk_now():
    return datetime.datetime.fromtimestamp(time.time() + 3 * 3600, datetime.UTC).replace(tzinfo=None)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


OPENAI_TAX = 0.18  # OpenAI adds 18% tax to the bill (user, 2026-11-17): shown amounts include it; the bot's counter doesn't


def spend_text():
    """«$1.23 потрачено на OpenAI с 13.11 12:00 МСК · 45 запросов» from the bot's counter (botctl.gpt_spend), with tax."""
    s = botctl.gpt_spend()
    total = s["usd"] * (1 + OPENAI_TAX)
    usd = f"${total:.2f}" if total >= 0.01 or total == 0 else f"${total:.4f}"
    since = (f" с {datetime.datetime.fromtimestamp(s['since'] + 3 * 3600, datetime.UTC):%d.%m.%Y %H:%M} МСК"
             if s["since"] else "")
    return ft.Column([ft.Text(usd, size=32, weight=ft.FontWeight.BOLD),
                      ui.hint(f"потрачено на токены OpenAI{since}, с налогом {OPENAI_TAX:.0%} · запросов: {s['calls']}")],
                     spacing=0)


def section(title, *controls, subtitle=None):
    head = [ft.Text(title, size=19, weight=ft.FontWeight.BOLD)]
    if subtitle:
        head.append(ui.hint(subtitle))
    return ft.Container(
        content=ft.Column(head + list(controls), spacing=12),
        padding=20, bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=ft.BorderRadius.all(ui.RADIUS))


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.admin = False
        self.selected = None  # product id
        self.view = None  # name of the open tab (the bot tab refreshes itself while it's open)
        self.pending = None  # (command id, action) sent to the bot, not applied yet
        self.picker = ft.FilePicker()
        self.body = ft.Container(expand=True, padding=ft.Padding.symmetric(horizontal=20, vertical=12))
        self.rail = ft.NavigationRail(
            selected_index=0, label_type=ft.NavigationRailLabelType.ALL, min_width=96, on_change=self.on_nav,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.SMART_TOY, label="Бот"),
                ft.NavigationRailDestination(icon=ft.Icons.INVENTORY_2, label="Товары"),
                ft.NavigationRailDestination(icon=ft.Icons.LOCAL_SHIPPING, label="Отправка"),
                ft.NavigationRailDestination(icon=ft.Icons.EDIT_NOTE, label="Промпт"),
                ft.NavigationRailDestination(icon=ft.Icons.KEY, label="Токены"),
            ])

    # ------------------------------------------------------------ helpers
    def toast(self, text, error=False):
        self.page.show_dialog(ft.SnackBar(ft.Text(text), bgcolor=ft.Colors.ERROR if error else None))

    def guard(self, fn, ok=None):
        """Run a save action: show the ValueError text to the manager, anything else as a short error."""
        try:
            fn()
        except ValueError as ex:
            self.toast(str(ex), error=True)
            return False
        except Exception as ex:
            traceback.print_exc()
            self.toast(f"Не получилось: {type(ex).__name__}", error=True)
            return False
        if ok:
            self.toast(ok)
        return True

    def start(self):
        self.page.controls.clear()
        self.page.add(ft.Row([self.rail, ft.VerticalDivider(width=1), self.body], expand=True))
        self.page.run_task(self.bot_refresher)
        self.show_bot()

    # ------------------------------------------------------------ bot on / off (everyone)
    async def bot_refresher(self):
        import asyncio
        while True:
            await asyncio.sleep(3)
            if self.view == "bot":
                try:
                    self.show_bot()
                except Exception:
                    traceback.print_exc()

    async def copy_text(self, text):
        try:
            await ft.Clipboard().set(text)
            self.toast("Скопировано")
        except Exception:
            self.toast("Не получилось скопировать: выделите код мышью и нажмите Ctrl+C", error=True)

    def show_bot(self):
        """Two independent switches: Avito (answer buyers) and Telegram (notifications, admin buttons, test chat),
        plus the bot program itself (start / restart / stop)."""
        self.view = "bot"
        self.rail.selected_index = 0
        st = botctl.status()
        if self.pending and st["seen"] == self.pending[0]:
            self.pending = None
        busy = self.pending[1] if self.pending else None

        def act(action):
            def go(e):
                if self.guard(lambda: setattr(self, "pending", (botctl.send(action), action))):
                    self.show_bot()
            return go

        def waiting():
            return ft.Row([ft.ProgressRing(width=18, height=18),
                           ft.Text("Команда отправлена, бот применит её через несколько секунд…")])

        def state_row(icon, color, text):
            return ft.Row([ft.Icon(icon, color=color, size=28), ft.Text(text, size=18, weight=ft.FontWeight.BOLD, color=color)])

        green = ft.ButtonStyle(bgcolor=ft.Colors.TERTIARY, color=ft.Colors.ON_TERTIARY)
        amber = ft.ButtonStyle(bgcolor=ft.Colors.SECONDARY, color=ft.Colors.ON_SECONDARY)
        off_hint = "Программа бота не запущена — сначала запустите её ниже."

        # --- Avito: answer buyers (only with a confirmed BOT BRO license)
        lic = st["license"]
        lic_ok = lic.get("state") in ("ok", "grace")
        if lic.get("state") == "ok":
            lic_line = ft.Text("Лицензия подтверждена" + (f" · действует до {lic['until']}" if lic.get("until") else ""),
                               size=12, color=GREY)
        elif lic.get("state") == "grace":
            lic_line = ft.Text("Список лицензий сейчас недоступен — работает по последней проверке (до 3 дней).",
                               size=12, color=ft.Colors.SECONDARY)
        else:
            lic_line = ft.Text(f"Лицензия не подтверждена: {lic.get('reason') or 'нет данных'}. Без неё бот на Авито не отвечает.",
                               color=ft.Colors.ERROR)
        if not st["running"]:
            avito = [state_row(ft.Icons.POWER_OFF, ft.Colors.ERROR, "Выключен"), ft.Text(off_hint, color=GREY)]
        else:
            on = st["live_on"] and not st["dry_run"]
            avito = [state_row(ft.Icons.CHECK_CIRCLE, ft.Colors.TERTIARY, "Отвечает покупателям") if on else
                     state_row(ft.Icons.PAUSE_CIRCLE, ft.Colors.SECONDARY, "На паузе"),
                     ft.Text("Отвечает во всех новых чатах на Авито, пока в чат не напишет менеджер." if on else
                             "Чаты на Авито записываются, но покупателям бот ничего не пишет.", color=GREY)]
            if busy in ("start", "pause"):
                avito.append(waiting())
            elif st["live_on"]:
                avito.append(ft.FilledButton(content="Поставить на паузу", icon=ft.Icons.PAUSE, on_click=act("pause"), style=amber))
            else:
                avito.append(ft.FilledButton(content="Включить: отвечать покупателям", icon=ft.Icons.PLAY_ARROW,
                                             on_click=act("start"), style=green, disabled=not lic_ok))
            avito.append(lic_line)
        avito.append(ft.Row([ft.Text("Код устройства для лицензии:", size=12, color=GREY),
                             ft.Text(botctl.device_code(), size=12, selectable=True, font_family=ui.MONO,
                                     weight=ft.FontWeight.W_600),
                             ft.IconButton(icon=ft.Icons.CONTENT_COPY, icon_size=16, tooltip="Скопировать",
                                           on_click=lambda e: self.page.run_task(self.copy_text, botctl.device_code()))], spacing=6))

        # --- Telegram bot
        if not st["running"]:
            tele = [state_row(ft.Icons.POWER_OFF, ft.Colors.ERROR, "Выключен"), ft.Text(off_hint, color=GREY)]
        else:
            tele = [state_row(ft.Icons.CHECK_CIRCLE, ft.Colors.TERTIARY, "Включён") if st["tg_on"] else
                    state_row(ft.Icons.PAUSE_CIRCLE, ft.Colors.SECONDARY, "Выключен"),
                    ft.Text("Уведомления менеджеру, кнопки администратора и тестовый чат «🧪 Тест» работают." if st["tg_on"] else
                            "Бот ничего не пишет в Telegram и не читает его: ни уведомлений, ни тестового чата.", color=GREY)]
            if not st["tg_on"] and st["live_on"]:
                tele.append(ft.Text("Внимание: на Авито бот отвечает, а когда он передаёт чат менеджеру, уведомление "
                                    "в Telegram не придёт.", color=ft.Colors.ERROR))
            if busy in ("tg_on", "tg_off"):
                tele.append(waiting())
            elif st["tg_on"]:
                tele.append(ft.FilledButton(content="Выключить Telegram-бота", icon=ft.Icons.PAUSE, on_click=act("tg_off"), style=amber))
            else:
                tele.append(ft.FilledButton(content="Включить Telegram-бота", icon=ft.Icons.PLAY_ARROW, on_click=act("tg_on"), style=green))

        # --- the program itself
        def do_restart():
            if self.guard(botctl.restart, "Программа бота запускается (около минуты). Авито после запуска на паузе."):
                self.show_bot()

        def do_stop():
            if self.guard(botctl.stop, "Программа бота остановлена."):
                self.show_bot()

        prog_buttons = [ft.OutlinedButton(
            content="Перезапустить программу бота" if st["running"] else "Запустить программу бота", icon=ft.Icons.RESTART_ALT,
            on_click=lambda e: self.confirm("Перезапустить программу бота? После запуска Авито будет на паузе — нажмите «Включить».",
                                            do_restart) if st["running"] else do_restart())]
        if st["running"]:
            prog_buttons.append(ft.TextButton(content="Остановить программу бота", icon=ft.Icons.STOP,
                                              on_click=lambda e: self.confirm(
                                                  "Остановить программу бота? Ни Авито, ни Telegram работать не будут.", do_stop)))
        since = (f"Последний запуск программы: {datetime.datetime.fromtimestamp(st['last_restart'] + 3 * 3600, datetime.UTC):%d.%m %H:%M} МСК"
                 if st["last_restart"] else "")
        prog_state = (state_row(ft.Icons.CHECK_CIRCLE, ft.Colors.TERTIARY, "Программа работает") if st["running"] else
                      state_row(ft.Icons.POWER_OFF, ft.Colors.ERROR, "Программа не запущена"))
        self.body.content = ft.Column([
            ui.page_title("Бот"),
            section("Авито: ответы покупателям", *avito),
            section("Telegram-бот", *tele),
            section("Программа бота", prog_state,
                    ft.Text("Запуск занимает около минуты. После него Авито всегда на паузе, Telegram — как был.", color=GREY),
                    ft.Row(prog_buttons, wrap=True),
                    ft.Text(since, size=12, color=GREY) if since else ft.Container()),
            section("Расходы на OpenAI", spend_text(),
                    subtitle="Сколько стоили все запросы бота к GPT (живые чаты и тестовый чат). "
                             "Обнуляется сам, когда вставляют новый ключ OpenAI на вкладке «Токены»."),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.page.update()

    def on_nav(self, e):
        i = e.control.selected_index
        [self.show_bot, self.show_products, self.show_shipping, self.show_prompt, self.show_tokens][i]()

    # ------------------------------------------------------------ products
    def show_products(self, pid=None):
        self.view = "other"
        prods = store.list_products()
        if pid:
            self.selected = pid
        if not any(p["id"] == self.selected for p in prods):
            self.selected = prods[0]["id"] if prods else None
        items = []
        for p in prods:
            items.append(ft.ListTile(
                title=ft.Text(p["name"]),
                subtitle=ft.Text(("в наличии" + (", кроме " + ", ".join(map(str, p.get("out_sizes") or []))
                                                 if p.get("out_sizes") else "")) if p["in_stock"] else "нет в наличии",
                                 color=(ft.Colors.SECONDARY if p.get("out_sizes") else ft.Colors.TERTIARY)
                                 if p["in_stock"] else ft.Colors.ERROR),
                selected=p["id"] == self.selected, data=p["id"],
                on_click=lambda e: self.show_products(e.control.data)))
        left = ft.Column([
            ui.page_title("Товары"),
            ft.Column(items, spacing=0),
            ft.Button(content="Добавить товар", icon=ft.Icons.ADD, on_click=self.new_product_dialog),
        ], width=260, spacing=10)
        right = self.product_editor(self.selected) if self.selected else ft.Text("Товаров пока нет")
        self.body.content = ft.Row([left, ft.VerticalDivider(width=1),
                                    ft.Column([right], expand=True, scroll=ft.ScrollMode.AUTO,
                                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH)],
                                   expand=True, vertical_alignment=ft.CrossAxisAlignment.START)
        self.page.update()

    def product_editor(self, pid):
        p = store.get_product(pid)
        key = store.raw_key(pid)
        name = ft.TextField(label="Название", value=p["name"])
        match = ft.TextField(label="Слова для узнавания объявления (через запятую)", value=", ".join(p["match"]),
                             helper="Если любое из этих слов есть в названии объявления на Авито, бот считает, что спрашивают про этот товар")
        warehouse = store.stock_source() == "warehouse"  # stock comes from HUB BRO: the switches only show it
        stock = ft.Switch(label="В наличии", value=p["in_stock"], disabled=warehouse)
        labels = store.size_labels()
        gone = set(p.get("out_sizes") or [])
        size_sw = {n: ft.Switch(label=f"{n} {labels.get(n, '')}".strip(), value=n not in gone, width=120)
                   for n in store.product_sizes(pid)}

        def sizes_enabled(_=None):  # the whole product off -> the per-size switches don't matter
            for sw in size_sw.values():
                sw.disabled = warehouse or not stock.value
            if _ is not None:
                self.page.update()
        stock.on_change = sizes_enabled
        sizes_enabled()
        mat = ft.TextField(label="Материал — короткий ответ", value=p["material"] or "", hint_text="98% хлопок")
        comp = ft.TextField(label="Полный состав (если переспрашивают)", value=p["composition"] or "",
                            hint_text="98% хлопок, 2% полиэстер")
        dens = ft.TextField(label="Плотность / тянутся ли", value=p["density"] or "", hint_text="немного тянутся")

        def save_main(e):
            def do():
                store.save_product(pid, name.value, match.value, stock.value,
                                   [n for n, sw in size_sw.items() if not sw.value])
                store.save_material(pid, mat.value, comp.value, dens.value)
            if self.guard(do, "Сохранено. Бот подхватит изменения сам в течение минуты."):
                self.show_products(pid)

        main = section(
            "Основное",
            name, match,
            ft.Row([ft.Icon(ft.Icons.LOCK, size=16, color=GREY),
                    ft.Text(f"Внутреннее имя: {key or '—'} (не меняется: на него завязан подбор размеров)", size=12, color=GREY,
                            expand=True)]),
            stock,
            *([ft.Text("Наличие берётся со склада (HUB BRO): здесь оно только показано, менять его — на складе.",
                       size=12, color=ft.Colors.SECONDARY)] if warehouse else []),
            ft.Text("Если выключить, бот на вопросы о наличии и заказе отвечает, что модели временно нет в наличии.",
                    size=12, color=GREY),
            ft.Text("Наличие по размерам", weight=ft.FontWeight.BOLD),
            ft.Row(list(size_sw.values()), wrap=True, spacing=8, run_spacing=4),
            ft.Text("Выключенный размер: если он подходит покупателю или его спрашивают, бот говорит, что этого размера "
                    "временно нет, и не предлагает его заказать. Не забудьте «Сохранить».", size=12, color=GREY),
            ft.Divider(),
            ft.Text("Материал", weight=ft.FontWeight.BOLD), mat, comp, dens,
            ft.FilledButton(content="Сохранить", icon=ft.Icons.SAVE, on_click=save_main))
        return ft.Column([ui.page_title(p["name"], "Товар"), main,
                          self.hw_section(pid), self.photos_section(pid, p), self.table_section(pid),
                          self.chart_section(pid, p)],
                         spacing=16, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    async def new_product_dialog(self, e):
        name = ft.TextField(label="Название", autofocus=True)
        folder = ft.TextField(label="Имя папки латиницей", hint_text="mom-jeans")
        key = ft.TextField(label="Внутреннее имя (необязательно)", helper="Потом поменять нельзя. Пусто = как название")

        def create(_):
            if self.guard(lambda: store.create_product(folder.value.strip(), name.value, key.value)):
                self.page.pop_dialog()  # close the dialog before the toast (pop_dialog closes the topmost one)
                self.show_products(folder.value.strip())
                self.toast("Товар добавлен. Заполните слова для узнавания, сетку и фото.")

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Новый товар (джинсы, та же размерная шкала)"),
            content=ft.Column([name, folder, key], tight=True, width=420),
            actions=[ft.TextButton(content="Отмена", on_click=lambda _: self.page.pop_dialog()),
                     ft.FilledButton(content="Создать", on_click=create)]))

    # photos
    def photos_section(self, pid, p):
        tiles = []
        for path in p["photos"]:
            fname = path.replace("\\", "/").rsplit("/", 1)[-1]
            img = ft.Image(src=read_bytes(path), width=150, height=190, fit=ft.BoxFit.COVER,
                           border_radius=ft.BorderRadius.all(8),
                           error_content=ft.Container(ft.Text(fname, size=11), width=150, height=190,
                                                      bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, alignment=ft.Alignment.CENTER))
            tiles.append(ft.Column([img, ft.Row([
                ft.Text(fname, size=11, color=GREY, width=110, no_wrap=True),
                ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Удалить фото", data=fname,
                              on_click=lambda e: self.confirm(f"Удалить фото {e.control.data}?",
                                                              lambda n=e.control.data: self.delete_photo(pid, n)))],
                spacing=0)], spacing=2))

        async def add(e):
            files = await self.picker.pick_files(dialog_title="Фото товара", allow_multiple=True,
                                                 file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=PHOTO_TYPES)
            paths = [f.path for f in files or [] if f.path]
            if paths and self.guard(lambda: store.add_photos(pid, paths), f"Добавлено фото: {len(paths)}"):
                self.show_products(pid)

        return section("Фото на человеке", ft.Row(tiles, wrap=True, spacing=12) if tiles else ft.Text("Фото нет", color=GREY),
                       ft.Button(content="Добавить фото", icon=ft.Icons.ADD_PHOTO_ALTERNATE, on_click=add),
                       subtitle="Бот отправляет их на «как сидят?». Можно jpg, png, heic, webp.")

    def delete_photo(self, pid, name):
        if self.guard(lambda: store.delete_photo(pid, name), "Фото удалено"):
            self.show_products(pid)

    def confirm(self, text, action):
        def yes(_):
            self.page.pop_dialog()
            action()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(text),
            actions=[ft.TextButton(content="Нет", on_click=lambda _: self.page.pop_dialog()),
                     ft.FilledButton(content="Да", on_click=yes)]))

    # size by height and weight (how the bot picks most sizes)
    def hw_section(self, pid):
        all_labels = store.size_labels()
        labels = {n: all_labels.get(n, str(n)) for n in store.product_sizes(pid)}  # only this product's sizes (HUB BRO)
        hw, prefilled = store.read_hw(pid), False
        if not hw:  # new product: start from another product's table
            for other in store.list_products():
                if other["id"] != pid and other["hw"]:
                    hw, prefilled = other["hw"], True
                    break
        hw = hw or {"heights": [170], "weights": [70], "cells": [[None]]}
        order = sorted(labels)
        W = 52  # cell width

        def tint(f):
            v = (f.value or "").strip()
            k = order.index(int(v)) if v.isdigit() and int(v) in labels else None
            f.fill_color = (ft.Colors.with_opacity(0.08 + 0.42 * k / max(1, len(order) - 1), ft.Colors.PRIMARY)
                            if k is not None else None)

        def cell(v):
            f = ft.TextField(value="" if v is None else str(v), width=W, dense=True, text_size=13,
                             text_align=ft.TextAlign.CENTER, content_padding=ft.Padding.symmetric(horizontal=2, vertical=8))
            tint(f)

            def changed(e):
                tint(e.control)
                e.control.update()
            f.on_change = changed
            return f

        head = lambda v: ft.TextField(value=str(v), width=W, dense=True, text_size=12, text_align=ft.TextAlign.CENTER,
                                      text_style=ft.TextStyle(font_family=ui.MONO, weight=ft.FontWeight.W_600),
                                      content_padding=ft.Padding.symmetric(horizontal=2, vertical=8))
        hs = [head(h) for h in hw["heights"]]
        ws = [head(w) for w in hw["weights"]]
        cells = [[cell(c) for c in row] for row in hw["cells"]]

        def build():
            corner = ft.Container(ui.hint("рост \\ вес", text_align=ft.TextAlign.CENTER), width=80,
                                  alignment=ft.Alignment.CENTER)
            top = ft.Row([corner] + [ft.Column([f, ft.IconButton(icon=ft.Icons.CLOSE, icon_size=12, tooltip="Убрать колонку",
                                                                 data=j, on_click=lambda e: del_col(e.control.data))],
                                               spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
                                     for j, f in enumerate(ws)], spacing=4)
            rows = [ft.Row([ft.Row([ft.IconButton(icon=ft.Icons.CLOSE, icon_size=12, tooltip="Убрать строку", data=i,
                                                  on_click=lambda e: del_row(e.control.data)), hs[i]], spacing=0, width=80)]
                           + cells[i], spacing=4) for i in range(len(hs))]
            return [top] + rows

        def add_row(e):
            last = int(hs[-1].value) if (hs[-1].value or "").strip().isdigit() else 0
            hs.append(head(last + 5))
            cells.append([cell(c.value) for c in cells[-1]])
            grid.controls = build(); self.page.update()

        def add_col(e):
            last = int(ws[-1].value) if (ws[-1].value or "").strip().isdigit() else 0
            ws.append(head(last + 5))
            for row in cells:
                row.append(cell(row[-1].value))
            grid.controls = build(); self.page.update()

        def del_row(i):
            if len(hs) > 1:
                hs.pop(i); cells.pop(i)
                grid.controls = build(); self.page.update()

        def del_col(j):
            if len(ws) > 1:
                ws.pop(j)
                for row in cells:
                    row.pop(j)
                grid.controls = build(); self.page.update()

        h_try, w_try = (ft.TextField(label=l, width=110, dense=True) for l in ("Рост, см", "Вес, кг"))
        result = ft.Text("", size=16, weight=ft.FontWeight.BOLD)

        def check(e):
            try:
                h, w = int(h_try.value), int(w_try.value)
                near = lambda fs, v: min(range(len(fs)), key=lambda i: (abs(int(fs[i].value) - v), -int(fs[i].value)))
                hi, wi = near(hs, h), near(ws, w)  # nearest height and weight, a tie goes to the bigger (as the bot does)
                v = cells[hi][wi].value.strip()
                result.value = (f"Бот ответит: {labels[int(v)]} размер (рост {hs[hi].value}, вес {ws[wi].value})"
                                if v.isdigit() and int(v) in labels
                                else "Пустая клетка: бот не называет размер, чат уходит менеджеру")
            except ValueError:
                result.value = "Введите рост и вес числами (и в шапке таблицы тоже числа)"
            self.page.update()

        def save(e):
            if self.guard(lambda: store.save_hw(pid, [f.value for f in hs], [f.value for f in ws],
                                                [[c.value for c in row] for row in cells]),
                          "Таблица сохранена. Бот начнёт отвечать по ней в течение минуты."):
                self.show_products(pid)

        grid = ft.Column(build(), spacing=4)
        notes = [ui.hint("Числа в шапке — рост, см (строки) и вес, кг (колонки). Бот берёт строку с ближайшим ростом и колонку "
                         "с ближайшим весом (если ровно посередине — большую). В клетке — размер этого товара: "
                         + ", ".join(map(str, labels)) + " (размеры задаются в HUB BRO). Пустая клетка — бот не называет "
                         "размер, а передаёт чат менеджеру."),
                 ui.hint("Если рост и вес явно не сходятся (например 180 см и 30 кг), бот тоже отдаёт чат менеджеру.")]
        if prefilled:
            notes.insert(0, ui.hint("Таблица взята у другого товара — проверьте и сохраните.", color=ft.Colors.SECONDARY))
        return section("Размер по росту и весу", *notes, ft.Row([grid], scroll=ft.ScrollMode.AUTO),
                       ft.Row([ft.Button(content="Добавить строку роста", icon=ft.Icons.ADD, on_click=add_row),
                               ft.Button(content="Добавить колонку веса", icon=ft.Icons.ADD, on_click=add_col),
                               ft.FilledButton(content="Сохранить таблицу", icon=ft.Icons.SAVE, on_click=save)], wrap=True),
                       ft.Divider(),
                       ft.Row([h_try, w_try, ft.Button(content="Проверить", icon=ft.Icons.SEARCH, on_click=check), result],
                              wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                       subtitle="Так бот подбирает большинство размеров: покупатель пишет рост и вес — бот берёт клетку "
                                "из этой таблицы.")

    # size table
    def table_section(self, pid):
        all_labels = store.size_labels()
        labels = {n: all_labels.get(n, str(n)) for n in store.product_sizes(pid)}  # one row per size of this product (HUB BRO)
        cols, rows = store.read_table(pid)
        prefilled = False
        if not cols:  # new product: start from the columns another product already uses
            for other in store.list_products():
                c, _ = store.read_table(other["id"])
                if c:
                    cols, prefilled = c, True
                    break
        by_size = {int(r["size"]): r for r in rows}
        col_fields = [ft.TextField(value=c, width=150, text_size=11, dense=True, tooltip=store.HEAD.get(c)) for c in cols]
        cells = {s: [ft.TextField(value=by_size.get(s, {}).get(c, ""), width=150, text_size=13, dense=True,
                                  text_align=ft.TextAlign.CENTER) for c in cols] for s in labels}

        def save(e):
            names = [f.value for f in col_fields]
            data = [{"size": s, **{n: cells[s][i].value for i, n in enumerate(names)}}
                    for s in labels if any(f.value.strip() for f in cells[s])]
            if self.guard(lambda: store.save_table(pid, [store.norm_column(n) for n in names],
                                                   [{k if k == "size" else store.norm_column(k): v for k, v in r.items()} for r in data]),
                          "Сетка сохранена. Картинку сетки можно перерисовать ниже."):
                self.show_products(pid)

        def add_col(e):
            col_fields.append(ft.TextField(value="новая_колонка", width=150, text_size=11, dense=True))
            for s in labels:
                cells[s].append(ft.TextField(width=150, text_size=13, dense=True, text_align=ft.TextAlign.CENTER))
            grid.controls = build()
            self.page.update()

        def del_col(i):
            col_fields.pop(i)
            for s in labels:
                cells[s].pop(i)
            grid.controls = build()
            self.page.update()

        def build():
            head = ft.Row([ft.Text("Размер", width=90, weight=ft.FontWeight.BOLD)] +
                          [ft.Column([f, ft.Text(store.HEAD.get(store.norm_column(f.value or ""), ""), size=11, color=GREY,
                                                 width=150, text_align=ft.TextAlign.CENTER),
                                      ft.IconButton(icon=ft.Icons.CLOSE, icon_size=14, tooltip="Убрать колонку",
                                                    data=i, on_click=lambda e: del_col(e.control.data))],
                                     spacing=0, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
                           for i, f in enumerate(col_fields)])
            return [head] + [ft.Row([ft.Text(f"{s} ({labels[s]})", width=90, weight=ft.FontWeight.BOLD)] + cells[s])
                             for s in labels]

        grid = ft.Column(build(), spacing=4)
        known = ", ".join(sorted(store.FIT_COLUMNS))
        notes = [ft.Text("Полуобхваты и длины в см. Пустая строка = такого размера нет.", size=12, color=GREY),
                 ft.Text(f"Бот сверяет замеры покупателя только по колонкам: {known}.", size=12, color=GREY)]
        if prefilled:
            notes.insert(0, ft.Text("Колонки взяты у другого товара — заполните цифры и сохраните.", size=12,
                                    color=ft.Colors.SECONDARY))
        return section("Размерная сетка", *notes, ft.Row([grid], scroll=ft.ScrollMode.AUTO),
                       ft.Row([ft.Button(content="Добавить колонку", icon=ft.Icons.ADD, on_click=add_col),
                               ft.FilledButton(content="Сохранить сетку", icon=ft.Icons.SAVE, on_click=save)]),
                       subtitle="Строки — размеры этого товара из HUB BRO: " + ", ".join(map(str, labels)) + ".")

    # size chart picture
    def chart_section(self, pid, p):
        preview = (ft.Image(src=read_bytes(p["chart"]), width=640, fit=ft.BoxFit.CONTAIN, border_radius=ft.BorderRadius.all(8))
                   if p["chart"] else ft.Text("Картинки сетки нет", color=GREY))

        def draw(e):
            if self.guard(lambda: store.draw_chart(pid), "Картинка сетки нарисована из таблицы"):
                self.show_products(pid)

        async def upload(e):
            files = await self.picker.pick_files(dialog_title="Скрин размерной сетки", file_type=ft.FilePickerFileType.CUSTOM,
                                                 allowed_extensions=CHART_TYPES)
            if files and files[0].path and self.guard(lambda: store.upload_chart(pid, files[0].path), "Скрин сетки загружен"):
                self.show_products(pid)

        def ask_draw(e):
            if p["chart"]:
                self.confirm("Заменить текущую картинку сетки нарисованной из таблицы?", lambda: draw(None))
            else:
                draw(None)

        return section("Картинка размерной сетки", preview,
                       ft.Row([ft.Button(content="Нарисовать из таблицы", icon=ft.Icons.AUTO_FIX_HIGH, on_click=ask_draw),
                               ft.Button(content="Загрузить готовый скрин", icon=ft.Icons.UPLOAD_FILE, on_click=upload)]),
                       subtitle="Бот отправляет её, когда просят замеры или сетку.")

    # ------------------------------------------------------------ shipping cutoffs
    def show_shipping(self):
        self.view = "other"
        c = store.read_cutoffs()
        wd = ft.TextField(label="Будни (пн–пт)", value=c["weekday_cutoff"], width=180, hint_text="19:30")
        we = ft.TextField(label="Выходные (сб–вс)", value=c["weekend_cutoff"], width=180, hint_text="18:30")
        now = msk_now()
        cur = c["weekend_cutoff"] if now.weekday() >= 5 else c["weekday_cutoff"]
        h, m = (int(x) for x in cur.split(":"))
        today = (now.hour, now.minute) < (h, m)
        status = ft.Text(f"Сейчас по Москве {now:%H:%M}, {['пн','вт','ср','чт','пт','сб','вс'][now.weekday()]} — "
                         f"бот скажет «отправим {'сегодня' if today else 'завтра'}».", color=GREY)

        def save(e):
            if self.guard(lambda: store.save_cutoffs(wd.value, we.value), "Сохранено. Бот подхватит сам."):
                self.show_shipping()

        self.body.content = ft.Column([
            ui.page_title("Отправка"),
            section("До какого времени отправляем сегодня",
                    ft.Text("Время московское. Если заказ сделан позже — бот говорит, что отправим завтра.", color=GREY),
                    ft.Row([wd, we]), status,
                    ft.FilledButton(content="Сохранить", icon=ft.Icons.SAVE, on_click=save)),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.page.update()

    # ------------------------------------------------------------ GPT prompt (everyone)
    def show_prompt(self):
        """The whole prompt GPT gets before every answer: an editor, the same text with the inserts in colour (optionally
        with example values from the bot), and the list of inserts. Saved to prompt.txt; the bot uses it from the next answer."""
        self.view = "other"
        saved, is_default = store.read_prompt()
        inserts = store.prompt_inserts()
        products = store.list_products()
        st = {"examples": None, "chat": ""}
        ins_style = ft.TextStyle(color=ft.Colors.PRIMARY, weight=ft.FontWeight.W_700,
                                 bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.PRIMARY))
        bad_style = ft.TextStyle(color=ft.Colors.ERROR, weight=ft.FontWeight.W_700,
                                 bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.ERROR))
        mono = ft.TextStyle(font_family=ui.MONO, size=13)

        editor = ft.TextField(value=saved, multiline=True, min_lines=16, max_lines=28, text_style=mono, expand=True,
                              on_change=lambda e: refresh())
        warn = ft.Text(color=ft.Colors.SECONDARY, visible=False)
        state = ui.hint("")
        preview = ft.Text(selectable=True, style=mono)
        show_ex = ft.Switch(label="Подставить пример вместо {вставок}", value=False, on_change=lambda e: refresh())
        prod_dd = ft.Dropdown(label="Товар для примера", width=420, value=products[0]["id"] if products else None,
                              options=[ft.DropdownOption(key=p["id"], text=p["name"]) for p in products],
                              on_select=lambda e: load_examples())
        ex_note = ui.hint("")
        ins_list = ft.Column(spacing=14)
        history = store.prompt_history()
        hist_dd = ft.Dropdown(label="Прошлые версии", width=260, options=[ft.DropdownOption(key=str(f), text=lbl)
                                                                         for lbl, f in history], visible=bool(history))

        def chip(name, error=False):
            return ft.Container(ft.Text("{" + name + "}", font_family=ui.MONO, size=13, weight=ft.FontWeight.W_700,
                                        color=ft.Colors.ERROR if error else ft.Colors.PRIMARY),
                                bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.ERROR if error else ft.Colors.PRIMARY),
                                padding=ft.Padding.symmetric(horizontal=8, vertical=3), border_radius=ui.RADIUS_SMALL)

        def spans(text):
            ex = st["examples"] if show_ex.value else None
            out, pos = [], 0
            for m in store.INSERT.finditer(text):
                if m.start() > pos:
                    out.append(ft.TextSpan(text[pos:m.start()]))
                name = m.group(1)
                if name not in inserts:
                    out.append(ft.TextSpan(m.group(0), style=bad_style))
                elif ex is not None and name in ex:
                    out.append(ft.TextSpan(ex[name] or f"(пусто: {inserts[name][0].lower()})", style=ins_style))
                else:
                    out.append(ft.TextSpan(m.group(0), style=ins_style))
                pos = m.end()
            out.append(ft.TextSpan(text[pos:]))
            return out

        def insert_rows(missing):
            rows = []
            for name, (title, where) in inserts.items():
                parts = [ft.Row([chip(name), ft.Text(title, weight=ft.FontWeight.BOLD)], spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER),
                         ft.Text(where, color=GREY)]
                if name in missing:
                    parts.append(ft.Text(f"Этой вставки нет в тексте — GPT не получит «{title.lower()}». Чтобы вернуть, "
                                         f"напишите {{{name}}} в нужном месте.", color=ft.Colors.SECONDARY))
                if st["examples"] is not None:
                    val = st["examples"].get(name, "")
                    parts.append(ft.Container(
                        ft.Text(val or "(пусто)", style=mono, selectable=True, color=None if val else GREY),
                        padding=10, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, border_radius=ui.RADIUS_SMALL))
                rows.append(ft.Column(parts, spacing=6))
            return rows

        def refresh(update=True):
            text = editor.value or ""
            missing, unknown = store.prompt_problems(text)
            msgs = []
            if missing:
                msgs.append("Нет вставок: " + ", ".join("{" + n + "}" for n in missing) + " — GPT не получит эти данные.")
            if unknown:
                msgs.append("Бот не знает " + ", ".join("{" + n + "}" for n in unknown) +
                            " — это уйдёт в GPT как обычный текст (красным ниже).")
            warn.value, warn.visible = "\n".join(msgs), bool(msgs)
            changed = text.replace("\r\n", "\n") != saved
            state.value = ("Есть несохранённые изменения." if changed else
                           "Сейчас у бота стандартный промпт." if is_default else "Это промпт, с которым бот работает сейчас.")
            save_btn.disabled = not changed
            preview.spans = spans(text)
            ins_list.controls = insert_rows(missing)
            if update:
                self.page.update()

        def load_examples():
            if not prod_dd.value:
                return
            ex_note.value = "Считаю пример… (несколько секунд)"
            self.page.update()

            def work():
                try:
                    d = store.prompt_examples(prod_dd.value)
                    st["examples"], st["chat"] = d["values"], d.get("example_chat", "")
                    ex_note.value = (f"Пример: покупатель пишет «{st['chat']}», цена 2 990 ₽ (для примера; в чате — цена "
                                     "объявления). «Когда отправим» — на текущее время.")
                except Exception as ex:
                    traceback.print_exc()
                    st["examples"] = None
                    ex_note.value = f"Не получилось посчитать пример: {type(ex).__name__}"
                refresh()
            self.page.run_thread(work)

        def do_save(text):
            if self.guard(lambda: store.save_prompt(text), "Сохранено. Бот берёт этот промпт со следующего ответа."):
                self.show_prompt()

        def save(e):
            text = editor.value or ""
            missing, unknown = store.prompt_problems(text)
            if missing or unknown:
                self.confirm("В тексте " + ("нет вставок " + ", ".join("{" + n + "}" for n in missing) if missing else "") +
                             (" и " if missing and unknown else "") +
                             ("есть незнакомые " + ", ".join("{" + n + "}" for n in unknown) if unknown else "") +
                             ". Всё равно сохранить?", lambda: do_save(text))
            else:
                do_save(text)

        def reset_default(e):
            self.confirm("Вернуть стандартный промпт? Текущий сохранится в прошлых версиях.",
                         lambda: do_save(store.default_prompt()))

        def load_old(e):
            if hist_dd.value:
                editor.value = Path(hist_dd.value).read_text(encoding="utf-8")
                refresh()
                self.toast("Версия загружена в редактор. Чтобы бот её использовал, нажмите «Сохранить».")

        save_btn = ft.FilledButton(content="Сохранить", icon=ft.Icons.SAVE, on_click=save)
        refresh(update=False)
        self.body.content = ft.Column([
            ui.page_title("Промпт"),
            section("Текст для GPT",
                    ft.Text("Этот текст GPT получает перед каждым ответом покупателю, а после него — саму переписку. "
                            "Слова в фигурных скобках, например {price}, — вставки: бот сам подставляет туда данные "
                            "товара и чата (список ниже). Остальной текст уходит как написан. Изменения работают со "
                            "следующего ответа, перезапуск не нужен.", color=GREY),
                    ft.Row([editor]), warn, state,
                    ft.Row([save_btn,
                            ft.OutlinedButton(content="Отменить изменения", icon=ft.Icons.UNDO,
                                              on_click=lambda e: self.show_prompt()),
                            ft.OutlinedButton(content="Вернуть стандартный", icon=ft.Icons.RESTART_ALT, on_click=reset_default)],
                           wrap=True),
                    ft.Row([hist_dd, ft.OutlinedButton(content="Загрузить в редактор", icon=ft.Icons.HISTORY,
                                                       on_click=load_old, visible=bool(history))], wrap=True)),
            section("Как это видит GPT", ft.Row([prod_dd, show_ex], wrap=True), ex_note,
                    ft.Row([ft.Container(preview, padding=14, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH, expand=True,
                                         border_radius=ui.RADIUS_SMALL)]),
                    subtitle="Вставки выделены цветом; красным — то, что бот не знает. Включите пример, чтобы увидеть "
                             "текст целиком, как его получит GPT."),
            section("Вставки", ins_list,
                    subtitle="Что бот подставляет на место каждой {вставки} и откуда это берётся. Сами данные меняются "
                             "на других вкладках («Товары», «Отправка»); здесь — только где они стоят в тексте. "
                             "Под каждой — пример значения для выбранного товара."),
            ui.hint("Кроме этого текста GPT получает описания инструментов (фото, сетка, заказ, передать менеджеру, "
                    "ничего не делать). Это меняется только в коде."),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.page.update()
        load_examples()

    # ------------------------------------------------------------ tokens (admin)
    def show_tokens(self):
        self.view = "other"
        if not self.admin:
            pin = ft.TextField(label="PIN администратора", password=True, width=260, autofocus=True)

            def enter(e):
                if sec.check_pin(pin.value or ""):
                    self.admin = True
                    self.show_tokens()
                else:
                    pin.value = ""
                    self.toast("Неверный PIN", error=True)
                    self.page.update()

            pin.on_submit = enter
            self.body.content = ft.Column([
                ui.page_title("Токены"),
                self.public_tokens_section(),
                section("Остальные ключи — только для администратора", pin,
                        ft.FilledButton(content="Войти", icon=ft.Icons.LOCK_OPEN, on_click=enter))],
                spacing=16, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
            self.page.update()
            return

        status = sec.token_status()
        admin_tokens = {n: l for n, l in sec.TOKENS.items() if n not in sec.PUBLIC_TOKENS}
        fields = {n: ft.TextField(label=label, password=True, hint_text="новое значение (пусто = не менять)", width=460)
                  for n, label in admin_tokens.items()}
        rows = []
        for n, label in admin_tokens.items():
            st = f"задан, изменён {status[n]}" if status[n] else "не задан"
            rows.append(ft.Column([fields[n], ft.Text(st, size=12, color=ft.Colors.TERTIARY if status[n] else ft.Colors.ERROR)],
                                  spacing=2))

        def save(e):
            new = {n: f.value for n, f in fields.items() if f.value and f.value.strip()}
            if not new:
                self.toast("Нечего сохранять: все поля пустые", error=True)
                return
            if self.guard(lambda: sec.set_tokens(new),
                          "Сохранено. Новые токены начнут работать после перезапуска бота."):
                self.show_tokens()

        new_pin, new_pin2 = (ft.TextField(label=l, password=True, width=220) for l in ("Новый PIN", "Ещё раз"))

        def change_pin(e):
            def do():
                if len(new_pin.value or "") < 4:
                    raise ValueError("PIN: минимум 4 символа")
                if new_pin.value != new_pin2.value:
                    raise ValueError("PIN не совпадает")
                sec.change_pin(new_pin.value)
            if self.guard(do, "PIN изменён"):
                self.show_tokens()

        def lock(e):
            self.admin = False
            self.show_tokens()

        self.body.content = ft.Column([
            ft.Row([ui.page_title("Токены"),
                    ft.TextButton(content="Выйти из режима администратора", icon=ft.Icons.LOCK, on_click=lock)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            self.public_tokens_section(),
            section("Ключи бота (администратор)", *rows,
                    ft.FilledButton(content="Сохранить", icon=ft.Icons.SAVE, on_click=save),
                    subtitle="Значения хранятся зашифрованными (Windows DPAPI) и здесь не показываются. "
                             "Бот читает их при запуске: после смены перезапустите бота."),
            section("PIN администратора", ft.Row([new_pin, new_pin2]),
                    ft.Button(content="Сменить PIN", on_click=change_pin)),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.page.update()

    def public_tokens_section(self):
        """Keys anyone may change (OpenAI): no PIN. Values are never shown, only when they were changed."""
        status = sec.token_status()
        fields = {n: ft.TextField(label=sec.TOKENS[n], password=True, hint_text="вставьте новый ключ", width=460)
                  for n in sorted(sec.PUBLIC_TOKENS)}
        rows = [ft.Column([f, ft.Text(f"задан в приложении, изменён {status[n]}" if status[n]
                                      else "в приложении не задан: бот использует старый ключ, сохранённый на сервере",
                                      size=12, color=ft.Colors.TERTIARY if status[n] else GREY)], spacing=2)
                for n, f in fields.items()]
        old_exp = botctl.key_expiry()
        exp = ft.TextField(label="Ключ OpenAI действует до (ДД.ММ.ГГГГ)", width=300, hint_text="пусто — срок не указан",
                           value=datetime.date.fromisoformat(old_exp).strftime("%d.%m.%Y") if old_exp else "")

        def save(e):
            new = {n: f.value for n, f in fields.items() if f.value and f.value.strip()}
            typed = (exp.value or "").strip()
            try:
                day = datetime.datetime.strptime(typed, "%d.%m.%Y").date().isoformat() if typed else None
            except ValueError:
                self.toast("Дата — в виде ДД.ММ.ГГГГ, например 31.12.2026", error=True)
                return
            if not new and day == old_exp:
                self.toast("Нечего сохранять: вставьте ключ или измените дату", error=True)
                return
            def do():
                if new:
                    sec.set_tokens(new)
                    if "OPENAI_API_KEY" in new:
                        botctl.reset_gpt_spend()  # a new key = the spend counter starts from $0
                if day != old_exp or new:
                    botctl.set_key_expiry(day)  # a new key gets the date typed now (empty = no date)
            done = ("Ключ сохранён. Он заработает после перезапуска бота. Счётчик расходов обнулён." if new
                    else "Срок ключа сохранён. Бот увидит его сразу, перезапуск не нужен.")
            if self.guard(do, done):
                self.show_tokens()

        def do_restart():
            if self.guard(botctl.restart, "Бот перезапускается (около минуты). Потом нажмите «Включить»."):
                self.show_bot()

        return section(
            "Ключ OpenAI", *rows, exp,
            ui.hint("Последний день, когда ключ работает. Бот напомнит админам в Telegram за 7 дней, за 1 день и в этот "
                    "день. Вставляете новый ключ — укажите и его срок."),
            spend_text(),
            ft.Row([ft.FilledButton(content="Сохранить", icon=ft.Icons.SAVE, on_click=save),
                    ft.OutlinedButton(content="Применить: перезапустить бота", icon=ft.Icons.RESTART_ALT,
                                      on_click=lambda e: self.confirm(
                                          "Перезапустить бота, чтобы он взял новый ключ? После запуска он будет на паузе — "
                                          "нажмите «Включить» на вкладке «Бот».", do_restart))], wrap=True),
            subtitle="Менять может любой. Ключ хранится зашифрованным и здесь не показывается. "
                     "Бот читает его при запуске — после замены перезапустите бота.")

    # ------------------------------------------------------------ first run (admin, on this PC)
    def show_setup(self):
        pin, pin2 = (ft.TextField(label=l, password=True, width=260) for l in ("PIN администратора", "PIN ещё раз"))
        required = ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET", "TG_BOT_TOKEN")
        fields = {n: ft.TextField(label=label + ("" if n in required else " — можно позже, вкладка «Токены»"),
                                  password=True, width=460) for n, label in sec.TOKENS.items()}

        def done(e):
            def do():
                if len(pin.value or "") < 4:
                    raise ValueError("PIN: минимум 4 символа")
                if pin.value != pin2.value:
                    raise ValueError("PIN не совпадает")
                missing = [sec.TOKENS[n] for n in required if not (fields[n].value or "").strip()]
                if missing:
                    raise ValueError("Нужно заполнить: " + ", ".join(missing))
                sec.setup(pin.value, {n: f.value for n, f in fields.items()})
                if (fields["OPENAI_API_KEY"].value or "").strip():
                    botctl.reset_gpt_spend()
            if self.guard(do, "Готово. Приложение привязано к этому компьютеру."):
                self.start()

        self.page.add(ft.Container(ft.Column([
            ui.page_title("Первый запуск на этом компьютере", "Настройка"),
            ft.Text("Для администратора. Токены шифруются и работают только на этом компьютере под этой учётной записью Windows.",
                    color=GREY),
            ft.Text("PIN понадобится, чтобы потом менять токены. Ключи Авито и токен Telegram обязательны.", color=GREY),
            ft.Row([pin, pin2]), *fields.values(),
            ft.Row([ft.Text("Код этого компьютера для лицензии BOT BRO:", size=12, color=GREY),
                    ft.Text(botctl.device_code(), size=12, selectable=True, font_family=ui.MONO, weight=ft.FontWeight.W_600)]),
            ft.FilledButton(content="Сохранить и начать", icon=ft.Icons.CHECK, on_click=done)],
            spacing=12, scroll=ft.ScrollMode.AUTO), padding=24, expand=True))


def quiet(page):
    """Not set up here / copied from another PC: an empty window, no explanation (SPEC: «тихо не работает»)."""
    page.add(ft.Container(ft.ProgressRing(), alignment=ft.Alignment.CENTER, expand=True))


def main(page: ft.Page):
    page.title = TITLE
    ui.apply(page)  # light/dark follows Windows
    page.window.width, page.window.height = 1280, 860
    if ICON.exists():
        page.window.icon = str(ICON)
    app = App(page)
    state = sec.state()
    if state == "ok":
        app.start()
    elif state == "new":  # never set up on this PC: the same shortcut opens the first-run form
        app.show_setup()
    else:
        quiet(page)


if __name__ == "__main__":
    ft.run(main)  # dev preview in a browser: FLET_FORCE_WEB_SERVER=1 FLET_SERVER_PORT=8555
