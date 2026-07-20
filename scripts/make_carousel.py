"""
purpose: Build a swipeable LinkedIn carousel (a multi-page PDF, one slide per swipe) that tells the
         Tableau -> Power BI migration story: a hook, three after/before proof slides (the Power BI
         result on top, the original Tableau below, each with an exact repo-verified number), a
         how-it-works slide, a "one agent caught another's bug" slide, and a close with the repo link
         and acknowledgments. Also writes each slide as a PNG. Every number traces to this repo's
         ground truth. Portrait 4:5 (1080x1350) reads best in the LinkedIn feed.
usage:   python scripts/make_carousel.py [--output docs/showcase/carousel/linkedin-carousel.pdf]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("make_carousel")

REPO_ROOT = Path(__file__).resolve().parent.parent
W, H = 1080, 1350  # default portrait 4:5; set per deck by _set_canvas()
MARGIN = 56


def _set_canvas(shape: str) -> None:
    """Set the slide dimensions for the deck about to be rendered: square 1:1 (best for the reveal
    layout, where one wide dashboard fills the frame) or portrait 4:5 (best for the stacked compare
    layout, and grabs the most vertical space in the LinkedIn feed)."""
    global W, H  # pylint: disable=global-statement
    W, H = (1080, 1080) if shape == "square" else (1080, 1350)


# Dark theme (matches the showcase accent colours).
BG = (12, 23, 37)
PANEL_BG = (247, 248, 250)
FG = (233, 238, 244)
MUTED = (156, 171, 188)
TABLEAU = (98, 150, 200)
PBI = (233, 131, 79)
GREEN = (94, 205, 148)
AMBER = (243, 197, 102)
CARD = (19, 32, 49)
LINE = (44, 62, 84)

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


def _slide() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """A blank dark slide with the repo tag in the footer."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text(
        (MARGIN, H - 48), "github.com/Guust-Franssens/tableau-to-powerbi-migration", font=font(_SANS, 22), fill=MUTED
    )
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


def _panel(  # pylint: disable=too-many-arguments  # cohesive panel builder: path + crop + label + accent + box dims
    path: Path, crop: dict[str, float] | None, label: str, accent: tuple[int, int, int], *, box_w: int, box_h: int
) -> Image.Image:
    """A labelled screenshot panel that fits within (box_w, box_h): a coloured label strip above a
    white card holding the (letterboxed) screenshot."""
    strip_h = 46
    inner_h = box_h - strip_h
    shot = _crop(Image.open(path).convert("RGB"), crop)
    scale = min(box_w / shot.width, inner_h / shot.height)
    shot = shot.resize(
        (max(1, round(shot.width * scale)), max(1, round(shot.height * scale))), Image.Resampling.LANCZOS
    )
    panel = Image.new("RGB", (box_w, box_h), CARD)
    d = ImageDraw.Draw(panel)
    d.rectangle((0, 0, box_w, strip_h), fill=accent)
    d.text((16, strip_h // 2), label, font=font(_B, 25), fill=(255, 255, 255), anchor="lm")
    card = Image.new("RGB", (box_w, inner_h), PANEL_BG)
    card.paste(shot, ((box_w - shot.width) // 2, (inner_h - shot.height) // 2))
    panel.paste(card, (0, strip_h))
    return panel


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


def slide_pair(title: str, faithful: str, spec: dict, note: str) -> Image.Image:
    """Compare layout: AFTER (Power BI) on top, BEFORE (Tableau) below, on one slide."""
    img, d = _slide()
    d.text((MARGIN, 70), title, font=font(_B, 52), fill=FG)
    d.text((MARGIN, 138), faithful, font=font(_SANS, 30), fill=MUTED)
    box_w = W - 2 * MARGIN
    box_h = 452
    top = 200
    after = _panel(
        REPO_ROOT / spec["powerbi"],
        spec.get("powerbi_crop"),
        "AFTER  \u00b7  Power BI (migrated)",
        PBI,
        box_w=box_w,
        box_h=box_h,
    )
    before = _panel(
        REPO_ROOT / spec["tableau"], None, "BEFORE  \u00b7  Tableau (source)", TABLEAU, box_w=box_w, box_h=box_h
    )
    img.paste(after, (MARGIN, top))
    img.paste(before, (MARGIN, top + box_h + 20))
    d.text((MARGIN, top + box_h * 2 + 20 + 26), note, font=font(_SANS, 28), fill=MUTED)
    return img


def slide_single(  # pylint: disable=too-many-arguments,too-many-locals  # cohesive full-slide panel
    title: str, sub: str, path: Path, crop: dict[str, float] | None, *, label: str, accent: tuple[int, int, int]
) -> Image.Image:
    """Reveal layout: one full-size panel per slide (Power BI on its own slide, Tableau on the next).
    The card is sized to the screenshot (no white letterbox) and centred on the dark background."""
    img, d = _slide()
    d.text((MARGIN, 70), title, font=font(_B, 52), fill=FG)
    d.text((MARGIN, 138), sub, font=font(_SANS, 30), fill=MUTED)
    box_w = W - 2 * MARGIN
    strip_h = 46
    area_top, area_bottom = 200, H - 96
    with Image.open(path) as raw:
        shot = _crop(raw.convert("RGB"), crop)
    scale = min(box_w / shot.width, (area_bottom - area_top - strip_h) / shot.height)
    panel_h = strip_h + round(shot.height * scale)
    panel = _panel(path, crop, label, accent, box_w=box_w, box_h=panel_h)
    img.paste(panel, (MARGIN, area_top + (area_bottom - area_top - panel_h) // 2))
    return img


def slide_pipeline() -> Image.Image:
    """How it reasons: the four agents."""
    img, d = _slide()
    d.text((MARGIN, 90), "How it works", font=font(_B, 60), fill=FG)
    d.text((MARGIN, 176), "A deterministic parser feeds four Copilot CLI agents.", font=font(_SANS, 34), fill=MUTED)
    steps = [
        (
            "1  Deterministic parser",
            "Tableau .twb XML \u2192 a schema-validated migration spec. No LLM guesswork here.",
            TABLEAU,
        ),
        ("2  Semantic-model builder", "Tables, relationships, and DAX (LOD expressions, table calculations).", GREEN),
        ("3  Report builder", "Native Power BI pages and visuals, laid out from the Tableau worksheets.", PBI),
        (
            "4  Fidelity validator",
            "Independent and read-only. Compares every figure against the Tableau original.",
            AMBER,
        ),
    ]
    y = 290
    for head, body, accent in steps:
        d.rounded_rectangle((MARGIN, y, W - MARGIN, y + 190), radius=18, fill=CARD, outline=LINE, width=2)
        d.rounded_rectangle((MARGIN, y, MARGIN + 12, y + 190), radius=6, fill=accent)
        d.text((MARGIN + 44, y + 42), head, font=font(_B, 40), fill=FG)
        for i, line in enumerate(_wrap(d, body, font(_SANS, 30), W - 2 * MARGIN - 88)):
            d.text((MARGIN + 44, y + 100 + i * 40), line, font=font(_SANS, 30), fill=MUTED)
        y += 216
    return img


def slide_money() -> Image.Image:
    """The differentiator: one agent caught another's bug."""
    img, d = _slide()
    d.text((MARGIN, 100), "One agent caught", font=font(_B, 62), fill=FG)
    d.text((MARGIN, 176), "another agent's bug", font=font(_B, 62), fill=AMBER)
    rows = [
        (
            "The validator flagged it",
            "On one dashboard, a \u201clatest day\u201d highlight was wrong: the Tableau source "
            "highlighted 9 of 9 cards, the Power BI render highlighted 0.",
            AMBER,
        ),
        (
            "It found the root cause",
            "An off-by-one in a date window: the \u201clast week\u201d range excluded the "
            "maximum date, so the highlighted day was filtered out.",
            TABLEAU,
        ),
        (
            "It routed the fix, then re-verified",
            "The model builder corrected the calc column; the fix was checked "
            "against ground truth and re-rendered. 9 of 9, matching Tableau.",
            GREEN,
        ),
    ]
    y = 320
    for head, body, accent in rows:
        d.rounded_rectangle((MARGIN, y, W - MARGIN, y + 226), radius=18, fill=CARD, outline=LINE, width=2)
        d.rounded_rectangle((MARGIN, y, MARGIN + 12, y + 226), radius=6, fill=accent)
        d.text((MARGIN + 44, y + 34), head, font=font(_B, 36), fill=FG)
        for i, line in enumerate(_wrap(d, body, font(_SANS, 30), W - 2 * MARGIN - 88)):
            d.text((MARGIN + 44, y + 92 + i * 40), line, font=font(_SANS, 30), fill=MUTED)
        y += 250
    d.text((MARGIN, y + 6), "That bug would have shipped silently.", font=font(_SB, 34), fill=FG)
    return img


def slide_close() -> Image.Image:
    """Close: honesty, repo, acknowledgments."""
    img, d = _slide()
    d.text((MARGIN, 110), "AI-assisted.", font=font(_B, 66), fill=FG)
    d.text((MARGIN, 188), "And honest about it.", font=font(_B, 66), fill=PBI)
    for i, line in enumerate(
        _wrap(
            d,
            "Numbers match the source exactly. Models come out Copilot and Q&A ready. Every bug and "
            "capability gap found along the way is documented in the repo, not hidden.",
            font(_SANS, 36),
            W - 2 * MARGIN,
        )
    ):
        d.text((MARGIN, 320 + i * 50), line, font=font(_SANS, 36), fill=MUTED)
    d.rounded_rectangle((MARGIN, 560, W - MARGIN, 690), radius=18, fill=CARD, outline=LINE, width=2)
    d.text((MARGIN + 34, 596), "Open source, 16 before/after examples", font=font(_SB, 34), fill=FG)
    d.text(
        (MARGIN + 34, 640), "github.com/Guust-Franssens/tableau-to-powerbi-migration", font=font(_SB, 30), fill=TABLEAU
    )
    d.text((MARGIN, 760), "Made possible by recent Microsoft work", font=font(_B, 34), fill=FG)
    acks = [
        "Rui Romano and team: the Power BI Modeling MCP, TMDL, and PBIR (authoring a model and report as code).",
        "The Power BI Desktop Bridge team (Emily Lisa, Harleen Kaur, Sara Lammini Rodriguez): the iterative "
        "open / refresh / screenshot loop this toolkit runs on.",
    ]
    y = 812
    for a in acks:
        for line in _wrap(d, a, font(_SANS, 28), W - 2 * MARGIN - 24):
            d.text((MARGIN + 24, y), line, font=font(_SANS, 28), fill=MUTED)
            y += 38
        y += 14
    return img


PROOFS = [
    {
        "title": "The Price of Prosperity",
        "faithful": "Global CO2, GDP, and population. Migrated end to end.",
        "powerbi": "migrations/price-of-prosperity/reference/powerbi-dashboard.png",
        "tableau": "migrations/price-of-prosperity/reference/tableau-dashboard.png",
        "powerbi_crop": None,
        "note": "2018 world population: 7,636,740,830, the same figure as the source.",
    },
    {
        "title": "Health Tracker",
        "faithful": "Nine KPI cards, each with a 7-day trend that highlights the latest day.",
        "powerbi": "migrations/health-tracker/reference/powerbi-health-tracker.png",
        "tableau": "migrations/health-tracker/reference/tableau-metrics.png",
        "powerbi_crop": {"right": 0.03},
        "note": "All nine cards highlight the latest day, every KPI to the same value.",
    },
    {
        "title": "NL Wind Energy Utilization",
        "faithful": "A wind fleet dashboard; Tableau's polar spiral rebuilt as DAX X/Y measures.",
        "powerbi": "migrations/wind-energy-utilization/reference/powerbi-wind-energy.png",
        "tableau": "migrations/wind-energy-utilization/reference/tableau-dashboard.png",
        "powerbi_crop": {"right": 0.025},
        "note": "Total actual output 453,167.284 MWh, CO2 saved 169,031.37 t.",
    },
]


def _compare_slides() -> list[Image.Image]:
    """The examples in compare layout (Power BI + Tableau on one slide each)."""
    return [slide_pair(p["title"], p["faithful"], p, p["note"]) for p in PROOFS]


def _reveal_slides() -> list[Image.Image]:
    """The examples in reveal layout (full Power BI slide, then full Tableau slide)."""
    out: list[Image.Image] = []
    for p in PROOFS:
        out.append(
            slide_single(
                p["title"],
                p["note"],
                REPO_ROOT / p["powerbi"],
                p["powerbi_crop"],
                label="AFTER  \u00b7  Power BI (migrated)",
                accent=PBI,
            )
        )
        out.append(
            slide_single(
                p["title"],
                "the original this was migrated from",
                REPO_ROOT / p["tableau"],
                None,
                label="BEFORE  \u00b7  Tableau (source)",
                accent=TABLEAU,
            )
        )
    return out


def build(output: Path, *, layout: str = "compare", shape: str | None = None, cover: bool = False) -> None:
    """Render the deck for `layout` ('compare' = both panels per slide, 'reveal' = full Power BI slide
    then full Tableau slide) and save a swipeable multi-page PDF + per-slide PNGs. `shape` overrides the
    canvas (square / portrait); by default reveal is square and compare is portrait. The deck opens on
    the first example (the post caption is the intro); pass cover=True to prepend a title slide."""
    _set_canvas(shape or ("square" if layout == "reveal" else "portrait"))
    slides = [slide_cover()] if cover else []
    slides += _reveal_slides() if layout == "reveal" else _compare_slides()
    if H >= 1350:  # portrait has the vertical room for the context slides
        slides += [slide_pipeline(), slide_money()]
    slides.append(slide_close())
    output.parent.mkdir(parents=True, exist_ok=True)
    png_dir = output.parent / f"slides-{layout}"
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
    """CLI entry point. By default builds BOTH layout variants so they can be compared side by side."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", type=Path, default=REPO_ROOT / "docs" / "showcase" / "carousel", help="output directory"
    )
    parser.add_argument(
        "--layout",
        choices=["compare", "reveal", "both"],
        default="both",
        help="compare (both panels per slide), reveal (full Power BI then full Tableau), or both",
    )
    parser.add_argument(
        "--shape",
        choices=["auto", "square", "portrait"],
        default="auto",
        help="canvas shape (default auto: reveal=square, compare=portrait)",
    )
    parser.add_argument(
        "--cover", action="store_true", help="prepend a title slide (default: open on the first example)"
    )
    args = parser.parse_args()
    shape = None if args.shape == "auto" else args.shape
    layouts = ["compare", "reveal"] if args.layout == "both" else [args.layout]
    for layout in layouts:
        build(args.out_dir / f"linkedin-carousel-{layout}.pdf", layout=layout, shape=shape, cover=args.cover)


if __name__ == "__main__":
    main()
