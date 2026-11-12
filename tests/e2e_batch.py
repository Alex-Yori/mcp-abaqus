"""Live test: parameter sweep queue + restart, through MCP tool calls.

    .venv/Scripts/python tests/e2e_batch.py

Uses abq_jobs/sweep/template.inp ({{T}} placeholder) and rsdeck.inp (writes restart data).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
SWEEP = HERE / "abq_jobs" / "sweep"

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
    return payload(await session.call_tool(name, kwargs, read_timeout_seconds=1800))


async def main() -> int:
    env = dict(os.environ)
    env["ABAQUS_MCP_ROOTS"] = os.pathsep.join([str(HERE / "abq_jobs"), str(Path.home())])
    env["ABAQUS_MCP_WORKDIR"] = str(HERE / "abq_jobs")
    params = StdioServerParameters(command=str(PY), args=["-m", "mcp_abaqus"], env=env, cwd=str(HERE))

    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            batch_id = "btest-" + uuid4().hex[:8]

            started = await call(
                session,
                "submit_batch",
                template_inp=str(SWEEP / "template.inp"),
                cases=[{"name": "T002", "T": "0.002"}, {"name": "T003", "T": "0.003"}],
                cpus=2,
                max_parallel=1,
                user_subroutine=str(SWEEP / "VUMATCGMAXH.f"),
                options={"double": "both"},
                batch_id=batch_id,
            )
            print("submit_batch:", json.dumps(started)[:500])
            check("batch created", started.get("ok") is True, json.dumps(started)[:300])
            check("first case launched", len(started.get("launched") or []) == 1, json.dumps(started)[:300])
            check("second case queued", started.get("pending") == 1, json.dumps(started)[:300])

            done = False
            status = {}
            for _ in range(60):
                await asyncio.sleep(10)
                status = await call(session, "batch_status", batch_id=batch_id)
                if status.get("done"):
                    done = True
                    break
            print("batch_status:", json.dumps(status)[:900])
            check("batch finished", done, json.dumps(status.get("summary")))
            states = {row["case"]: row["state"] for row in status.get("cases", [])}
            check("both cases completed", states == {"T002": "completed", "T003": "completed"}, str(states))

            batches = await call(session, "list_batches")
            check("list_batches sees it", any(b.get("batch_id") == batch_id for b in batches.get("batches", [])))

            # restart: run a restart-enabled deck, kill it, then recover
            sub = await call(
                session,
                "submit_job",
                inp=str(SWEEP / "rsdeck.inp"),
                job_name="rsrun",
                cpus=2,
                user_subroutine=str(SWEEP / "VUMATCGMAXH.f"),
                options={"double": "both"},
                overwrite=True,
            )
            check("restart deck submitted", sub.get("ok") is True, json.dumps(sub)[:300])
            await asyncio.sleep(45)
            stop = await call(session, "stop_job", job_name="rsrun", workdir=str(SWEEP))
            print("stop:", json.dumps(stop)[:400])
            res_file = SWEEP / "rsrun.res"
            check(".res written before stop", res_file.is_file(), "no rsrun.res")

            rec = await call(session, "restart_job", job_name="rsrun", workdir=str(SWEEP), mode="recover", cpus=2)
            print("restart:", json.dumps(rec)[:500])
            check("recover accepted", rec.get("ok") is True, json.dumps(rec)[:400])

            final = None
            for _ in range(40):
                await asyncio.sleep(10)
                st = await call(session, "job_status", job_name="rsrun", workdir=str(SWEEP))
                final = st.get("state")
                if final in {"completed", "aborted", "error"}:
                    break
            print("recovered status:", final)
            check("recovered run completed", final == "completed", str(final))

    print("\n" + ("BATCH CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
