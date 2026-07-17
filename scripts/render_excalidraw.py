"""
purpose: Render a subset of an .excalidraw file (rectangles, bound/standalone text, arrows) to a PNG,
         so architecture diagrams authored as Excalidraw JSON can be committed as repo/README visuals
         without depending on the (SSO-gated) hosted Excalidraw export. The .excalidraw stays the
         editable source of truth; regenerate the PNG after edits.
usage:   python scripts/render_excalidraw.py docs/architecture.excalidraw -o docs/architecture.png
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("render_excalidraw")

SCALE = 2  # supersample for crisp text, downscaled at the end
PAD = 30


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default()


def _bounds(elements: list[dict[str, Any]]) -> tuple[float, float]:
    max_x = max((e.get("x", 0) + e.get("width", 0) for e in elements), default=0)
    max_y = max((e.get("y", 0) + e.get("height", 0) for e in elements), default=0)
    return max_x + PAD, max_y + PAD


def _draw_multiline(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], text: str, size: int) -> None:
    """Center multi-line text within box (x, y, w, h), all in device pixels."""
    x, y, w, h = box
    font = _font(size)
    lines = text.split("\n")
    line_h = size * SCALE * 1.35
    start_y = y + (h - line_h * len(lines)) / 2
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=font)
        draw.text((x + (w - tw) / 2, start_y + i * line_h), line, fill="#000000", font=font)


def _draw_rect(draw: ImageDraw.ImageDraw, el: dict[str, Any]) -> None:
    x, y, w, h = (el[k] * SCALE for k in ("x", "y", "width", "height"))
    fill = el.get("backgroundColor", "transparent")
    fill = None if fill == "transparent" else fill
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=10 * SCALE,
        fill=fill,
        outline=el.get("strokeColor", "#000000"),
        width=max(1, el.get("strokeWidth", 1)) * SCALE,
    )


def _draw_arrow(draw: ImageDraw.ImageDraw, el: dict[str, Any]) -> None:
    x, y = el["x"] * SCALE, el["y"] * SCALE
    pts = [(x + px * SCALE, y + py * SCALE) for px, py in el.get("points", [[0, 0]])]
    color = el.get("strokeColor", "#000000")
    width = max(1, el.get("strokeWidth", 1)) * SCALE
    dashed = el.get("strokeStyle") == "dashed"
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if dashed:
            _dashed_line(draw, (x0, y0), (x1, y1), color, width)
        else:
            draw.line((x0, y0, x1, y1), fill=color, width=width)
    # arrowhead at the final point
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    ang = math.atan2(y1 - y0, x1 - x0)
    size = 10 * SCALE
    for da in (math.radians(150), math.radians(-150)):
        draw.line((x1, y1, x1 + size * math.cos(ang + da), y1 + size * math.sin(ang + da)), fill=color, width=width)


def _dashed_line(
    draw: ImageDraw.ImageDraw, p0: tuple[float, float], p1: tuple[float, float], color: str, width: int
) -> None:
    x0, y0 = p0
    x1, y1 = p1
    dist = math.hypot(x1 - x0, y1 - y0)
    dash = 8 * SCALE
    n = max(1, int(dist / dash))
    for i in range(0, n, 2):
        s, e = i / n, min((i + 1) / n, 1.0)
        draw.line(
            (x0 + (x1 - x0) * s, y0 + (y1 - y0) * s, x0 + (x1 - x0) * e, y0 + (y1 - y0) * e), fill=color, width=width
        )


def render(src: Path, out: Path) -> None:
    """Render the .excalidraw file to a PNG (rectangles, text, arrows)."""
    doc = json.loads(src.read_text(encoding="utf-8"))
    elements = doc.get("elements", [])
    by_id = {e["id"]: e for e in elements if "id" in e}
    width, height = _bounds(elements)
    img = Image.new(
        "RGB", (int(width * SCALE), int(height * SCALE)), doc.get("appState", {}).get("viewBackgroundColor", "#ffffff")
    )
    draw = ImageDraw.Draw(img)

    for el in elements:
        if el.get("type") == "rectangle":
            _draw_rect(draw, el)
    for el in elements:
        if el.get("type") == "arrow":
            _draw_arrow(draw, el)
    for el in elements:
        if el.get("type") != "text":
            continue
        size = el.get("fontSize", 16)
        container = by_id.get(el.get("containerId")) if el.get("containerId") else None
        if container is not None:
            box = (
                container["x"] * SCALE,
                container["y"] * SCALE,
                container["width"] * SCALE,
                container["height"] * SCALE,
            )
        else:
            box = (el["x"] * SCALE, el["y"] * SCALE, el.get("width", 200) * SCALE, el.get("height", size * 1.5) * SCALE)
        _draw_multiline(draw, box, el.get("text", ""), size)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((int(width), int(height)), Image.Resampling.LANCZOS).save(out)
    logger.info("Wrote %s (%dx%d)", out, int(width), int(height))


def main() -> None:
    """Parse args and render the given .excalidraw file to PNG."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args()
    render(args.src, args.out)


if __name__ == "__main__":
    main()
