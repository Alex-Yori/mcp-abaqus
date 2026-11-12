"""Run the Python-2.7 ODB extractor inside `abaqus python` and hand back its JSON."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path

from .config import Settings, ToolError
from .launcher import run_blocking

log = logging.getLogger("mcp_abaqus.odb")

_EXTRACTOR = Path(__file__).with_name("odb_extract.py")


def run_odb(settings: Settings, odb_path: Path, mode: str, params: dict, timeout: int | None = None) -> dict:
    if not odb_path.is_file():
        raise ToolError(f"ODB not found: {odb_path}")
    tmp = settings.workdir / ".odb-run"
    tmp.mkdir(parents=True, exist_ok=True)
    run_dir = tmp / uuid.uuid4().hex[:8]
    run_dir.mkdir()
    try:
        script = run_dir / "odb_extract.py"
        shutil.copyfile(_EXTRACTOR, script)
        payload = {"odb": str(odb_path), "mode": mode}
        payload.update({k: v for k, v in params.items() if v not in (None, "")})
        params_path = run_dir / "params.json"
        out_path = run_dir / "out.json"
        params_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        budget = timeout or settings.odb_timeout_s
        cp = run_blocking(settings, [], timeout=budget, python_script=[str(script), str(params_path), str(out_path)])
        if not out_path.is_file() or out_path.stat().st_size == 0:
            detail = (cp["stderr"] or cp["stdout"] or "")[-1500:]
            raise ToolError(f"ODB extractor produced no output (rc={cp['returncode']}). {detail}".strip())
        result = json.loads(out_path.read_text(encoding="utf-8"))
        if not result.get("ok", False):
            raise ToolError(f"ODB read failed: {result.get('error', 'unknown error')}")
        result.pop("ok", None)
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
