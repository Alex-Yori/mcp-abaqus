# -*- coding: utf-8 -*-
"""Build an editable axisymmetric CAE model + deck (run under ``abaqus cae noGUI=``).

Params (params.json):
    {
      "name": "S-my",
      "out_dir": "C:/.../models/S-my",
      "points": [[0,-45],[4,-45],[4,-20],[2,-15],[2,15],[4,20],[4,45],[0,45]],
      "density": 7.874e-08,
      "mesh_size": 0.2,
      "step_time": 1.0,
      "displacement": 8.0,
      "amplitude": "smooth" | "linear",
      "initial_field": 1.0,          # normalized chemical potential, NOT Kelvin
      "surface_bc": 1.0 | null,      # null = closed hydrogen boundary
      "save_cae": true,
      "preview": true
    }

Verified facts this script relies on (CAE 2018):
  * axisymmetric part = Part(dimensionality=AXISYMMETRIC, type=DEFORMABLE_BODY);
  * the sketch MUST carry an explicit ConstructionLine on the Y axis (the GUI draws
    it automatically; without it BaseShell fails with "Shell extrude feature failed");
  * the explicit coupled step class is TempDisplacementDynamicsStep;
  * the initial temperature field is m.Temperature(magnitudes=(v,));
  * a TemperatureBC writes DOF 11 (the hydrogen chemical-potential boundary);
  * the sketch is kept in the model so the saved .cae stays parametrically editable.
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
    AXISYMMETRIC,
    CAX4RT,
    DEFORMABLE_BODY,
    EXPLICIT,
    FREE,
    FROM_SECTION,
    MIDDLE_SURFACE,
    OFF,
    ON,
    PNG,
    QUAD,
    STEP,
)
import mesh
import regionToolset
import visualization  # noqa: F401  - required before a viewport accepts parts

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


def classify_edges(inst, ymin, ymax, tol):
    """Split instance edges into Bottom/Top/Left(axis)/right(outer surface)."""
    groups = {"Bottom": [], "Top": [], "Left": [], "right": []}
    for e in inst.edges:
        vids = e.getVertices()
        ep = [inst.vertices[v].pointOn[0] for v in vids]
        if all(abs(p[0]) < tol for p in ep):
            groups["Left"].append(e)
        elif all(abs(p[1] - ymin) < tol for p in ep):
            groups["Bottom"].append(e)
        elif all(abs(p[1] - ymax) < tol for p in ep):
            groups["Top"].append(e)
        else:
            groups["right"].append(e)
    return groups


def main(argv):
    params_path, out_path = _paths(argv)
    with io.open(params_path, "r", encoding="utf-8") as fh:
        params = to_native(json.load(fh))
    result = {"ok": True}
    try:
        name = params.get("name") or "CaeModel"
        out_dir = params["out_dir"]
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        pts = [(float(p[0]), float(p[1])) for p in params["points"]]
        if len(pts) < 3:
            raise ValueError("points needs at least 3 [r, y] pairs")
        ys = [p[1] for p in pts]
        ymin, ymax = min(ys), max(ys)
        span = max(ymax - ymin, max(p[0] for p in pts), 1.0)
        tol = 1e-6 * span

        # ---- part: axisymmetric sketch -> base shell ----
        m = mdb.Model(name=name)
        s = m.ConstrainedSketch(name="prof", sheetSize=span * 3.0 + 10.0)
        s.ConstructionLine(point1=(0.0, -(span * 2 + 10.0)), point2=(0.0, span * 2 + 10.0))
        for i in range(len(pts)):
            s.Line(point1=pts[i], point2=pts[(i + 1) % len(pts)])
        p = m.Part(name="Part-1", dimensionality=AXISYMMETRIC, type=DEFORMABLE_BODY)
        p.BaseShell(sketch=s)

        # ---- material shell + section (full user card is spliced into the .inp later) ----
        mat = m.Material(name=name)
        mat.Density(table=((float(params.get("density", 7.874e-08)),),))
        m.HomogeneousSolidSection(name=name, material=name, thickness=None)
        p.SectionAssignment(
            region=regionToolset.Region(faces=p.faces),
            sectionName=name, offset=0.0, offsetType=MIDDLE_SURFACE,
            offsetField="", thicknessAssignment=FROM_SECTION,
        )

        # ---- mesh: quad CAX4RT ----
        size = float(params.get("mesh_size", 0.2))
        p.setMeshControls(regions=p.faces, elemShape=QUAD, technique=FREE)
        p.seedPart(size=size, deviationFactor=0.1, minSizeFactor=0.1)
        p.generateMesh()
        ntri = 0
        for e in p.elements:
            if len(e.connectivity) == 3:
                ntri += 1
        et = mesh.ElemType(elemCode=CAX4RT, elemLibrary=EXPLICIT)
        p.setElementType(regions=(p.faces,), elemTypes=(et,))

        # ---- assembly + sets ----
        a = m.rootAssembly
        inst = a.Instance(name="Part-1-1", part=p, dependent=ON)
        groups = classify_edges(inst, ymin, ymax, tol)
        for key in ("Bottom", "Top", "Left", "right"):
            if not groups[key]:
                raise ValueError("no edges classified as %s - check the profile" % key)
            # CAE 2018 rejects tuples of individually-indexed edges ("Feature creation
            # failed"); re-select via findAt with a point on each edge instead.
            probes = tuple((e.pointOn[0],) for e in groups[key])
            a.Set(name=key, edges=inst.edges.findAt(*probes))
        a.Set(name="All", faces=inst.faces)

        # ---- step: Dynamic, Temp-disp, Explicit ----
        step_time = float(params.get("step_time", 1.0))
        m.TempDisplacementDynamicsStep(name="Step-1", previous="Initial",
                                       timePeriod=step_time, nlgeom=ON)

        # ---- amplitude + BCs + initial chemical-potential field ----
        disp = float(params.get("displacement", 8.0))
        amp_data = ((0.0, 0.0), (step_time, 1.0))
        if str(params.get("amplitude", "smooth")).lower() == "linear":
            m.TabularAmplitude(name="Amp-1", timeSpan=STEP, data=amp_data)
        else:
            m.SmoothStepAmplitude(name="Amp-1", timeSpan=STEP, data=amp_data)
        m.DisplacementBC(name="Bottom", createStepName="Initial",
                         region=a.sets["Bottom"], u2=0.0)
        m.DisplacementBC(name="Left", createStepName="Initial",
                         region=a.sets["Left"], u1=0.0)
        m.DisplacementBC(name="Top", createStepName="Step-1",
                         region=a.sets["Top"], u2=disp, amplitude="Amp-1")
        surface_bc = params.get("surface_bc", 1.0)
        if surface_bc is not None:
            m.TemperatureBC(name="H-surface", createStepName="Step-1",
                            region=a.sets["right"], magnitude=float(surface_bc))
        m.Temperature(name="H-initial", createStepName="Initial",
                      region=a.sets["All"],
                      magnitudes=(float(params.get("initial_field", 1.0)),))

        # ---- save the editable model, then write the deck ----
        cae_path = ""
        if params.get("save_cae", True):
            cae_path = os.path.join(out_dir, name + ".cae")
            mdb.saveAs(pathName=cae_path)
        job = mdb.Job(name=name, model=name, type=ANALYSIS, numCpus=8, numDomains=8)
        job.writeInput(consistencyChecking=OFF)
        inp_path = os.path.join(out_dir, name + ".inp")

        png_path = ""
        if params.get("preview", True):
            vp = session.viewports["Viewport: 1"]
            vp.setValues(displayedObject=p)
            try:
                vp.partDisplay.setValues(mesh=ON)
            except Exception:
                pass
            vp.view.fitView()
            try:
                session.printOptions.setValues(vpDecorations=False)
            except Exception:
                pass
            png_path = os.path.join(out_dir, name + "_mesh.png")
            session.printToFile(fileName=png_path, format=PNG, canvasObjects=(vp,))

        result.update({
            "name": name,
            "model": name,
            "cae": cae_path,
            "inp": inp_path,
            "node_count": len(p.nodes),
            "element_count": len(p.elements),
            "tri_count": ntri,
            "mesh_size": size,
            "set_sizes": dict((k, len(v)) for k, v in groups.items()),
            "preview_png": png_path,
            "bbox": [min(q[0] for q in pts), ymin, max(q[0] for q in pts), ymax],
        })
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    finally:
        with io.open(out_path, "w", encoding="utf-8") as fh:
            fh.write(dump_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))