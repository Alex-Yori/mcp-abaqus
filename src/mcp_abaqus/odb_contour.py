# -*- coding: utf-8 -*-
"""Render one ODB contour/frame to PNG, executed by ``abaqus cae noGUI=``.

Usage:  abaqus cae noGUI=odb_contour.py -- <params.json> <out.json>
(params.json carries the real arguments; see ``contour.py`` on the caller side.)
"""
from __future__ import print_function

import io
import json
import sys
import traceback

from abaqus import session
from abaqusConstants import (
    CENTROID,
    COMPONENT,
    CONTOURS_ON_DEF,
    ELEMENT_NODAL,
    INTEGRATION_POINT,
    INVARIANT,
    NODAL,
    PNG,
)
import visualization  # noqa: F401  - registers ODB display types on the viewport

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


def get_viewport():
    try:
        return session.viewports[session.currentViewportName]
    except Exception:
        pass
    if "Viewport: 1" not in session.viewports.keys():
        session.Viewport(name="Viewport: 1")
    return session.viewports["Viewport: 1"]


_INVARIANTS = {
    "MISES": "Mises",
    "TRESC": "Tresca",
    "PRESS": "Pressure",
    "INV3": "Invariant3",
    "MAXPS": "Max. Principal",
    "MIDPS": "Mid. Principal",
    "MINPS": "Min. Principal",
    "MAXPR": "Max. In-Plane Principal",
    "MINPR": "Min. In-Plane Principal",
}


def pick_refinement(field_name, field_output, component):
    """Map a requested component/invariant onto a CAE refinement tuple."""
    labels = [str(l) for l in (getattr(field_output, "componentLabels", None) or [])]
    upper = [l.upper() for l in labels]
    wanted = (component or "").strip().upper()
    if wanted in _INVARIANTS:
        name = _INVARIANTS[wanted]
        return (INVARIANT, name), labels, name
    if wanted and wanted in upper:
        label = labels[upper.index(wanted)]
        return (COMPONENT, label), labels, label
    if not wanted:
        if str(field_name).upper() == "S":
            return (INVARIANT, "Mises"), labels, "Mises"
        if labels:
            return (COMPONENT, labels[0]), labels, labels[0]
    return None, labels, "(scalar)"


_POSITIONS = {
    "INTEGRATION_POINT": INTEGRATION_POINT,
    "ELEMENT_NODAL": ELEMENT_NODAL,
    "NODAL": NODAL,
    "CENTROID": CENTROID,
}


def set_primary(viewport, field_name, field_output, refinement):
    """ODB `location` is a string; setPrimaryVariable wants the constant. Try, then fall back."""
    raw = str(getattr(field_output, "location", "") or "").upper()
    order = []
    if raw in _POSITIONS:
        order.append(_POSITIONS[raw])
    for pos in (INTEGRATION_POINT, ELEMENT_NODAL, NODAL, CENTROID):
        if pos not in order:
            order.append(pos)
    last = None
    for pos in order:
        try:
            if refinement is None:
                viewport.odbDisplay.setPrimaryVariable(variableLabel=field_name, outputPosition=pos)
            else:
                viewport.odbDisplay.setPrimaryVariable(
                    variableLabel=field_name, outputPosition=pos, refinement=refinement
                )
            return pos
        except Exception as exc:
            last = exc
    raise last


def _paths(argv):
    """Arguments arrive as globals injected by the generated runner (abaqus cae noGUI= ignores argv)."""
    env = globals()
    if env.get("PARAMS_PATH") and env.get("OUT_PATH"):
        return env["PARAMS_PATH"], env["OUT_PATH"]
    return argv[-2], argv[-1]


def main(argv):
    params_path, out_path = _paths(argv)
    with io.open(params_path, "r", encoding="utf-8") as fh:
        params = to_native(json.load(fh))
    result = {"ok": True}
    odb = None
    try:
        odb = session.openOdb(name=params["odb"], readOnly=True)
        vp = get_viewport()
        vp.setValues(displayedObject=odb)

        step_names = list(odb.steps.keys())
        step_name = params.get("step") or step_names[-1]
        step = odb.steps[step_name]
        frames = step.frames
        index = int(params.get("frame", -1))
        if index < 0:
            index = len(frames) - 1
        vp.odbDisplay.display.setValues(plotState=(CONTOURS_ON_DEF,))
        vp.odbDisplay.setFrame(step=step_name, frame=index)

        field_name = params.get("field") or "S"
        fo = frames[index].fieldOutputs[field_name]
        refinement, labels, chosen = pick_refinement(field_name, fo, params.get("component") or "")
        set_primary(vp, field_name, fo, refinement)
        vp.view.fitView()
        try:
            session.printOptions.setValues(vpDecorations=False)
        except Exception:
            pass
        session.printToFile(fileName=params["out"], format=PNG, canvasObjects=(vp,))
        result.update(
            {
                "step": step_name,
                "frame": index,
                "frame_value": float(frames[index].frameValue),
                "field": field_name,
                "component": chosen,
                "components": labels,
                "out": params["out"],
            }
        )
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    finally:
        if odb is not None:
            try:
                odb.close()
            except Exception:
                pass
        with io.open(out_path, "w", encoding="utf-8") as fh:
            fh.write(dump_json(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
