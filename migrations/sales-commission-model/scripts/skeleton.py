"""Layout skeleton + space audit for the Sales Commission Model report.

Self-contained (does not import the shared repo-root helpers) so this migration
folder stays independent of concurrent builds. Renders a 1280x720 wireframe that
mirrors the Tableau dashboard zone tree, and reports any overlapping regions.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

PAGE_W, PAGE_H = 1280, 720

# region: (name, x, y, w, h, group)
REGIONS = [
    ("title",        8,    6, 700, 36, "text"),
    ("subtitle",     8,   44, 950, 22, "text"),
    ("slicerQuota",  8,   74, 414, 48, "slicer"),
    ("slicerRate",  430,  74, 414, 72, "slicer"),
    ("slicerBase",  852,  74, 420, 48, "slicer"),
    ("sectionTitle", 8,  148, 820, 28, "text"),
    ("salesChart",   8,  180, 588, 502, "chart"),
    ("compChart",   600, 180, 524, 502, "chart"),
    ("sortBy",     1132, 182, 140, 160, "slicer"),
    ("qaLegend",   1132, 350, 140, 130, "legend"),
    ("compLegend", 1132, 490, 140,  90, "legend"),
]

COLORS = {
    "text":   "#d9e6f2",
    "slicer": "#fce8d5",
    "chart":  "#e2f0dc",
    "legend": "#efe2f0",
}


def audit():
    """Return list of overlapping (nameA, nameB) pairs and out-of-bounds regions."""
    overlaps = []
    for i in range(len(REGIONS)):
        for j in range(i + 1, len(REGIONS)):
            a = REGIONS[i]; b = REGIONS[j]
            ax, ay, aw, ah = a[1], a[2], a[3], a[4]
            bx, by, bw, bh = b[1], b[2], b[3], b[4]
            if ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by:
                overlaps.append((a[0], b[0]))
    oob = []
    for r in REGIONS:
        if r[1] < 0 or r[2] < 0 or r[1] + r[3] > PAGE_W or r[2] + r[4] > PAGE_H:
            oob.append(r[0])
    return overlaps, oob


def render(path):
    fig, ax = plt.subplots(figsize=(PAGE_W / 100, PAGE_H / 100), dpi=100)
    ax.set_xlim(0, PAGE_W); ax.set_ylim(PAGE_H, 0)  # invert y (top-left origin)
    ax.set_xticks([]); ax.set_yticks([])
    ax.add_patch(patches.Rectangle((0, 0), PAGE_W, PAGE_H, fill=True,
                                   facecolor="white", edgecolor="#888"))
    for name, x, y, w, h, group in REGIONS:
        ax.add_patch(patches.Rectangle((x, y), w, h, fill=True,
                                       facecolor=COLORS[group], edgecolor="#4a4a4a",
                                       linewidth=1.2))
        ax.text(x + w / 2, y + h / 2, name, ha="center", va="center",
                fontsize=8, color="#333")
    plt.tight_layout(pad=0)
    fig.savefig(path, dpi=100, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


if __name__ == "__main__":
    overlaps, oob = audit()
    print("SPACE AUDIT")
    print("  overlaps:", overlaps if overlaps else "NONE (clean)")
    print("  out-of-bounds:", oob if oob else "NONE (clean)")
    out = __file__.replace("scripts", "reference").replace("skeleton.py", "skeleton.png")
    render(out)
    print("  rendered:", out)
