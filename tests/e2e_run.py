"""Live run test: datacheck -> submit -> poll -> stop, driven through MCP tool calls.

    .venv/Scripts/python tests/e2e_run.py

Copies a real deck into abq_jobs/run_test so the project tree is never touched.
Checks both the happy path and the "Abaqus exits 0 but the job failed" path.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
SRC = Path(r"D:\files\workProject\Explicit algorithm\WithH\Lin2024_reproduction")

BROKEN = """*Heading
deliberately broken deck: element without a material/section
*Node
1, 0., 0.
2, 1., 0.
3, 1., 1.
4, 0., 1.
*Element, type=CPE4
1, 1, 2, 3, 4
*Step, name=Step-1
*Static
0.1, 1., 1e-05, 0.1
*End Step
"""

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def payload(res) -> dict:
    text = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


async def call(session: ClientSession, name: str, **kwargs) -> dict:
    return payload(await session.call_tool(name, kwargs, read_timeout_seconds=900))


async def main() -> int:
    work = HERE / "abq_jobs" / "run_test"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for f in SRC.iterdir():
        if f.suffix.lower() in {".inp", ".f"}:
            shutil.copy2(f, work / f.name)
    (work / "broken.inp").write_text(BROKEN, encoding="ascii")
    deck = work / "check_diffusion.inp"
    sub = work / "VUMATCGMAXH.f"

    env = dict(os.environ)
    env["ABAQUS_MCP_ROOTS"] = os.pathsep.join([str(HERE / "abq_jobs"), str(Path.home())])
    env["ABAQUS_MCP_WORKDIR"] = str(HERE / "abq_jobs")
    params = StdioServerParameters(command=str(PY), args=["-m", "mcp_abaqus"], env=env, cwd=str(HERE))

    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1) broken input deck must be reported as a failure (exit code is 0 regardless)
            dc = await call(session, "datacheck", inp=str(work / "broken.inp"))
            print("broken datacheck:", json.dumps(dc)[:500])
            check("broken deck -> verdict failed", dc.get("verdict") == "failed", json.dumps(dc)[:300])
            check("broken deck -> evidence present", bool(dc.get("evidence")), json.dumps(dc)[:300])

            # 2) real deck, double precision (single precision aborts on this model)
            t0 = time.time()
            dc = await call(session, "datacheck", inp=str(deck), user_subroutine=str(sub), options={"double": "both"})
            print(f"real datacheck took {time.time() - t0:.1f}s -> {json.dumps(dc)[:400]}")
            check("real deck -> verdict passed", dc.get("verdict") == "passed", json.dumps(dc)[:400])
            check("real datacheck wrote .dat", "dat" in json.dumps(dc.get("warnings", {})))

            # 3) submit -> poll -> must complete
            s = await call(
                session,
                "submit_job",
                inp=str(deck),
                job_name="lc_run",
                cpus=2,
                user_subroutine=str(sub),
                options={"double": "both"},
            )
            print("submit:", json.dumps(s)[:400])
            check("submit ok", s.get("ok") is True, json.dumps(s)[:300])
            check("submit returned pid", bool(s.get("pid")))

            state = None
            for _ in range(60):
                await asyncio.sleep(5)
                st = await call(session, "job_status", job_name="lc_run", workdir=str(work))
                state = st.get("state")
                if state in {"completed", "aborted", "error"}:
                    break
            print("final status:", json.dumps(st)[:900])
            check("job completed", state == "completed", str(state))
            check("status counted increments", (st.get("increment_count") or 0) > 0, str(st.get("increment_count")))

            tail = await call(session, "tail_output", job_name="lc_run", workdir=str(work), file="console", lines=6)
            check("console log readable", tail.get("ok") is True and tail.get("lines"), json.dumps(tail)[:200])

            # 4) the failed run must be reported as an error state, not "completed"
            bad = await call(session, "submit_job", inp=str(work / "broken.inp"), job_name="lc_bad", overwrite=True)
            check("broken submit ok", bad.get("ok") is True, json.dumps(bad)[:300])
            await asyncio.sleep(20)
            bst = await call(session, "job_status", job_name="lc_bad", workdir=str(work))
            print("broken status:", json.dumps(bst)[:600])
            check("broken run -> error state", bst.get("state") in {"error", "aborted"}, str(bst.get("state")))

            # 5) stop a running job
            s2 = await call(
                session,
                "submit_job",
                inp=str(deck),
                job_name="lc_stop",
                cpus=2,
                user_subroutine=str(sub),
                options={"double": "both"},
                overwrite=True,
            )
            check("second submit ok", s2.get("ok") is True, json.dumps(s2)[:300])
            await asyncio.sleep(8)
            stop = await call(session, "stop_job", job_name="lc_stop", workdir=str(work))
            print("stop:", json.dumps(stop)[:700])
            check("stop_job reports stopped", stop.get("stopped") is True, json.dumps(stop)[:400])

    print("\n" + ("RUN CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
