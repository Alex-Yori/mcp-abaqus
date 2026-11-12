"""Render an ODB frame to PNG through `abaqus cae noGUI` (no CAE window)."""

from __future__ import annotations

from pathlib import Path

from .cae import run_cae
from .config import Settings, ToolError

_SCRIPT = Path(__file__).with_name("odb_contour.py")


def render_contour(
    settings: Settings,
    odb_path: Path,
    *,
    out_png: Path,
    field: str,
    component: str,
    step: str,
    frame: int,
    timeout: int | None = None,
) -> dict:
    if not odb_path.is_file():
        raise ToolError(f"ODB not found: {odb_path}")
    if out_png.exists():
        raise ToolError(f"output exists: {out_png}")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    result = run_cae(
        settings,
        _SCRIPT,
        {
            "odb": str(odb_path),
            "out": str(out_png),
            "field": field,
            "component": component,
            "step": step,
            "frame": int(frame),
        },
        timeout=timeout or 900,
    )
    if not out_png.is_file():
        raise ToolError(f"CAE reported success but wrote no PNG: {out_png}")
    result["png"] = str(out_png)
    result["size_bytes"] = out_png.stat().st_size
    return result
