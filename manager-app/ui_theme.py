"""Look of the manager app: light/dark follows Windows (ThemeMode.SYSTEM); palette, fonts and shapes after
teacher57.github.io (warm neutral greys, bold Segoe UI headings, small monospace labels and buttons, thin 1px borders,
14px cards, one blue accent). Code uses the semantic colors below, never fixed ones, so both themes just work:
  ON_SURFACE_VARIANT = muted text, TERTIARY = good (green), ERROR = bad (red), SECONDARY = warning (amber)."""
import flet as ft

SANS = "Segoe UI"
MONO = "Consolas"
RADIUS = 14          # cards, dialogs
RADIUS_SMALL = 8     # buttons, chips, inputs

LIGHT = dict(bg="#fcfcfb", surface="#ffffff", surface2="#f4f3f0", border="#e4e3df", border_strong="#cfcec9",
             text="#0b0b0b", text2="#52514e", text3="#75746f", accent="#2a78d6", accent_ink="#1c5cab",
             bad="#d03b3b", good="#0a7d0a", warn="#b45309", on_color="#ffffff")
DARK = dict(bg="#121211", surface="#1a1a19", surface2="#232321", border="#2f2f2d", border_strong="#45453f",
            text="#f4f4f2", text2="#c3c2b7", text3="#9a9990", accent="#3987e5", accent_ink="#86b6ef",
            bad="#e66767", good="#3fbf3f", warn="#e0a050", on_color="#121211")


def _theme(c):
    mono = lambda size=13, weight=ft.FontWeight.W_600, color=None: ft.TextStyle(font_family=MONO, size=size, weight=weight,
                                                                                color=color)
    button = lambda **kw: ft.ButtonStyle(text_style=mono(), shape=ft.RoundedRectangleBorder(radius=RADIUS_SMALL),
                                         padding=ft.Padding.symmetric(horizontal=18, vertical=14), elevation=0, **kw)
    return ft.Theme(
        font_family=SANS,
        use_material3=True,
        scaffold_bgcolor=c["bg"],
        color_scheme=ft.ColorScheme(
            primary=c["accent"], on_primary="#ffffff",
            primary_container=c["surface2"], on_primary_container=c["accent_ink"],
            secondary=c["warn"], on_secondary=c["on_color"],
            secondary_container=c["surface2"], on_secondary_container=c["text"],
            tertiary=c["good"], on_tertiary=c["on_color"],
            error=c["bad"], on_error="#ffffff",
            surface=c["bg"], on_surface=c["text"], on_surface_variant=c["text3"],
            surface_container_lowest=c["surface"], surface_container_low=c["surface"],
            surface_container=c["surface"], surface_container_high=c["surface2"],
            surface_container_highest=c["surface2"], surface_tint="#00000000",
            outline=c["border_strong"], outline_variant=c["border"],
            inverse_surface=c["text"], on_inverse_surface=c["bg"], inverse_primary=c["accent_ink"]),
        text_theme=ft.TextTheme(body_medium=ft.TextStyle(size=14, color=c["text"]),
                                body_large=ft.TextStyle(size=15, color=c["text"])),
        filled_button_theme=ft.FilledButtonTheme(style=button()),
        button_theme=ft.ButtonTheme(style=button(bgcolor=c["surface"], color=c["text"],
                                                 side=ft.BorderSide(1, c["border"]))),
        outlined_button_theme=ft.OutlinedButtonTheme(style=button(color=c["text"], side=ft.BorderSide(1, c["border_strong"]))),
        text_button_theme=ft.TextButtonTheme(style=button(color=c["accent_ink"])),
        navigation_rail_theme=ft.NavigationRailTheme(
            bgcolor=c["bg"], indicator_color=c["surface2"],
            indicator_shape=ft.RoundedRectangleBorder(radius=RADIUS_SMALL),
            selected_label_text_style=mono(12, ft.FontWeight.W_600, c["text"]),
            unselected_label_text_style=mono(12, ft.FontWeight.W_500, c["text3"])),
        list_tile_theme=ft.ListTileTheme(selected_tile_color=c["surface2"], selected_color=c["text"],
                                         shape=ft.RoundedRectangleBorder(radius=RADIUS_SMALL)),
        dialog_theme=ft.DialogTheme(bgcolor=c["surface"], shape=ft.RoundedRectangleBorder(radius=RADIUS)),
        snackbar_theme=ft.SnackBarTheme(bgcolor=c["text"], content_text_style=ft.TextStyle(color=c["bg"], size=14),
                                        shape=ft.RoundedRectangleBorder(radius=RADIUS_SMALL)),
        divider_theme=ft.DividerTheme(color=c["border"], thickness=1),
        input_decoration_theme=ft.InputDecorationTheme(filled=True, fill_color=c["surface"],
                                                       hint_style=ft.TextStyle(color=c["text3"])),
    )


def apply(page: ft.Page):
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = _theme(LIGHT)
    page.dark_theme = _theme(DARK)


def eyebrow(text):
    """Small monospace accent label above a page title (like the site's «LLM Evaluation & …» line)."""
    return ft.Text(text, font_family=MONO, size=12, weight=ft.FontWeight.W_500, color=ft.Colors.PRIMARY)


def page_title(text, label="BRO x BRO SHOP"):
    return ft.Column([eyebrow(label), ft.Text(text, size=30, weight=ft.FontWeight.BOLD)], spacing=2)


def hint(text, **kw):
    """Muted small text in monospace (the site's meta lines: dates, statuses)."""
    return ft.Text(text, font_family=MONO, size=12, color=kw.pop("color", ft.Colors.ON_SURFACE_VARIANT), **kw)
