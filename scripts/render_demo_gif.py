#!/usr/bin/env python3
"""Regenerate assets/demo.gif from real bounded-loops command output."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:  # pragma: no cover - developer utility
    raise SystemExit("Install Pillow first: python -m pip install Pillow") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "assets" / "demo.gif"
WIDTH, HEIGHT = 1200, 680
BACKGROUND = "#0b1020"
FOREGROUND = "#dbeafe"
MUTED = "#94a3b8"
GREEN = "#4ade80"
RED = "#fb7185"


def _python() -> Path:
    candidate = REPO_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    return candidate if candidate.is_file() else Path(sys.executable)


def _capture(command: list[str], expected_code: int) -> str:
    env = os.environ.copy()
    venv_bin = _python().parent
    env["PATH"] = os.pathsep.join([str(venv_bin), env.get("PATH", "")])
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != expected_code:
        raise RuntimeError(
            f"expected exit {expected_code}, got {result.returncode}: "
            f"{result.stdout}{result.stderr}"
        )
    return (result.stdout + result.stderr).replace(str(REPO_ROOT), ".")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _selected_wreck_lines(output: str) -> list[str]:
    markers = (
        "===",
        "WARNING:",
        "--- Lap",
        "<promise>",
        "LOOP BELIEVES:",
        "WRECK_MODE=",
        "LIE CONFIRMED:",
    )
    return [line for line in output.splitlines() if line.startswith(markers)]


def _color(line: str) -> str:
    if "DONE" in line or line.startswith("Gate verified:"):
        return GREEN
    if "WRECK" in line or "LIE" in line or "WARNING" in line:
        return RED
    if line.startswith("$") or line.startswith("Next:"):
        return MUTED
    return FOREGROUND


def _frame(lines: list[str], cursor: bool = False) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = _font(22)
    body_font = _font(20)
    draw.rounded_rectangle((18, 18, WIDTH - 18, HEIGHT - 18), radius=16, outline="#334155", width=2)
    draw.ellipse((42, 39, 58, 55), fill="#fb7185")
    draw.ellipse((68, 39, 84, 55), fill="#fbbf24")
    draw.ellipse((94, 39, 110, 55), fill="#4ade80")
    draw.text((132, 35), "bounded-loops · gate decides done", font=title_font, fill=MUTED)
    y = 86
    for line in lines[-23:]:
        draw.text((42, y), line[:100], font=body_font, fill=_color(line))
        y += 24
    if cursor:
        draw.rectangle((42, y + 2, 54, y + 22), fill=GREEN)
    return image


def main() -> None:
    wreck = _capture(
        ["bash", str(REPO_ROOT / "loops" / "bug-fix-red-green" / "wreck.sh")],
        expected_code=1,
    )
    bounded = _capture(
        [
            str(_python()),
            "-m",
            "bounded_loops.cli",
            "run",
            str(REPO_ROOT / "loops" / "bug-fix-red-green"),
            "--yes",
        ],
        expected_code=0,
    )

    transcript = ["$ ./loops/bug-fix-red-green/wreck.sh"]
    transcript.extend(_selected_wreck_lines(wreck))
    transcript.extend(["", "$ bl run loops/bug-fix-red-green --yes"])
    transcript.extend(bounded.splitlines())

    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(1, len(transcript) + 1):
        frames.append(_frame(transcript[:index], cursor=index == len(transcript)))
        durations.append(170 if index < len(transcript) else 2800)
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"Wrote {OUTPUT} from verified command output ({len(frames)} frames).")


if __name__ == "__main__":
    main()
