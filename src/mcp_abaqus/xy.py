"""XY curve figures from an ODB.

Points are extracted by ``abaqus python`` (``odb_extract.py``, mode ``xy``) and the
figure is drawn here with matplotlib, because Abaqus 2018's XYPlot API exposes no
writable axis title and cannot be styled for publication.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # no display in an MCP server process

import matplotlib.pyplot as plt  # noqa: E402
from pathlib import Path  # noqa: E402

from .config import Settings, ToolError  # noqa: E402
from .odb import run_odb  # noqa: E402

_KINDS = {
    "time",
    "frame",
    "node",
    "ratio_min",
    "field_max",
    "field_min",
    "rf_pos_sum",
    "constant",
    "vline",
}


def _check_spec(spec: dict, where: str) -> None:
    if not isinstance(spec, dict):
        raise ToolError(f"{where} must be a dict, got {type(spec).__name__}")
    kind = str(spec.get("kind") or "").lower()
    if kind not in _KINDS:
        raise ToolError(f"{where}.kind={spec.get('kind')!r} not in {sorted(_KINDS)}")
    if kind in {"node", "ratio_min"} and "node" not in spec:
        raise ToolError(f"{where}.node is required for kind={kind}")


def plot_series(
    data: dict,
    out_path: Path,
    *,
    xlabel: str = "",
    ylabel: str = "",
    title: str = "",
    image_size: tuple[int, int] = (1400, 1000),
    dpi: int = 200,
) -> dict:
    series = data.get("series") or []
    real = [s for s in series if "x" in s]
    if not real:
        raise ToolError(f"no plottable series: {data.get('warnings')}")

    width_in, height_in = image_size[0] / float(dpi), image_size[1] / float(dpi)
    fig, ax = plt.subplots(figsize=(max(width_in, 3.0), max(height_in, 2.5)), dpi=dpi)
    for item in real:
        label = item.get("label") or "curve"
        if len(item["x"]) <= 40:
            ax.plot(item["x"], item["y"], marker="o", ms=3.0, lw=1.2, label=label)
        else:
            ax.plot(item["x"], item["y"], lw=1.2, label=label)

    xmin = min(min(s["x"]) for s in real)
    xmax = max(max(s["x"]) for s in real)
    for item in series:
        if "vline" in item:
            ax.axvline(item["vline"], ls="--", lw=0.9, color="0.35", label=item.get("label") or None)

    ax.grid(True, which="major", ls=":", lw=0.6, color="0.75")
    ax.tick_params(direction="out", length=3.5)
    span = (xmax - xmin) or abs(xmin) or 1.0
    ax.set_xlim(xmin - 0.02 * span, xmax + 0.02 * span)
    if xlabel:
        ax.set_xlabel(xlabel)
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    if any(s.get("label") for s in series):
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight")
    plt.close(fig)

    ys = [v for s in real for v in s["y"]]
    return {
        "png": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "series": len(real),
        "points": sum(len(s["x"]) for s in real),
        "x_range": [xmin, xmax],
        "y_range": [min(ys), max(ys)],
        "step": data.get("step"),
        "frames": data.get("frame_count"),
        "warnings": data.get("warnings") or [],
    }


def render_xy(
    settings: Settings,
    odb_path: Path,
    series: list[dict],
    *,
    out_png: Path,
    xlabel: str = "",
    ylabel: str = "",
    title: str = "",
    step: str = "",
    image_size: tuple[int, int] | None = None,
    dpi: int = 200,
    timeout: int | None = None,
) -> dict:
    if not odb_path.is_file():
        raise ToolError(f"ODB not found: {odb_path}")
    if not series:
        raise ToolError("series is empty")
    for i, item in enumerate(series):
        if not isinstance(item, dict):
            raise ToolError(f"series[{i}] must be a dict")
        xspec = item.get("x") or {}
        _check_spec(xspec, f"series[{i}].x")
        if str(xspec.get("kind", "")).lower() != "vline":  # a vertical line has no y spec
            _check_spec(item.get("y") or {}, f"series[{i}].y")

    data = run_odb(settings, odb_path, "xy", {"series": series, "step": step}, timeout=timeout)
    return plot_series(
        data,
        out_png,
        xlabel=xlabel,
        ylabel=ylabel,
        title=title,
        image_size=image_size or (1400, 1000),
        dpi=dpi,
    )
