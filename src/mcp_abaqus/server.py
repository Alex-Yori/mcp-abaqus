"""MCP tool surface for driving Abaqus jobs and reading ODB results.

Design rules:
  * every tool returns one JSON string; failures are ``{"ok": false, "error": ...}``
    instead of exceptions, so the calling model always gets an actionable message;
  * nothing is printed to stdout (stdio is the protocol channel) - logs go to stderr;
  * every user-supplied path passes the sandbox in ``config.Settings.resolve_path``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import axisym
from . import batch as batch_mod
from .cae_model import build_cae_model
from .config import Settings, ToolError, job_name_ok
from .contour import render_contour
from .xy import render_xy
from .part import build_part
from .dat import export_csv, read_block, scan_tables
from .launcher import (
    console_failures,
    console_tail,
    process_state,
    restart as _restart,
    run_blocking,
    stop_job as _stop_job,
    submit as _submit,
)
from .launcher import option_strings
from .odb import run_odb
from .parsers import lint_dat, parse_messages, parse_sta, tail_file

logging.basicConfig(
    level=os.environ.get("ABAQUS_MCP_LOG_LEVEL", "INFO").upper(),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcp_abaqus")

server = MCPServer(
    name="abaqus",
    version="0.1.0",
    instructions=(
        "Drive local Abaqus (2018) jobs: validate decks, submit, watch .sta/.msg/.dat, "
        "stop jobs, and read ODB field/history results without opening CAE. "
        "Paths are confined to ABAQUS_MCP_ROOTS."
    ),
)


# --------------------------------------------------------------------- helpers
def _ok(**payload) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, default=str)


def _err(exc: BaseException) -> str:
    log.warning("tool error: %s", exc)
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def _settings() -> Settings:
    return Settings.load()


def _find_job_dir(settings: Settings, job: str, workdir: str) -> Path:
    """Resolve the directory holding a job's artifacts (explicit > default workdir > roots).

    A directory holding .sta/.odb wins over one that only has the .inp: copies of a
    deck are everywhere, completed runs are not.
    """
    if workdir:
        return settings.resolve_path(workdir, must_exist=True)
    best: tuple[int, float, Path] | None = None
    for base in (settings.workdir, *settings.roots):
        for cand, score in _scan_for_job(base, job):
            mtime = max(
                (p.stat().st_mtime for ext in ("sta", "odb", "inp") if (p := cand / f"{job}.{ext}").is_file()),
                default=0.0,
            )
            if score >= 3:
                return cand
            if best is None or (score, mtime) > (best[0], best[1]):
                best = (score, mtime, cand)
    if best is not None:
        return best[2]
    raise ToolError(f"job '{job}' not found under {[str(r) for r in settings.roots]}; pass workdir explicitly")


def _scan_for_job(base: Path, job: str, max_depth: int = 3, max_dirs: int = 4000) -> list[tuple[Path, int]]:
    """Directories under *base* that carry <job> artifacts, with a 3/1 score (run / deck only)."""
    base = base.resolve()
    skip = {"node_modules", ".git", ".venv", "__pycache__", "site-packages", ".mcp-run"}
    hits: list[tuple[Path, int]] = []
    seen = 0
    for root, dirs, files in os.walk(base):
        seen += 1
        if seen > max_dirs:
            break
        depth = len(Path(root).relative_to(base).parts)
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        if any(f"{job}.{ext}" in files for ext in ("sta", "odb")):
            hits.append((Path(root), 3))
        elif f"{job}.inp" in files:
            hits.append((Path(root), 1))
    return hits


def _status_snapshot(settings: Settings, workdir: Path, job: str) -> dict:
    sta = parse_sta(workdir / f"{job}.sta")
    msg = parse_messages(workdir / f"{job}.msg", kind_limit=8)
    dat_path = workdir / f"{job}.dat"
    dat = parse_messages(dat_path, kind_limit=5)
    proc = process_state(workdir, job)
    running = bool(proc["pid_alive"] or proc["lck_present"])

    def count(name: str, kind: str = "ERROR") -> int:
        block = {"msg": msg, "dat": dat}.get(name, {})
        return int((block.get("counts") or {}).get(kind, 0))

    # Abaqus writes the same message to both .msg and .dat, and this server's own
    # dropped-input-line warning lands in .dat only. Max (not sum) keeps a single
    # real error from being reported twice, and max on both files keeps .dat-only
    # warnings visible instead of reporting warnings=0 next to a non-empty datalint.
    errors = max(count("msg"), count("dat"))
    warnings = max(count("msg", "WARNING"), count("dat", "WARNING"))
    lint = lint_dat(dat_path)
    datalint, datalint_truncated, dat_present = lint.findings, lint.truncated, lint.present

    if sta.get("completed") is True:
        state = "completed"
    elif sta.get("completed") is False:
        state = "aborted"
    elif (count("msg") or count("dat")) and not running:
        state = "error"
    elif running:
        state = "running"
    elif (
        sta.get("present") or msg.get("present") or dat.get("present")
        or proc.get("launched_at") or proc.get("pid")
        or (workdir / f"{job}.console.log").is_file()
    ):
        state = "unknown"
    else:
        state = "not_started"

    con_lines = console_tail(workdir, job, lines=20)
    con_errs = console_failures("\n".join(con_lines))
    if state not in {"completed", "running"} and con_errs:
        state = "error"

    files = {}
    for ext in ("inp", "sta", "msg", "dat", "log", "odb", "lck", "console.log"):
        p = workdir / f"{job}.{ext}"
        if p.exists():
            st = p.stat()
            files[ext] = {"size_bytes": st.st_size, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}

    return {
        "job": job,
        "workdir": str(workdir),
        "state": state,
        "running": running,
        "process": proc,
        "last_increment": sta.get("last"),
        "increment_count": sta.get("increment_count"),
        "solver": sta.get("solver"),
        "sta_tail": sta.get("tail"),
        "errors": errors,
        "warnings": warnings,
        "datalint": datalint,
        "datalint_truncated": datalint_truncated,
        "dat_present": dat_present,
        "messages": msg.get("items", []) + dat.get("items", []),
        "console_errors": con_errs[:6],
        "console_tail": con_lines[-6:],
        "files": files,
    }


# ----------------------------------------------------------------------- tools
@server.tool(
    description=(
        "Check the Abaqus installation this server drives: launcher path, toolchain env script, "
        "job workspace, allowed path roots, default CPU count. Call this first when a job fails to start."
    )
)
def abaqus_environment() -> str:
    try:
        s = _settings()
        exe = s.resolve_abaqus()
        return _ok(
            launcher=exe,
            launcher_exists=Path(exe).is_file(),
            env_script=s.env_bat,
            env_script_exists=bool(s.env_bat and Path(s.env_bat).is_file()),
            workdir=str(s.workdir),
            allowed_roots=[str(r) for r in s.roots],
            default_cpus=s.default_cpus,
            odb_timeout_s=s.odb_timeout_s,
            python=sys.executable,
            platform=sys.platform,
            license_env={k: v for k, v in os.environ.items() if "LICENSE" in k.upper() or k.upper().startswith("LM_")},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as JSON
        return _err(exc)


@server.tool(
    description=(
        "Validate an input deck without solving: runs `abaqus job=<name>_datacheck datacheck` synchronously "
        "and returns a verdict plus the .dat/.msg ERROR blocks and console failures. Takes 10s-3min; use it "
        "before a long run. Abaqus exits 0 even when it fails, so the verdict is judged from the artifacts. "
        "Decks needing double precision must pass options={\"double\": \"both\"} (single precision can abort "
        "with 'zero or negative capacitance'). Also lints the .dat echo for silently dropped input lines "
        "(`suspicious` / `datalint`): a deck can pass datacheck while a keyword swallowed another keyword's "
        "data lines, e.g. *Fixed Mass Scaling inserted inside a *Boundary block."
    )
)
def datacheck(inp: str, user_subroutine: str = "", options: dict | None = None, timeout_s: int = 0) -> str:
    try:
        s = _settings()
        inp_path = s.resolve_path(inp, must_exist=True)
        job = f"{inp_path.stem}_datacheck"
        args = [f"job={job}", f"input={inp_path.name}", "interactive", "datacheck", "ask_delete=OFF"]
        args.extend(option_strings(options))
        if user_subroutine:
            sub = s.resolve_path(user_subroutine, must_exist=True)
            args.append(f"user={sub.name}")
        cp = run_blocking(s, args, timeout=timeout_s or s.check_timeout_s, cwd=inp_path.parent)
        dat_path = inp_path.parent / f"{job}.dat"
        dat = parse_messages(dat_path, kind_limit=12)
        msg = parse_messages(inp_path.parent / f"{job}.msg", kind_limit=8)
        errors = (dat.get("counts") or {}).get("ERROR", 0) + (msg.get("counts") or {}).get("ERROR", 0)
        console_errs = cp["failures"]
        # Abaqus returns 0 even on failure: a missing .dat or a console error is a failure.
        failed = bool(console_errs) or errors > 0 or not dat_path.is_file()
        evidence = console_errs or [
            item["text"] for item in dat.get("items", []) + msg.get("items", []) if item["kind"] == "ERROR"
        ]
        # A deck can validate cleanly and still have dropped input lines (Abaqus only
        # warns). Kept out of `verdict` on purpose: the run is valid, it is just not
        # the model the deck describes.
        lint = lint_dat(dat_path)
        datalint, datalint_truncated, dat_present = lint.findings, lint.truncated, lint.present
        return _ok(
            job=job,
            workdir=str(inp_path.parent),
            returncode=cp["returncode"],
            verdict="failed" if failed else "passed",
            suspicious=bool(datalint),
            error_count=errors,
            evidence=evidence[:10],
            datalint=datalint,
            datalint_truncated=datalint_truncated,
            dat_present=dat_present,
            dat_messages=dat.get("items", []),
            msg_messages=msg.get("items", []),
            warnings={
                "dat": (dat.get("counts") or {}).get("WARNING", 0),
                "msg": (msg.get("counts") or {}).get("WARNING", 0),
            },
            console_tail=(cp["stdout"] or "").splitlines()[-15:],
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Submit an .inp deck as a detached Abaqus job (interactive mode, console output to <job>.console.log). "
        "Returns immediately with pid + workdir; poll with job_status. Refuses to overwrite an existing job "
        "unless overwrite=true."
    )
)
def submit_job(
    inp: str,
    job_name: str = "",
    cpus: int = 0,
    user_subroutine: str = "",
    options: dict | None = None,
    overwrite: bool = False,
    extra_cli: list[str] | None = None,
) -> str:
    try:
        s = _settings()
        inp_path = s.resolve_path(inp, must_exist=True)
        job = job_name or inp_path.stem
        sub = s.resolve_path(user_subroutine, must_exist=True) if user_subroutine else None
        meta = _submit(
            s,
            inp=inp_path,
            job=job,
            cpus=cpus or s.default_cpus,
            user_subroutine=sub,
            options=options,
            overwrite=overwrite,
            extra_cli=extra_cli,
        )
        return _ok(
            submitted=True,
            job=job,
            workdir=str(inp_path.parent),
            pid=meta["pid"],
            cpus=meta["cpus"],
            cmdline=meta["cmdline"],
            console_log=meta["console_log"],
            hint=".sta appears within ~10-60s; call job_status(job) to follow progress.",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Status of a submitted job: state (running/completed/aborted/error), last increment, total step time, "
        "error+warning counts with the offending message lines, and artifact sizes."
    )
)
def job_status(job_name: str, workdir: str = "") -> str:
    try:
        s = _settings()
        wd = _find_job_dir(s, job_name, workdir)
        return _ok(**_status_snapshot(s, wd, job_name))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Tail a job artifact: file='sta' (progress table), 'msg' (solver messages), 'dat' (input processing), "
        "'log' (abaqus launcher log) or 'console' (stdout of this server's launch). Optional regex filter."
    )
)
def tail_output(job_name: str, workdir: str = "", file: str = "sta", lines: int = 40, pattern: str = "") -> str:
    try:
        s = _settings()
        wd = _find_job_dir(s, job_name, workdir)
        ext = "console.log" if file == "console" else file.strip().lstrip(".")
        if ext not in {"sta", "msg", "dat", "log", "console.log", "prt"}:
            raise ToolError("file must be one of: sta, msg, dat, log, console, prt")
        info = tail_file(wd / f"{job_name}.{ext}", lines=max(1, min(lines, 2000)), pattern=pattern or None)
        if not info.get("present"):
            raise ToolError(f"{job_name}.{ext} not found in {wd}")
        return _ok(**info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "List Abaqus jobs (grouped by .inp/.sta/.odb stem) in a directory, with state, last increment and "
        "newest artifact time. Defaults to the server's job workspace."
    )
)
def list_jobs(path: str = "", limit: int = 20) -> str:
    try:
        s = _settings()
        base = s.resolve_path(path, must_exist=True) if path else s.workdir
        stems: dict[str, dict] = {}
        for entry in sorted(base.iterdir()):
            if not entry.is_file():
                continue
            stem, dot, ext = entry.name.rpartition(".")
            if not dot or ext not in {"inp", "sta", "odb", "msg", "lck"}:
                continue
            rec = stems.setdefault(stem, {"job": stem, "exts": [], "mtime": 0.0})
            rec["exts"].append(ext)
            rec["mtime"] = max(rec["mtime"], entry.stat().st_mtime)
        out = []
        for rec in sorted(stems.values(), key=lambda r: -r["mtime"])[:limit]:
            snap = _status_snapshot(s, base, rec["job"])
            out.append(
                {
                    "job": rec["job"],
                    "state": snap["state"],
                    "last_increment": snap["last_increment"],
                    "errors": snap["errors"],
                    "files": sorted(rec["exts"]),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["mtime"])),
                }
            )
        return _ok(directory=str(base), count=len(out), jobs=out)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Stop a running job: `abaqus terminate`, then kill the process tree if it ignores that. "
        "Removes a stale .lck left behind."
    )
)
def stop_job(job_name: str, workdir: str = "") -> str:
    try:
        s = _settings()
        wd = _find_job_dir(s, job_name, workdir)
        return _ok(**_stop_job(s, workdir=wd, job=job_name))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Summarize an ODB without CAE: steps (frames, frame times, available field names), instances "
        "(node/element counts), node sets and history regions. Reads through `abaqus python`, so it takes ~20s."
    )
)
def odb_summary(odb: str) -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        return _ok(odb=str(p), **run_odb(s, p, "summary", {}))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Min/max of one field component in one frame of an ODB, with the node/element label where it occurs "
        "(e.g. field='S', component='MISES', frame=-1 for the last frame; field='U', component='U2'). "
        "Use `instance` to restrict to one part instance."
    )
)
def odb_field_extremes(
    odb: str,
    field: str = "S",
    component: str = "",
    step: str = "",
    frame: int = -1,
    instance: str = "",
    samples: int = 0,
) -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        params = {"field": field, "component": component, "step": step, "instance": instance, "limit": samples}
        params["frame"] = int(frame)
        return _ok(odb=str(p), **run_odb(s, p, "field", params))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Time history of one field component at fixed nodes across all frames of a step - the crack-tip "
        "hydrogen/stress trace. Example: field='SDV', component='SDV19', nodes=[1234], instance='PART-1-1'; "
        "or field='U', component='U2' for load-point displacement."
    )
)
def odb_node_series(
    odb: str,
    nodes: list[int],
    field: str = "S",
    component: str = "",
    step: str = "",
    instance: str = "",
) -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        params = {"field": field, "component": component, "step": step, "instance": instance, "nodes": nodes}
        return _ok(odb=str(p), **run_odb(s, p, "node_series", params))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Read history output from an ODB step (e.g. ALLKE, energy components, reaction forces at a node). "
        "Filter with variable substring and/or region name; node=<label> picks that node's region."
    )
)
def odb_history(odb: str, variable: str = "", region: str = "", node: int = 0, step: str = "") -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        params = {"variable": variable, "region": region, "step": step, "nodes": [int(node)] if node else []}
        return _ok(odb=str(p), **run_odb(s, p, "history", params))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --------------------------------------------------- reports / sweeps / images
@server.tool(
    description=(
        "Find and dump numeric tables from a .dat/.msg report (CGM load-displacement from *PRINT, SDV dumps, "
        "energy summaries). Without `block` it lists detected blocks with title, line range and per-column "
        "min/max/last; with `block=<index>` it returns rows and can export the whole block to CSV."
    )
)
def dat_tables(
    path: str = "",
    job_name: str = "",
    workdir: str = "",
    block: int = -1,
    rows: int = 30,
    min_cols: int = 2,
    to_csv: str = "",
) -> str:
    try:
        s = _settings()
        if path:
            target = s.resolve_path(path, must_exist=True)
        elif job_name:
            target = _find_job_dir(s, job_name, workdir) / f"{job_name}.dat"
            if not target.is_file():
                raise ToolError(f"{job_name}.dat not found in {target.parent}")
        else:
            raise ToolError("pass either path=<file.dat|file.msg> or job_name=")
        if block < 0:
            blocks = scan_tables(target, min_cols=min_cols)
            slim = [{**b, "first": b["first"][:8], "last": b["last"][:8]} for b in blocks]
            return _ok(
                file=str(target),
                count=len(blocks),
                blocks=slim,
                hint="pass block=<index> to dump rows; add to_csv to export the whole block",
            )
        data = read_block(target, block, min_cols=min_cols, max_rows=max(1, rows))
        payload: dict = {"file": str(target), **data}
        if to_csv:
            dest = s.resolve_path(to_csv, base=target.parent)
            full = read_block(target, block, min_cols=min_cols, max_rows=1_000_000)
            header = [f"c{i + 1}" for i in range(full["columns"])]
            payload["csv"] = export_csv(dest, header, full["rows"])
            payload["csv_rows"] = len(full["rows"])
        return _ok(**payload)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Parameter sweep: render one deck per case from a template containing {{placeholders}}, then run them "
        "through a queue with at most max_parallel at a time. Each case gets its own folder and job name; the "
        "queue is reconciled on submission and batch_status calls, so it survives a server restart. Use cpus=1 for decks with "
        "very few elements: Abaqus/Explicit cannot split a tiny mesh into several domains and the packager "
        "aborts with 'requested number of domains cannot be created', which reads like a broken model."
    )
)
def submit_batch(
    template_inp: str,
    cases: list[dict],
    cpus: int = 0,
    max_parallel: int = 1,
    user_subroutine: str = "",
    options: dict | None = None,
    batch_id: str = "",
) -> str:
    try:
        s = _settings()
        tpl = s.resolve_path(template_inp, must_exist=True)
        sub = s.resolve_path(user_subroutine, must_exist=True, base=tpl.parent) if user_subroutine else None
        b = batch_mod.create(
            s,
            template=tpl,
            cases=cases,
            cpus=cpus or s.default_cpus,
            max_parallel=max_parallel,
            options=options,
            user_subroutine=sub,
            batch_id=batch_id,
        )
        return _ok(
            batch_id=b["batch_id"],
            root=b["root"],
            cases=len(b["cases"]),
            launched=[c["name"] for c in b["cases"] if c["state"] == "running"],
            pending=sum(1 for c in b["cases"] if c["state"] == "pending"),
            **batch_mod.warning_summary(b),
            hint="poll with batch_status(batch_id=...) - each call also launches freed slots",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


_TERMINAL_STATES = {"completed", "failed", "cancelled", "error", "aborted"}
# `unknown` = artifacts exist but there is no completion marker (hard-killed job, reboot,
# licence lost after preprocessing). It will not change on its own, so it is settled - but
# it did NOT finish, which is what `stalled` says. One field, one meaning.
_SETTLED_STATES = _TERMINAL_STATES | {"unknown"}


def _batch_rollup(rows: list[dict]) -> dict:
    """Aggregate per-case rows: each field answers exactly one question."""
    summary: dict = {}
    for row in rows:
        summary[row["state"]] = summary.get(row["state"], 0) + 1
    return {
        "summary": summary,
        "suspicious_cases": [r["case"] for r in rows if r.get("datalint")],
        "datalint_unchecked": [
            r["case"] for r in rows
            if r.get("dat_present") is False and r["state"] in _SETTLED_STATES
            and r.get("audit_required", True)
        ],
        "datalint_truncated": any(r.get("datalint_truncated") for r in rows),
        "stalled": [r["case"] for r in rows if r["state"] == "unknown"],
        "done": all(r["state"] in _SETTLED_STATES for r in rows),
    }


@server.tool(
    description=(
        "Progress of a parameter sweep: per-case state, increments, errors and a state summary. Calling it "
        "also starts pending cases as slots free up. Each case carries `datalint` (dropped input lines in "
        "its .dat) and `suspicious_cases` lists those cases: a case can read completed while what it "
        "loaded is not what the template asked for. `datalint_unchecked` names settled cases whose .dat "
        "is gone, so an empty datalint cannot be mistaken for a clean deck. Includes a bounded render "
        "warning summary; use batch_warnings for the complete, read-only, paginated warning list."
    )
)
def batch_status(batch_id: str) -> str:
    try:
        s = _settings()
        b = batch_mod.reconcile(s, batch_mod.load(s, batch_id))
        rows = []
        for case in b["cases"]:
            if case["state"] == "pending":
                rows.append({"case": case["name"], "state": case["state"], "note": case.get("note", "")})
                continue
            snap = _status_snapshot(s, Path(case["dir"]), case["job"])
            started = bool(
                case.get("submitted_at") or case.get("pid")
                or case["state"] in {"running", "finished"}
                or snap["state"] != "not_started"
                or any(extension != "inp" for extension in snap["files"])
            )
            state = snap["state"]
            if snap["running"]:
                state = "running"
            elif case["state"] in {"failed", "cancelled"}:
                state = case["state"]
            elif state == "not_started" and started:
                state = "unknown"
            rows.append(
                {
                    "case": case["name"],
                    "state": state,
                    "queue_state": case["state"],
                    "started": started,
                    "audit_required": started or case["state"] == "failed",
                    "increment_count": snap["increment_count"],
                    "last": snap["last_increment"],
                    "errors": snap["errors"],
                    "datalint": snap["datalint"],
                    "datalint_truncated": snap["datalint_truncated"],
                    "dat_present": snap["dat_present"],
                    "dir": case["dir"],
                    "note": case.get("note", ""),
                }
            )
        return _ok(
            batch_id=batch_id, root=b.get("root"), cases=rows,
            **_batch_rollup(rows), **batch_mod.warning_summary(b),
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Read saved render warnings without reconciling the queue or starting jobs. Returns raw per-case "
        "warnings (including every case name), total count and next_offset. Optional exact case_name filter. "
        "offset must be >= 0; limit is 1-100. Unlike summary counts, total counts unmerged occurrences."
    )
)
def batch_warnings(batch_id: str, offset: int = 0, limit: int = 50, case_name: str = "") -> str:
    try:
        return _ok(**batch_mod.warning_page(_settings(), batch_id, offset, limit, case_name))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(description="List parameter-sweep batches known to this server (newest first).")
def list_batches() -> str:
    try:
        s = _settings()
        return _ok(batches=batch_mod.list_batches(s))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(description="Cancel a parameter sweep: stop running cases and mark the rest cancelled.")
def stop_batch(batch_id: str) -> str:
    try:
        s = _settings()
        return _ok(**batch_mod.stop(s, batch_id))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Restart an interrupted job. mode='recover' continues the same job from its .res (deck needs "
        "*RESTART, WRITE); mode='oldjob' starts new_job_name from an .inp containing *RESTART, READ."
    )
)
def restart_job(
    job_name: str,
    workdir: str = "",
    mode: str = "recover",
    new_job_name: str = "",
    inp: str = "",
    cpus: int = 0,
    user_subroutine: str = "",
    options: dict | None = None,
) -> str:
    try:
        s = _settings()
        wd = _find_job_dir(s, job_name, workdir)
        inp_path = s.resolve_path(inp, must_exist=True, base=wd) if inp else None
        sub = s.resolve_path(user_subroutine, must_exist=True, base=wd) if user_subroutine else None
        meta = _restart(
            s,
            workdir=wd,
            job=job_name,
            mode=mode,
            new_job=new_job_name,
            inp=inp_path,
            cpus=cpus or s.default_cpus,
            user_subroutine=sub,
            options=options,
        )
        return _ok(**meta)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Render one ODB frame to a PNG contour through `abaqus cae noGUI` (no CAE window). Pick field and "
        "component/invariant (e.g. field='S', component='MISES'; field='SDV19'). Takes ~30-60s and returns "
        "the PNG path, which can be shown to the user directly."
    )
)
def odb_contour(
    odb: str,
    field: str = "S",
    component: str = "",
    step: str = "",
    frame: int = -1,
    output: str = "",
    overwrite: bool = False,
) -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        tag = field + (f"_{component}" if component else "")
        default_name = f"{p.stem}_{tag}_f{'last' if frame < 0 else frame}.png"
        if output:
            out = s.resolve_path(output, base=s.workdir)
        else:
            out = s.workdir / default_name
        if out.exists():
            if not overwrite:
                raise ToolError(f"output exists: {out}; pass overwrite=true")
            out.unlink()
        info = render_contour(s, p, out_png=out, field=field, component=component, step=step, frame=frame)
        return _ok(odb=str(p), **info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Plot XY curves from an ODB: points are read by `abaqus python`, the figure is drawn with matplotlib "
        "(real axis titles, DPI, multi-curve). Each series is {label, x:{...}, y:{...}} where a spec is: "
        "{'kind':'time'}, {'kind':'frame'}, {'kind':'node','node':1,'comp':0}, "
        "{'kind':'ratio_min','node':1,'comp':0,'ref':1.98} (2*ln(ref/(ref+u))), "
        "{'kind':'field_max'|'field_min','field':'SDV19'}, {'kind':'rf_pos_sum','comp':1}, "
        "{'kind':'constant','value':590}, {'kind':'vline','value':1.575} (vertical reference line, x only). "
        "Example: strain vs stress  x={'kind':'ratio_min','node':1,'comp':0,'ref':1.98}, "
        "y={'kind':'rf_pos_sum','comp':1}. Takes ~5-20s for a few hundred frames."
    )
)
def odb_xy(
    odb: str,
    series: list[dict],
    xlabel: str = "",
    ylabel: str = "",
    title: str = "",
    step: str = "",
    image_size: str = "1400x1000",
    dpi: int = 200,
    output: str = "",
    overwrite: bool = False,
) -> str:
    try:
        s = _settings()
        p = s.resolve_path(odb, must_exist=True)
        if output:
            out = s.resolve_path(output, base=s.workdir)
        else:
            out = s.workdir / f"{p.stem}_xy.png"
        if out.exists():
            if not overwrite:
                raise ToolError(f"output exists: {out}; pass overwrite=true")
            out.unlink()
        try:
            w, h = str(image_size).lower().split("x")
            size = (int(w), int(h))
        except Exception:
            raise ToolError("image_size must look like '1400x1000'")
        info = render_xy(
            s,
            p,
            series,
            out_png=out,
            xlabel=xlabel,
            ylabel=ylabel,
            title=title,
            step=step,
            image_size=size,
            dpi=int(dpi),
        )
        return _ok(odb=str(p), **info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Side-by-side table for several jobs: state, increments, total time, error count, ODB time, and "
        "optionally one field's min/max per job (with_odb=true, ~20s per job). Returns rows plus a markdown "
        "table ready to display."
    )
)
def compare_jobs(
    jobs: list[dict],
    field: str = "",
    component: str = "",
    instance: str = "",
    frame: int = -1,
    with_odb: bool = False,
) -> str:
    try:
        s = _settings()
        rows = []
        for item in jobs:
            if isinstance(item, dict):
                job = str(item.get("job") or "").strip()
                wd_arg = str(item.get("workdir") or "")
            else:
                job, wd_arg = str(item), ""
            if not job:
                raise ToolError("each entry needs a 'job' name")
            wd = _find_job_dir(s, job, wd_arg)
            snap = _status_snapshot(s, wd, job)
            last = snap.get("last_increment") or {}
            row = {
                "job": job,
                "state": snap["state"],
                "increment_count": snap["increment_count"],
                "total_time": last.get("total_time"),
                "errors": snap["errors"],
                "warnings": snap["warnings"],
                "workdir": str(wd),
                "odb_time": (snap["files"].get("odb") or {}).get("mtime"),
            }
            if with_odb and field:
                odb_path = wd / f"{job}.odb"
                if odb_path.is_file():
                    try:
                        ext = run_odb(
                            s,
                            odb_path,
                            "field",
                            {"field": field, "component": component, "instance": instance, "frame": int(frame)},
                        )
                        row["field"] = {
                            "name": ext.get("field"),
                            "component": ext.get("component"),
                            "frame_value": ext.get("frame_value"),
                            "min": ext.get("min"),
                            "max": ext.get("max"),
                        }
                    except Exception as exc:  # noqa: BLE001
                        row["field_error"] = str(exc)[:300]
                else:
                    row["field_error"] = "no .odb"
            rows.append(row)

        show_field = bool(with_odb and field)
        header = ["job", "state", "increments", "total_time", "errors"]
        if show_field:
            header += [f"{field} min", f"{field} max"]
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for row in rows:
            cells = [
                str(row["job"]),
                str(row["state"]),
                str(row["increment_count"]),
                str(row["total_time"]),
                str(row["errors"]),
            ]
            if show_field:
                fld = row.get("field") or {}
                cells += [
                    str((fld.get("min") or {}).get("value")),
                    str((fld.get("max") or {}).get("value")),
                ]
            lines.append("| " + " | ".join(cells) + " |")
        return _ok(count=len(rows), jobs=rows, markdown="\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Build a solid part from an engineering drawing. Read the drawing first (vision) and turn its "
        "dimensions into `spec`; this tool sketches the profile + holes + fillets, extrudes or revolves it, "
        "meshes it, and writes <name>_mesh.inp plus a preview PNG you can show back for confirmation. "
        "spec = {profile:{kind:rect|circle|poly,...}, holes:[...], rounds:[{r,at}], solid:{kind:extrude|revolve,...}, "
        "mesh:{size,elem}}: rect uses x0,y0,w,h; circle uses cx,cy,r; poly uses points:[[x,y],...]. "
        "revolve spins the profile about the sketch Y axis, so keep it entirely on one side of x=0, and "
        "rounds only apply to extrude (it blends the vertical corner edges). "
        "Units are the drawing's - nothing is rescaled."
    )
)
def part_builder(
    name: str,
    spec: dict,
    out_dir: str = "",
    source_image: str = "",
    save_cae: bool = False,
    preview: bool = True,
    timeout_s: int = 0,
) -> str:
    try:
        s = _settings()
        if out_dir:
            target = s.resolve_path(out_dir, base=s.workdir)
        else:
            target = s.workdir / "parts" / name
        img = s.resolve_path(source_image, must_exist=True) if source_image else None
        info = build_part(
            s,
            name=name,
            spec=spec,
            out_dir=target,
            source_image=img,
            save_cae=save_cae,
            preview=preview,
            timeout=timeout_s or None,
        )
        return _ok(**info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Build a runnable Abaqus deck for an axisymmetric (half) bar or notched bar, e.g. a drawing's "
        "tensile specimen: geometry is meshed with structured CAX4RT quads in Python, and the physics "
        "(material, VUMAT constants, BCs, step, output requests) is spliced verbatim from a template deck "
        "(everything after *End Assembly), so the validated setup is reused instead of re-invented. "
        "geometry = {kind:'axisym_notched_bar'|'axisym_plain_bar', root_radius, body_radius, half_length, "
        "notch_radius, notch_kind:'parabolic'|'circular'}; mesh = {n_radial, dz_notch, dz_far, growth}. "
        "The template's BCs reference the set names Bottom/Top/Left/right/All - they are regenerated here, "
        "so keep those names in the template. Copies *.f/*.for/*.inc next to the deck for datacheck. "
        "loading = {step_time, displacement, mass_scaling} rewrites those three values from the template "
        "(step time is the physical test duration, displacement is the top BC in mm)."
    )
)
def model_builder(
    name: str,
    geometry: dict,
    template_deck: str,
    mesh: dict | None = None,
    loading: dict | None = None,
    out_dir: str = "",
    copy_subroutines: bool = True,
    preview: bool = True,
) -> str:
    try:
        s = _settings()
        tpl = s.resolve_path(template_deck, must_exist=True)
        target = s.resolve_path(out_dir, base=s.workdir) if out_dir else s.workdir / "models" / name
        if not job_name_ok(name):
            raise ToolError("model name must be alphanumeric/._-")
        model = axisym.build_bar(geometry, mesh or {})
        sets = axisym.make_sets(model, geometry)
        tail = axisym.split_template(tpl)
        tail, loading_notes = axisym.apply_loading(tail, loading or {})
        deck = target / f"{name}.inp"
        info = axisym.write_deck(
            deck,
            name=name,
            model=model,
            sets=sets,
            physics_tail=tail,
            header_extra=f"template physics from {tpl.name}",
        )
        copied = []
        if copy_subroutines:
            for src in tpl.parent.iterdir():
                ascii_name = all(ord(ch) < 128 for ch in src.name)
                if ascii_name and src.is_file() and src.suffix.lower() in {".f", ".for", ".f90", ".inc"}:
                    dst = target / src.name
                    if not dst.exists():
                        dst.write_bytes(src.read_bytes())
                        copied.append(src.name)
        png = ""
        summary = axisym.summarise(geometry, model)
        if preview:
            png = axisym.plot_mesh(
                target / f"{name}_mesh.png", model, title=f"{name}: {summary['element_count']} elems"
            )
        return _ok(
            name=name,
            deck=str(deck),
            deck_size_bytes=deck.stat().st_size,
            physics_tail_from=str(tpl),
            set_sizes={k: len(v) for k, v in sets.items()},
            copied_subroutines=copied,
            preview_png=png,
            loading_applied=loading_notes,
            hint="run datacheck on the deck next",
            **summary,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@server.tool(
    description=(
        "Build an EDITABLE Abaqus/CAE model (.cae) plus a runnable deck (.inp) for an axisymmetric bar "
        "using the real CAE geometry kernel (sketch -> feature -> CAX4RT mesh). Unlike model_builder's "
        "pure-Python mesh (which imports as an orphan mesh), the .cae opens like a hand-drawn model and "
        "its sketch can be edited in the GUI (Features > Shell planar-1 > Edit). "
        "geometry = {r_gauge, r_grip, l_gauge, l_trans, l_end} for the standard smooth bar, or "
        "{points:[[r,y],...]} for any closed profile (e.g. notched; r>=0, must touch the axis r=0). "
        "The material card (DEPVAR names + VUMAT/VUMATHT constants) is spliced verbatim from "
        "template_deck's *Material block; sets Bottom/Top/Left/right/All are regenerated from the "
        "geometry. loading = {step_time, displacement, mass_scaling, amplitude:'smooth'|'linear', "
        "output_interval, initial_field, surface_bc} - defaults 1.0 s / 8 mm / x10 / smooth / 1e-3 / "
        "1.0 / 1.0. initial_field and surface_bc are the NORMALIZED CHEMICAL POTENTIAL (heat analogy), "
        "not Kelvin; surface_bc=null closes the hydrogen boundary. Copies *.f/*.for/*.inc next to the "
        "deck. Run datacheck with options={'double':'both'} next."
    )
)
def cae_model_builder(
    name: str,
    template_deck: str,
    geometry: dict | None = None,
    mesh: dict | None = None,
    loading: dict | None = None,
    out_dir: str = "",
    save_cae: bool = True,
    preview: bool = True,
    copy_subroutines: bool = True,
    timeout_s: int = 0,
) -> str:
    try:
        s = _settings()
        tpl = s.resolve_path(template_deck, must_exist=True)
        target = s.resolve_path(out_dir, base=s.workdir) if out_dir else s.workdir / "models" / name
        info = build_cae_model(
            s,
            name=name,
            geometry=geometry or {},
            template_deck=tpl,
            mesh=mesh or {},
            loading=loading or {},
            out_dir=target,
            save_cae=save_cae,
            preview=preview,
            copy_subroutines=copy_subroutines,
            timeout=timeout_s or None,
        )
        return _ok(**info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    log.info("starting abaqus MCP server (python %s)", sys.version.split()[0])
    server.run("stdio")


if __name__ == "__main__":
    main()
