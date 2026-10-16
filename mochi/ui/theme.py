"""Design tokens for "Soft Serve" (DS-1..5).

Contrast fixes from the critique (problem 14):
- Light-mode muted text is darkened from #8C7B75 (3.8:1) to #796964 (>= 4.7:1 on every surface).
- Text on pastel fills uses a dedicated "on_fill" colour (>= 5.5:1); in dark mode that is
  the dark background colour, because light text on pastels was only 1.5-2.2:1.
- Lines, rings, badges and band edges use "strong" variants that reach >= 3:1 against
  the background (WCAG 1.4.11); pastel colours are only used for fills.

The contrast helpers at the top are pure Python so tests can check every pair.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path


# --------------------------------------------------------------- contrast maths
def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@dataclass(frozen=True)
class Palette:
    name: str
    background: str
    surface: str
    surface_alt: str
    text: str
    text_muted: str
    on_fill: str
    accent: str
    info: str
    good: str
    warning: str
    critical: str
    accent_strong: str
    info_strong: str
    good_strong: str
    warning_strong: str
    critical_strong: str
    shadow: str
    mochi_body: str
    mochi_outline: str
    blush: str

    def as_dict(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


LIGHT = Palette(
    name="light",
    background="#FFF8F0",      # Rice Cream
    surface="#FFFFFF",         # Mochi White
    surface_alt="#FFF1F3",
    text="#4A3F3F",            # Cocoa
    text_muted="#796964",      # Milk Tea, darkened for AA
    on_fill="#3B3131",
    accent="#FFB5C2",          # Strawberry Milk
    info="#C9B8F0",            # Lavender
    good="#A8D5BA",            # Matcha
    warning="#FFD38A",         # Mango
    critical="#FF8A8A",        # Cherry
    accent_strong="#D9566F",
    info_strong="#8A6FD6",
    good_strong="#3F8F62",
    warning_strong="#B9770A",
    critical_strong="#D95252",
    shadow="#4A3F3F",
    mochi_body="#FFFDF9",
    mochi_outline="#4A3F3F",
    blush="#FFB5C2",
)

DARK = Palette(
    name="dark",
    background="#1E1B2E",      # Midnight Mochi
    surface="#2A2640",
    surface_alt="#342F4E",
    text="#F3EDE7",
    text_muted="#A79FB8",
    on_fill="#1E1B2E",
    accent="#FF9FB2",
    info="#A996E6",
    good="#7FC39A",
    warning="#F5BE5C",
    critical="#FF7676",
    accent_strong="#FF9FB2",
    info_strong="#A996E6",
    good_strong="#7FC39A",
    warning_strong="#F5BE5C",
    critical_strong="#FF7676",
    shadow="#000000",
    mochi_body="#FFF6EE",
    mochi_outline="#2A2640",
    blush="#FF9FB2",
)

# Text pairs that must reach 4.5:1 and strokes that must reach 3:1 (checked by tests).
TEXT_PAIRS = [("text", "background"), ("text", "surface"), ("text_muted", "background"), ("text_muted", "surface"),
              ("text", "surface_alt"), ("text_muted", "surface_alt"),
              ("on_fill", "accent"), ("on_fill", "info"), ("on_fill", "good"), ("on_fill", "warning"),
              ("on_fill", "critical")]
STROKE_PAIRS = [(s, bg) for s in ("accent_strong", "info_strong", "good_strong", "warning_strong", "critical_strong")
                for bg in ("background", "surface")]

LEVEL_FILL = {1: "info", 2: "warning", 3: "critical"}
LEVEL_STROKE = {1: "info_strong", 2: "warning_strong", 3: "critical_strong"}
STATUS_FILL = {"fine": "good", "look": "warning", "action": "critical", "unavailable": "surface_alt"}

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FAMILY = "Nunito"
FALLBACK_FAMILY = "Segoe UI Variable Text"

SPACING = 8
RADIUS_CARD = 20
RADIUS_INNER = 12


class Theme:
    """The active palette and fonts. Qt pieces are imported lazily."""

    def __init__(self) -> None:
        self.palette = LIGHT
        self.family = FALLBACK_FAMILY
        self.high_contrast = False
        self.reduce_motion = False
        self.listeners: list = []

    def load_fonts(self) -> str:
        from PySide6.QtGui import QFontDatabase

        for f in FONT_DIR.glob("*.ttf"):
            fid = QFontDatabase.addApplicationFont(str(f))
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                self.family = fams[0]
        return self.family

    def font(self, px: int = 14, weight: int = 500, tabular: bool = False):
        from PySide6.QtGui import QFont

        f = QFont(self.family)
        f.setPixelSize(px)
        f.setWeight(QFont.Weight(weight))
        if tabular and hasattr(f, "setFeature"):
            try:
                from PySide6.QtGui import QFont as _QF

                f.setFeature(_QF.Tag("tnum"), 1)  # DS-4: live numbers don't jiggle
            except Exception:
                pass
        return f

    def color(self, token: str):
        from PySide6.QtGui import QColor

        return QColor(getattr(self.palette, token))

    def set_mode(self, mode: str, system_dark: bool) -> Palette:
        self.palette = DARK if (mode == "dark" or (mode == "system" and system_dark)) else LIGHT
        for fn in list(self.listeners):
            fn(self.palette)
        return self.palette

    def qss(self) -> str:
        p = self.palette
        if self.high_contrast:
            return f"* {{ font-family: '{self.family}'; }}"
        return f"""
        * {{ font-family: '{self.family}'; color: {p.text}; }}
        QMainWindow, QDialog, #Page, QStackedWidget, QScrollArea, #ScrollBody {{ background: {p.background}; }}
        QToolTip {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.info_strong};
                    border-radius: 8px; padding: 6px 8px; }}
        #Card {{ background: {p.surface}; border-radius: {RADIUS_CARD}px; }}
        #CardAlt {{ background: {p.surface_alt}; border-radius: {RADIUS_CARD}px; }}
        #Sidebar {{ background: {p.surface}; border-top-right-radius: 24px; border-bottom-right-radius: 24px; }}
        QLabel {{ background: transparent; }}
        QLabel#Muted, QLabel[muted="true"] {{ color: {p.text_muted}; }}
        QLabel#H1 {{ font-size: 26px; font-weight: 800; }}
        QLabel#H2 {{ font-size: 18px; font-weight: 800; }}
        QLabel#H3 {{ font-size: 15px; font-weight: 700; }}
        QPushButton {{ background: {p.surface_alt}; color: {p.text}; border: none; border-radius: 17px;
                       padding: 8px 18px; font-weight: 700; font-size: 14px; min-height: 18px; }}
        QPushButton:hover {{ background: {p.accent}; color: {p.on_fill}; }}
        QPushButton:pressed {{ background: {p.info}; color: {p.on_fill}; }}
        QPushButton:focus {{ outline: none; border: 2px solid {p.accent_strong}; }}
        QPushButton:disabled {{ color: {p.text_muted}; background: {p.surface_alt}; }}
        QPushButton#Primary {{ background: {p.accent}; color: {p.on_fill}; }}
        QPushButton#Primary:hover {{ background: {p.accent}; color: {p.on_fill}; border: 2px solid {p.accent_strong}; }}
        QPushButton#Ghost {{ background: transparent; color: {p.text_muted}; padding: 6px 10px; }}
        QPushButton#Ghost:hover {{ color: {p.text}; background: {p.surface_alt}; }}
        QPushButton#Danger {{ background: {p.critical}; color: {p.on_fill}; }}
        QPushButton#Nav {{ text-align: left; padding: 10px 16px; border-radius: 18px; background: transparent;
                           font-size: 15px; color: {p.text}; }}
        QPushButton#Nav:hover {{ background: {p.surface_alt}; color: {p.text}; }}
        QPushButton#Nav:checked {{ background: {p.accent}; color: {p.on_fill}; }}
        QPushButton#Chip {{ border-radius: 14px; padding: 5px 14px; font-size: 13px; background: {p.surface_alt}; }}
        QPushButton#Chip:checked {{ background: {p.info}; color: {p.on_fill}; }}
        QProgressBar {{ background: {p.surface_alt}; border: none; border-radius: 7px; height: 14px; text-align: center;
                        color: {p.text}; font-size: 11px; font-weight: 700; }}
        QProgressBar::chunk {{ background: {p.good}; border-radius: 7px; }}
        QSlider::groove:horizontal {{ height: 10px; background: {p.surface_alt}; border-radius: 5px; }}
        QSlider::sub-page:horizontal {{ background: {p.info}; border-radius: 5px; }}
        QSlider::handle:horizontal {{ background: {p.surface}; border: 3px solid {p.info_strong}; width: 18px;
                                      height: 18px; margin: -7px 0; border-radius: 12px; }}
        QComboBox, QLineEdit {{ background: {p.surface_alt}; border: 2px solid transparent; border-radius: 12px;
                                padding: 6px 10px; color: {p.text}; selection-background-color: {p.info}; }}
        QComboBox:focus, QLineEdit:focus {{ border: 2px solid {p.info_strong}; }}
        QComboBox {{ padding-right: 28px; }}
        QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center right; }}
        QComboBox::down-arrow {{ width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent;
                                 border-top: 6px solid {p.text_muted}; margin-right: 10px; }}
        QComboBox QAbstractItemView {{ background: {p.surface}; color: {p.text}; border-radius: 8px;
                                       selection-background-color: {p.info}; selection-color: {p.on_fill}; }}
        QCheckBox {{ spacing: 10px; }}
        QCheckBox::indicator {{ width: 20px; height: 20px; border-radius: 6px; border: 2px solid {p.text_muted};
                                background: {p.surface}; }}
        QCheckBox::indicator:checked {{ background: {p.good_strong}; border: 2px solid {p.good_strong}; }}
        QCheckBox:focus {{ outline: none; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
        QScrollBar::handle:vertical {{ background: {p.surface_alt}; border-radius: 5px; min-height: 30px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QScrollBar:horizontal {{ height: 0; }}
        QTableWidget {{ background: {p.surface}; border: none; border-radius: 12px; gridline-color: {p.surface_alt};
                        selection-background-color: {p.info}; selection-color: {p.on_fill}; }}
        QHeaderView::section {{ background: {p.surface_alt}; color: {p.text_muted}; border: none; padding: 6px;
                                font-weight: 700; }}
        QMenu {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.surface_alt}; border-radius: 10px;
                 padding: 6px; }}
        QMenu::item {{ padding: 7px 22px; border-radius: 8px; }}
        QMenu::item:selected {{ background: {p.accent}; color: {p.on_fill}; }}
        QMessageBox {{ background: {p.surface}; }}
        """


THEME = Theme()
