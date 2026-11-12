"""Build a solid part from a drawing-derived spec (sketch profile -> solid -> mesh -> .inp).

The drawing itself is read by the calling model (vision), which turns the dimensioned
sketch into the JSON spec below; this module only guarantees that the geometry that
gets built is exactly what the spec says, and hands back a preview PNG to check it.
"""

from __future__ import annotations

import math
import numbers
import shutil
from pathlib import Path

from .cae import run_cae
from .config import Settings, ToolError, job_name_ok

_SCRIPT = Path(__file__).with_name("part_build.py")

_PROFILE_ARGS = {
    "rect": ("x0", "y0", "w", "h"),
    "circle": ("cx", "cy", "r"),
    "poly": ("points",),
}


def _num(value, label: str) -> float:
    if not isinstance(value, numbers.Real):
        raise ToolError(f"{label} must be a number (got {value!r})")
    if not math.isfinite(float(value)):
        raise ToolError(f"{label} must be finite")
    return float(value)


def validate_spec(spec: dict) -> dict:
    """Fail fast with actionable messages before spending 20s launching CAE."""
    if not isinstance(spec, dict):
        raise ToolError("spec must be an object")
    profile = spec.get("profile")
    if not isinstance(profile, dict):
        raise ToolError("spec.profile is required, e.g. {\"kind\": \"rect\", \"x0\": 0, \"y0\": 0, \"w\": 100, \"h\": 60}")
    kind = profile.get("kind")
    if kind not in _PROFILE_ARGS:
        raise ToolError("spec.profile.kind must be rect, circle or poly")
    for key in _PROFILE_ARGS[kind]:
        if key not in profile:
            raise ToolError(f"spec.profile.{key} is required for a {kind} profile")
        if key == "points":
            pts = profile["points"]
            if not isinstance(pts, list) or len(pts) < 3:
                raise ToolError("spec.profile.points needs at least 3 [x, y] pairs")
            for i, pt in enumerate(pts):
                if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                    raise ToolError(f"spec.profile.points[{i}] must be [x, y]")
                _num(pt[0], f"points[{i}].x")
                _num(pt[1], f"points[{i}].y")
        else:
            _num(profile[key], f"profile.{key}")
    if kind == "circle" and float(profile["r"]) <= 0:
        raise ToolError("profile.r must be > 0")
    if kind == "rect" and (float(profile["w"]) <= 0 or float(profile["h"]) <= 0):
        raise ToolError("profile.w and profile.h must be > 0")

    solid = spec.get("solid") or {"kind": "extrude", "depth": 10.0}
    if not isinstance(solid, dict) or solid.get("kind") not in {"extrude", "revolve"}:
        raise ToolError("spec.solid.kind must be extrude or revolve")
    if solid["kind"] == "extrude":
        if "depth" not in solid:
            solid["depth"] = 10.0
        _num(solid["depth"], "solid.depth")
        if float(solid["depth"]) <= 0:
            raise ToolError("solid.depth must be > 0")
    else:
        solid.setdefault("angle", 360.0)
        _num(solid["angle"], "solid.angle")

    for i, hole in enumerate(spec.get("holes") or []):
        if not isinstance(hole, dict):
            raise ToolError(f"holes[{i}] must be an object")
        hk = hole.get("kind", "circle")
        need = ("cx", "cy", "r") if hk == "circle" else ("x0", "y0", "w", "h")
        for key in need:
            if key not in hole:
                raise ToolError(f"holes[{i}].{key} is required for a {hk} hole")
            _num(hole[key], f"holes[{i}].{key}")

    for i, rnd in enumerate(spec.get("rounds") or []):
        if not isinstance(rnd, dict) or "r" not in rnd or "at" not in rnd:
            raise ToolError(f"rounds[{i}] needs {{'r': 3, 'at': [x, y]}}")
        _num(rnd["r"], f"rounds[{i}].r")
        at = rnd["at"]
        if not isinstance(at, (list, tuple)) or len(at) != 2:
            raise ToolError(f"rounds[{i}].at must be [x, y]")
        _num(at[0], f"rounds[{i}].at[0]")
        _num(at[1], f"rounds[{i}].at[1]")

    mesh_cfg = spec.get("mesh") or {}
    if mesh_cfg:
        if "size" in mesh_cfg:
            _num(mesh_cfg["size"], "mesh.size")
            if float(mesh_cfg["size"]) <= 0:
                raise ToolError("mesh.size must be > 0")
        if mesh_cfg.get("elem") not in (None, "", "C3D8R", "C3D4"):
            raise ToolError("mesh.elem must be C3D8R or C3D4")
    return spec


def build_part(
    settings: Settings,
    *,
    name: str,
    spec: dict,
    out_dir: Path,
    source_image: Path | None = None,
    save_cae: bool = False,
    preview: bool = True,
    timeout: int | None = None,
) -> dict:
    if not job_name_ok(name):
        raise ToolError("part name must be alphanumeric/._- (max 80 chars)")
    validate_spec(spec)
    out_dir.mkdir(parents=True, exist_ok=True)

    if source_image is not None:
        if not source_image.is_file():
            raise ToolError(f"drawing not found: {source_image}")
        shutil.copy2(source_image, out_dir / ("drawing" + source_image.suffix.lower()))

    params = {"name": name, "out_dir": str(out_dir), "save_cae": bool(save_cae), "preview": bool(preview)}
    for key in ("profile", "holes", "rounds", "solid", "mesh"):
        if spec.get(key) is not None:
            params[key] = spec[key]

    result = run_cae(settings, _SCRIPT, params, timeout=timeout or 900, cwd=out_dir)
    inp = Path(result.get("inp", ""))
    if not inp.is_file():
        raise ToolError(f"CAE reported success but wrote no .inp: {inp}")
    result["inp_size_bytes"] = inp.stat().st_size
    if result.get("preview_png") and Path(result["preview_png"]).is_file():
        result["preview_size_bytes"] = Path(result["preview_png"]).stat().st_size
    return result
