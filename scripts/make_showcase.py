"""
purpose: Build the Tableau -> Power BI migration showcase gallery: for each migrated workbook page,
         compose a side-by-side "Tableau (source)" vs "Power BI (migrated)" comparison image and emit
         a docs/showcase/README.md gallery. Driven by docs/showcase/showcase.json so new workbooks can
         be added as their clean Power BI Desktop screenshots become available.
usage:   python scripts/make_showcase.py [--config docs/showcase/showcase.json]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("make_showcase")

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL_HEIGHT = 720
GAP = 40
MARGIN = 24
LABEL_H = 44
BG = (255, 255, 255)
LABEL_BG = (243, 243, 243)
TABLEAU_ACCENT = (78, 121, 167)
PBI_ACCENT = (225, 119, 66)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _crop_fractional(img: Image.Image, crop: dict[str, float] | None) -> Image.Image:
    """Crop by fractional insets {top,bottom,left,right} (0..1) - used to trim Power BI Desktop chrome
    (the refresh banner and the Filters pane) out of a raw screenshot."""
    if not crop:
        return img
    w, h = img.size
    left = int(w * crop.get("left", 0))
    right = w - int(w * crop.get("right", 0))
    top = int(h * crop.get("top", 0))
    bottom = h - int(h * crop.get("bottom", 0))
    return img.crop((left, top, right, bottom))


def _scaled(path: Path, crop: dict[str, float] | None) -> Image.Image:
    img = Image.open(path).convert("RGB")
    img = _crop_fractional(img, crop)
    scale = PANEL_HEIGHT / img.height
    return img.resize((max(1, int(img.width * scale)), PANEL_HEIGHT), Image.Resampling.LANCZOS)


def _labeled_panel(img: Image.Image, label: str, accent: tuple[int, int, int]) -> Image.Image:
    """Stack a colored label strip above an image panel."""
    panel = Image.new("RGB", (img.width, img.height + LABEL_H), BG)
    strip = Image.new("RGB", (img.width, LABEL_H), LABEL_BG)
    draw = ImageDraw.Draw(strip)
    draw.rectangle((0, 0, 6, LABEL_H), fill=accent)
    draw.text((18, LABEL_H // 2), label, fill=(40, 40, 40), font=_font(20), anchor="lm")
    panel.paste(strip, (0, 0))
    panel.paste(img, (0, LABEL_H))
    return panel


def compose_pair(tableau_path: Path, pbi_path: Path, pbi_crop: dict[str, float] | None, out_path: Path) -> None:
    """Compose one side-by-side Tableau|Power BI comparison PNG."""
    left = _labeled_panel(_scaled(tableau_path, None), "Tableau  (source)", TABLEAU_ACCENT)
    right = _labeled_panel(_scaled(pbi_path, pbi_crop), "Power BI  (migrated)", PBI_ACCENT)
    width = MARGIN * 2 + left.width + GAP + right.width
    height = MARGIN * 2 + max(left.height, right.height)
    canvas = Image.new("RGB", (width, height), BG)
    canvas.paste(left, (MARGIN, MARGIN))
    canvas.paste(right, (MARGIN + left.width + GAP, MARGIN))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    logger.info("Wrote %s (%dx%d)", out_path.relative_to(REPO_ROOT), width, height)


def _render_entry(entry: dict[str, Any], assets_dir: Path) -> list[str]:
    """Render every page-pair for one workbook; return the gallery markdown lines for it."""
    slug = entry["slug"]
    lines = [f"### {entry['title']}", "", entry.get("caption", ""), ""]
    for i, page in enumerate(entry["pages"], 1):
        tableau = REPO_ROOT / page["tableau"]
        pbi = page.get("powerbi")
        page_name = page.get("name", f"Page {i}")
        if not pbi or not (REPO_ROOT / pbi).exists():
            lines += [f"- **{page_name}** — Power BI screenshot pending Desktop capture.", ""]
            continue
        out = assets_dir / f"{slug}-{i}.png"
        compose_pair(tableau, REPO_ROOT / pbi, page.get("powerbi_crop"), out)
        rel = out.relative_to(REPO_ROOT / "docs" / "showcase").as_posix()
        lines += [f"**{page_name}**", "", f"![{entry['title']} - {page_name}]({rel})", ""]
    return lines


def main() -> None:
    """Read the showcase config, compose all comparison images, and write the gallery README."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "docs" / "showcase" / "showcase.json")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    showcase_dir = args.config.parent
    assets_dir = showcase_dir / "assets"
    md = [
        "# Migration Showcase — Tableau → Power BI",
        "",
        "Each row shows the original **Tableau** dashboard (left) next to the **Power BI** report our "
        "AI-assisted pipeline generated from it (right). Power BI screenshots are live Power BI Desktop "
        "renders of the generated PBIR report over the migrated semantic model.",
        "",
        "> Generated by `scripts/make_showcase.py` from `docs/showcase/showcase.json`.",
        "",
    ]
    for entry in config["workbooks"]:
        md += _render_entry(entry, assets_dir)
    (showcase_dir / "README.md").write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    logger.info("Wrote %s", (showcase_dir / "README.md").relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
