# -*- coding: utf-8 -*-
"""Build a solid part from a drawing-derived spec, then mesh / export / preview it.

Run under ``abaqus cae noGUI=`` by ``cae.run_cae``. Params (params.json):

    {
      "name": "Plate",
      "out_dir": "C:/.../parts/Plate",
      "profile": {"kind": "rect", "x0": 0, "y0": 0, "w": 100, "h": 60}
                 | {"kind": "circle", "cx": 0, "cy": 0, "r": 25}
                 | {"kind": "poly", "points": [[0,0],[80,0],[80,40],[0,40]]},
      "holes": [{"kind": "circle", "cx": 20, "cy": 30, "r": 5},
                {"kind": "rect", "x0": 60, "y0": 20, "w": 20, "h": 10}],
      "rounds": [{"r": 3, "at": [0, 0]}],
      "solid": {"kind": "extrude", "depth": 10}
               | {"kind": "revolve", "angle": 360},
      "mesh": {"size": 2.0, "elem": "C3D8R"},
      "save_cae": false,
      "preview": true
    }

All lengths are whatever unit the drawing uses - this script never rescales.
"""
from __future__ import print_function

import io
import json
import os
import sys
import traceback

from abaqus import mdb, session
from abaqusConstants import (
    ANALYSIS,
    FEATURE,
    C3D4,
    C3D8R,
    DEFORMABLE_BODY,
    HEX,
    OFF,
    ON,
    PNG,
    STANDARD,
    TET,
    THREE_D,
)
from abaqusConstants import PERCENTAGE
import mesh
import visualization  # noqa: F401  - required before a viewport accepts parts/ODBs

_TEXT = type(u"")


def to_native(obj):
    if isinstance(obj, _TEXT):
        return obj.encode("utf-8")
    if isinstance(obj, list):
        return [to_native(x) for x in obj]
    if isinstance(obj, dict):
        return dict((to_native(k), to_native(v)) for k, v in obj.items())
    return obj


def dump_json(obj):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if not isinstance(s, _TEXT):
        s = s.decode("utf-8", "replace")
    return s


def _paths(argv):
    env = globals()
    if env.get("PARAMS_PATH") and env.get("OUT_PATH"):
        return env["PARAMS_PATH"], env["OUT_PATH"]
    return argv[-2], argv[-1]


def profile_extent(profile, holes, rounds):
    xs, ys = [], []
    kind = profile.get("kind")
    if kind == "rect":
        xs = [profile["x0"], profile["x0"] + profile["w"]]
        ys = [profile["y0"], profile["y0"] + profile["h"]]
    elif kind == "circle":
        xs = [profile["cx"] - profile["r"], profile["cx"] + profile["r"]]
        ys = [profile["cy"] - profile["r"], profile["cy"] + profile["r"]]
    elif kind == "poly":
        pts = profile.get("points") or []
        if len(pts) < 3:
            raise ValueError("poly needs at least 3 points")
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
    else:
        raise ValueError("profile.kind must be rect, circle or poly (got %r)" % (kind,))
    return xs, ys


def draw_profile(sketch, profile):
    kind = profile.get("kind")
    if kind == "rect":
        sketch.rectangle(
            point1=(float(profile["x0"]), float(profile["y0"])),
            point2=(float(profile["x0"]) + float(profile["w"]), float(profile["y0"]) + float(profile["h"])),
        )
    elif kind == "circle":
        cx, cy, r = float(profile["cx"]), float(profile["cy"]), float(profile["r"])
        sketch.CircleByCenterPerimeter(center=(cx, cy), point1=(cx + r, cy))
    elif kind == "poly":
        pts = [(float(p[0]), float(p[1])) for p in profile["points"]]
        for i in range(len(pts)):
            sketch.Line(point1=pts[i], point2=pts[(i + 1) % len(pts)])
    else:
        raise ValueError("unsupported profile kind: %r" % (kind,))


def draw_holes(sketch, holes):
    for i, hole in enumerate(holes or []):
        kind = hole.get("kind", "circle")
        if kind == "circle":
            cx, cy, r = float(hole["cx"]), float(hole["cy"]), float(hole["r"])
            sketch.CircleByCenterPerimeter(center=(cx, cy), point1=(cx + r, cy))
        elif kind == "rect":
            sketch.rectangle(
                point1=(float(hole["x0"]), float(hole["y0"])),
                point2=(float(hole["x0"]) + float(hole["w"]), float(hole["y0"]) + float(hole["h"])),
            )
        else:
            raise ValueError("hole %d: kind must be circle or rect" % i)


def round_edges(part_a, rounds, depth):
    """Blend the vertical corner edges nearest each requested point (sketch fillet is picky in 2018)."""
    done = []
    for item in rounds or []:
        at = item["at"]
        radius = float(item["r"])
        x, y = float(at[0]), float(at[1])
        probe = (x, y, float(depth) / 2.0)
        try:
            edges = part_a.edges.findAt((probe,))
        except Exception as exc:
            raise ValueError("round at (%g, %g): no edge through %s (%s)" % (x, y, probe, exc))
        if not len(edges):
            raise ValueError(
                "round at (%g, %g): no vertical edge passes through %s; for a plate the corner "
                "coordinates are the profile corners" % (x, y, probe)
            )
        part_a.Round(edgeList=tuple(edges), radius=radius)
        done.append({"at": [x, y], "r": radius})
    return done


def main(argv):
    params_path, out_path = _paths(argv)
    with io.open(params_path, "r", encoding="utf-8") as fh:
        params = to_native(json.load(fh))
    result = {"ok": True}
    try:
        name = params.get("name") or "Part"
        out_dir = params["out_dir"]
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)

        profile = params["profile"]
        holes = params.get("holes") or []
        rounds = params.get("rounds") or []
        solid = params.get("solid") or {"kind": "extrude", "depth": 10.0}

        xs, ys = profile_extent(profile, holes, rounds)
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        sheet = span * 3.0 + 10.0

        mdb.Model(name=name)
        model = mdb.models[name]
        sketch = model.ConstrainedSketch(name="__profile__", sheetSize=sheet)
        kind = solid.get("kind", "extrude")
        if kind == "revolve":
            # A solid revolve needs an explicit axis in the sketch; the Y axis is the CAE default.
            if min(xs) < 0.0 < max(xs):
                raise ValueError(
                    "revolve profile straddles x=0: keep it entirely on one side of the Y axis "
                    "(x range is %g..%g)" % (min(xs), max(xs))
                )
            margin = sheet
            sketch.ConstructionLine(point1=(0.0, -margin), point2=(0.0, margin))
        draw_profile(sketch, profile)
        draw_holes(sketch, holes)
        rounded = []

        part_a = model.Part(name=name, dimensionality=THREE_D, type=DEFORMABLE_BODY)
        if kind == "extrude":
            part_a.BaseSolidExtrude(sketch=sketch, depth=float(solid.get("depth", 10.0)))
        elif kind == "revolve":
            part_a.BaseSolidRevolve(sketch=sketch, angle=float(solid.get("angle", 360.0)))
        else:
            raise ValueError("solid.kind must be extrude or revolve")

        if rounds:
            if kind != "extrude":
                raise ValueError("rounds are supported for extrude only (revolve corners are arcs)")
            rounded = round_edges(part_a, rounds, float(solid.get("depth", 10.0)))

        mesh_cfg = params.get("mesh") or {}
        size = float(mesh_cfg.get("size") or (span / 12.0))
        elem_name = mesh_cfg.get("elem") or ("C3D8R" if kind == "extrude" else "C3D4")
        code = {"C3D8R": C3D8R, "C3D4": C3D4}.get(elem_name)
        if code is None:
            raise ValueError("mesh.elem must be C3D8R or C3D4")
        family = HEX if code == C3D8R else TET
        part_a.setElementType(regions=(part_a.cells,), elemTypes=(mesh.ElemType(elemCode=code, elemLibrary=STANDARD),))
        part_a.seedPart(size=size, deviationFactor=0.1, minSizeFactor=0.1)
        part_a.generateMesh()
        del model.sketches["__profile__"]

        # An .inp is written from the assembly, so the part must be instanced (dependent keeps the mesh on the part).
        model.rootAssembly.Instance(name=name + "-1", part=part_a, dependent=ON)

        job_name = name + "_mesh"
        mdb.Job(name=job_name, model=name, type=ANALYSIS)
        mdb.jobs[job_name].writeInput(consistencyChecking=OFF)
        inp_path = os.path.join(out_dir, job_name + ".inp")

        cae_path = ""
        if params.get("save_cae"):
            cae_path = os.path.join(out_dir, name + ".cae")
            mdb.saveAs(pathName=cae_path)

        png_path = ""
        if params.get("preview", True):
            vp = session.viewports[session.currentViewportName] if hasattr(session, "currentViewportName") else None
            if vp is None:
                vp = session.viewports["Viewport: 1"]
            # partDisplay(mesh=ON) is the only mode that actually draws elements in noGUI;
            # renderStyle/visibleEdges keywords there raise and silently fall back to shaded.
            vp.setValues(displayedObject=part_a)
            try:
                vp.partDisplay.setValues(mesh=ON)
            except Exception:
                pass
            vp.view.fitView()
            try:
                session.printOptions.setValues(vpDecorations=False)
            except Exception:
                pass
            png_path = os.path.join(out_dir, name + "_preview.png")
            session.printToFile(fileName=png_path, format=PNG, canvasObjects=(vp,))

        result.update(
            {
                "part": name,
                "model": name,
                "solid": kind,
                "profile": profile.get("kind"),
                "holes": len(holes),
                "rounds": rounded,
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                "mesh_size": size,
                "elem": elem_name,
                "node_count": len(part_a.nodes),
                "element_count": len(part_a.elements),
                "inp": inp_path,
                "cae": cae_path,
                "preview_png": png_path,
            }
        )
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    finally:
        with io.open(out_path, "w", encoding="utf-8") as fh:
            fh.write(dump_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
