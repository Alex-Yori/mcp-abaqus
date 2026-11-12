# -*- coding: utf-8 -*-
"""Read-only ODB extraction, executed by ``abaqus python`` (Python 2.7).

Usage:  abaqus python odb_extract.py <params.json> <out.json>

params.json keys: odb, mode (summary|field|node_series|history), step, frame,
instance, field, component, nodes, region, variable, limit
Writes UTF-8 JSON to out.json. Never prints to stdout as a protocol channel.
"""
from __future__ import print_function

import io
import json
import math
import sys
import traceback

from odbAccess import openOdb


def to_num(x):
    try:
        return float(x)
    except Exception:
        return None


def as_floats(dat):
    if dat is None:
        return []
    if isinstance(dat, (int, float)):
        return [float(dat)]
    out = []
    try:
        for item in dat:
            n = to_num(item)
            if n is not None:
                out.append(n)
    except Exception:
        n = to_num(dat)
        if n is not None:
            out.append(n)
    return out


def value_scalar(value, comp_index):
    vals = as_floats(value.data)
    if comp_index is None:
        if len(vals) == 1:
            return vals[0]
        mag = to_num(getattr(value, "magnitude", None))
        return mag if mag is not None else (vals[0] if vals else None)
    if comp_index < len(vals):
        return vals[comp_index]
    return None


def pick_step(odb, name):
    steps = odb.steps
    keys = list(steps.keys())
    if not keys:
        raise ValueError("ODB has no steps")
    if not name:
        return steps[keys[-1]]
    for key in keys:
        if key == name or key.upper() == str(name).upper():
            return steps[key]
    raise ValueError("step not found: %s (available: %s)" % (name, keys))


def pick_instance(odb, name):
    if not name:
        return None
    insts = odb.rootAssembly.instances
    for key in insts.keys():
        if key == name or key.upper() == str(name).upper():
            return insts[key]
    raise ValueError("instance not found: %s (available: %s)" % (name, list(insts.keys())))


def mode_summary(odb, params):
    steps = []
    for name in odb.steps.keys():
        step = odb.steps[name]
        frames = step.frames
        first = frames[0] if len(frames) else None
        steps.append(
            {
                "name": name,
                "domain": str(getattr(step, "domain", "")),
                "procedure": str(getattr(step, "procedure", "")),
                "frame_count": len(frames),
                "frame_times": [to_num(f.frameValue) for f in frames][:200],
                "fields": list(first.fieldOutputs.keys()) if first is not None else [],
                "history": list(step.historyRegions.keys()) if hasattr(step, "historyRegions") else [],
            }
        )
    instances = []
    for name in odb.rootAssembly.instances.keys():
        inst = odb.rootAssembly.instances[name]
        try:
            nnodes, nelems = len(inst.nodes), len(inst.elements)
        except Exception:
            nnodes, nelems = None, None
        instances.append(
            {
                "name": name,
                "type": str(getattr(inst, "type", "")),
                "node_count": nnodes,
                "element_count": nelems,
                "node_sets": len(getattr(inst, "nodeSets", {})),
                "element_sets": len(getattr(inst, "elementSets", {})),
            }
        )
    asm_sets = list(odb.rootAssembly.nodeSets.keys())[:60]
    return {
        "steps": steps,
        "instances": instances,
        "assembly_node_sets": asm_sets,
        "assembly_element_sets": list(odb.rootAssembly.elementSets.keys())[:60],
        "part_instances": list(odb.rootAssembly.instances.keys()),
    }


def pick_field(frame, field_name, component):
    """Locate a field output; tolerate SDV written either as one field or as SDV1..SDVn."""
    keys = list(frame.fieldOutputs.keys())
    upper = dict((k.upper(), k) for k in keys)
    name = field_name or "S"
    if name.upper() in upper:
        return frame.fieldOutputs[upper[name.upper()]], component
    if name.upper().startswith("SDV") and "SDV" in upper:
        return frame.fieldOutputs[upper["SDV"]], name
    raise ValueError("field %s not in frame (available: %s)" % (name, keys))


def mode_field(odb, params):
    step = pick_step(odb, params.get("step"))
    inst = pick_instance(odb, params.get("instance"))
    frames = step.frames
    if not len(frames):
        raise ValueError("step has no frames")
    idx = params.get("frame")
    if idx is None or idx == "" or int(idx) < 0:
        frame = frames[-1]
        frame_index = len(frames) - 1
    else:
        frame_index = int(idx)
        frame = frames[frame_index]
    fo, auto_comp = pick_field(frame, params.get("field"), params.get("component"))
    if inst is not None:
        fo = fo.getSubset(region=inst)
    labels = [str(l) for l in (getattr(fo, "componentLabels", None) or [])]
    comp = params.get("component") or auto_comp or ""
    comp_index = None
    if comp:
        upper = [l.upper() for l in labels]
        if comp.upper() in upper:
            comp_index = upper.index(comp.upper())
        else:
            raise ValueError("component %s not in %s" % (comp, labels))
    lo = hi = None
    lo_id = hi_id = None
    total = 0
    samples = []
    limit = int(params.get("limit") or 0)
    for value in fo.values:
        scalar = value_scalar(value, comp_index)
        if scalar is None:
            continue
        total += 1
        label = getattr(value, "nodeLabel", None)
        if label is None:
            label = getattr(value, "elementLabel", None)
        if lo is None or scalar < lo:
            lo, lo_id = scalar, label
        if hi is None or scalar > hi:
            hi, hi_id = scalar, label
        if limit and len(samples) < limit:
            samples.append({"label": label, "value": scalar})
    return {
        "step": step.name,
        "frame": frame_index,
        "frame_value": to_num(frame.frameValue),
        "field": fo.name,
        "component": comp or "(magnitude)",
        "components": labels,
        "value_count": total,
        "min": {"value": lo, "label": lo_id},
        "max": {"value": hi, "label": hi_id},
        "samples": samples,
    }


def mode_node_series(odb, params):
    step = pick_step(odb, params.get("step"))
    inst = pick_instance(odb, params.get("instance"))
    wanted = set(int(n) for n in (params.get("nodes") or []))
    if not wanted:
        raise ValueError("nodes list is empty")
    frames = step.frames
    if not len(frames):
        raise ValueError("step has no frames")
    fo0, auto_comp = pick_field(frames[0], params.get("field"), params.get("component"))
    labels = [str(l) for l in (getattr(fo0, "componentLabels", None) or [])]
    comp = params.get("component") or auto_comp or ""
    comp_index = None
    if comp:
        upper = [l.upper() for l in labels]
        if comp.upper() in upper:
            comp_index = upper.index(comp.upper())
        else:
            raise ValueError("component %s not in %s" % (comp, labels))
    series = []
    resolved = fo0.name
    for i, frame in enumerate(frames):
        try:
            fo, _ = pick_field(frame, resolved, "")
        except ValueError:
            continue
        if inst is not None:
            fo = fo.getSubset(region=inst)
        row = {}
        for value in fo.values:
            label = getattr(value, "nodeLabel", None)
            if label is None or int(label) not in wanted:
                continue
            row[str(int(label))] = value_scalar(value, comp_index)
        series.append({"frame": i, "time": to_num(frame.frameValue), "values": row})
    return {
        "step": step.name,
        "field": fo0.name,
        "component": comp or "(magnitude)",
        "components": labels,
        "node_labels": sorted(wanted),
        "series": series,
    }


def mode_history(odb, params):
    step = pick_step(odb, params.get("step"))
    variable = params.get("variable") or ""
    region_name = params.get("region") or ""
    node = params.get("nodes")
    node_label = int(node[0]) if node else None
    regions = step.historyRegions
    keys = list(regions.keys())
    if not keys:
        raise ValueError("step has no history regions")
    chosen = []
    if region_name:
        for key in keys:
            if key == region_name or key.upper() == str(region_name).upper():
                chosen = [regions[key]]
                break
        if not chosen:
            raise ValueError("history region not found: %s (available: %s)" % (region_name, keys[:20]))
    else:
        for key in keys:
            if node_label is not None and ("Node %d" % node_label) in key:
                chosen.append(regions[key])
        if not chosen:
            chosen = [regions[keys[0]]]
    out = []
    for reg in chosen:
        names = list(reg.historyOutputs.keys())
        picked = []
        for nm in names:
            if variable and variable.upper() not in nm.upper():
                continue
            picked.append(nm)
        if not picked:
            picked = names[:5]
        for nm in picked:
            ho = reg.historyOutputs[nm]
            points = []
            for pair in ho.data:
                try:
                    t_val, vals = pair[0], pair[1]
                except Exception:
                    continue
                arr = as_floats(vals)
                points.append([to_num(t_val), arr[0] if len(arr) == 1 else arr])
            out.append(
                {
                    "region": reg.name,
                    "variable": nm,
                    "points": points[:5000],
                    "point_count": len(points),
                }
            )
    return {"step": step.name, "history_outputs": out, "regions": keys[:40]}


def _xy_value(frame, index, spec, odb, inst, cache):
    """One x or y value of an XY curve spec, for modes 'time|frame|node|ratio_min|field_max|field_min|rf_pos_sum|constant'."""
    kind = (spec.get("kind") or "").lower()
    if kind == "time":
        return to_num(frame.frameValue)
    if kind == "frame":
        return float(index)
    if kind in ("node", "ratio_min"):
        field_name = spec.get("field") or "U"
        comp = int(spec.get("comp", 0))
        fo, _auto = pick_field(frame, field_name, "")
        if inst is not None:
            fo = fo.getSubset(region=inst)
        node_label = int(spec["node"])
        scalar = None
        for value in fo.values:
            label = getattr(value, "nodeLabel", None)
            if label is not None and int(label) == node_label:
                vals = as_floats(value.data)
                if comp < len(vals):
                    scalar = vals[comp]
                break
        if scalar is None:
            return None
        if kind == "node":
            return scalar
        ref = float(spec.get("ref", 1.98))
        a = ref + scalar
        if a <= 0:
            return None
        return 2.0 * math.log(ref / a)
    if kind in ("field_max", "field_min"):
        fo, auto_comp = pick_field(frame, spec.get("field"), "")
        if inst is not None:
            fo = fo.getSubset(region=inst)
        best = None
        for value in fo.values:
            scalar = value_scalar(value, None)
            if scalar is None:
                continue
            if best is None or (scalar > best if kind == "field_max" else scalar < best):
                best = scalar
        return best
    if kind == "rf_pos_sum":
        fo, _auto = pick_field(frame, "RF", "")
        if inst is not None:
            fo = fo.getSubset(region=inst)
        comp = int(spec.get("comp", 1))
        total = 0.0
        for value in fo.values:
            vals = as_floats(value.data)
            if comp < len(vals) and vals[comp] > 0.0:
                total += vals[comp]
        return total
    if kind == "constant":
        return float(spec["value"])
    raise ValueError("unknown xy spec kind: %r" % (spec.get("kind"),))


def mode_xy(odb, params):
    step = pick_step(odb, params.get("step"))
    inst = pick_instance(odb, params.get("instance"))
    frames = step.frames
    out_series = []
    warnings = []
    for spec in params.get("series") or []:
        label = spec.get("label") or "curve"
        sx = spec.get("x") or {}
        sy = spec.get("y") or {}
        if str(sx.get("kind", "")).lower() == "vline":
            out_series.append({"label": label, "vline": float(sx["value"])})
            continue
        xs, ys = [], []
        for i, frame in enumerate(frames):
            xv = _xy_value(frame, i, sx, odb, inst, {})
            yv = _xy_value(frame, i, sy, odb, inst, {})
            if xv is None or yv is None:
                continue
            xs.append(float(xv))
            ys.append(float(yv))
        if len(xs) < 2:
            warnings.append("%s: only %d points" % (label, len(xs)))
            continue
        sc = sy.get("scale")
        if sc:
            ys = [v * float(sc) for v in ys]
        out_series.append({"label": label, "x": xs, "y": ys})
    return {
        "step": step.name,
        "frame_count": len(frames),
        "series": out_series,
        "warnings": warnings,
    }


MODES = {
    "summary": mode_summary,
    "field": mode_field,
    "node_series": mode_node_series,
    "history": mode_history,
    "xy": mode_xy,
}

_TEXT = type(u"")


def dump_json(obj):
    """json.dumps -> unicode, compatible with Python 2.7 io.open()."""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if not isinstance(s, _TEXT):
        s = s.decode("utf-8", "replace")
    return s


def to_native(obj):
    """Abaqus APIs want byte strings on Python 2.7; json gives unicode."""
    if isinstance(obj, _TEXT):
        return obj.encode("utf-8")
    if isinstance(obj, list):
        return [to_native(x) for x in obj]
    if isinstance(obj, dict):
        return dict((to_native(k), to_native(v)) for k, v in obj.items())
    return obj


def main(argv):
    params_path, out_path = argv[1], argv[2]
    with io.open(params_path, "r", encoding="utf-8") as fh:
        params = to_native(json.load(fh))
    result = {"ok": True}
    odb = None
    try:
        odb = openOdb(params["odb"], readOnly=True)
        mode = params.get("mode") or "summary"
        if mode not in MODES:
            raise ValueError("unknown mode: %s" % mode)
        result.update(MODES[mode](odb, params))
        result["odb_path"] = params["odb"]
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
