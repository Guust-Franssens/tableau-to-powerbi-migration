"""
purpose: Render a short, captioned, silent 1080p MP4 that shows HOW the Tableau -> Power BI
         migration agents reason (an animated 4-agent pipeline + a terminal-style replay of the
         fidelity validator catching a real bug and the model builder self-correcting) and THAT the
         migration is faithful (before/after reveals with exact, repo-verified numbers). Frames are
         drawn with Pillow and piped as rawvideo to ffmpeg for H.264 encoding. Content is authentic:
         the reasoning transcript and every on-screen number trace to this repo's ground-truth files
         and committed agent output.
usage:   python scripts/make_demo_video.py [--output docs/showcase/demo/agents-reasoning.mp4]
                                            [--fps 30] [--quick]
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("make_demo_video")

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "docs" / "showcase" / "assets"

WIDTH, HEIGHT = 1920, 1080

# Palette - matches the dark showcase theme.
BG = (11, 22, 36)
PANEL = (17, 29, 45)
PANEL_HI = (24, 40, 60)
FG = (232, 237, 243)
MUTED = (150, 165, 182)
TABLEAU = (98, 150, 200)
PBI = (232, 130, 78)
GREEN = (86, 204, 140)
AMBER = (242, 194, 96)
BLUE = (104, 170, 240)
LINE = (46, 64, 86)
BLACK = (0, 0, 0)

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_SANS = ["segoeui.ttf"]
_SANS_SB = ["seguisb.ttf", "segoeuib.ttf"]
_SANS_B = ["segoeuib.ttf", "seguisb.ttf"]
_MONO = ["consola.ttf"]
_MONO_B = ["consolab.ttf", "consola.ttf"]


def font(kind: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Load (and cache) the first available TrueType font in `kind` at `size`."""
    key = (kind[0], size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for name in kind:
        try:
            f = ImageFont.truetype(name, size)
            _FONT_CACHE[key] = f
            return f
        except OSError:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def smooth(t: float) -> float:
    """Smoothstep easing on [0,1]."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, **kw) -> None:
    """Rounded rectangle wrapper (keeps call sites terse)."""
    draw.rounded_rectangle(box, radius=radius, **kw)


def new_frame(color: tuple[int, int, int] = BG) -> Image.Image:
    """A fresh full-size canvas."""
    return Image.new("RGB", (WIDTH, HEIGHT), color)


def draw_check(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]) -> None:
    """Draw a filled circle with a white checkmark - a self-drawn glyph (no font dependency)."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    w = max(3, r // 4)
    draw.line(
        [(cx - r * 0.45, cy), (cx - r * 0.05, cy + r * 0.42), (cx + r * 0.5, cy - r * 0.4)],
        fill=FG,
        width=w,
        joint="curve",
    )


class VideoWriter:
    """Pipe rawvideo RGB frames to ffmpeg. Remembers the last frame so scene changes can crossfade."""

    def __init__(self, path: Path, fps: int) -> None:
        self.fps = fps
        self.last: Image.Image | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{WIDTH}x{HEIGHT}",
            "-framerate",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)  # noqa: S603  # pylint: disable=consider-using-with

    def _norm(self, img: Image.Image) -> Image.Image:
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (WIDTH, HEIGHT):
            img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        return img

    def _raw(self, img: Image.Image) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(img.tobytes())

    def emit(self, img: Image.Image) -> None:
        """Write one frame."""
        img = self._norm(img)
        self._raw(img)
        self.last = img

    def hold(self, img: Image.Image, seconds: float) -> None:
        """Write a still frame for `seconds` (bytes reused - cheap)."""
        img = self._norm(img)
        for _ in range(max(1, round(seconds * self.fps))):
            self._raw(img)
        self.last = img

    def transition(self, img: Image.Image, seconds: float) -> None:
        """Crossfade from the last frame (or black) to `img`."""
        img = self._norm(img)
        base = self.last if self.last is not None else new_frame(BLACK)
        n = max(1, round(seconds * self.fps))
        for i in range(n):
            self._raw(Image.blend(base, img, smooth((i + 1) / n)))
        self.last = img

    def fade_out(self, seconds: float) -> None:
        """Fade the last frame to black."""
        if self.last is None:
            return
        black = new_frame(BLACK)
        n = max(1, round(seconds * self.fps))
        for i in range(n):
            self._raw(Image.blend(self.last, black, smooth((i + 1) / n)))
        self.last = black

    def close(self) -> None:
        """Flush and finish encoding."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait()


def _footer(draw: ImageDraw.ImageDraw) -> None:
    """Consistent bottom-left repo tag on content scenes."""
    draw.text(
        (64, HEIGHT - 52), "github.com/Guust-Franssens/tableau-to-powerbi-migration", font=font(_SANS, 24), fill=MUTED
    )


# --------------------------------------------------------------------------------------------------
# Scene 1 - title
# --------------------------------------------------------------------------------------------------
def scene_title(vw: VideoWriter) -> None:
    """Opening card."""
    img = new_frame()
    d = ImageDraw.Draw(img)
    d.text((WIDTH // 2, 366), "Tableau  \u2192  Power BI", font=font(_SANS_B, 108), fill=FG, anchor="mm")
    d.text(
        (WIDTH // 2, 468),
        "migrated by AI agents \u2014 that catch their own mistakes",
        font=font(_SANS, 46),
        fill=TABLEAU,
        anchor="mm",
    )
    d.line((WIDTH // 2 - 120, 536, WIDTH // 2 + 120, 536), fill=PBI, width=4)
    d.text(
        (WIDTH // 2, 636),
        "A deterministic parser feeds four Copilot CLI agents:",
        font=font(_SANS, 34),
        fill=MUTED,
        anchor="mm",
    )
    d.text(
        (WIDTH // 2, 686),
        "semantic-model builder  \u00b7  report builder  \u00b7  independent fidelity validator",
        font=font(_SANS_SB, 34),
        fill=FG,
        anchor="mm",
    )
    _footer(d)
    vw.transition(img, 0.7)
    vw.hold(img, 3.0)


# --------------------------------------------------------------------------------------------------
# Scene 2 - the agent pipeline (animated)
# --------------------------------------------------------------------------------------------------
_STAGES = [
    ("Deterministic\nParser", ".twb XML \u2192 migration-spec.json", TABLEAU),
    ("Semantic-Model\nBuilder", "tables, relationships, DAX", GREEN),
    ("Report\nBuilder", "PBIR pages & visuals", PBI),
    ("Fidelity\nValidator", "read-only, checks every figure", AMBER),
]


def _pipeline_base() -> tuple[Image.Image, list[tuple[int, int, int, int]]]:
    """Static pipeline background; returns the image and each stage box rect."""
    img = new_frame()
    d = ImageDraw.Draw(img)
    d.text((WIDTH // 2, 150), "How the agents reason", font=font(_SANS_B, 62), fill=FG, anchor="mm")
    d.text(
        (WIDTH // 2, 220),
        "each agent owns one layer; the validator is independent and read-only",
        font=font(_SANS, 34),
        fill=MUTED,
        anchor="mm",
    )
    boxes: list[tuple[int, int, int, int]] = []
    bw, bh, gap = 380, 220, 76
    total = bw * 4 + gap * 3
    x = (WIDTH - total) // 2
    y = 430
    for i, (name, sub, color) in enumerate(_STAGES):
        box = (x, y, x + bw, y + bh)
        boxes.append(box)
        rounded(d, box, 20, fill=PANEL, outline=LINE, width=2)
        d.rounded_rectangle((x, y, x + 10, y + bh), radius=6, fill=color)
        d.multiline_text(
            (x + bw // 2 + 5, y + 78), name, font=font(_SANS_SB, 38), fill=FG, anchor="mm", align="center", spacing=6
        )
        d.text((x + bw // 2 + 5, y + 158), sub, font=font(_SANS, 24), fill=MUTED, anchor="mm")
        if i < 3:
            ax = x + bw
            d.line((ax + 12, y + bh // 2, ax + gap - 12, y + bh // 2), fill=MUTED, width=3)
            d.polygon(
                [(ax + gap - 12, y + bh // 2 - 8), (ax + gap - 12, y + bh // 2 + 8), (ax + gap, y + bh // 2)],
                fill=MUTED,
            )
        x += bw + gap
    return img, boxes


def scene_pipeline(vw: VideoWriter) -> None:  # pylint: disable=too-many-locals
    """Highlight each stage in turn, then pulse the validator's feedback arrow."""
    base, boxes = _pipeline_base()
    vy = boxes[0][1] + 220 // 2
    feedback_y = boxes[0][3] + 96
    total_sec = 9.5
    n = round(total_sec * vw.fps)
    vw.transition(base.copy(), 0.5)
    for fidx in range(n):
        t = fidx / n
        img = base.copy()
        d = ImageDraw.Draw(img)
        # active stage sweeps across during the first 62% of the scene
        active = min(3, int(t / 0.62 * 4)) if t < 0.62 else 3
        x0, y0, x1, y1 = boxes[active]
        color = _STAGES[active][2]
        rounded(d, boxes[active], 20, fill=PANEL_HI, outline=color, width=4)
        d.rounded_rectangle((x0, y0, x0 + 10, y1), radius=6, fill=color)
        d.multiline_text(
            ((x0 + x1) // 2 + 5, y0 + 78),
            _STAGES[active][0],
            font=font(_SANS_SB, 38),
            fill=FG,
            anchor="mm",
            align="center",
            spacing=6,
        )
        d.text(((x0 + x1) // 2 + 5, y0 + 158), _STAGES[active][1], font=font(_SANS, 24), fill=MUTED, anchor="mm")
        # travelling data token along the top arrows
        if t < 0.62:
            frac = (t / 0.62) * (boxes[3][2] - boxes[0][0] - 40)
            tx = boxes[0][0] + 20 + frac
            d.ellipse((tx - 7, vy - 7, tx + 7, vy + 7), fill=FG)
        # feedback loop from validator back to model builder
        if t >= 0.5:
            pulse = smooth(min(1.0, (t - 0.5) / 0.3))
            fb_color = tuple(int(a + (b - a) * pulse) for a, b in zip(LINE, AMBER))
            vx0 = (boxes[1][0] + boxes[1][2]) // 2
            vx1 = (boxes[3][0] + boxes[3][2]) // 2
            d.line((vx1, boxes[3][3], vx1, feedback_y), fill=fb_color, width=3)
            d.line((vx1, feedback_y, vx0, feedback_y), fill=fb_color, width=3)
            d.line((vx0, feedback_y, vx0, boxes[1][3]), fill=fb_color, width=3)
            d.polygon([(vx0 - 8, boxes[1][3] + 14), (vx0 + 8, boxes[1][3] + 14), (vx0, boxes[1][3] + 2)], fill=fb_color)
            if pulse > 0.4:
                d.text(
                    ((vx0 + vx1) // 2, feedback_y + 26),
                    "catches discrepancies  \u2192  routes the fix back",
                    font=font(_SANS_SB, 28),
                    fill=AMBER,
                    anchor="mm",
                )
        _footer(d)
        vw.emit(img)
    vw.hold(img, 0.8)


# --------------------------------------------------------------------------------------------------
# Scene 3 - the reasoning transcript (centerpiece). Authentic validator -> fix -> verify loop.
# --------------------------------------------------------------------------------------------------
Span = tuple[str, tuple[int, int, int]]
SCRIPT: list[list[Span]] = [
    [("$ ", MUTED), ("copilot", FG), ('  "migrate the health-tracker Tableau workbook"', MUTED)],
    [("", FG)],
    [("[pbi-migration-validator]", AMBER), ("  independent, read-only fidelity check", MUTED)],
    [("  comparing the built report against the Tableau source, figure by figure...", FG)],
    [('  page "Last Week": 9 KPI cards, each a 7-day trend bar chart', FG)],
    [("", FG)],
    [("  WARN  ", AMBER), ("discrepancy - Tableau highlights the latest day (Dec 31) in every card;", FG)],
    [("        the Power BI render highlights ", FG), ("0 of 9", AMBER), (".", FG)],
    [("", FG)],
    [("  tracing the highlight logic -> the ", FG), ("Latest Week", BLUE), (" date window...", FG)],
    [("  Latest Week = last complete Sun-Sat week   ->  Dec 24-30", FG)],
    [("  but MAX(Date) = ", FG), ("Dec 31", AMBER), ("   ->  the highlighted day is filtered OUT", FG)],
    [("  root cause: ", FG), ("off-by-one", AMBER), (" - the week window excludes the max date", FG)],
    [("", FG)],
    [("  ->  routing to  ", BLUE), ("[pbi-semantic-builder]", GREEN), ("   (the model layer owns this)", MUTED)],
    [("", FG)],
    [
        ("[pbi-semantic-builder]", GREEN),
        ("  editing calc column ", MUTED),
        ("Latest Week", BLUE),
        ("  in Health Data.tmdl", MUTED),
    ],
    [("  before:  last-complete-week      ->  Dec 24-30   ", FG), ("(excludes Dec 31)", AMBER)],
    [("  after :  trailing 7 days <= MAX  ->  Dec 25-31   ", FG), ("(includes Dec 31)", GREEN)],
    [("  verified offline (TOM) + data ground-truth -> highlighted day = Dec 31   ", FG), ("OK", GREEN)],
    [("", FG)],
    [
        ("[pbi-report-builder]", PBI),
        ("  re-render  ->  ", MUTED),
        ("9 / 9", GREEN),
        (" cards now highlight Dec 31", FG),
    ],
    [("  ", FG), ("OK  matches Tableau  \u00b7  numbers exact  \u00b7  committed", GREEN)],
]

TERM_X, TERM_Y = 150, 250
TERM_W, TERM_H = WIDTH - 300, 720
LINE_H = 32
VISIBLE_ROWS = (TERM_H - 40) // LINE_H
BLANK_DELAY = 6  # "characters" of pause emitted at each blank line


def _line_len(line: list[Span]) -> int:
    total = sum(len(txt) for txt, _ in line)
    return total if total else BLANK_DELAY


def _terminal_base() -> Image.Image:
    """Draw the terminal window chrome once."""
    img = new_frame()
    d = ImageDraw.Draw(img)
    d.text(
        (WIDTH // 2, 130),
        "Watch the validator catch a bug \u2014 and the model fix it",
        font=font(_SANS_B, 52),
        fill=FG,
        anchor="mm",
    )
    rounded(d, (TERM_X, TERM_Y, TERM_X + TERM_W, TERM_Y + TERM_H), 16, fill=(9, 16, 26), outline=LINE, width=2)
    title_bar = (TERM_X, TERM_Y, TERM_X + TERM_W, TERM_Y + 44)
    d.rounded_rectangle(title_bar, radius=16, fill=PANEL)
    d.rectangle((TERM_X, TERM_Y + 22, TERM_X + TERM_W, TERM_Y + 44), fill=PANEL)
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse((TERM_X + 20 + i * 26, TERM_Y + 15, TERM_X + 34 + i * 26, TERM_Y + 29), fill=c)
    d.text(
        (TERM_X + TERM_W // 2, TERM_Y + 22),
        "Copilot CLI  \u00b7  tableau-migrator",
        font=font(_MONO, 22),
        fill=MUTED,
        anchor="mm",
    )
    _footer(d)
    return img


def _render_terminal(base: Image.Image, nchars: int, blink: bool) -> Image.Image:  # pylint: disable=too-many-locals
    """Render the transcript revealed up to `nchars`, with autoscroll + block cursor."""
    # locate the cursor line for autoscroll
    budget = nchars
    cursor_line = 0
    for idx, line in enumerate(SCRIPT):
        ll = _line_len(line)
        if budget < ll:
            cursor_line = idx
            break
        budget -= ll
        cursor_line = idx
    top = max(0, cursor_line - (VISIBLE_ROWS - 2))

    img = base.copy()
    d = ImageDraw.Draw(img)
    mono = font(_MONO, 25)
    mono_b = font(_MONO_B, 25)
    char_w = d.textlength("M", font=mono)
    budget = nchars
    text_top = TERM_Y + 60
    for idx, line in enumerate(SCRIPT):
        ll = _line_len(line)
        drawn_full = budget >= ll
        row = idx - top
        if 0 <= row < VISIBLE_ROWS and idx <= cursor_line:
            y = text_top + row * LINE_H
            x = TERM_X + 26
            remaining = ll if drawn_full else budget
            for txt, color in line:
                if remaining <= 0 or not txt:
                    break
                shown = txt[:remaining]
                use = mono_b if color in (AMBER, GREEN, PBI, BLUE) else mono
                d.text((x, y), shown, font=use, fill=color)
                x += d.textlength(shown, font=use)
                remaining -= len(shown)
            if idx == cursor_line and blink:
                d.rectangle((x + 2, y + 4, x + 2 + char_w, y + LINE_H - 8), fill=FG)
        budget -= ll
        if budget < 0:
            break
    return img


def scene_terminal(vw: VideoWriter) -> None:
    """Type the authentic reasoning loop, then hold."""
    base = _terminal_base()
    vw.transition(_render_terminal(base, 0, True), 0.5)
    total_chars = sum(_line_len(line) for line in SCRIPT)
    cps = 45.0
    reveal_frames = round(total_chars / cps * vw.fps)
    for fidx in range(reveal_frames):
        nchars = int(total_chars * (fidx + 1) / reveal_frames)
        blink = (fidx // 14) % 2 == 0
        vw.emit(_render_terminal(base, nchars, blink))
    # hold full transcript with a couple of cursor blinks
    for fidx in range(round(3.2 * vw.fps)):
        blink = (fidx // 14) % 2 == 0
        vw.emit(_render_terminal(base, total_chars, blink))


# --------------------------------------------------------------------------------------------------
# Scene 4 - the proof (before/after reveals with exact numbers)
# --------------------------------------------------------------------------------------------------
_PROOF = [
    (
        "price-of-prosperity-1.png",
        "The Price of Prosperity",
        "Migrated end to end by the orchestrator, signed off by the validator",
        ["Global CO2 / GDP / population", "2018 world population", "7,636,740,830  \u2192  matches exactly"],
    ),
    (
        "health-tracker-1.png",
        "Health Tracker",
        "The bug from the transcript \u2014 fixed and re-verified",
        ["9 of 9 trend cards", "now highlight the latest day", "every KPI numerically exact"],
    ),
    (
        "wind-energy-utilization-1.png",
        "NL Wind Energy Utilization",
        "Star schema; Tableau's polar spiral rebuilt as DAX X/Y measures",
        ["Total actual output", "453,167.284 MWh", "CO2 saved  169,031.37 t  \u2713"],
    ),
]


def _proof_base(asset: str, title: str, sub: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Compose the dark scene with the before/after asset and headline; return image + callout anchor."""
    img = new_frame()
    d = ImageDraw.Draw(img)
    d.text((80, 96), title, font=font(_SANS_B, 56), fill=FG)
    d.text((82, 168), sub, font=font(_SANS, 32), fill=MUTED)
    comp = Image.open(ASSETS / asset).convert("RGB")
    target_w = WIDTH - 200
    scale = target_w / comp.width
    target_h = int(comp.height * scale)
    max_h = HEIGHT - 300
    if target_h > max_h:
        scale = max_h / comp.height
        target_w = int(comp.width * scale)
        target_h = max_h
    comp = comp.resize((target_w, target_h), Image.Resampling.LANCZOS)
    px = (WIDTH - target_w) // 2
    py = 226
    d.rounded_rectangle((px - 6, py - 6, px + target_w + 6, py + target_h + 6), radius=10, outline=LINE, width=2)
    img.paste(comp, (px, py))
    _footer(d)
    return img, (px, py, target_w, target_h)


def _callout_overlay(lines: list[str], anchor: tuple[int, int, int, int]) -> Image.Image:
    """Transparent RGBA callout chip (green check + number) anchored bottom-right of the asset."""
    ov = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    px, py, aw, ah = anchor
    cw, ch = 560, 176
    cx = px + aw - cw - 24
    cy = py + ah - ch - 24
    d.rounded_rectangle(
        (cx, cy, cx + cw, cy + ch), radius=16, fill=(9, 16, 26, 235), outline=(86, 204, 140, 255), width=3
    )
    draw_check(d, cx + 52, cy + ch // 2, 30, GREEN)
    tx = cx + 100
    d.text((tx, cy + 34), lines[0], font=font(_SANS, 28), fill=MUTED)
    d.text((tx, cy + 72), lines[1], font=font(_SANS_B, 40), fill=FG)
    d.text((tx, cy + 126), lines[2], font=font(_SANS_SB, 26), fill=GREEN)
    return ov


def _ken_burns(base: Image.Image, s: float) -> Image.Image:
    """Center zoom by factor s (>=1)."""
    if s <= 1.001:
        return base
    cw, chh = int(WIDTH / s), int(HEIGHT / s)
    left = (WIDTH - cw) // 2
    top = (HEIGHT - chh) // 2
    return base.crop((left, top, left + cw, top + chh)).resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def scene_proof(vw: VideoWriter) -> None:
    """Three before/after reveals; each fades in a numeric callout then slowly zooms."""
    per = 6.4
    for asset, title, sub, lines in _PROOF:
        base, anchor = _proof_base(asset, title, sub)
        overlay = _callout_overlay(lines, anchor)
        vw.transition(base.copy(), 0.55)
        n = round(per * vw.fps)
        for fidx in range(n):
            t = (fidx + 1) / n
            frame = base.copy()
            alpha = smooth(min(1.0, t / 0.22))
            if alpha > 0:
                ov = overlay.copy()
                ov.putalpha(ov.getchannel("A").point(lambda a, k=alpha: int(a * k)))
                frame = Image.alpha_composite(frame.convert("RGBA"), ov).convert("RGB")
            frame = _ken_burns(frame, 1.0 + 0.035 * smooth(t))
            vw.emit(frame)


# --------------------------------------------------------------------------------------------------
# Scene 5 - payoff
# --------------------------------------------------------------------------------------------------
def scene_payoff(vw: VideoWriter) -> None:
    """Closing stats + call to action."""
    img = new_frame()
    d = ImageDraw.Draw(img)
    d.text((WIDTH // 2, 250), "AI-assisted. And honest about it.", font=font(_SANS_B, 76), fill=FG, anchor="mm")
    stats = [
        ("16", "real Tableau Public\ndashboards migrated", TABLEAU),
        ("4", "reasoning agents,\none independent validator", GREEN),
        ("100%", "models made\nCopilot / Q&A ready", PBI),
    ]
    cw = 460
    total = cw * 3 + 80 * 2
    x = (WIDTH - total) // 2
    y = 420
    for value, label, color in stats:
        rounded(d, (x, y, x + cw, y + 250), 20, fill=PANEL, outline=LINE, width=2)
        d.text((x + cw // 2, y + 88), value, font=font(_SANS_B, 92), fill=color, anchor="mm")
        d.multiline_text(
            (x + cw // 2, y + 186), label, font=font(_SANS, 30), fill=MUTED, anchor="mm", align="center", spacing=6
        )
        x += cw + 80
    d.text(
        (WIDTH // 2, 772),
        "Every bug found along the way is documented, not hidden.",
        font=font(_SANS, 34),
        fill=FG,
        anchor="mm",
    )
    d.text(
        (WIDTH // 2, 848),
        "github.com/Guust-Franssens/tableau-to-powerbi-migration",
        font=font(_SANS_SB, 36),
        fill=TABLEAU,
        anchor="mm",
    )
    vw.transition(img, 0.6)
    vw.hold(img, 4.2)
    vw.fade_out(0.8)


def build(output: Path, fps: int) -> None:
    """Render every scene into the MP4 at `output`."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    vw = VideoWriter(output, fps)
    for scene in (scene_title, scene_pipeline, scene_terminal, scene_proof, scene_payoff):
        logger.info("rendering %s", scene.__name__)
        scene(vw)
    vw.close()
    try:
        logger.info("wrote %s", output.relative_to(REPO_ROOT))
    except ValueError:
        logger.info("wrote %s", output)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "docs" / "showcase" / "demo" / "agents-reasoning.mp4"
    )
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--quick", action="store_true", help="render at 15 fps for a fast preview")
    args = parser.parse_args()
    build(args.output, 15 if args.quick else args.fps)


if __name__ == "__main__":
    main()
