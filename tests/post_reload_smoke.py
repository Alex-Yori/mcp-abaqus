"""Post-reload smoke: prove the four datalint render fields survive the MCP wire.

Unlike tests/lint_*.py (in-process), this spawns the server over stdio exactly like a
client does and reads the JSON payloads a client would see. Run with the venv python:

    .venv/Scripts/python tests/post_reload_smoke.py

Submits 3 tiny real jobs (2 s each) from tests/fixtures/mixed_batch/mixed_tmpl.inp.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
FIX = HERE / "tests" / "fixtures" / "mixed_batch" / "mixed_tmpl.inp"
WORK_ROOT = HERE / "abq_jobs" / "smoke_relooad"
MFS = "*Fixed Mass Scaling, factor=100"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def payload(result) -> dict:
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


async def call(session: ClientSession, name: str, **kw) -> dict:
    res = await session.call_tool(name, kw, read_timeout_seconds=900)
    return payload(res)


async def main() -> int:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    WORK = WORK_ROOT / (time.strftime("run-%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    WORK.mkdir(parents=True)
    tmpl = WORK / "smoke_tmpl.inp"
    shutil.copy2(FIX, tmpl)

    env = dict(os.environ)
    env["ABAQUS_MCP_ROOTS"] = os.pathsep.join([str(WORK), str(HERE / "abq_jobs"), str(Path.home())])
    env["ABAQUS_MCP_WORKDIR"] = str(HERE / "abq_jobs")
    params = StdioServerParameters(command=str(PY), args=["-m", "mcp_abaqus"], env=env, cwd=str(HERE))

    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("\n== 1) submit_batch: render_warnings at submit time ==")
            sub = await call(
                session,
                "submit_batch",
                template_inp=str(tmpl),
                cases=[
                    {"name": "S01", "INJECT": "", "MS": MFS, "DISP": "0.01"},
                    {"name": "S02", "INJECT": MFS + "\n", "MS": "", "DISP": "0.01"},
                    {"name": "S03", "INJECT": "", "MS": MFS, "DISP": "0.02", "TEMPERATURE": "350"},
                ],
                cpus=1,
                max_parallel=2,
            )
            print(json.dumps(sub, ensure_ascii=False)[:700])
            batch_id = sub.get("batch_id")
            rw = sub.get("render_warnings") or []
            kinds = {w.get("kind") for w in rw}
            check("submit_batch returns render_warnings", "render_warnings" in sub)
            check("multiline_value reported", "multiline_value" in kinds, str(kinds))
            check("unused_param reported", "unused_param" in kinds, str(kinds))
            check("unused key named", any("TEMPERATURE" in (w.get("keys") or []) for w in rw), str(rw))
            check("render warning total is explicit", sub.get("render_warnings_total") == 2, str(sub))
            check("small warning summary is not truncated", sub.get("render_warnings_truncated") is False)
            warning_page = await call(session, "batch_warnings", batch_id=batch_id, case_name="S03")
            check("read-only warning query preserves case and key",
                  warning_page.get("total") == 1
                  and warning_page.get("warnings", [{}])[0].get("keys") == ["TEMPERATURE"], str(warning_page))

            print("\n== 2) batch_status: datalint / suspicious_cases / datalint_truncated ==")
            st: dict = {}
            for _ in range(120):
                st = await call(session, "batch_status", batch_id=batch_id)
                if st.get("done"):
                    break
                await asyncio.sleep(5)
            print(json.dumps({k: v for k, v in st.items() if k != "cases"}, ensure_ascii=False)[:700])
            check("batch_status has suspicious_cases", "suspicious_cases" in st)
            check("batch_status has datalint_truncated", "datalint_truncated" in st)
            check("nothing stalled (unknown state means killed/incomplete)",
                  st.get("stalled") == [], str(st.get("stalled")))
            check("all 3 cases completed", st.get("summary") == {"completed": 3}, str(st.get("summary")))
            check("batch_status == ['S02']", st.get("suspicious_cases") == ["S02"], str(st.get("suspicious_cases")))
            rows = {r.get("case"): r for r in (st.get("cases") or [])}
            check("per-case datalint present", all("datalint" in r for r in rows.values()), str(list(rows)))
            check("S01 datalint empty", (rows.get("S01") or {}).get("datalint") == [], str(rows.get("S01")))
            check(
                "S02 datalint hits FIXED MASS SCALING",
                any("FIXED MASS SCALING" in (f.get("keyword") or "") for f in (rows.get("S02") or {}).get("datalint") or []),
                str(rows.get("S02")),
            )
            check("S03 clean (only a param typo, deck itself fine)", (rows.get("S03") or {}).get("datalint") == [])

            print("\n== 3) datacheck: verdict/suspicious/datalint on one deck ==")
            dc = await call(session, "datacheck", inp=str(WORK / ("_batch_" + str(batch_id)) / "S02" / "S02.inp"))
            print(json.dumps({k: dc.get(k) for k in ("verdict", "suspicious", "error_count", "datalint")}, ensure_ascii=False)[:600])
            check("datacheck verdict still passed", dc.get("verdict") == "passed", str(dc.get("verdict")))
            check("datacheck suspicious true", dc.get("suspicious") is True)
            check("datacheck datalint non-empty", len(dc.get("datalint") or []) == 1)

            print("\n== 4) datalint_unchecked: a completed case whose .dat is gone ==")
            check("all completed -> nothing unchecked", st.get("datalint_unchecked") == [], str(st.get("datalint_unchecked")))
            victim = WORK / ("_batch_" + str(batch_id)) / "S03" / "S03.dat"
            saved = victim.read_bytes() if victim.is_file() else b""
            if saved:
                victim.unlink()
            try:
                after = await call(session, "batch_status", batch_id=batch_id)
                check("deleted .dat is reported, not silently clean",
                      after.get("datalint_unchecked") == ["S03"], str(after.get("datalint_unchecked")))
                row = next((r for r in (after.get("cases") or []) if r.get("case") == "S03"), {})
                check("... with dat_present false", row.get("dat_present") is False, str(row))
            finally:
                if saved:
                    victim.write_bytes(saved)

    print(f"\n{'OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
