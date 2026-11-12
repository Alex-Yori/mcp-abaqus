# -*- coding: utf-8 -*-
"""Import an .inp into Abaqus/CAE (noGUI) and render it, so a generated deck can be eyeballed."""
from __future__ import print_function

import io
import json
import os
import sys
import traceback

from abaqus import mdb, session
from abaqusConstants import *
import visualization  # noqa: F401

_TEXT = type(u"")


def _native(o):
    if isinstance(o, _TEXT):
        return o.encode("utf-8")
    if isinstance(o, list):
        return [_native(x) for x in o]
    if isinstance(o, dict):
        return dict((_native(k), _native(v)) for k, v in o.items())
    return o


def dump_json(o):
    s = json.dumps(o, ensure_ascii=False, default=str)
    return s.decode("utf-8", "replace") if not isinstance(s, _TEXT) else s


def _paths(argv):
    env = globals()
    if env.get("PARAMS_PATH") and env.get("OUT_PATH"):
        return env["PARAMS_PATH"], env["OUT_PATH"]
    return argv[-2], argv[-1]


def main(argv):
    pp, op = _paths(argv)
    params = _native(json.load(io.open(pp, encoding="utf-8")))
    res = {"ok": True}
    try:
        deck = params["inp"]
        model_name = params.get("model") or "shown"
        mdb.ModelFromInputFile(name=model_name, inputFileName=deck)
        model = mdb.models[model_name]
        asm = model.rootAssembly

        parts = []
        for pname in model.parts.keys():
            part = model.parts[pname]
            parts.append(
                {
                    "name": pname,
                    "nodes": len(part.nodes),
                    "elements": len(part.elements),
                    "elem_types": sorted(set([e.type for e in part.elements])),
                }
            )
        res["parts"] = parts
        res["instances"] = list(asm.instances.keys())
        res["sets"] = list(asm.sets.keys())

        res["materials"] = list(model.materials.keys())
        res["steps"] = list(model.steps.keys())
        try:
            res["step_time"] = float(model.steps[model.steps.keys()[-1]].timePeriod)
        except Exception:
            pass

        vp = session.viewports["Viewport: 1"]
        vp.setValues(displayedObject=asm)
        try:
            session.graphicsOptions.setValues(backgroundStyle=SOLID, backgroundColor="#FFFFFF")
        except Exception:
            pass
        # WIREFRAME + mesh: that is the CAE "mesh view" look, and unlike shaded it
        # keeps every element edge visible even when the part is 90 mm long / 4 mm wide.
        try:
            vp.assemblyDisplay.setValues(mesh=ON, renderStyle=WIREFRAME)
        except Exception:
            vp.assemblyDisplay.setValues(mesh=ON)
        vp.view.fitView()
        session.printOptions.setValues(vpDecorations=False)

        out_full = params["out"]
        try:
            session.pngOptions.setValues(imageSize=(700, 2000))
        except Exception:
            pass
        session.printToFile(fileName=out_full, format=PNG, canvasObjects=(vp,))
        shots = [out_full]
        cae_path = params.get("save_cae")
        if cae_path:
            mdb.saveAs(pathName=cae_path)
            res["cae"] = cae_path

        # second shot: zoom on the notch
        zoom = params.get("zoom_out")
        if zoom:
            try:
                vp.view.zoomRectangle(xmin=0.30, ymin=0.44, xmax=0.72, ymax=0.56)
            except Exception:
                pass
            try:
                session.pngOptions.setValues(imageSize=(1400, 900))
            except Exception:
                pass
            session.printToFile(fileName=zoom, format=PNG, canvasObjects=(vp,))
            shots.append(zoom)
        res["shots"] = shots
        res["png"] = params["out"]
    except Exception:
        res = {"ok": False, "error": traceback.format_exc()}
    finally:
        with io.open(op, "w", encoding="utf-8") as fh:
            fh.write(dump_json(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
