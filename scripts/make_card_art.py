#!/usr/bin/env python3
"""Draw ResourcePilot's card art as SVG, then render each one to JPEG with headless Chrome.

    uv run python scripts/make_card_art.py            # write assets/src/*.svg and render JPEGs
    uv run python scripts/make_card_art.py --svg-only # write the SVG sources only

Every image is a flat illustration built from the shapes below: one palette, a gradient sky, a
glassy tile, and an object. No image contains text, so each reads in light and dark themes and in
any language. Cards are 1200x675 (16:9); agent avatars are 512x512. Rendering needs Chrome or
Chromium, plus macOS ``sips`` or ImageMagick to write JPEG (about 60KB each, so cards load fast).

The script draws exactly the names in ``CARD_NAMES`` and one avatar per key in ``AVATAR_KEYS``,
and removes any other ``.svg`` or ``.jpg`` it finds under ``assets``.

To use other art (for example from an image model), save a JPEG under the same name in
``assets/cards`` or ``assets/avatars``. ``assets/PROMPTS.md`` has a prompt for each one. Cards pick
images by name, so no code changes.
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "src"
CARDS = ROOT / "assets" / "cards"
AVATARS = ROOT / "assets" / "avatars"
CHROME = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
)

# The card art names and agent avatar keys the cards use. Nothing else is drawn.
CARD_NAMES = (
    "menu-banner",
    "svc-rooms",
    "svc-wellness",
    "svc-housing",
    "svc-emergency",
    "svc-bookings",
    "room-study-1",
    "room-study-2",
    "room-study-3",
    "room-wellness",
    "wellness-hero",
    "event-default",
    "hall-default",
    "hero-info",
    "hero-warning",
    "hero-link",
    "hero-receipt",
    "hero-call",
    "role-orchestrator",
    "role-intake",
    "role-researcher",
    "role-campus-rooms",
    "role-verification",
    "role-planner",
    "role-memory-audit",
    "role-emergency",
)
AVATAR_KEYS = (
    "orchestrator",
    "intake",
    "researcher",
    "campus_rooms",
    "verification",
    "planner",
    "memory_audit",
    "emergency",
)

# Sky gradients, top-left to bottom-right, plus a glow color.
THEMES: dict[str, tuple[str, str, str]] = {
    "night": ("#1b1f5e", "#5b2bb5", "#8f7bff"),
    "rooms": ("#1d2b7a", "#4a3cc9", "#7fb2ff"),
    "wellness": ("#0d5c63", "#3fb59a", "#b6f5d8"),
    "housing": ("#5a2a83", "#f0745a", "#ffd27a"),
    "bookings": ("#123d7a", "#2fb3d9", "#9ff0ff"),
    "trust": ("#14365c", "#2f7fd0", "#8fd3ff"),
    "money": ("#0f4a3c", "#24a36b", "#c9ff9e"),
    "warn": ("#5c2311", "#e0782f", "#ffd27a"),
    "event": ("#3a1663", "#d94f9b", "#ffb3d9"),
    # A calm practice-call sky for the emergency demo drill: no reds, no flashing lights.
    "drill": ("#10324f", "#2d6f9e", "#a8e6ff"),
}

INK = "#1c1d3a"
WHITE = "#ffffff"
GOLD = "#ffcf4a"
CORAL = "#ff6f61"
MINT = "#4fe0b0"
SKY = "#6cc4ff"
LILAC = "#b9a6ff"
PINK = "#ff8fc7"
WOOD = "#c98a5a"
PEACH = "#ffb36b"
PERIWINKLE = "#8fa8ff"
BRICK = "#c96a54"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def rect(x: float, y: float, w: float, h: float, fill: str, r: float = 0, extra: str = "") -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" {extra}/>'


def circle(cx: float, cy: float, r: float, fill: str, extra: str = "") -> str:
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" {extra}/>'


def path(d: str, fill: str, extra: str = "") -> str:
    return f'<path d="{d}" fill="{fill}" {extra}/>'


def line(
    x1: float, y1: float, x2: float, y2: float, stroke: str, width: float, extra: str = ""
) -> str:
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linecap="round" {extra}/>'
    )


def group(
    content: list[str], x: float = 0, y: float = 0, scale: float = 1.0, rotate: float = 0.0
) -> str:
    return (
        f'<g transform="translate({x} {y}) rotate({rotate}) scale({scale})">{"".join(content)}</g>'
    )


def sparkle(x: float, y: float, size: float, fill: str = WHITE, opacity: float = 0.9) -> str:
    s = size
    return path(
        f"M{x} {y - s} Q{x + s * 0.18} {y - s * 0.18} {x + s} {y} "
        f"Q{x + s * 0.18} {y + s * 0.18} {x} {y + s} Q{x - s * 0.18} {y + s * 0.18} {x - s} {y} "
        f"Q{x - s * 0.18} {y - s * 0.18} {x} {y - s}Z",
        fill,
        f'opacity="{opacity}"',
    )


def shine(x: float, y: float, w: float, h: float, r: float) -> str:
    """A soft highlight across the top of a shape."""
    return rect(x + w * 0.08, y + h * 0.08, w * 0.84, h * 0.18, WHITE, r * 0.6, 'opacity="0.22"')


def check(points: str, color: str, width: float) -> str:
    return path(
        points,
        "none",
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"',
    )


# ---------------------------------------------------------------------------
# Objects, each drawn in a 400x400 box
# ---------------------------------------------------------------------------


def obj_campus() -> list[str]:
    return [
        rect(40, 250, 320, 110, "#e9e4ff", 14),
        rect(40, 250, 320, 26, "#cfc6ff", 10),
        *[rect(70 + i * 56, 292, 30, 46, "#5b4fd6", 6) for i in range(6) if i != 3],
        path("M170 360 L170 300 Q200 272 230 300 L230 360Z", "#3d33a8"),
        rect(150, 120, 100, 140, "#f4f1ff", 10),
        path("M140 128 L200 52 L260 128Z", "#6b5cff"),
        circle(200, 170, 30, WHITE, 'stroke="#3d33a8" stroke-width="8"'),
        line(200, 170, 200, 150, INK, 7),
        line(200, 170, 216, 178, INK, 7),
        rect(178, 214, 44, 46, "#5b4fd6", 8),
        path("M30 252 L200 196 L370 252Z", "#8b7dff"),
        sparkle(330, 90, 18, GOLD),
        sparkle(70, 120, 12),
    ]


def obj_study_room(variant: int = 0) -> list[str]:
    lamp = ["#ffcf4a", "#7ff0d2", "#ff9fd0"][variant % 3]
    return [
        rect(30, 60, 340, 220, "#2a2f7a", 18),
        rect(60, 88, 130, 110, "#8fd3ff", 10),
        line(125, 88, 125, 198, "#2a2f7a", 6),
        line(60, 143, 190, 143, "#2a2f7a", 6),
        circle(155, 115, 14, WHITE, 'opacity="0.8"'),
        rect(220, 96, 120, 18, "#f4f1ff", 6),
        *[
            rect(226 + i * 22, 64, 16, 32, c, 3)
            for i, c in enumerate([CORAL, SKY, GOLD, MINT, LILAC])
        ],
        rect(40, 250, 320, 26, WOOD, 10),
        rect(70, 276, 18, 84, "#8a5a3a", 6),
        rect(312, 276, 18, 84, "#8a5a3a", 6),
        rect(120, 208, 110, 44, "#e9e4ff", 8),
        rect(128, 214, 94, 30, "#5b4fd6", 4),
        path("M260 250 L272 196 L306 196 L318 250Z", lamp),
        line(289, 196, 289, 168, INK, 6),
        path("M266 172 Q289 140 312 172Z", lamp),
        circle(289, 214, 40, lamp, 'opacity="0.25"'),
        rect(150, 300, 100, 22, "#5b4fd6", 10),
        rect(160, 322, 14, 40, "#3d33a8", 6),
        rect(226, 322, 14, 40, "#3d33a8", 6),
    ]


def obj_lotus() -> list[str]:
    petals = []
    for angle, color in (
        (-60, "#ff9fd0"),
        (60, "#ff9fd0"),
        (-30, PINK),
        (30, PINK),
        (0, "#ffd1e8"),
    ):
        petals.append(
            group(
                [path("M0 0 Q-44 -80 0 -150 Q44 -80 0 0Z", color)],
                200,
                270,
                1.0,
                angle,
            )
        )
    return [
        circle(200, 200, 150, WHITE, 'opacity="0.12"'),
        circle(200, 200, 112, WHITE, 'opacity="0.10"'),
        *petals,
        path("M80 280 Q200 330 320 280 Q200 360 80 280Z", MINT),
        path("M110 300 Q200 340 290 300", "none", 'stroke="#1f9e78" stroke-width="6"'),
        sparkle(320, 110, 16, GOLD),
        sparkle(90, 140, 11),
    ]


def obj_calendar(accent: str = CORAL) -> list[str]:
    cells = []
    for row in range(3):
        for col in range(4):
            fill = accent if (row, col) == (1, 2) else "#e2ddff"
            cells.append(rect(92 + col * 58, 186 + row * 50, 42, 34, fill, 8))
    return [
        rect(60, 90, 280, 260, WHITE, 26),
        rect(60, 90, 280, 72, accent, 26),
        rect(60, 140, 280, 22, accent),
        rect(116, 64, 22, 56, INK, 11),
        rect(262, 64, 22, 56, INK, 11),
        *cells,
        check("M250 236 L264 250 L290 222", WHITE, 8),
    ]


def obj_house() -> list[str]:
    return [
        rect(70, 170, 260, 190, "#fff3e6", 14),
        path("M48 186 L200 64 L352 186Z", CORAL),
        rect(240, 84, 34, 70, "#c94f45", 6),
        *[
            rect(100 + c * 74, 204 + r * 66, 48, 44, "#5a2a83", 8)
            for r in range(2)
            for c in range(3)
            if not (r == 1 and c == 1)
        ],
        *[
            rect(104 + c * 74, 208 + r * 66, 18, 16, GOLD, 4)
            for r in range(2)
            for c in range(3)
            if not (r == 1 and c == 1)
        ],
        path("M174 360 L174 290 Q200 266 226 290 L226 360Z", "#7a3fa8"),
        circle(214, 326, 5, GOLD),
        sparkle(330, 90, 16, GOLD),
    ]


def obj_hall() -> list[str]:
    """A four-story campus residence hall with lit windows, an entry canopy, and two trees."""
    lit = {(0, 1), (0, 4), (1, 0), (1, 3), (2, 2), (2, 5), (3, 1), (3, 4)}
    windows = []
    for floor in range(4):
        for col in range(6):
            x, y = 96 + col * 36, 112 + floor * 52
            if floor == 3 and col in (2, 3):
                continue
            windows.append(rect(x, y, 24, 32, GOLD if (floor, col) in lit else "#5a2a83", 5))
            if (floor, col) in lit:
                windows.append(rect(x + 3, y + 3, 8, 10, "#fff1b8", 3))
    return [
        rect(76, 92, 248, 268, BRICK, 12),
        rect(66, 78, 268, 24, "#fff3e6", 8),
        rect(76, 102, 248, 6, "#a8523f", 3),
        *windows,
        rect(166, 300, 68, 60, "#fff3e6", 8),
        path("M150 304 L200 276 L250 304Z", "#7a3fa8"),
        rect(176, 312, 48, 48, "#7a3fa8", 6),
        line(200, 312, 200, 360, "#5a2a83", 4),
        circle(192, 338, 4, GOLD),
        circle(208, 338, 4, GOLD),
        rect(150, 356, 100, 10, "#fff3e6", 5),
        rect(54, 300, 12, 62, "#8a5a3a", 5),
        circle(60, 288, 34, "#3fb59a"),
        circle(46, 304, 22, "#2f9a80"),
        rect(334, 300, 12, 62, "#8a5a3a", 5),
        circle(340, 288, 34, "#3fb59a"),
        circle(354, 304, 22, "#2f9a80"),
        sparkle(340, 60, 16, GOLD),
        sparkle(56, 70, 11),
    ]


def obj_shield() -> list[str]:
    return [
        path("M200 50 L330 100 L330 200 Q330 300 200 360 Q70 300 70 200 L70 100Z", "#e9f4ff"),
        path("M200 82 L300 120 L300 200 Q300 278 200 326 Q100 278 100 200 L100 120Z", SKY),
        check("M150 205 L188 242 L256 168", WHITE, 22),
        sparkle(330, 70, 16, GOLD),
    ]


def obj_lock() -> list[str]:
    return [
        path(
            "M130 180 L130 130 Q130 64 200 64 Q270 64 270 130 L270 180",
            "none",
            'stroke="#e9e4ff" stroke-width="30" stroke-linecap="round"',
        ),
        rect(90, 170, 220, 180, GOLD, 30),
        shine(90, 170, 220, 180, 30),
        circle(200, 245, 24, INK),
        rect(190, 250, 20, 56, INK, 8),
    ]


def obj_receipt() -> list[str]:
    zigzag = " ".join(f"L{310 - i * 22} {350 if i % 2 else 336}" for i in range(11))
    return [
        path(f"M90 60 L310 60 {zigzag}Z", WHITE),
        *[rect(120, 110 + i * 38, 160 if i % 2 else 110, 16, "#d6d2f5", 8) for i in range(4)],
        circle(270, 280, 48, MINT),
        check("M248 280 L264 296 L294 264", WHITE, 12),
        sparkle(340, 80, 16, GOLD),
    ]


def obj_link() -> list[str]:
    return [
        group(
            [rect(0, 0, 150, 76, "none", 38, f'stroke="{WHITE}" stroke-width="24"')],
            70,
            130,
            1.0,
            -30,
        ),
        group(
            [rect(0, 0, 150, 76, "none", 38, f'stroke="{GOLD}" stroke-width="24"')],
            160,
            170,
            1.0,
            -30,
        ),
        circle(290, 270, 56, "none", f'stroke="{INK}" stroke-width="20"'),
        circle(290, 270, 40, SKY, 'opacity="0.6"'),
        line(330, 312, 370, 352, INK, 24),
    ]


def obj_info() -> list[str]:
    return [
        circle(200, 200, 140, WHITE),
        circle(200, 126, 22, "#2f7fd0"),
        rect(178, 170, 44, 130, "#2f7fd0", 22),
        sparkle(340, 80, 16, GOLD),
    ]


def obj_warning() -> list[str]:
    return [
        path("M200 60 L350 330 Q356 344 340 344 L60 344 Q44 344 50 330Z", GOLD),
        rect(186, 140, 28, 120, INK, 14),
        circle(200, 296, 18, INK),
    ]


def obj_bookings() -> list[str]:
    return [
        group(obj_calendar("#2fb3d9"), 0, 0, 0.8),
        group(
            [
                rect(0, 0, 170, 64, WHITE, 18),
                circle(34, 32, 16, MINT),
                rect(64, 24, 84, 16, "#c9e9f5", 8),
            ],
            200,
            230,
            1.0,
        ),
        group(
            [
                rect(0, 0, 170, 64, WHITE, 18),
                circle(34, 32, 16, GOLD),
                rect(64, 24, 84, 16, "#c9e9f5", 8),
            ],
            220,
            310,
            1.0,
        ),
    ]


def obj_ranking() -> list[str]:
    """Three option cards in ranked order, the best one starred and checked."""
    rows = []
    for index, (accent, width) in enumerate(((GOLD, 250), ("#d8d2ff", 210), ("#d8d2ff", 180))):
        y = 80 + index * 92
        rows.append(rect(70, y, 260, 72, WHITE if index == 0 else "#f4f1ff", 20))
        rows.append(circle(108, y + 36, 20, accent))
        rows.append(rect(142, y + 22, width - 110, 12, "#c9c4f5", 6))
        rows.append(rect(142, y + 42, (width - 110) * 0.6, 10, "#e2ddff", 5))
    return [
        *rows,
        check("M98 116 L106 124 L120 108", INK, 6),
        sparkle(320, 92, 24, GOLD, 1.0),
        sparkle(352, 140, 12, GOLD),
        sparkle(70, 360, 12),
    ]


def obj_event() -> list[str]:
    confetti = [
        rect(
            40 + (i * 37) % 320,
            40 + (i * 53) % 110,
            14,
            26,
            c,
            4,
            f'transform="rotate({(i * 47) % 90 - 45} {47 + (i * 37) % 320} {53 + (i * 53) % 110})"',
        )
        for i, c in enumerate([CORAL, GOLD, MINT, SKY, LILAC, PINK] * 2)
    ]
    return [
        *confetti,
        group(obj_calendar(PINK), 50, 120, 0.68),
        group(
            [
                rect(0, 0, 170, 96, GOLD, 16),
                circle(0, 48, 18, "#d94f9b"),
                circle(170, 48, 18, "#d94f9b"),
                line(56, 0, 56, 96, "#c79a1e", 4, 'stroke-dasharray="8 8"'),
            ],
            210,
            240,
            1.0,
            -12,
        ),
    ]


def _drill_badge(x: float, y: float) -> list[str]:
    """A practice badge: a gold rosette with a dashed ring and a play mark (a rehearsal)."""
    return [
        circle(x, y, 46, GOLD),
        circle(x, y, 34, "none", f'stroke="{WHITE}" stroke-width="5" stroke-dasharray="9 7"'),
        path(f"M{x - 10} {y - 16} L{x + 16} {y} L{x - 10} {y + 16}Z", WHITE),
        path(f"M{x - 30} {y + 36} L{x - 40} {y + 72} L{x - 18} {y + 60} L{x - 6} {y + 44}Z", GOLD),
        path(f"M{x + 30} {y + 36} L{x + 40} {y + 72} L{x + 18} {y + 60} L{x + 6} {y + 44}Z", GOLD),
    ]


def obj_phone(ringing: bool = False) -> list[str]:
    """A smartphone on a practice call: a caller circle, a green call button, a drill badge."""
    waves = (
        [
            path(
                f"M{300 + i * 24} {150 - i * 16} Q{330 + i * 30} 210 {300 + i * 24} {270 + i * 16}",
                "none",
                f'stroke="{WHITE}" stroke-width="10" stroke-linecap="round" '
                f'opacity="{0.85 - i * 0.25:.2f}"',
            )
            for i in range(3)
        ]
        if ringing
        else []
    )
    return [
        *waves,
        rect(120, 40, 170, 320, INK, 34),
        rect(132, 56, 146, 288, "#e9f4ff", 24),
        rect(180, 64, 50, 10, INK, 5),
        circle(205, 140, 38, "#bfe0ff"),
        circle(205, 128, 16, "#5b8dc9"),
        path("M177 166 Q205 140 233 166Z", "#5b8dc9"),
        rect(160, 196, 90, 12, "#c9dcff", 6),
        rect(176, 218, 58, 10, "#dbe8ff", 5),
        circle(205, 296, 28, MINT),
        path(
            "M193 286 Q191 300 205 310 Q213 314 217 308 L213 302 Q209 304 205 300 "
            "Q199 294 201 288 L195 284Z",
            WHITE,
        ),
        shine(120, 40, 170, 320, 34),
        *_drill_badge(96, 112),
        sparkle(340, 330, 14, GOLD),
    ]


def obj_transcript() -> list[str]:
    """Two speech bubbles holding lines of a call transcript."""
    return [
        rect(50, 80, 220, 120, WHITE, 28),
        path("M90 196 L80 240 L130 198Z", WHITE),
        *[rect(80, 108 + i * 30, 160 - i * 40, 14, "#c9dcff", 7) for i in range(3)],
        rect(140, 220, 210, 110, "#bfe0ff", 28),
        path("M310 326 L330 366 L280 328Z", "#bfe0ff"),
        *[rect(170, 248 + i * 30, 150 - i * 50, 14, WHITE, 7) for i in range(2)],
        sparkle(340, 90, 16, GOLD),
    ]


def obj_robot(emblem: Callable[[], list[str]], color: str) -> list[str]:
    return [
        line(200, 70, 200, 40, WHITE, 10),
        circle(200, 34, 14, color),
        rect(80, 70, 240, 190, "#f4f1ff", 70),
        rect(108, 108, 184, 112, "#1c1d3a", 50),
        circle(160, 164, 20, color),
        circle(240, 164, 20, color),
        path(
            "M178 196 Q200 212 222 196",
            "none",
            f'stroke="{color}" stroke-width="8" stroke-linecap="round"',
        ),
        rect(60, 140, 26, 60, "#d8d2ff", 13),
        rect(314, 140, 26, 60, "#d8d2ff", 13),
        rect(130, 268, 140, 100, "#e9e4ff", 40),
        circle(200, 318, 44, color),
        group(emblem(), 166, 284, 0.17),
    ]


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


def _defs(uid: str, theme: str) -> str:
    top, bottom, glow = THEMES[theme]
    return (
        f'<defs><linearGradient id="sky{uid}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
        f'</linearGradient><radialGradient id="glow{uid}" cx="0.5" cy="0.45" r="0.5">'
        f'<stop offset="0" stop-color="{glow}" stop-opacity="0.75"/>'
        f'<stop offset="1" stop-color="{glow}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="tile{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="#ffffff" stop-opacity="0.30"/>'
        f'<stop offset="1" stop-color="#ffffff" stop-opacity="0.08"/></linearGradient></defs>'
    )


def _stars(width: int, height: int, seed: int) -> str:
    out = []
    for i in range(18):
        x = (seed * 97 + i * 211) % width
        y = (seed * 61 + i * 137) % int(height * 0.55)
        out.append(circle(x, y, 2 + (i % 3), WHITE, f'opacity="{0.25 + (i % 4) * 0.12:.2f}"'))
    return "".join(out)


def _bubble(uid: str, x: float, y: float, radius: float, drawing: list[str]) -> str:
    """A floating glass bubble holding a smaller related object."""
    scale = radius * 1.35 / 400
    return circle(
        x, y, radius, f"url(#tile{uid})", f'stroke="{WHITE}" stroke-opacity="0.35" stroke-width="3"'
    ) + group(drawing, x - 200 * scale, y - 200 * scale, scale)


def scene(
    uid: str,
    theme: str,
    objects: list[list[str]],
    satellites: list[list[str]] | None = None,
    *,
    width: int = 1200,
    height: int = 675,
) -> str:
    """A gradient sky, a soft glow, a glass tile with the main object, and side bubbles."""
    seed = sum(map(ord, uid))
    square = width == height
    size = min(width, height) * (0.8 if square else 0.84)
    cx, cy = width / 2, height / 2
    parts = [
        _defs(uid, theme),
        rect(0, 0, width, height, f"url(#sky{uid})"),
        _stars(width, height, seed),
        circle(cx, cy, size * 0.95, f"url(#glow{uid})"),
        path(
            f"M0 {height * 0.8} Q{width * 0.3} {height * 0.68} {width * 0.6} {height * 0.78} "
            f"T{width} {height * 0.74} L{width} {height} L0 {height}Z",
            WHITE,
            'opacity="0.10"',
        ),
    ]
    if satellites and not square:
        spots = (
            (width * 0.165, height * 0.40, height * 0.23),
            (width * 0.835, height * 0.60, height * 0.23),
        )
        for (x, y, radius), drawing in zip(spots, satellites, strict=False):
            parts.append(_bubble(uid, x, y, radius, drawing))
    count = len(objects)
    tile_w = size if count == 1 or square else min(width * 0.5, size * 1.25)
    parts.append(
        rect(
            cx - tile_w / 2,
            cy - size / 2,
            tile_w,
            size,
            f"url(#tile{uid})",
            size * 0.2,
            f'stroke="{WHITE}" stroke-opacity="0.35" stroke-width="3"',
        )
    )
    for index, drawing in enumerate(objects):
        if count == 1:
            scale, ox = size / 400 * 0.86, cx
        else:
            spread = tile_w * 0.56
            scale = size / 400 * 0.5
            ox = cx - spread / 2 + spread * index / (count - 1)
        parts.append(group(drawing, ox - 200 * scale, cy - 200 * scale, scale))
    for i in range(5):
        angle = seed + i * 1.3
        parts.append(
            sparkle(
                cx + math.cos(angle) * width * 0.36,
                cy + math.sin(angle) * height * 0.42,
                9 + i * 3,
                WHITE,
                0.75,
            )
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">{"".join(parts)}</svg>'
    )


Draw = Callable[[], list[str]]


def _pink_calendar() -> list[str]:
    return obj_calendar(PINK)


def _ringing_phone() -> list[str]:
    return obj_phone(ringing=True)


ROLE_EMBLEMS: dict[str, tuple[Draw, str, Draw]] = {
    # role: (emblem on the robot and in the left bubble, robot color, right bubble)
    "orchestrator": (obj_campus, GOLD, obj_bookings),
    "intake": (obj_calendar, CORAL, obj_shield),
    "researcher": (obj_campus, PINK, obj_link),
    "campus_rooms": (partial(obj_study_room, 0), PEACH, obj_lotus),
    "verification": (obj_shield, SKY, obj_lock),
    "planner": (obj_ranking, LILAC, obj_hall),
    "memory_audit": (obj_receipt, MINT, obj_bookings),
    "emergency": (obj_phone, PERIWINKLE, obj_info),
}

# name: (theme, main objects, side bubbles). Each name is assets/cards/<name>.jpg and an art
# name in resourcepilot/cards.py.
CARD_SCENES: dict[str, tuple[str, list[Draw], list[Draw]]] = {
    "menu-banner": ("night", [obj_campus, obj_lotus, obj_house], [obj_calendar, obj_phone]),
    "svc-rooms": ("rooms", [partial(obj_study_room, 0)], [obj_calendar, obj_campus]),
    "svc-wellness": ("wellness", [obj_lotus], [_pink_calendar, obj_event]),
    "svc-housing": ("housing", [obj_house], [obj_campus, obj_calendar]),
    "svc-emergency": ("drill", [obj_phone], [obj_info, obj_shield]),
    "svc-bookings": ("bookings", [obj_bookings], [obj_receipt, obj_link]),
    "room-study-1": ("rooms", [partial(obj_study_room, 0)], [obj_calendar, obj_link]),
    "room-study-2": ("night", [partial(obj_study_room, 1)], [obj_campus, obj_calendar]),
    "room-study-3": ("trust", [partial(obj_study_room, 2)], [obj_link, obj_campus]),
    "room-wellness": ("wellness", [obj_lotus], [_pink_calendar, obj_house]),
    "wellness-hero": ("wellness", [obj_lotus, _pink_calendar], [obj_event, obj_shield]),
    "event-default": ("event", [obj_event], [obj_lotus, _pink_calendar]),
    "hall-default": ("housing", [obj_hall], [obj_house, obj_campus]),
    "hero-info": ("trust", [obj_info], [obj_shield, obj_calendar]),
    "hero-warning": ("warn", [obj_warning], [obj_shield, obj_info]),
    "hero-link": ("rooms", [obj_link], [obj_campus, obj_calendar]),
    "hero-receipt": ("money", [obj_receipt], [obj_link, obj_calendar]),
    "hero-call": ("drill", [_ringing_phone], [obj_transcript, obj_info]),
}
for _role, (_emblem, _color, _side) in ROLE_EMBLEMS.items():
    CARD_SCENES[f"role-{_role.replace('_', '-')}"] = (
        "night",
        [partial(obj_robot, _emblem, _color)],
        [_emblem, _side],
    )


def avatar_scenes() -> dict[str, str]:
    """One square avatar per agent key, from the same drawings as its role card."""
    out = {}
    for name, (theme, objects, _) in CARD_SCENES.items():
        if name.startswith("role-"):
            key = name.split("-", 1)[1].replace("-", "_")
            out[key] = scene(f"av{key}", theme, [draw() for draw in objects], width=512, height=512)
    return out


def check_names() -> None:
    """Fail loudly if the drawings drift from the names the cards use."""
    if sorted(CARD_SCENES) != sorted(CARD_NAMES):
        missing = sorted(set(CARD_NAMES) - set(CARD_SCENES))
        extra = sorted(set(CARD_SCENES) - set(CARD_NAMES))
        raise SystemExit(f"card art drifted: missing {missing}, extra {extra}")
    if sorted(avatar_scenes()) != sorted(AVATAR_KEYS):
        raise SystemExit(f"avatars drifted: {sorted(avatar_scenes())}")


def write_svgs() -> list[tuple[Path, Path, int, int]]:
    SRC.mkdir(parents=True, exist_ok=True)
    jobs = []
    for name, (theme, objects, sides) in CARD_SCENES.items():
        svg = SRC / f"{name}.svg"
        svg.write_text(
            scene(
                name.replace("-", ""),
                theme,
                [draw() for draw in objects],
                [draw() for draw in sides],
            )
        )
        jobs.append((svg, CARDS / f"{name}.jpg", 1200, 675))
    for key, content in avatar_scenes().items():
        svg = SRC / f"avatar-{key}.svg"
        svg.write_text(content)
        jobs.append((svg, AVATARS / f"{key}.jpg", 512, 512))
    return jobs


def report_stale(jobs: list[tuple[Path, Path, int, int]]) -> None:
    """Name art that no card uses any more; it is left in place for you to remove."""
    keep = {path for job in jobs for path in job[:2]}
    for folder, suffix in ((SRC, ".svg"), (CARDS, ".jpg"), (AVATARS, ".jpg")):
        if not folder.exists():
            continue
        for stale in folder.glob(f"*{suffix}"):
            if stale not in keep:
                print(f"unused: {stale.relative_to(ROOT)}", file=sys.stderr)


def chrome() -> str:
    for candidate in CHROME:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    raise SystemExit("Install Google Chrome or Chromium to render images (or pass --svg-only)")


def to_jpeg(png: Path, jpg: Path) -> None:
    if shutil.which("sips"):
        command = [
            "sips",
            "-s",
            "format",
            "jpeg",
            "-s",
            "formatOptions",
            "84",
            str(png),
            "--out",
            str(jpg),
        ]
    elif shutil.which("magick"):
        command = ["magick", str(png), "-quality", "84", str(jpg)]
    else:
        raise SystemExit("Install ImageMagick (or use macOS) to write JPEG")
    subprocess.run(command, check=True, capture_output=True, timeout=60)


def render(jobs: list[tuple[Path, Path, int, int]]) -> None:
    binary = chrome()
    CARDS.mkdir(parents=True, exist_ok=True)
    AVATARS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for svg, jpg, width, height in jobs:
            png = Path(scratch) / f"{svg.stem}.png"
            page = Path(scratch) / f"{svg.stem}.html"
            page.write_text(
                "<!doctype html><html><body style='margin:0;overflow:hidden'>"
                f"{svg.read_text()}</body></html>"
            )
            subprocess.run(
                [
                    binary,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--force-device-scale-factor=1",
                    f"--user-data-dir={scratch}/profile",
                    f"--window-size={width},{height}",
                    f"--screenshot={png}",
                    page.as_uri(),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            to_jpeg(png, jpg)
            print(f"{jpg.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--svg-only", action="store_true")
    args = parser.parse_args()
    check_names()
    jobs = write_svgs()
    report_stale(jobs)
    if not args.svg_only:
        render(jobs)
    print(f"{len(jobs)} images", file=sys.stderr)


if __name__ == "__main__":
    main()
