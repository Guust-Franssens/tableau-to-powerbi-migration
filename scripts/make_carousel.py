"""
purpose: Build a swipeable LinkedIn carousel (a multi-page PDF, one slide per swipe) that tells the
         Tableau -> Power BI migration story as clean before/after proof. Each real dashboard is floated
         as a rounded, soft-shadowed card on a brand-tinted background (Power BI gold, then Tableau
         blue), with a compact top bar (brand eyebrow + example name + product logo) and a subtle
         "swipe to see the original" hint. A close slide carries the repo link and acknowledgments.
         Also writes each slide as a PNG. Square 1:1 reads best in the LinkedIn feed.
usage:   python scripts/make_carousel.py [--output docs/showcase/carousel/linkedin-carousel.pdf]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("make_carousel")

REPO_ROOT = Path(__file__).resolve().parent.parent
W, H = 1080, 1080  # square 1:1: a wide dashboard fills the frame, the report title sits in the padding above
MARGIN = 56


# Dark theme, with each tool's brand identity carried on its own slide.
BG = (12, 23, 37)
FG = (233, 238, 244)
MUTED = (156, 171, 188)
TABLEAU = (98, 150, 200)  # Tableau blue: the "before" brand accent (glow + eyebrow), readable on dark
PBI = (242, 200, 17)  # Power BI gold: the "after" brand accent (glow + eyebrow), readable on dark
CARD = (19, 32, 49)
LINE = (44, 62, 84)

REPO_TAG = "github.com/Guust-Franssens/tableau-to-powerbi-migration"
LOGO_PBI = REPO_ROOT / "docs" / "showcase" / "assets" / "logo-powerbi.png"
LOGO_TABLEAU = REPO_ROOT / "docs" / "showcase" / "assets" / "logo-tableau.png"

_FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_SANS = ["segoeui.ttf"]
_SB = ["seguisb.ttf", "segoeuib.ttf"]
_B = ["segoeuib.ttf", "seguisb.ttf"]


def font(kind: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Load (and cache) the first available TrueType font in `kind`."""
    key = (kind[0], size)
    if key not in _FONTS:
        for name in kind:
            try:
                _FONTS[key] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def _footer(d: ImageDraw.ImageDraw) -> None:
    """The repo tag, centred along the bottom of every slide (aligns with the centred swipe hint)."""
    d.text((W // 2, H - 46), REPO_TAG, font=font(_SANS, 22), fill=MUTED, anchor="mm")


def _slide() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A blank dark slide with the repo tag in the footer."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _footer(d)
    return img, d


def _wrap(d: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy word-wrap to `max_w` pixels."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if d.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _crop(img: Image.Image, crop: dict[str, float] | None) -> Image.Image:
    """Fractional inset crop {top,bottom,left,right} to trim chrome (e.g. the Filters pane)."""
    if not crop:
        return img
    w, h = img.size
    return img.crop(
        (
            int(w * crop.get("left", 0)),
            int(h * crop.get("top", 0)),
            w - int(w * crop.get("right", 0)),
            h - int(h * crop.get("bottom", 0)),
        )
    )


_CHIPS: dict[tuple[str, int], Image.Image] = {}


def _logo_chip(path: Path, height: int) -> Image.Image:
    """A small white rounded 'chip' holding a product logo, trimmed to its content and centred. Both
    logos read cleanly on white, whatever the brand-coloured strip behind them."""
    key = (str(path), height)
    if key in _CHIPS:
        return _CHIPS[key]
    logo = Image.open(path).convert("RGBA")
    alpha = logo.split()[3]
    if alpha.getextrema()[0] == 0:  # transparent border: trim by the alpha channel
        bbox = alpha.getbbox()
    else:  # opaque (white) border: trim by difference from white
        bbox = ImageChops.difference(logo.convert("RGB"), Image.new("RGB", logo.size, (255, 255, 255))).getbbox()
    if bbox:
        logo = logo.crop(bbox)
    pad_y, pad_x = 7, 24  # more horizontal room so the wordmark clears the pill's rounded ends
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


def _fit_shot(path: Path, crop: dict[str, float] | None, box_w: int, box_h: int) -> Image.Image:
    """Load, crop, and resize a screenshot to fit within (box_w, box_h), preserving aspect."""
    shot = _crop(Image.open(path).convert("RGB"), crop)
    scale = min(box_w / shot.width, box_h / shot.height)
    return shot.resize(
        (max(1, round(shot.width * scale)), max(1, round(shot.height * scale))), Image.Resampling.LANCZOS
    )


def _add_glow(
    img: Image.Image, accent: tuple[int, int, int], box: tuple[float, float, float, float], strength: float
) -> Image.Image:
    """Composite a soft radial glow of `accent` (an elliptical `box`, heavily blurred) onto `img`."""
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((int(box[0]), int(box[1]), int(box[2]), int(box[3])), fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(200)).point(lambda v: int(v * strength))
    return Image.composite(Image.new("RGB", (W, H), accent), img, glow)


def _brand_bg(accent: tuple[int, int, int]) -> Image.Image:
    """The dark slide background with a soft brand-coloured glow behind the card, so the whole slide
    carries the tool's colour rather than a saturated bar."""
    return _add_glow(Image.new("RGB", (W, H), BG), accent, (-W * 0.10, H * 0.02, W * 1.10, H * 0.92), 0.18)


def _place_card(bg: Image.Image, shot: Image.Image, top: int, bottom: int) -> Image.Image:
    """Paste `shot` onto `bg` as a rounded card with a soft drop shadow, centred in [top, bottom]."""
    radius, cw, ch = 16, shot.width, shot.height
    x, y = (W - cw) // 2, top + (bottom - top - ch) // 2
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x - 4, y + 14, x + cw + 4, y + ch + 30), radius=radius + 6, fill=(0, 0, 0, 150)
    )
    base = Image.alpha_composite(bg.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(30)))
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=radius, fill=255)
    base.paste(shot, (x, y), mask)
    return base.convert("RGB")


# --------------------------------------------------------------------------------------------------
# Slides
# --------------------------------------------------------------------------------------------------
def slide_cover() -> Image.Image:
    """Hook slide."""
    img, d = _slide()
    d.text((MARGIN, 240), "Tableau  \u2192  Power BI", font=font(_B, 78), fill=FG)
    d.text((MARGIN, 340), "end to end, automated", font=font(_B, 78), fill=PBI)
    for i, line in enumerate(
        _wrap(
            d,
            "BI migrations were always expensive: the front-end had to be rebuilt by hand. Recent work on "
            "TMDL, semantic modeling, and the Power BI Desktop Bridge changes that.",
            font(_SANS, 38),
            W - 2 * MARGIN,
        )
    ):
        d.text((MARGIN, 520 + i * 52), line, font=font(_SANS, 38), fill=MUTED)
    d.line((MARGIN, 780, MARGIN + 120, 780), fill=PBI, width=5)
    d.text((MARGIN, 820), "16 real dashboards, migrated by an agent pipeline.", font=font(_SB, 36), fill=FG)
    d.text((MARGIN, 872), "Swipe for after / before.", font=font(_SB, 36), fill=FG)
    d.text((W - MARGIN, H - 100), "swipe \u2192", font=font(_B, 40), fill=PBI, anchor="rm")
    return img


def slide_single(  # pylint: disable=too-many-arguments  # cohesive full-slide builder
    title: str,
    path: Path,
    crop: dict[str, float] | None,
    *,
    label: str,
    accent: tuple[int, int, int],
    swipe_hint: bool = False,
    logo: Path | None = None,
) -> Image.Image:
    """One wide dashboard per slide, floated as a rounded card with a soft shadow on a brand-tinted
    background: the migrated Power BI result on its own slide, then the Tableau original to swipe to. A
    compact top bar (brand eyebrow + example name on the left, product logo on the right) replaces a
    redundant big title, since each dashboard already shows its own name."""
    band_top = 164  # just below the title block
    band_bottom = H - (128 if swipe_hint else 66)  # centre the card between the title and the hint / footer
    shot = _fit_shot(path, crop, W - 32, band_bottom - band_top)  # near full-bleed width: minimal side margin
    img = _place_card(_brand_bg(accent), shot, band_top, band_bottom)
    d = ImageDraw.Draw(img)
    _footer(d)
    d.text((MARGIN, 70), label, font=font(_SB, 24), fill=accent)
    d.text((MARGIN, 102), title, font=font(_B, 44), fill=FG)
    if logo is not None:
        chip = _logo_chip(logo, 56)
        img.paste(chip, (W - MARGIN - chip.width, 90), chip)
    if swipe_hint:
        d.text((W // 2, H - 106), "swipe to see the original  \u2192", font=font(_SB, 27), fill=accent, anchor="mm")
    return img


def slide_close() -> Image.Image:
    """Close: the repo and acknowledgments, on a Tableau-blue -> Power BI-gold wash."""
    img = _add_glow(Image.new("RGB", (W, H), BG), TABLEAU, (-W * 0.45, H * 0.42, W * 0.72, H * 1.35), 0.16)
    img = _add_glow(img, PBI, (W * 0.30, -H * 0.32, W * 1.45, H * 0.58), 0.16)
    d = ImageDraw.Draw(img)
    d.text((MARGIN, 118), "It's all open source.", font=font(_B, 60), fill=FG)
    for i, line in enumerate(
        _wrap(
            d,
            "The semantic model, the report, and the migration notes for every example are in the "
            "repo, including the bugs found and fixed along the way.",
            font(_SANS, 32),
            W - 2 * MARGIN,
        )
    ):
        d.text((MARGIN, 214 + i * 44), line, font=font(_SANS, 32), fill=MUTED)
    d.rounded_rectangle((MARGIN, 402, W - MARGIN, 512), radius=16, fill=CARD, outline=LINE, width=2)
    d.text((MARGIN + 34, 428), "GITHUB", font=font(_SB, 22), fill=MUTED)
    d.text((MARGIN + 34, 462), REPO_TAG, font=font(_SB, 32), fill=TABLEAU)
    d.text((MARGIN, 616), "Made possible by recent Microsoft work", font=font(_B, 32), fill=FG)
    acks = [
        "Rui Romano and team, for the Power BI Modeling MCP, TMDL, and PBIR: a model and report as code.",
        "The Power BI Desktop Bridge team (Emily Lisa, Harleen Kaur, Sara Lammini Rodriguez), for the "
        "iterative open / refresh / screenshot loop this toolkit runs on.",
    ]
    y = 668
    for a in acks:
        for line in _wrap(d, a, font(_SANS, 27), W - 2 * MARGIN - 24):
            d.text((MARGIN + 24, y), line, font=font(_SANS, 27), fill=MUTED)
            y += 37
        y += 16
    return img


PROOFS = [
    {
        "title": "The Price of Prosperity",
        "powerbi": "migrations/price-of-prosperity/reference/powerbi-dashboard.png",
        "tableau": "migrations/price-of-prosperity/reference/tableau-dashboard.png",
        "powerbi_crop": None,
    },
    {
        "title": "Health Tracker",
        "powerbi": "migrations/health-tracker/reference/powerbi-health-tracker.png",
        "tableau": "migrations/health-tracker/reference/tableau-metrics.png",
        "powerbi_crop": {"left": 0.018, "right": 0.042},
    },
    {
        "title": "NL Wind Energy Utilization",
        "powerbi": "migrations/wind-energy-utilization/reference/powerbi-wind-energy.png",
        "tableau": "migrations/wind-energy-utilization/reference/tableau-dashboard.png",
        "powerbi_crop": {"left": 0.095, "right": 0.125},
    },
]


def _example_slides() -> list[Image.Image]:
    """The examples: for each proof, a full Power BI slide (with the swipe hint), then the full Tableau
    original to swipe to."""
    out: list[Image.Image] = []
    for p in PROOFS:
        out.append(
            slide_single(
                p["title"],
                REPO_ROOT / p["powerbi"],
                p["powerbi_crop"],
                label="AFTER  \u00b7  MIGRATED",
                accent=PBI,
                swipe_hint=True,
                logo=LOGO_PBI,
            )
        )
        out.append(
            slide_single(
                p["title"],
                REPO_ROOT / p["tableau"],
                None,
                label="BEFORE  \u00b7  ORIGINAL",
                accent=TABLEAU,
                logo=LOGO_TABLEAU,
            )
        )
    return out


def build(output: Path, *, cover: bool = False) -> None:
    """Render the square swipeable carousel and save a multi-page PDF + per-slide PNGs. Each proof is a
    Power BI slide followed by its Tableau original (swipe to reveal); a close slide carries the repo
    link and acknowledgments. The deck opens on the first example (the post caption is the intro); pass
    cover=True to prepend a title slide."""
    slides = [slide_cover()] if cover else []
    slides += _example_slides()
    slides.append(slide_close())
    output.parent.mkdir(parents=True, exist_ok=True)
    png_dir = output.parent / "slides"
    png_dir.mkdir(parents=True, exist_ok=True)
    for old in png_dir.glob("slide-*.png"):
        old.unlink()
    for i, s in enumerate(slides, 1):
        s.save(png_dir / f"slide-{i:02d}.png")
    slides[0].save(output, "PDF", save_all=True, append_images=slides[1:], resolution=96.0)
    logger.info(
        "wrote %s (%d slides) + PNGs in %s", output.relative_to(REPO_ROOT), len(slides), png_dir.relative_to(REPO_ROOT)
    )


def main() -> None:
    """CLI entry point: build the square swipeable LinkedIn carousel."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "docs" / "showcase" / "carousel" / "linkedin-carousel.pdf",
        help="output PDF path",
    )
    parser.add_argument(
        "--cover", action="store_true", help="prepend a title slide (default: open on the first example)"
    )
    args = parser.parse_args()
    build(args.output, cover=args.cover)


if __name__ == "__main__":
    main()
