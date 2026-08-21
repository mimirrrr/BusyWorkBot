"""Phase 5: matplotlib charts for the end-of-season report.

Each render_* function takes (parts of) src/report_stats.py's
season_summary_stats() blob and returns a base64-encoded PNG string, for
direct <img src="data:image/png;base64,..."> embedding in report_html.py --
no temp files, no separate image assets, keeps the report a single file.

Agg backend must be selected before importing pyplot -- required for
headless rendering in GitHub Actions (no display server).
"""

import base64
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

VERDICT_COLORS = {
    "slammed": "#E74C3C", "busy": "#F1C40F", "normal": "#95A5A6",
    "slow": "#3498DB", "dead": "#2C3E50",
}


def _fig_to_base64(fig) -> str:
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def render_accuracy_bar(stats: dict) -> str:
    """Overall vs. live-only vs. backfill-only accuracy, side by side."""
    by_backfill = stats["by_backfill"]
    labels = ["Celkem", "Live", "Backfill"]
    groups = [stats["overall"], by_backfill.get("False", {"accuracy": None}),
              by_backfill.get("True", {"accuracy": None})]
    values = [g["accuracy"] * 100 if g["accuracy"] is not None else 0 for g in groups]
    ns = [g["n"] for g in groups]

    fig, ax = plt.subplots(figsize=(5, 4))
    bars = ax.bar(labels, values, color=["#3498DB", "#2ECC71", "#F1C40F"])
    for bar, n, v in zip(bars, ns, values):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 2, f"{v:.0f}% (n={n})",
                ha="center", fontsize=9)
    ax.set_ylabel("Přesnost predikce (%)")
    ax.set_ylim(0, 110)
    ax.set_title("Přesnost predikce podle zdroje dat")
    fig.tight_layout()
    return _fig_to_base64(fig)


def render_confusion_matrix(stats: dict) -> str:
    matrix = stats["confusion_matrix"]["matrix"]
    labels = stats["confusion_matrix"]["labels"]

    fig, ax = plt.subplots(figsize=(5, 5))
    im = ax.imshow(matrix, cmap="Blues")
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticklabels(labels)
    ax.set_xlabel("Predikce")
    ax.set_ylabel("Realita")
    ax.set_title("Konfúzní matice (realita × predikce)")
    vmax = max((max(row) for row in matrix), default=0) or 1
    for i, row in enumerate(matrix):
        for j, count in enumerate(row):
            ax.text(j, i, str(count), ha="center", va="center",
                     color="white" if count > vmax / 2 else "black")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    fig.tight_layout()
    return _fig_to_base64(fig)


def render_scatter(points: list[dict], title: str, xlabel: str, ylabel: str) -> str:
    fig, ax = plt.subplots(figsize=(5, 4))
    for is_backfill, marker, edge_label in ((False, "o", "live"), (True, "^", "backfill")):
        subset = [p for p in points if p.get("is_backfill") == is_backfill]
        if not subset:
            continue
        ax.scatter(
            [p["x"] for p in subset], [p["y"] for p in subset],
            c=[VERDICT_COLORS.get(p["verdict"], "#7F8C8D") for p in subset],
            marker=marker, edgecolors="black", linewidths=0.5, s=60,
            label=edge_label,
        )
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(title="zdroj", loc="best")
    fig.tight_layout()
    return _fig_to_base64(fig)


def render_live_vs_backfill(stats: dict) -> str:
    """Sample-size-vs-accuracy view -- makes the small-N caveat visible
    rather than just stated in text.
    """
    by_backfill = stats["by_backfill"]
    live = by_backfill.get("False", {"n": 0, "accuracy": None})
    backfill = by_backfill.get("True", {"n": 0, "accuracy": None})

    fig, ax = plt.subplots(figsize=(5, 4))
    for label, group, color in (("Live", live, "#2ECC71"), ("Backfill", backfill, "#F1C40F")):
        if group["n"] == 0:
            continue
        ax.scatter(group["n"], (group["accuracy"] or 0) * 100, s=200, color=color, label=label)
    ax.set_xlabel("Počet dní (n)")
    ax.set_ylabel("Přesnost predikce (%)")
    ax.set_ylim(0, 110)
    ax.set_title("Live vs. backfill: velikost vzorku vs. přesnost")
    ax.legend()
    fig.tight_layout()
    return _fig_to_base64(fig)
