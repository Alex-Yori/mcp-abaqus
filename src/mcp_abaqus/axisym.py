"""Build an axisymmetric bar/notched-bar model as a runnable Abaqus deck.

Why generate the mesh in Python instead of CAE: the geometry is a single
surface of revolution, so a structured quad mesh is exact, deterministic and
takes milliseconds - no CAE round trip, no graphics, easy to diff.

The physics (material, VUMAT constants, BCs, step, output requests) is NOT
re-invented here: it is spliced verbatim from a template deck (everything after
``*End Assembly``). That keeps the validated H-CGM setup exactly as it is and
makes the generated deck runnable as long as it defines the same set names the
template's BCs reference (Bottom / Top / Left / right / All).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .config import ToolError

TEMPLATE_SETS = ("All", "Bottom", "Top", "Left", "right")


# --------------------------------------------------------------------- geometry
def profile_radius(z: float, geom: dict) -> float:
    """Outer radius at height z for the requested bar geometry."""
    kind = geom.get("kind", "axisym_notched_bar")
    a = float(geom["root_radius"])
    r_out = float(geom["body_radius"])
    half = float(geom["half_length"])
    if abs(z) > half:
        return 0.0
    if kind == "axisym_plain_bar":
        return r_out
    notch = geom.get("notch_kind", "parabolic")
    R = float(geom["notch_radius"])
    if notch == "parabolic":
        # r(z) = a + z^2/(2R): curvature 1/R at the root, reaches the body radius at |z| = sqrt(2R(r_out-a))
        return min(r_out, a + z * z / (2.0 * R))
    if notch == "circular":
        # circular arc of radius R tangent to the root: needs R <= (r_out - a)
        drop = r_out - a
        if R > drop + 1e-9:
            raise ToolError(
                "circular notch needs notch_radius <= body_radius - root_radius "
                f"({drop}); use notch_kind='parabolic' for a sharp flare"
            )
        if abs(z) >= R:
            return r_out
        return a + drop - math.sqrt(R * R - z * z) + (drop - R) * 0.0
    raise ToolError("notch_kind must be 'parabolic' or 'circular'")


def notch_half_width(geom: dict) -> float:
    kind = geom.get("kind", "axisym_notched_bar")
    if kind != "axisym_notched_bar":
        return 0.0
    a = float(geom["root_radius"])
    r_out = float(geom["body_radius"])
    R = float(geom["notch_radius"])
    if geom.get("notch_kind", "parabolic") == "parabolic":
        return math.sqrt(2.0 * R * (r_out - a))
    return R


def _z_stations(geom: dict, mesh: dict) -> list[float]:
    half = float(geom["half_length"])
    z1 = notch_half_width(geom)
    dz_root = float(mesh.get("dz_notch", 0.02))
    dz_far = float(mesh.get("dz_far", 1.0))
    growth = float(mesh.get("growth", 1.12))
    zs = [0.0]
    if z1 > 0:
        z = 0.0
        while z < z1 - 1e-12:
            z = min(z1, z + dz_root)
            zs.append(z)
    z = z1
    dz = dz_root * 4.0 if z1 > 0 else dz_far
    while z < half - 1e-12:
        dz = min(dz * growth, dz_far)
        z = min(half, z + dz)
        zs.append(z)
    if abs(zs[-1] - half) > 1e-9:
        zs.append(half)
    return [-v for v in reversed(zs[1:])] + zs


def build_bar(geom: dict, mesh: dict) -> dict:
    n_r = max(2, int(mesh.get("n_radial", 16)))
    zs = _z_stations(geom, mesh)
    nodes: list[tuple[float, float]] = []
    index: dict[tuple[int, int], int] = {}
    for j, z in enumerate(zs):
        rmax = profile_radius(z, geom)
        if rmax <= 0:
            raise ToolError(f"profile radius is zero at z={z}; check half_length")
        for i in range(n_r + 1):
            frac = float(i) / n_r
            nodes.append((rmax * frac, z))
            index[(j, i)] = len(nodes)
    elements: list[tuple[int, int, int, int]] = []
    for j in range(len(zs) - 1):
        for i in range(n_r):
            n1 = index[(j, i)]
            n2 = index[(j, i + 1)]
            n3 = index[(j + 1, i + 1)]
            n4 = index[(j + 1, i)]
            elements.append((n1, n2, n3, n4))
    return {"nodes": nodes, "elements": elements, "z_stations": zs, "n_radial": n_r}


def make_sets(model: dict, geom: dict) -> dict:
    nodes = model["nodes"]
    half = float(geom["half_length"])
    tol = 1e-6
    sets = {
        "All": [i + 1 for i in range(len(nodes))],
        "Bottom": [i + 1 for i, (r, z) in enumerate(nodes) if abs(z + half) < tol],
        "Top": [i + 1 for i, (r, z) in enumerate(nodes) if abs(z - half) < tol],
        "Left": [i + 1 for i, (r, z) in enumerate(nodes) if abs(r) < tol],
    }
    # outer surface: nodes whose radius equals the profile radius at that z
    surface = []
    zs = model["z_stations"]
    n_r = model["n_radial"]
    for j in range(len(zs)):
        surface.append(j * (n_r + 1) + n_r + 1)  # outermost node of row j
    sets["right"] = sorted(set(surface))
    for name in TEMPLATE_SETS:
        if not sets.get(name):
            raise ToolError(f"generated set '{name}' is empty - the template deck's BCs need it")
    return sets


# ------------------------------------------------------------------------- deck
def split_template(path: Path) -> str:
    """Everything after ``*End Assembly`` - the validated physics tail."""
    if not path.is_file():
        raise ToolError(f"template deck not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*\*End Assembly\s*$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        raise ToolError(f"template deck has no '*End Assembly' line: {path}")
    return text[m.end():].lstrip("\r\n")


def _fmt(v: float) -> str:
    return ("%.6f" % v).rstrip("0").rstrip(".") if v != int(v) else "%d" % int(v)


def write_deck(
    dest: Path,
    *,
    name: str,
    model: dict,
    sets: dict,
    physics_tail: str,
    elem_type: str = "CAX4RT",
    header_extra: str = "",
) -> dict:
    nodes, elements = model["nodes"], model["elements"]
    out: list[str] = [
        "*Heading",
        f"** Job name: {name}",
        "** Generated by mcp-abaqus model_builder (axisymmetric, structured mesh)",
    ]
    if header_extra:
        out.append(f"** {header_extra}")
    out += [
        "*Preprint, echo=NO, model=NO, history=NO, contact=NO",
        "**",
        "** PARTS",
        "**",
        "*Part, name=Part-1",
        "*Node",
    ]
    out += [f"{i + 1}, {_fmt(r)}, {_fmt(z)}" for i, (r, z) in enumerate(nodes)]
    out.append(f"*Element, type={elem_type}")
    out += [f"{i + 1}, {a}, {b}, {c}, {d}" for i, (a, b, c, d) in enumerate(elements)]
    out += [
        "*Nset, nset=Gurson, generate",
        f"1, {len(nodes)}, 1",
        "*Elset, elset=Gurson, generate",
        f"1, {len(elements)}, 1",
        "** Section: Gurson",
        "*Solid Section, elset=Gurson, material=Gurson",
        ",",
        "*End Part",
        "**",
        "** ASSEMBLY",
        "**",
        "*Assembly, name=Assembly",
        "*Instance, name=Part-1-1, part=Part-1",
        "*End Instance",
    ]
    for set_name in ("All", "Bottom", "Top", "Left", "right"):
        ids = sets[set_name]
        if set_name == "All":
            out += [f"*Nset, nset={set_name}, instance=Part-1-1, generate", f"1, {len(nodes)}, 1"]
            continue
        out.append(f"*Nset, nset={set_name}, instance=Part-1-1")
        # wrap long set definitions for readability
        line: list[str] = []
        for nid in ids:
            line.append(str(nid))
            if len(line) == 16:
                out.append(", ".join(line))
                line = []
        if line:
            out.append(", ".join(line))
    out += ["*End Assembly"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n" + physics_tail, encoding="utf-8")
    return {"nodes": len(nodes), "elements": len(elements), "deck": str(dest)}


def apply_loading(tail: str, loading: dict) -> tuple[str, list[str]]:
    """Rewrite step time / top displacement / mass-scaling factor in the spliced physics tail."""
    notes: list[str] = []
    if not loading:
        return tail, notes
    out = tail
    if "step_time" in loading:
        new_t = float(loading["step_time"])
        pattern = re.compile(
            r"(^[ \t]*\*Dynamic Temperature-displacement,[ \t]*Explicit[ \t\r\n]*,)[ \t]*[-+0-9.eE]+",
            re.MULTILINE,
        )
        out, n = pattern.subn(lambda m: m.group(1) + " %g" % new_t, out)
        notes.append("step_time=%g (%d hit)" % (new_t, n))
    if "displacement" in loading:
        new_u = float(loading["displacement"])
        pattern = re.compile(
            r"(^[ \t]*Top[ \t]*,[ \t]*2[ \t]*,[ \t]*2[ \t]*,)[ \t]*[-+0-9.eE]+",
            re.MULTILINE,
        )
        out, n = pattern.subn(lambda m: m.group(1) + " %g" % new_u, out)
        notes.append("top displacement=%g mm (%d hit)" % (new_u, n))
    if "mass_scaling" in loading:
        new_f = float(loading["mass_scaling"])
        pattern = re.compile(r"(\*Fixed Mass Scaling,[ \t]*factor=)[ \t]*[-+0-9.eE]+", re.IGNORECASE)
        out, n = pattern.subn(lambda m: m.group(1) + "%g" % new_f, out)
        notes.append("mass scaling factor=%g (%d hit)" % (new_f, n))
    return out, notes


def plot_mesh(png: Path, model: dict, *, title: str = "") -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    nodes, elements = model["nodes"], model["elements"]
    segs = []
    for a, b, c, d in elements:
        pts = [nodes[a - 1], nodes[b - 1], nodes[c - 1], nodes[d - 1], nodes[a - 1]]
        segs.append([(p[0], p[1]) for p in pts])
    fig, ax = plt.subplots(figsize=(4.2, 9.0), dpi=160)
    ax.add_collection(LineCollection(segs, linewidths=0.15, colors="0.25"))
    ax.set_aspect("equal")
    ax.set_xlabel("r (mm)")
    ax.set_ylabel("z (mm)")
    if title:
        ax.set_title(title, fontsize=8)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(png))
    plt.close(fig)
    return str(png)


def summarise(geom: dict, model: dict) -> dict:
    nodes = model["nodes"]
    z1 = notch_half_width(geom)
    a = float(geom["root_radius"])
    R = float(geom["notch_radius"])
    triax = 1.0 / 3.0 + math.log(1.0 + a / (2.0 * R)) if R > 0 else 1.0 / 3.0
    return {
        "kind": geom.get("kind", "axisym_notched_bar"),
        "notch_kind": geom.get("notch_kind", "parabolic"),
        "root_radius": a,
        "body_radius": float(geom["body_radius"]),
        "notch_radius": R,
        "half_length": float(geom["half_length"]),
        "notch_half_width": round(z1, 4),
        "bridgman_triaxiality": round(triax, 3),
        "node_count": len(nodes),
        "element_count": len(model["elements"]),
        "r_max": max(r for r, _ in nodes),
        "z_range": [min(z for _, z in nodes), max(z for _, z in nodes)],
    }
