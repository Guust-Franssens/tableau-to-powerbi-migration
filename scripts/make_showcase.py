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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("make_showcase")

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL_HEIGHT = 720
GAP = 40
MARGIN = 34
LABEL_H = 52
BG = (255, 255, 255)
# Brand-coloured label strips (match the LinkedIn carousel), tuned to read on the README's white canvas.
TABLEAU_STRIP = (31, 78, 130)  # Tableau navy strip, white label text
PBI_STRIP = (242, 200, 17)  # Power BI gold strip, dark label text
TABLEAU_ACCENT = (78, 121, 167)  # lighter Tableau blue, accent text for the hero / social strip titles
PBI_ACCENT = (225, 119, 66)  # Power BI orange, accent text for the hero / social strip titles
STRIP_TEXT_LIGHT = (255, 255, 255)
STRIP_TEXT_DARK = (18, 28, 44)
CORNER_RADIUS = 14
LOGO_TABLEAU = REPO_ROOT / "docs" / "showcase" / "assets" / "logo-tableau.png"
LOGO_PBI = REPO_ROOT / "docs" / "showcase" / "assets" / "logo-powerbi.png"
DEFAULT_HERO = [
    "price-of-prosperity",
    "health-tracker",
    "wind-energy-utilization",
    "shipping-kpis",
]
SOCIAL_STRIP = [
    "price-of-prosperity",
    "health-tracker",
    "wind-energy-utilization",
]


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


_CHIPS: dict[tuple[str, int], Image.Image] = {}


def _logo_chip(path: Path, height: int) -> Image.Image:
    """A small white rounded chip holding a product logo, trimmed to its content, so the logo reads
    cleanly on any brand-coloured strip."""
    key = (str(path), height)
    if key in _CHIPS:
        return _CHIPS[key]
    logo = Image.open(path).convert("RGBA")
    bbox = logo.split()[3].getbbox()
    if bbox:
        logo = logo.crop(bbox)
    pad_y, pad_x = 6, 18
    inner_h = height - 2 * pad_y
    logo = logo.resize((max(1, round(logo.width * inner_h / logo.height)), inner_h), Image.Resampling.LANCZOS)
    chip_w = logo.width + 2 * pad_x
    chip = Image.new("RGBA", (chip_w, height), (0, 0, 0, 0))
    mask = Image.new("L", (chip_w, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, chip_w - 1, height - 1), radius=height // 2, fill=255)
    chip.paste(Image.new("RGBA", (chip_w, height), (255, 255, 255, 255)), (0, 0), mask)
    chip.paste(logo, (pad_x, pad_y), logo)
    _CHIPS[key] = chip
    return chip


def _labeled_panel(
    img: Image.Image, label: str, strip_color: tuple[int, int, int], text_color: tuple[int, int, int], logo: Path
) -> Image.Image:
    """A rounded, branded card: a brand-coloured label strip (role text on the left, product-logo chip on
    the right) above the screenshot, with rounded corners so it sits as a card on the white README canvas."""
    width, height = img.width, img.height + LABEL_H
    panel = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    strip = Image.new("RGBA", (width, LABEL_H), strip_color + (255,))
    draw = ImageDraw.Draw(strip)
    draw.text((20, LABEL_H // 2), label, fill=text_color, font=_font(22), anchor="lm")
    chip = _logo_chip(logo, LABEL_H - 18)
    strip.alpha_composite(chip, (width - 16 - chip.width, (LABEL_H - chip.height) // 2))
    panel.paste(strip, (0, 0))
    panel.paste(img.convert("RGBA"), (0, LABEL_H))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=CORNER_RADIUS, fill=255)
    panel.putalpha(mask)
    return panel


def _paste_with_shadow(canvas: Image.Image, panel: Image.Image, pos: tuple[int, int]) -> None:
    """Composite a rounded RGBA panel onto the canvas with a soft drop shadow, for depth on white."""
    x, y = pos
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    tint = Image.new("RGBA", panel.size, (30, 41, 59, 255))
    tint.putalpha(panel.split()[3].point(lambda v: int(v * 0.28)))
    shadow.paste(tint, (x, y + 8), tint)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(panel, (x, y))


def compose_pair(  # pylint: disable=too-many-arguments  # cohesive compositor: 2 image paths + crop + out + layout + order
    tableau_path: Path,
    pbi_path: Path,
    pbi_crop: dict[str, float] | None,
    out_path: Path,
    layout: str,
    *,
    order: str = "before-after",
) -> None:
    """Compose one Tableau vs Power BI comparison PNG. layout='side-by-side' (wide, good for the README)
    or 'stacked' (taller aspect ratio that reads better on a LinkedIn feed). order='before-after' puts
    Tableau first (the natural 'migration' reading for the repo); order='after-before' puts the Power BI
    result first (a stronger scroll-stopper for LinkedIn: show the polished result, then reveal the
    source it was auto-migrated from). Each panel is a branded, rounded card (Tableau navy / Power BI
    gold strip with the product logo) with a soft drop shadow on the white canvas."""
    tab_label = "BEFORE" if order == "after-before" else "SOURCE"
    pbi_label = "AFTER" if order == "after-before" else "MIGRATED"
    tableau_panel = _labeled_panel(
        _scaled(tableau_path, None), tab_label, TABLEAU_STRIP, STRIP_TEXT_LIGHT, LOGO_TABLEAU
    )
    pbi_panel = _labeled_panel(_scaled(pbi_path, pbi_crop), pbi_label, PBI_STRIP, STRIP_TEXT_DARK, LOGO_PBI)
    first, second = (pbi_panel, tableau_panel) if order == "after-before" else (tableau_panel, pbi_panel)
    if layout == "stacked":
        width = MARGIN * 2 + max(first.width, second.width)
        height = MARGIN * 2 + first.height + GAP + second.height
        canvas = Image.new("RGBA", (width, height), BG + (255,))
        _paste_with_shadow(canvas, first, ((width - first.width) // 2, MARGIN))
        _paste_with_shadow(canvas, second, ((width - second.width) // 2, MARGIN + first.height + GAP))
    else:
        width = MARGIN * 2 + first.width + GAP + second.width
        height = MARGIN * 2 + max(first.height, second.height)
        canvas = Image.new("RGBA", (width, height), BG + (255,))
        _paste_with_shadow(canvas, first, (MARGIN, MARGIN))
        _paste_with_shadow(canvas, second, (MARGIN + first.width + GAP, MARGIN))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path)
    logger.info("Wrote %s (%dx%d)", out_path.relative_to(REPO_ROOT), width, height)


def build_hero(featured: list[str], assets_dir: Path, out_path: Path, max_width: int = 1600) -> None:
    """Stack the (already cropped) side-by-side composites of the featured slugs into one tall hero
    image for the top of the README. Reuses the same chrome-cropped panels the gallery uses, so the
    hero and gallery stay consistent."""
    tiles = [
        Image.open(assets_dir / f"{slug}-1.png").convert("RGB")
        for slug in featured
        if (assets_dir / f"{slug}-1.png").exists()
    ]
    if not tiles:
        logger.warning("build_hero: no composite tiles found for %s", featured)
        return
    width = max(t.width for t in tiles)
    scaled = [
        t if t.width == width else t.resize((width, round(t.height * width / t.width)), Image.Resampling.LANCZOS)
        for t in tiles
    ]
    title_h, gap = 72, 28
    total_h = title_h + sum(t.height + gap for t in scaled)
    canvas = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (width // 2, title_h // 2),
        "Tableau  \u2192  Power BI     before / after",
        fill=(40, 40, 40),
        font=_font(34),
        anchor="mm",
    )
    y = title_h
    for tile in scaled:
        canvas.paste(tile, (0, y))
        y += tile.height + gap
    if canvas.width > max_width:
        canvas = canvas.resize((max_width, round(canvas.height * max_width / canvas.width)), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    logger.info("Wrote %s (%dx%d)", out_path.relative_to(REPO_ROOT), canvas.width, canvas.height)


def build_social_strip(  # pylint: disable=too-many-locals  # cohesive multi-panel layout
    featured: list[str], assets_dir: Path, out_path: Path, tile_height: int = 1400
) -> None:
    """Combine the featured after/before composites (Power BI on top, Tableau below) side by side into a
    single titled image: three proofs in one asset for a LinkedIn post. Uses the -stacked-afterbefore
    tiles so each column already reads AFTER over BEFORE."""
    tiles: list[Image.Image] = []
    for slug in featured:
        p = assets_dir / f"{slug}-1-stacked-afterbefore.png"
        if p.exists():
            img = Image.open(p).convert("RGB")
            tiles.append(
                img.resize((round(img.width * tile_height / img.height), tile_height), Image.Resampling.LANCZOS)
            )
    if not tiles:
        logger.warning("build_social_strip: no after/before tiles found for %s", featured)
        return
    title_h, pad, gap = 96, 40, 32
    strip_w = pad * 2 + sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    canvas = Image.new("RGB", (strip_w, title_h + pad + tile_height + pad), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, title_h // 2 + 6), "AFTER  \u00b7  Power BI", fill=PBI_ACCENT, font=_font(38), anchor="lm")
    draw.text(
        (strip_w - pad, title_h // 2 + 6),
        "migrated by AI from  \u00b7  Tableau (BEFORE)",
        fill=TABLEAU_ACCENT,
        font=_font(38),
        anchor="rm",
    )
    x = pad
    for tile in tiles:
        canvas.paste(tile, (x, title_h))
        x += tile.width + gap
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    logger.info("Wrote %s (%dx%d)", out_path.relative_to(REPO_ROOT), canvas.width, canvas.height)


def _render_entry(entry: dict[str, Any], assets_dir: Path, layout: str, order: str = "before-after") -> list[str]:
    """Render every page-pair for one workbook; return the gallery markdown lines for it."""
    slug = entry["slug"]
    suffix = "-stacked" if layout == "stacked" else ""
    suffix += "-afterbefore" if order == "after-before" else ""
    lines = [f"### {entry['title']}", "", entry.get("caption", ""), ""]
    if entry.get("source_url"):
        lines += [f"**Source:** [original Tableau Public dashboard \u2197]({entry['source_url']})", ""]
    for i, page in enumerate(entry["pages"], 1):
        tableau = REPO_ROOT / page["tableau"]
        pbi = page.get("powerbi")
        page_name = page.get("name", f"Page {i}")
        if not pbi or not (REPO_ROOT / pbi).exists():
            lines += [f"- **{page_name}** — Power BI screenshot pending Desktop capture.", ""]
            continue
        out = assets_dir / f"{slug}-{i}{suffix}.png"
        compose_pair(tableau, REPO_ROOT / pbi, page.get("powerbi_crop"), out, layout, order=order)
        rel = out.relative_to(REPO_ROOT / "docs" / "showcase").as_posix()
        lines += [f"**{page_name}**", "", f"![{entry['title']} - {page_name}]({rel})", ""]
    return lines


def main() -> None:
    """Read the showcase config, compose all comparison images, and write the gallery README."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "docs" / "showcase" / "showcase.json")
    parser.add_argument(
        "--layout",
        choices=["side-by-side", "stacked"],
        default="side-by-side",
        help="side-by-side (wide, for the README) or stacked (tall, better for a LinkedIn feed / mobile)",
    )
    parser.add_argument(
        "--order",
        choices=["before-after", "after-before"],
        default="before-after",
        help="before-after: Tableau first (repo showcase). after-before: Power BI result first, a stronger "
        "scroll-stopper for LinkedIn. Writes -afterbefore assets and README-afterbefore.md without "
        "touching the repo's before/after showcase.",
    )
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    showcase_dir = args.config.parent
    assets_dir = showcase_dir / "assets"
    after_before = args.order == "after-before"
    if after_before:
        lead = (
            "Each row leads with the **Power BI** report our AI-assisted pipeline generated (left), next to "
            "the original **Tableau** dashboard it was migrated from (right). Power BI screenshots are live "
            "Power BI Desktop renders of the generated PBIR report over the migrated semantic model."
        )
        title = "# Migration Showcase (after / before) — Power BI ← Tableau"
        hint = "> After/before variant for social posts. Generated by `scripts/make_showcase.py --order after-before`."
    else:
        lead = (
            "Each row shows the original **Tableau** dashboard (left) next to the **Power BI** report our "
            "AI-assisted pipeline generated from it (right). Power BI screenshots are live Power BI Desktop "
            "renders of the generated PBIR report over the migrated semantic model."
        )
        title = "# Migration Showcase — Tableau → Power BI"
        hint = (
            "> Generated by `scripts/make_showcase.py` from `docs/showcase/showcase.json`. Run with "
            "`--layout stacked` for tall, LinkedIn/mobile-friendly versions, or `--order after-before` "
            "to lead with the Power BI result."
        )
    md = [title, "", lead, "", hint, ""]
    md += [
        "Every example is a public Tableau Public workbook by its original author; each entry links back to "
        "the source it was migrated from. Full provenance for all 16 migrations is in "
        "[`migrations/README.md`](../../migrations/README.md).",
        "",
    ]
    for entry in config["workbooks"]:
        md += _render_entry(entry, assets_dir, args.layout, args.order)
    if args.layout == "side-by-side" and not after_before:
        build_hero(config.get("hero", DEFAULT_HERO), assets_dir, showcase_dir / "hero-before-after.png")
    if after_before:
        build_social_strip(SOCIAL_STRIP, assets_dir, showcase_dir / "social-after-before.png")
    if after_before:
        readme_name = "README-afterbefore.md"
    elif args.layout == "stacked":
        readme_name = "README-stacked.md"
    else:
        readme_name = "README.md"
    readme = showcase_dir / readme_name
    readme.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    logger.info("Wrote %s", readme.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
