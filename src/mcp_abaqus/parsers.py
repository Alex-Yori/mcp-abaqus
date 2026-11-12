"""Parsers for Abaqus status/error artifacts: .sta, .msg, .dat, console logs.

Everything here is pure (path in, dict out) so it is unit-testable without a
solver. Abaqus writes .sta rows for both solvers but with different columns,
so the solver is sniffed from the header line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

_STD_ROW = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)"
)
_EXP_ROW = re.compile(r"^\s*(\d+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([0-9:.]+)\s+([-+0-9.Ee]+)")
_MSG_START = re.compile(r"^\s*\*\*\*(ERROR|WARNING|NOTE)", re.IGNORECASE)
_COMPLETED = "THE ANALYSIS HAS COMPLETED SUCCESSFULLY"
_NOT_COMPLETED = "THE ANALYSIS HAS NOT BEEN COMPLETED"


def _read_lines(path: Path, limit: int | None = None) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    return lines[-limit:] if limit else lines


def parse_sta(path: Path) -> dict:
    """Summarize an Abaqus .sta file (Standard increment table or Explicit frame table)."""
    if not path.is_file():
        return {"present": False}
    lines = _read_lines(path)
    solver = None
    for line in lines[:15]:
        low = line.lower()
        if "abaqus/standard" in low:
            solver = "Standard"
            break
        if "abaqus/explicit" in low:
            solver = "Explicit"
            break
    increments: list[dict] = []
    for line in lines:
        m = _STD_ROW.match(line)
        if m:
            increments.append(
                {
                    "step": int(m.group(1)),
                    "increment": int(m.group(2)),
                    "attempt": int(m.group(3)),
                    "severe_discon": int(m.group(4)),
                    "equil_iters": int(m.group(6)),
                    "total_time": float(m.group(7)),
                    "step_time": float(m.group(8)),
                    "dt": float(m.group(9)),
                }
            )
            continue
        m = _EXP_ROW.match(line)
        if m and solver == "Explicit":
            increments.append(
                {
                    "increment": int(m.group(1)),
                    "total_time": float(m.group(2)),
                    "step_time": float(m.group(3)),
                    "dt": float(m.group(5)),
                    "attempt": 1,
                }
            )
    text = "\n".join(lines)
    completed = True if _COMPLETED in text else (False if _NOT_COMPLETED in text else None)
    steps: dict[str, dict] = {}
    for inc in increments:
        s = steps.setdefault(str(inc.get("step") or 1), {"increments": 0, "last_total_time": 0.0})
        s["increments"] += 1
        s["last_total_time"] = inc["total_time"]
    tail = [ln.strip() for ln in lines[-6:] if ln.strip()]
    return {
        "present": True,
        "solver": solver,
        "increment_count": len(increments),
        "last": increments[-1] if increments else None,
        "steps": steps,
        "completed": completed,
        "tail": tail,
    }


def parse_messages(path: Path, kind_limit: int = 10) -> dict:
    """Count and collect ***ERROR / ***WARNING / ***NOTE blocks from .msg/.dat/.sta."""
    if not path.is_file():
        return {"present": False}
    lines = _read_lines(path)
    counts = {"ERROR": 0, "WARNING": 0, "NOTE": 0}
    items: list[dict] = []
    i = 0
    while i < len(lines):
        m = _MSG_START.match(lines[i])
        if not m:
            i += 1
            continue
        kind = m.group(1).upper()
        counts[kind] = counts.get(kind, 0) + 1
        if kind != "NOTE" and len(items) < kind_limit:
            body = [lines[i].strip()]
            j = i + 1
            while j < len(lines) and len(body) < 6:
                nxt = lines[j]
                if _MSG_START.match(nxt) or not nxt.strip():
                    break
                body.append(nxt.strip())
                j += 1
            items.append({"kind": kind, "line": i + 1, "text": " ".join(body)})
        i += 1
    return {"present": True, "counts": counts, "items": items, "line_count": len(lines)}


def tail_file(path: Path, lines: int = 40, pattern: str | None = None) -> dict:
    """Return the last N lines, optionally only lines matching a regex."""
    if not path.is_file():
        return {"present": False, "path": str(path)}
    raw = _read_lines(path)
    out: list[str]
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        out = [ln for ln in raw if rx.search(ln)][-lines:]
    else:
        out = raw[-lines:]
    return {
        "present": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "mtime": path.stat().st_mtime,
        "line_count": len(raw),
        "lines": out,
    }


# Abaqus announces silently-dropped input lines as a ***WARNING in the .dat echo
# area, e.g. "*Fixed Mass Scaling inserted between *Boundary and its data lines":
#      ***WARNING: DATA CARDS AFTER *FIXED MASS SCALING  ARE IGNORED
# The run still completes normally, so the job looks healthy while the BC it
# dropped never loaded (symptom: uniform u on the whole part). These anchors are
# deliberately narrow: a bare "ARE IGNORED" would match the *BOUNDARY,TYPE=
# DISPLACEMENT note that every multi-step deck prints, and that false positive
# would flag every case in a sweep.
_DAT_LINT_RULES = (
    (
        "input_lines_dropped",
        # Character class must cover everything Abaqus can put in a keyword name:
        # a keyword it fails to match whole makes the regex miss the line entirely
        # (silent under-report, never a false positive).
        re.compile(r"DATA CARDS AFTER \*([A-Z0-9 ,=_\-./()]+?)\s+ARE IGNORED", re.IGNORECASE),
        "Abaqus ignored the data lines belonging to that keyword, so this case did not "
        "load what the deck asked for",
    ),
)

LINT_MAX_FINDINGS = 20


class LintResult(NamedTuple):
    """`truncated`: matches beyond the cap were dropped.
    `present`: the .dat was actually read - an empty `findings` with `present=False`
    means "cannot know", not "clean" (a deleted .dat must not look like a pass).
    """

    findings: list[dict]
    truncated: bool
    present: bool


def lint_dat(path: Path, max_findings: int = LINT_MAX_FINDINGS) -> LintResult:
    """Flag .dat statements that mean the model silently differs from the deck.

    Scans the whole file: these statements sit in the model-echo area, which can be
    past the ``kind_limit`` window that :func:`parse_messages` keeps.
    """
    if not path.is_file():
        return LintResult([], False, False)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LintResult([], False, False)
    findings: list[dict] = []
    seen: set[str] = set()
    truncated = False
    for rule_id, rx, why in _DAT_LINT_RULES:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            key = f"{rule_id}:{line}"
            if key in seen:
                continue
            seen.add(key)
            if len(findings) >= max_findings:
                truncated = True
                break
            findings.append(
                {
                    "rule": rule_id,
                    "line": line,
                    "keyword": (m.group(1) or "").strip(),
                    "text": " ".join(m.group(0).split()),
                    "why": why,
                }
            )
        if truncated:
            break
    return LintResult(findings, truncated, True)
