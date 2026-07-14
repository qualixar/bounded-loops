#!/usr/bin/env python3
"""Render the README-friendly ports-and-adapters architecture PNG."""

from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:  # pragma: no cover - developer utility
    raise SystemExit("Install Pillow first: python -m pip install Pillow") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "diagrams" / "ports-and-adapters.png"
WIDTH, HEIGHT = 1800, 1600

BG = "#07111f"
GRID = "#102036"
INK = "#f8fafc"
MUTED = "#a8b7ca"
LINE = "#70829a"
CYAN = "#22d3ee"
GREEN = "#34d399"
VIOLET = "#a78bfa"
AMBER = "#fbbf24"
ROSE = "#fb7185"
PANEL = "#0d1a2b"
BOX = "#111f33"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _center(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, *, size: int, color: str, bold: bool = False) -> None:
    font = _font(size, bold=bold)
    left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font, spacing=9, align="center")
    width, height = right - left, bottom - top
    x1, y1, x2, y2 = box
    draw.multiline_text(
        (x1 + (x2 - x1 - width) / 2, y1 + (y2 - y1 - height) / 2),
        text,
        font=font,
        fill=color,
        spacing=9,
        align="center",
    )


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, color: str) -> None:
    draw.rounded_rectangle(box, radius=24, fill=PANEL, outline=color, width=3)
    x1, y1, _, _ = box
    draw.rounded_rectangle((x1 + 24, y1 + 20, x1 + 62, y1 + 58), radius=9, fill=color)
    draw.text((x1 + 80, y1 + 19), title, font=_font(32, bold=True), fill=INK)


def _box(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, detail: str, color: str) -> None:
    draw.rounded_rectangle(box, radius=18, fill=BOX, outline=color, width=3)
    x1, y1, x2, y2 = box
    draw.text((x1 + 24, y1 + 17), title, font=_font(30, bold=True), fill=color)
    draw.line((x1 + 24, y1 + 62, x2 - 24, y1 + 62), fill="#2a3d55", width=2)
    detail_font = _font(21)
    spacing = 6
    content_top = y1 + 72
    content_bottom = y2 - 10
    bbox = draw.multiline_textbbox((0, 0), detail, font=detail_font, spacing=spacing)
    detail_width = bbox[2] - bbox[0]
    detail_height = bbox[3] - bbox[1]
    available_width = x2 - x1 - 48
    available_height = content_bottom - content_top
    if detail_width > available_width or detail_height > available_height:
        raise ValueError(f"Text does not fit in {title!r}: {detail!r}")
    detail_y = content_top + (available_height - detail_height) // 2 - bbox[1]
    draw.multiline_text(
        (x1 + 24, detail_y),
        detail,
        font=detail_font,
        fill=INK,
        spacing=spacing,
    )


def _arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], label: str = "", *, color: str = LINE) -> None:
    x1, y1 = start
    x2, y2 = end
    draw.line((x1, y1, x2, y2), fill=color, width=5)
    if y2 >= y1:
        tip = [(x2, y2), (x2 - 13, y2 - 22), (x2 + 13, y2 - 22)]
    else:
        tip = [(x2, y2), (x2 - 13, y2 + 22), (x2 + 13, y2 + 22)]
    draw.polygon(tip, fill=color)
    if label:
        font = _font(19, bold=True)
        bbox = draw.textbbox((0, 0), label, font=font)
        width = bbox[2] - bbox[0]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        draw.rounded_rectangle((cx - width // 2 - 12, cy - 20, cx + width // 2 + 12, cy + 17), radius=8, fill=BG)
        draw.text((cx - width // 2, cy - 14), label, font=font, fill=MUTED)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, WIDTH, 40):
        draw.line((x, 0, x, HEIGHT), fill=GRID, width=1)
    for y in range(0, HEIGHT, 40):
        draw.line((0, y, WIDTH, y), fill=GRID, width=1)

    draw.text((80, 55), "bounded-loops architecture", font=_font(54, bold=True), fill=INK)
    draw.text((82, 125), "Ports and adapters keep policy pure and implementations replaceable.", font=_font(26), fill=MUTED)

    # Connections are drawn before opaque boxes so arrows never cross labels.
    _arrow(draw, (460, 385), (900, 500), "calls", color=CYAN)
    _arrow(draw, (1340, 385), (900, 500), "calls", color=CYAN)
    _arrow(draw, (900, 630), (900, 745), "builds", color=AMBER)
    _arrow(draw, (660, 900), (660, 1045), "uses ports", color=GREEN)
    _arrow(draw, (380, 1340), (660, 1200), "implements", color=VIOLET)
    _arrow(draw, (900, 1340), (660, 1200), "implements", color=VIOLET)
    _arrow(draw, (1420, 1340), (660, 1200), "implements", color=VIOLET)
    _arrow(draw, (1040, 820), (1260, 820), color=GREEN)

    _panel(draw, (70, 205, 1730, 410), "1  ENTRY POINTS", CYAN)
    _box(draw, (125, 275, 795, 385), "CLI · bl", "pip install → run · lint · doctor · runs", CYAN)
    _box(draw, (1005, 275, 1675, 385), "MCP SERVER", "tools · resources · prompts", CYAN)

    _panel(draw, (320, 430, 1480, 650), "2  COMPOSITION ROOT", AMBER)
    _box(
        draw,
        (450, 500, 1350, 630),
        "COMPOSITION · composition.py",
        "The only module that imports concrete adapters\nand injects them into the application.",
        AMBER,
    )

    _panel(draw, (70, 675, 1120, 1235), "3  APPLICATION / ORCHESTRATION", GREEN)
    _box(draw, (125, 745, 760, 900), "RUN LOOP · run_loop.py", "RunLoopUseCase\nThe lap algorithm; the gate decides DONE.", GREEN)
    _box(draw, (790, 745, 1065, 900), "BOUNDS", "BoundsEnforcer\nHard-limit checks", GREEN)
    _box(draw, (125, 915, 535, 1065), "MANIFEST", "Loads and validates\nLoopManifest.", GREEN)
    _box(
        draw,
        (565, 1045, 1065, 1200),
        "PORTS · ports.py",
        "Runner · Gate · Memory · Ledger\nTracer · Budget · KillSwitch\nApproval · Clock",
        GREEN,
    )

    _panel(draw, (1160, 675, 1730, 1235), "4  PURE DOMAIN", ROSE)
    _box(draw, (1215, 745, 1675, 885), "MODELS", "Spec · Bounds · Verdict\nRunResult · Outcome", ROSE)
    _box(draw, (1215, 910, 1675, 1050), "RULES", "stop_condition_met\nno_progress · approval", ROSE)
    _box(draw, (1215, 1075, 1675, 1210), "ERRORS", "Manifest · Runner · Gate\nKillSwitch errors", ROSE)

    _panel(draw, (70, 1260, 1730, 1515), "5  CONCRETE ADAPTERS", VIOLET)
    _box(draw, (125, 1340, 635, 1485), "RUNNERS", "stub · shell · Python\nClaude Code · Codex · Antigravity", VIOLET)
    _box(draw, (645, 1340, 1155, 1485), "GATES", "command · pytest · JSON Schema\nsecurity · data · browser", VIOLET)
    _box(draw, (1165, 1340, 1675, 1485), "SERVICES", "ledger · memory · tracing · budget\nkill switch · approval · clock", VIOLET)

    draw.text((80, 1545), "Dependency rule: policy points inward; concrete tools stay behind ports.", font=_font(23, bold=True), fill=MUTED)
    image.save(OUTPUT, format="PNG", optimize=True)
    print(f"Wrote {OUTPUT} ({WIDTH}×{HEIGHT}).")


if __name__ == "__main__":
    main()
