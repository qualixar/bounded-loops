#!/usr/bin/env python3
"""Render the deterministic 1280×640 GitHub social preview."""

from __future__ import annotations

from pathlib import Path
import math

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:  # pragma: no cover - developer utility
    raise SystemExit("Install Pillow first: python -m pip install Pillow") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "assets" / "social-preview.png"
WIDTH, HEIGHT = 1280, 640
INK = "#e7e6e5"
MUTED = "#94a3b8"
ACCENT = "#ef7957"
GREEN = "#4ade80"
RED = "#fb7185"
PANEL = "#111827"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/SFNSMono.ttf" if not bold else "/System/Library/Fonts/SFNSMonoBold.ttf"),
        Path("/System/Library/Fonts/SFNSMono.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _draw_mark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    stroke = 12
    draw.line((x, y, x, y + size), fill=ACCENT, width=stroke)
    draw.line((x, y, x + size // 4, y), fill=ACCENT, width=stroke)
    draw.line((x, y + size, x + size // 4, y + size), fill=ACCENT, width=stroke)
    right = x + size
    draw.line((right, y, right, y + size), fill=ACCENT, width=stroke)
    draw.line((right - size // 4, y, right, y), fill=ACCENT, width=stroke)
    draw.line((right - size // 4, y + size, right, y + size), fill=ACCENT, width=stroke)
    inset = size // 4
    draw.arc((x + inset, y + inset, right - inset, y + size - inset), 35, 330, fill=ACCENT, width=stroke)
    angle = math.radians(35)
    cx, cy = x + size // 2, y + size // 2
    radius = size // 4
    tip = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    draw.polygon(
        [
            tip,
            (tip[0] - 22, tip[1] - 4),
            (tip[0] - 7, tip[1] + 18),
        ],
        fill=ACCENT,
    )


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, lines: list[tuple[str, str]]) -> None:
    draw.rounded_rectangle(box, radius=18, fill=PANEL, outline="#334155", width=2)
    x1, y1, _, _ = box
    draw.text((x1 + 28, y1 + 24), title, font=_font(22, bold=True), fill=MUTED)
    y = y1 + 74
    for text, color in lines:
        draw.text((x1 + 28, y), text, font=_font(21), fill=color)
        y += 38


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0b1020")
    draw = ImageDraw.Draw(image)
    _draw_mark(draw, 72, 64, 142)
    draw.text((246, 62), "bounded-loops", font=_font(62, bold=True), fill=INK)
    draw.text(
        (248, 144),
        "The gate decides when the agent is done.",
        font=_font(28),
        fill=MUTED,
    )
    draw.rounded_rectangle((72, 230, 1208, 246), radius=8, fill=ACCENT)
    _panel(
        draw,
        (72, 286, 614, 548),
        "UNGATED",
        [
            ("agent: <promise>GREEN</promise>", INK),
            ("loop: accepted the claim", INK),
            ("✗ pytest still fails", RED),
        ],
    )
    _panel(
        draw,
        (666, 286, 1208, 548),
        "BOUNDED",
        [
            ("agent: proposes a fix", INK),
            ("gate: pytest -q", INK),
            ("✓ DONE · receipt recorded", GREEN),
        ],
    )
    draw.text(
        (74, 580),
        "9 enforced bounds  ·  independent gates  ·  auditable ledgers",
        font=_font(23, bold=True),
        fill=INK,
    )
    image.save(OUTPUT, format="PNG", optimize=True)
    print(f"Wrote {OUTPUT} ({WIDTH}×{HEIGHT}).")


if __name__ == "__main__":
    main()
