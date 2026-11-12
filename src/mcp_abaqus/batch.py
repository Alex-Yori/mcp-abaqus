"""Parameter sweeps: render one deck per case, keep a queue, launch up to N at a time.

Abaqus itself has no queue on Windows, so the queue is a JSON file per batch and
the scheduler is *reconciled* on submission and batch_status calls: count what is actually
running (pid + .lck), launch pending cases into free slots. That keeps the batch
alive across MCP server restarts without a daemon.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from .config import Settings, ToolError, job_name_ok
from .launcher import process_state, submit as _submit, stop_job as _stop_job

_COPY_SUFFIXES = {".f", ".for", ".f90", ".inc", ".f77"}
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _batches_dir(settings: Settings) -> Path:
    d = settings.workdir / ".mcp-batches"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _batch_path(settings: Settings, batch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", batch_id):
        raise ToolError("batch_id must be alphanumeric/._-")
    return _batches_dir(settings) / f"{batch_id}.json"


def _save(settings: Settings, batch: dict) -> None:
    batch["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _batch_path(settings, batch["batch_id"]).write_text(
        json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load(settings: Settings, batch_id: str) -> dict:
    p = _batch_path(settings, batch_id)
    if not p.is_file():
        raise ToolError(f"batch not found: {batch_id} ({[q.stem for q in _batches_dir(settings).glob('*.json')]})")
    return json.loads(p.read_text(encoding="utf-8"))


def list_batches(settings: Settings) -> list[dict]:
    out = []
    for p in sorted(_batches_dir(settings).glob("*.json"), key=lambda q: -q.stat().st_mtime):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "batch_id": b.get("batch_id"),
                "created": b.get("created"),
                "cases": len(b.get("cases", [])),
                "template": b.get("template"),
            }
        )
    return out


def render(text: str, params: dict) -> str:
    """Substitute ``{{NAME}}`` slots. Pure text substitution: no deck parsing, no card
    reordering - the rendered deck is the template byte-for-byte except at the slots.

    Values are not escaped or formatted, so a value containing a newline injects a whole
    line (which can become a real keyword card) and an empty value leaves an empty data
    card. Both are the caller's problem, not silently repaired here.

    ``str(value)`` IS the contract: ``100.0`` renders as ``100.0``, ``1e-5`` as
    ``1e-05``, ``100`` as ``100``. Switching to a format spec would silently change every
    deck ever rendered from a template; ``tests/lint_render.py`` pins these three.
    """
    return _PLACEHOLDER.sub(lambda m: str(params[m.group(1)]), text)


def validate_params(name: str, params: dict) -> None:
    """Reject values that would leave a second-order placeholder in the deck.

    `render` does one pass only, so a value containing `{{...}}` lands in the deck
    verbatim as something that looks like an unfilled slot but is really a typo.
    """
    for key, value in params.items():
        s = str(value)
        if "{{" in s or "}}" in s:
            raise ToolError(
                f"case {name!r}: value for {key!r} contains a placeholder brace "
                f"({s!r}); it would appear in the deck as an unrendered {{...}}"
            )


_MAX_RENDERED_LINE = 256


def render_warnings(name: str, rendered: str, params: dict, template_keys: set[str] | None = None) -> list[dict]:
    """Advisory only: legal text/values that usually mean an accident.

    Nothing here is repaired - a warning says "look at this before you trust the sweep".
    """
    out: list[dict] = []
    for key, value in params.items():
        s = str(value)
        if "\n" in s:
            out.append(
                {"case": name, "kind": "multiline_value", "key": key, "lines_added": s.count("\n")}
            )
    if template_keys is not None:
        # The complement of the missing-key check: the template never reads these, so the
        # case runs with the template's value while the author believes it swept them.
        unused = sorted(set(params) - template_keys)
        if unused:
            out.append({"case": name, "kind": "unused_param", "keys": unused})
    for i, line in enumerate(rendered.splitlines(), 1):
        if len(line) > _MAX_RENDERED_LINE:
            out.append({"case": name, "kind": "long_line", "line": i, "chars": len(line)})
    return out


def _warn_value(v):
    """Hashable stand-in for a warning field (lists/dicts are legal values here)."""
    if isinstance(v, (list, tuple)):
        return tuple(v)
    if isinstance(v, dict):
        return tuple(sorted(v.items()))
    return v


def dedupe_warnings(items: list[dict], max_names: int = 5) -> list[dict]:
    """Collapse case-identical warnings so one long template line is not reported N times.

    Identity is *every* field except the case name - a field whitelist silently merges
    warnings that differ in a field nobody remembered to add (a 1-line injection and a
    3-line injection are not the same finding). Each merged entry keeps the case names
    it stands for, bounded so a 200-case sweep cannot flood the response.
    """
    out: dict[tuple, dict] = {}
    for w in items:
        key = tuple(
            sorted(
                (k, _warn_value(v)) for k, v in w.items() if k not in {"case", "cases", "case_names"}
            )
        )
        cur = out.get(key)
        if cur is None:
            entry = {k: v for k, v in w.items() if k != "case"}
            entry["case_names"] = [w["case"]] if w.get("case") else []
            entry["cases"] = 1
            out[key] = entry
            continue
        cur["cases"] += 1
        name = w.get("case")
        if name and name not in cur["case_names"]:
            cur["case_names"].append(name)
    for entry in out.values():
        names = entry["case_names"]
        if len(names) > max_names:
            entry["case_names"] = names[:max_names] + [f"…+{len(names) - max_names} more"]
    return list(out.values())


def warning_summary(batch: dict, limit: int = 10) -> dict:
    warnings = dedupe_warnings(batch.get("render_warnings") or [])
    priority = {"unused_param": 0, "multiline_value": 1, "long_line": 2}
    warnings.sort(key=lambda warning: priority.get(warning["kind"], 3))
    return {
        "render_warnings": warnings[:limit],
        "render_warnings_total": len(warnings),
        "render_warnings_truncated": len(warnings) > limit,
    }


def warning_page(settings: Settings, batch_id: str, offset: int = 0, limit: int = 50, case_name: str = "") -> dict:
    if offset < 0 or not 1 <= limit <= 100:
        raise ToolError("offset must be >= 0 and limit must be between 1 and 100")
    batch = load(settings, batch_id)
    if case_name and not any(case["name"] == case_name for case in batch["cases"]):
        raise ToolError(f"unknown case: {case_name!r}")
    warnings = [
        warning for warning in batch.get("render_warnings") or []
        if not case_name or warning.get("case") == case_name
    ]
    page = warnings[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "batch_id": batch_id,
        "case_name": case_name or None,
        "warnings": page,
        "total": len(warnings),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < len(warnings) else None,
    }


def create(
    settings: Settings,
    *,
    template: Path,
    cases: list[dict],
    cpus: int,
    max_parallel: int,
    options: dict | None,
    user_subroutine: Path | None,
    batch_id: str = "",
) -> dict:
    if not template.is_file():
        raise ToolError(f"template deck not found: {template}")
    if not cases:
        raise ToolError("cases list is empty; pass e.g. [{\"name\":\"E20\",\"E\":\"200000\"}]")
    if max_parallel < 1:
        raise ToolError("max_parallel must be >= 1")

    text = template.read_text(encoding="utf-8", errors="replace")
    keys_in_template = set(_PLACEHOLDER.findall(text))
    if not keys_in_template:
        raise ToolError(
            "template has no {{placeholder}} tokens; add them (e.g. {{E}}) so each case can differ"
        )

    entries = []
    names: dict[str, str] = {}
    missing: set[str] = set()
    for raw in cases:
        if not isinstance(raw, dict):
            raise ToolError("each case must be an object, e.g. {\"name\": \"E200\", \"E\": \"200000\"}")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ToolError("each case needs a 'name'")
        if not job_name_ok(name) or name.endswith(".") or PureWindowsPath(name).is_reserved():
            raise ToolError(f"invalid case name {name!r}: use alphanumeric/._-, no trailing dot or Windows reserved name")
        identity = name.casefold()
        if identity in names:
            raise ToolError(f"case names must be unique ignoring case: {names[identity]!r} and {name!r}")
        names[identity] = name
        params = {key: value for key, value in raw.items() if key != "name"}
        validate_params(name, params)
        missing |= keys_in_template - params.keys()
        entries.append({"name": name, "params": params})
    if missing:
        raise ToolError(f"cases are missing template placeholders: {sorted(missing)}")

    batch_id = batch_id or f"b{time.strftime('%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    batch_path = _batch_path(settings, batch_id)
    if batch_path.exists():
        raise ToolError(f"batch_id already exists: {batch_id!r}; use a new batch_id")
    root = settings.resolve_path(str(template.parent / f"_batch_{batch_id}"))
    if root.exists():
        raise ToolError(f"batch directory already exists: {root}; use a new batch_id")

    warn_items: list[dict] = []
    rendered_decks = {}
    for entry in entries:
        rendered = render(text, entry["params"])
        warn_items.extend(render_warnings(entry["name"], rendered, entry["params"], keys_in_template))
        rendered_decks[entry["name"]] = rendered

    root.mkdir(parents=True, exist_ok=False)
    for entry in entries:
        case_dir = root / entry["name"]
        case_dir.mkdir(exist_ok=False)
        for src in template.parent.iterdir():
            if src.is_file() and src.suffix.lower() in _COPY_SUFFIXES:
                shutil.copy2(src, case_dir / src.name)
        inp = case_dir / f"{entry['name']}.inp"
        inp.write_text(rendered_decks[entry["name"]], encoding="utf-8")
        entry.update(
            {
                "dir": str(case_dir),
                "inp": str(inp),
                "job": entry["name"],
                "state": "pending",
                "pid": 0,
                "submitted_at": None,
                "note": "",
            }
        )

    batch = {
        "batch_id": batch_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "template": str(template),
        "root": str(root),
        "cpus": int(cpus),
        "max_parallel": int(max_parallel),
        "options": options or {},
        "user_subroutine": str(user_subroutine) if user_subroutine else "",
        "render_warnings": warn_items,
        "cases": entries,
    }
    _save(settings, batch)
    reconcile(settings, batch)
    return batch


def _case_running(case: dict) -> bool:
    wd = Path(case["dir"])
    st = process_state(wd, case["job"])
    return bool(st["pid_alive"] or st["lck_present"])


def reconcile(settings: Settings, batch: dict) -> dict:
    """Launch pending cases into free slots; mark finished ones."""
    cases = batch["cases"]
    if batch.get("cancelled"):
        return batch
    running = 0
    for case in cases:
        if case["state"] == "cancelled":
            continue
        if case["state"] in {"running", "finished"}:
            if _case_running(case):
                if case["state"] != "running":
                    case["state"] = "running"
                running += 1
            elif case["state"] == "running":
                case["state"] = "finished"
                case["note"] = "process exited"
    for case in cases:
        if running >= int(batch["max_parallel"]):
            break
        if case["state"] != "pending":
            continue
        try:
            meta = _submit(
                settings,
                inp=Path(case["inp"]),
                job=case["job"],
                cpus=int(batch["cpus"]),
                user_subroutine=Path(batch["user_subroutine"]) if batch.get("user_subroutine") else None,
                options=batch.get("options") or None,
                overwrite=True,
            )
            case.update({"state": "running", "pid": meta["pid"], "submitted_at": meta["launched_at"]})
            running += 1
        except Exception as exc:  # noqa: BLE001 - one bad case must not kill the batch
            case.update({"state": "failed", "note": str(exc)[:400]})
    _save(settings, batch)
    return batch


def stop(settings: Settings, batch_id: str) -> dict:
    batch = load(settings, batch_id)
    stopped = []
    for case in batch["cases"]:
        if case["state"] == "running" or _case_running(case):
            try:
                stopped.append({"case": case["name"], **_stop_job(settings, workdir=Path(case["dir"]), job=case["job"])})
            except Exception as exc:  # noqa: BLE001
                stopped.append({"case": case["name"], "error": str(exc)})
        if _case_running(case):
            case["state"] = "running"
            case["note"] = "cancellation requested, but process or lock is still present"
        elif case["state"] in {"running", "pending"}:
            case["state"] = "cancelled"
    batch["cancelled"] = True
    _save(settings, batch)
    return {"batch_id": batch_id, "stopped": stopped, "cases": batch["cases"]}
