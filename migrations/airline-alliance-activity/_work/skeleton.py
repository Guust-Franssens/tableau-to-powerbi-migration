"""Render a PBIR page as a labeled skeleton wireframe for gestalt comparison
against the Tableau reference screenshot. Reads real visual.json positions.
Usage: python skeleton.py <pageIndex 0-based> <outfile.png>
Canvas is 1400x950 (build.js); scaled to 1600x1000 to match reference PNGs.
"""
import os, sys, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

REPORT = r"C:\Users\gfranssens\vscode-projects\tableau-to-pbi-migration\migrations\airline-alliance-activity\fabric\AirlineAllianceActivity.Report\definition\pages"
CW, CH = 1400, 950          # build.js canvas
OW, OH = 1600, 1000         # output (match reference)

COLOR = {
    "textbox": ("#FFFFFF", "#8899AA"),
    "cardVisual": ("#FFF3E6", "#C77700"),
    "columnChart": ("#E6F0FF", "#1F5FA8"),
    "barChart": ("#E6FFF0", "#1F8A5F"),
    "clusteredColumnChart": ("#E6F0FF", "#1F5FA8"),
    "clusteredBarChart": ("#E6FFF0", "#1F8A5F"),
    "pivotTable": ("#F1E6FF", "#6A3FA0"),
    "tableEx": ("#F1E6FF", "#6A3FA0"),
    "donutChart": ("#FFE6EC", "#B3305C"),
    "slicer": ("#FFFFFF", "#34657F"),
    "advancedSlicerVisual": ("#FFFFFF", "#34657F"),
    "azureMap": ("#EAF2F0", "#2E7D5B"),
    "image": ("#EDEDED", "#777777"),
    "pageNavigator": ("#8FA9B3", "#1F2E35"),
    "actionButton": ("#8FA9B3", "#1F2E35"),
}

def scale(v, dim_in, dim_out):
    return v / dim_in * dim_out

def label_for(j):
    t = j["visual"]["visualType"]
    txt = ""
    try:
        runs = j["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
        txt = "".join(r.get("value", "") for r in runs)[:26]
    except Exception:
        pass
    if not txt:
        # try measure/column display name from query projections
        try:
            proj = j["visual"]["query"]["queryState"]
            for role in proj.values():
                for p in role.get("projections", []):
                    txt = p.get("displayName") or p.get("queryRef", "")
                    if txt:
                        break
                if txt:
                    break
        except Exception:
            pass
    return t, txt[:26]

def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    out = sys.argv[2] if len(sys.argv) > 2 else "skeleton.png"
    pages = sorted(os.listdir(REPORT))
    pages = [p for p in pages if os.path.isdir(os.path.join(REPORT, p))]
    pg = pages[idx]
    vdir = os.path.join(REPORT, pg, "visuals")
    vis = []
    for n in os.listdir(vdir):
        with open(os.path.join(vdir, n, "visual.json"), encoding="utf-8") as f:
            j = json.load(f)
        vis.append(j)
    vis.sort(key=lambda j: j["position"].get("z", 0))

    fig, ax = plt.subplots(figsize=(16, 10), dpi=100)
    ax.set_xlim(0, OW); ax.set_ylim(0, OH); ax.invert_yaxis()
    ax.set_facecolor("#F7F7FA")
    ax.set_title(f"Skeleton wireframe (gestalt only) — page {idx} '{pg}' — {len(vis)} visuals", fontsize=11)

    for j in vis:
        p = j["position"]
        x = scale(p["x"], CW, OW); y = scale(p["y"], CH, OH)
        w = scale(p["width"], CW, OW); h = scale(p["height"], CH, OH)
        t, txt = label_for(j)
        fill, edge = COLOR.get(t, ("#EEEEEE", "#999999"))
        z = p.get("z", 0)
        alpha = 0.35 if z < 4000 else 0.92     # backgrounds faded
        rect = patches.Rectangle((x, y), w, h, facecolor=fill, edgecolor=edge,
                                 linewidth=1.0, alpha=alpha)
        ax.add_patch(rect)
        if z >= 4000 and w > 30 and h > 14:
            short = t.replace("Chart", "").replace("Visual", "")
            lbl = f"{short}\n{txt}" if txt else short
            ax.text(x + w/2, y + h/2, lbl, fontsize=5.2, ha="center", va="center", color="#222")

    plt.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print("saved", out)

if __name__ == "__main__":
    main()
