"""Numeric table extraction from Abaqus .dat/.msg text reports.

Abaqus prints free-format tables (load-displacement from *PRINT, CGM/SDV dumps
from WRITE(7), energy summaries). There is no schema, so blocks are detected
structurally: runs of lines that are entirely numbers, with the nearest
non-numeric line above them kept as the table title.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .config import ToolError

MAX_LINES = 2_000_000


def _floats(line: str) -> list[float] | None:
    tokens = line.replace("D", "E").replace("d", "e").split()
    if not tokens:
        return None
    values: list[float] = []
    for tok in tokens:
        try:
            values.append(float(tok))
        except ValueError:
            return None
    return values


def _stats(rows: list[list[float]], ncols: int) -> list[dict]:
    out = []
    for i in range(ncols):
        col = [r[i] for r in rows if i < len(r)]
        if not col:
            out.append({})
            continue
        out.append({"min": min(col), "max": max(col), "last": col[-1]})
    return out


def scan_tables(path: Path, *, min_cols: int = 2, max_blocks: int = 40) -> list[dict]:
    """Detect numeric blocks; returns summaries (title, line range, column stats)."""
    if not path.is_file():
        raise ToolError(f"file not found: {path}")
    blocks: list[dict] = []
    title = ""
    start = 0
    rows: list[list[float]] = []
    blanks = 0
    prev_non_numeric = ""

    def flush(end_line: int) -> None:
        nonlocal rows, start, title
        if rows and len(blocks) < max_blocks:
            ncols = max(len(r) for r in rows)
            blocks.append(
                {
                    "index": len(blocks),
                    "title": title[:120],
                    "start_line": start,
                    "end_line": end_line,
                    "columns": ncols,
                    "row_count": len(rows),
                    "first": rows[0],
                    "last": rows[-1],
                    "stats": _stats(rows, ncols),
                }
            )
        rows = []

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if lineno > MAX_LINES:
                break
            stripped = line.strip()
            vals = _floats(stripped)
            if vals is not None and len(vals) >= min_cols:
                if not rows:
                    start = lineno
                    title = prev_non_numeric
                rows.append(vals)
                blanks = 0
                continue
            if not stripped:
                blanks += 1
                if rows and blanks > 2:
                    flush(lineno - blanks)
                continue
            flush(lineno - 1)
            blanks = 0
            prev_non_numeric = stripped
        flush(lineno)
    return blocks


def read_block(path: Path, index: int, *, min_cols: int = 2, max_rows: int = 5000) -> dict:
    blocks = scan_tables(path, min_cols=min_cols, max_blocks=max(index + 1, 40))
    if index < 0 or index >= len(blocks):
        raise ToolError(f"block {index} not found ({len(blocks)} numeric blocks; call without `block` to list them)")
    meta = blocks[index]
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if lineno < meta["start_line"]:
                continue
            if lineno > meta["end_line"]:
                break
            vals = _floats(line.strip())
            if vals is not None and len(vals) >= min_cols:
                rows.append(vals)
    truncated = len(rows) > max_rows
    shown = rows[:max_rows]
    return {
        **{k: v for k, v in meta.items() if k not in ("first", "last")},
        "rows": shown,
        "truncated": truncated,
        "stats": _stats(rows, meta["columns"]),
    }


def export_csv(dest: Path, header: list[str] | None, rows: list[list[float]]) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if header:
            writer.writerow(header)
        writer.writerows(rows)
    return str(dest)
