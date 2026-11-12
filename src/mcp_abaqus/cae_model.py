"""Build an editable CAE model (.cae) + runnable deck (.inp) via the CAE geometry kernel.

Unlike ``axisym``/``model_builder`` (structured mesh generated in pure Python, which
imports as an orphan mesh), this drives a real CAE sketch -> feature -> mesh, so the
saved .cae opens like a hand-drawn model and its sketch stays editable in the GUI.

The physics card (DEPVAR names + VUMAT/VUMATHT constants) has no CAE GUI, so it is
spliced verbatim from the template deck's *Material block; loading/output requests
are rewritten from ``loading``. Mass scaling is inserted AFTER the step's boundary
cards (before it, Abaqus can silently swallow them as data cards).
"""

from __future__ import annotations

import math
import numbers
from pathlib import Path

from .cae import run_cae
from .config import Settings, ToolError, job_name_ok

_SCRIPT = Path(__file__).with_name("cae_model_build.py")

# Keywords that may appear INSIDE a *Material block (everything else ends it).
_MAT_SUB = {
    "density", "depvar", "user material", "inelastic heat fraction",
    "elastic", "plastic", "hardening", "rate dependent", "anneal temperature",
    "field", "expansion", "conductivity", "specific heat", "latent heat",
    "heat generation", "damping", "rayleigh damping", "viscoelastic", "creep",
    "hyperelastic", "hyperfoam", "swelling", "mohr coulomb", "drucker prager",
    "cast iron", "concrete", "no compression", "no tension", "brittle cracking",
    "damage initiation", "damage evolution", "damage stabilization",
    "element deletion", "thermal expansion", "anisotropic hyperelastic",
}

_DEFAULT_LOADING = {
    "step_time": 1.0,
    "displacement": 8.0,
    "mass_scaling": 10.0,
    "amplitude": "smooth",
    "output_interval": 0.001,
    "initial_field": 1.0,
    "surface_bc": 1.0,
}


def _num(value, label: str) -> float:
    if not isinstance(value, numbers.Real):
        raise ToolError(f"{label} must be a number (got {value!r})")
    if not math.isfinite(float(value)):
        raise ToolError(f"{label} must be finite")
    return float(value)


def _fmt(value: float) -> str:
    """Keyword-safe number: plain decimal, trailing dot for integers (10. not 1e+1.)."""
    s = "%g" % float(value)
    if "e" in s or "E" in s:
        s = repr(float(value))
    if "." not in s:
        s += "."
    return s


def profile_points(geometry: dict) -> list[list[float]]:
    """Parametric smooth bar, or an explicit closed profile (e.g. notched)."""
    if not isinstance(geometry, dict):
        raise ToolError("geometry must be an object")
    if geometry.get("points"):
        raw = geometry["points"]
        if not isinstance(raw, list) or len(raw) < 3:
            raise ToolError("geometry.points needs at least 3 [r, y] pairs")
        pts = []
        for i, pt in enumerate(raw):
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ToolError(f"geometry.points[{i}] must be [r, y]")
            x, y = _num(pt[0], f"points[{i}].r"), _num(pt[1], f"points[{i}].y")
            if x < -1e-9:
                raise ToolError("profile must stay at r>=0 (Y axis is the axis of symmetry)")
            pts.append([x, y])
        if min(p[0] for p in pts) > 1e-9:
            raise ToolError("profile must touch the axis r=0 (solid bar); tubes are not supported")
        return pts
    rg = _num(geometry.get("r_gauge", 2.0), "geometry.r_gauge")
    rr = _num(geometry.get("r_grip", 4.0), "geometry.r_grip")
    lg = _num(geometry.get("l_gauge", 15.0), "geometry.l_gauge")
    lt = _num(geometry.get("l_trans", 20.0), "geometry.l_trans")
    le = _num(geometry.get("l_end", 45.0), "geometry.l_end")
    if not (0 < rg and 0 < lg < lt < le):
        raise ToolError("need 0 < r_gauge and 0 < l_gauge < l_trans < l_end")
    if rr <= rg:
        raise ToolError("geometry.r_grip must be larger than r_gauge")
    return [[0.0, -le], [rr, -le], [rr, -lt], [rg, -lg],
            [rg, lg], [rr, lt], [rr, le], [0.0, le]]


def _keyword(line: str) -> str:
    return line.strip().split(",")[0].strip().lower().lstrip("*").strip()


def extract_material_block(tpl: Path) -> tuple[list[str], float]:
    """The template's whole *Material block (sub-keywords + data + comments) and its density."""
    lines = tpl.read_text(encoding="latin-1").splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip().lower().startswith("*material")), None)
    if start is None:
        raise ToolError(f"no *Material block found in template: {tpl}")
    block = [lines[start]]
    density = None
    i = start + 1
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("*") and not s.startswith("**"):
            if _keyword(lines[i]) not in _MAT_SUB:
                break
            if _keyword(lines[i]) == "density":
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("**")):
                    j += 1
                if j < len(lines):
                    try:
                        density = float(lines[j].split(",")[0].strip())
                    except ValueError:
                        pass
        block.append(lines[i])
        i += 1
    if density is None:
        raise ToolError(f"template *Material block has no readable *Density: {tpl}")
    return block, density


def _splice_deck(inp: Path, mat_block: list[str], loading: dict) -> list[str]:
    """Replace the CAE material shell with the template card; add mass scaling + output."""
    lines = inp.read_text(encoding="latin-1").splitlines()
    out: list[str] = []
    notes: list[str] = []
    ms = loading.get("mass_scaling")
    oi = _fmt(loading["output_interval"])
    replaced_mat = inserted_ms = replaced_field = added_hist = False
    i = 0
    while i < len(lines):
        line = lines[i]
        low = line.strip().lower()
        if low.startswith("*material"):
            j = i + 1
            while j < len(lines):
                t = lines[j].strip()
                if t.startswith("*") and not t.startswith("**") and _keyword(lines[j]) not in _MAT_SUB:
                    break
                j += 1
            out.extend(mat_block)
            replaced_mat = True
            i = j
            continue
        if ms and not inserted_ms and (low == "** output requests" or low.startswith("*restart")):
            out.append("*Fixed Mass Scaling, factor=%s" % _fmt(ms))
            inserted_ms = True
        if low == "*output, field, variable=preselect":
            out.extend(["*Output, field, time interval=%s" % oi,
                        "*Node Output", "U, RF",
                        "*Element Output, directions=YES", "S, STATUS, SDV"])
            replaced_field = True
            i += 1
            continue
        out.append(line)
        if low == "*output, history, variable=preselect":
            out.extend(["*Output, history, time interval=%s" % oi,
                        "*Incrementation Output", "DMASS, DT"])
            added_hist = True
        i += 1
    if not replaced_mat:
        raise ToolError("CAE deck has no *Material block to replace (unexpected format)")
    if ms and not inserted_ms:
        raise ToolError("could not place *Fixed Mass Scaling (no output/restart section found)")
    if not replaced_field:
        raise ToolError("CAE deck has no PRESELECT field output to replace (unexpected format)")
    inp.write_bytes(("\r\n".join(out) + "\r\n").encode("latin-1"))
    notes.append("material card spliced from template")
    if ms:
        notes.append("fixed mass scaling x%s after step BCs" % _fmt(ms))
    notes.append("field output U/RF/S/STATUS/SDV every %s" % oi)
    if added_hist:
        notes.append("history DMASS/DT added")
    return notes


def build_cae_model(
    settings: Settings,
    *,
    name: str,
    geometry: dict,
    template_deck: Path,
    mesh: dict,
    loading: dict,
    out_dir: Path,
    save_cae: bool = True,
    preview: bool = True,
    copy_subroutines: bool = True,
    timeout: int | None = None,
) -> dict:
    if not job_name_ok(name):
        raise ToolError("model name must be alphanumeric/._- (max 80 chars)")
    pts = profile_points(geometry)
    size = _num((mesh or {}).get("size", 0.2), "mesh.size")
    if size <= 0:
        raise ToolError("mesh.size must be > 0")
    load = dict(_DEFAULT_LOADING)
    for k, v in (loading or {}).items():
        if k not in _DEFAULT_LOADING:
            raise ToolError(f"unknown loading key '{k}'; allowed: {sorted(_DEFAULT_LOADING)}")
        load[k] = v
    for k in ("step_time", "displacement", "output_interval", "initial_field"):
        load[k] = _num(load[k], f"loading.{k}")
    if load["step_time"] <= 0 or load["output_interval"] <= 0:
        raise ToolError("loading.step_time and loading.output_interval must be > 0")
    if load["mass_scaling"] is not None:
        load["mass_scaling"] = _num(load["mass_scaling"], "loading.mass_scaling")
        if load["mass_scaling"] < 0:
            raise ToolError("loading.mass_scaling must be >= 0 (0 disables)")
    if str(load["amplitude"]).lower() not in ("smooth", "linear"):
        raise ToolError("loading.amplitude must be 'smooth' or 'linear'")
    if load["surface_bc"] is not None:
        load["surface_bc"] = _num(load["surface_bc"], "loading.surface_bc")

    mat_block, density = extract_material_block(template_deck)
    mat_block = ["*Material, name=%s" % name] + mat_block[1:]
    out_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "name": name,
        "out_dir": str(out_dir),
        "points": pts,
        "density": density,
        "mesh_size": size,
        "step_time": load["step_time"],
        "displacement": load["displacement"],
        "amplitude": str(load["amplitude"]).lower(),
        "initial_field": load["initial_field"],
        "surface_bc": load["surface_bc"],
        "save_cae": bool(save_cae),
        "preview": bool(preview),
    }
    result = run_cae(settings, _SCRIPT, params, timeout=timeout or 900, cwd=out_dir)
    inp = Path(result.get("inp", ""))
    if not inp.is_file():
        raise ToolError(f"CAE reported success but wrote no .inp: {inp}")
    head = inp.read_text(encoding="latin-1")
    if "*Element, type=CAX4RT" not in head:
        raise ToolError("CAE wrote a non-CAX4RT element type (DOF 11 would be inactive); "
                        "check the element-type assignment in cae_model_build.py")
    if save_cae and not Path(result.get("cae", "")).is_file():
        raise ToolError("CAE reported success but wrote no .cae")
    notes = _splice_deck(inp, mat_block, load)

    copied = []
    if copy_subroutines:
        for src in template_deck.parent.iterdir():
            ascii_name = all(ord(ch) < 128 for ch in src.name)
            if ascii_name and src.is_file() and src.suffix.lower() in {".f", ".for", ".f90", ".inc"}:
                dst = out_dir / src.name
                if not dst.exists():
                    dst.write_bytes(src.read_bytes())
                    copied.append(src.name)

    result["inp_size_bytes"] = inp.stat().st_size
    result["material_from"] = str(template_deck)
    result["density"] = density
    result["loading_applied"] = load
    result["splice_notes"] = notes
    result["copied_subroutines"] = copied
    if result.get("preview_png") and Path(result["preview_png"]).is_file():
        result["preview_size_bytes"] = Path(result["preview_png"]).stat().st_size
    result["hint"] = (
        "open the .cae in CAE (File > Open) to edit the sketch like a hand-drawn model; "
        "run datacheck on the .inp with options={'double':'both'}; if you edit the .cae and "
        "re-write the input, re-splice the material card (re-run this tool with the new geometry)"
    )
    return result