"""Generic channel for running a CAE script under `abaqus cae noGUI=` and reading its JSON.

Two Windows/Abaqus facts shape this module:
  * `abaqus cae` only accepts ONE option token ``noGUI=<script>`` - extra argv is rejected or
    dropped, so paths are handed to the script through a generated runner that sets globals;
  * noGUI has no display, so anything that shows geometry must ``import visualization`` first.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from .config import Settings, ToolError
from .launcher import run_blocking


def run_cae(
    settings: Settings,
    script: Path,
    params: dict,
    *,
    timeout: int = 900,
    cwd: Path | None = None,
) -> dict:
    """Run *script* in a fresh CAE session; it reads params.json and writes out.json."""
    if not script.is_file():
        raise ToolError(f"CAE script not found: {script}")
    tmp = settings.workdir / ".cae-run"
    tmp.mkdir(parents=True, exist_ok=True)
    run_dir = tmp / uuid.uuid4().hex[:8]
    run_dir.mkdir()
    try:
        copy = run_dir / script.name
        shutil.copyfile(script, copy)
        params_path = run_dir / "params.json"
        out_path = run_dir / "out.json"
        params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

        runner = run_dir / "runner.py"
        runner.write_text(
            os.linesep.join(
                [
                    "# -*- coding: utf-8 -*-",
                    "PARAMS_PATH = r'%s'" % params_path.as_posix(),
                    "OUT_PATH = r'%s'" % out_path.as_posix(),
                    "execfile(r'%s')" % copy.as_posix(),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        cp = run_blocking(settings, ["cae", "noGUI=%s" % runner.as_posix()], timeout=timeout, cwd=cwd)
        if not out_path.is_file():
            detail = (cp["stderr"] or cp["stdout"] or "")[-2500:]
            raise ToolError(f"CAE produced no result (rc={cp['returncode']}). {detail}".strip())
        result = json.loads(out_path.read_text(encoding="utf-8"))
        if not result.get("ok", False):
            raise ToolError(f"CAE script failed: {result.get('error', 'unknown')}")
        result.pop("ok", None)
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
