"""End-to-end test: spawn the server over stdio exactly like an MCP client does.

Run:  .venv/Scripts/python tests/e2e_stdio.py [--with-odb]

Checks every tool against real artifacts on this machine (Abaqus 2018 logs and
ODBs from the Lin2024 reproduction). No mocking: if the parser or the launcher
breaks, this fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
LIN24 = Path(r"D:\files\workProject\Explicit algorithm\WithH\Lin2024_reproduction")

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def payload(result) -> dict:
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


async def call(session: ClientSession, tool_name: str, **kwargs) -> dict:
    res = await session.call_tool(tool_name, kwargs, read_timeout_seconds=900)
    data = payload(res)
    print(f"\n>>> {tool_name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())[:90]})")
    print(json.dumps(data, ensure_ascii=False)[:900])
    return data


async def main() -> int:
    with_odb = "--with-odb" in sys.argv
    env = dict(os.environ)
    env["ABAQUS_MCP_ROOTS"] = os.pathsep.join(
        [str(HERE / "abq_jobs"), str(LIN24), str(LIN24.parent), str(Path.home())]
    )
    env["ABAQUS_MCP_WORKDIR"] = str(HERE / "abq_jobs")
    params = StdioServerParameters(command=str(PY), args=["-m", "mcp_abaqus"], env=env, cwd=str(HERE))

    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.server_info.name} {init.server_info.version} / protocol {init.protocol_version}")

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"tools ({len(names)}): {names}")
            check("tool count == 23", len(names) == 23, str(len(names)))
            check("every tool has a description", all(t.description for t in tools))
            for t in tools:
                if not t.input_schema.get("properties"):
                    check(f"{t.name} schema not empty", True)

            data = await call(session, "abaqus_environment")
            check("environment ok", data.get("ok") is True and data.get("launcher_exists"), str(data)[:200])

            data = await call(session, "list_jobs", path=str(LIN24), limit=5)
            check("list_jobs finds decks", data.get("ok") and data.get("count", 0) >= 4, str(data)[:200])

            data = await call(session, "job_status", job_name="check_diffusion")
            check("job_status finds job by scan", data.get("ok") is True, str(data)[:200])
            check("job_status reports completed", data.get("state") == "completed", str(data.get("state")))
            check("job_status has increments", (data.get("increment_count") or 0) > 0)

            data = await call(session, "tail_output", job_name="check_diffusion", file="sta", lines=3)
            check("tail_output sta", data.get("ok") and len(data.get("lines", [])) == 3)

            data = await call(session, "tail_output", job_name="check_diffusion", file="sta", pattern="COMPLETED")
            check("tail_output regex", data.get("ok") and any("COMPLETED" in ln for ln in data.get("lines", [])))

            data = await call(session, "job_status", job_name="no_such_job_xyz")
            check("unknown job -> ok:false", data.get("ok") is False and "not found" in data.get("error", ""))

            data = await call(session, "submit_job", inp="C:/Windows/win.ini")
            check("sandbox rejects path", data.get("ok") is False)

            data = await call(
                session,
                "model_builder",
                name="mb_smoke",
                geometry={
                    "kind": "axisym_notched_bar",
                    "root_radius": 2.0,
                    "body_radius": 4.0,
                    "notch_radius": 1.0,
                    "notch_kind": "parabolic",
                    "half_length": 10.0,
                },
                mesh={"n_radial": 6, "dz_notch": 0.1, "dz_far": 1.0},
                template_deck=str(Path(r"D:/files/workProject/Explicit algorithm/WithH/S-explicit-H.inp")),
            )
            check("model_builder ok", data.get("ok") is True, str(data)[:400])
            check("model_builder wrote a deck", Path(data.get("deck", "")).is_file(), str(data.get("deck")))
            check("model_builder set sizes", all(v > 0 for v in (data.get("set_sizes") or {}).values()))

            data = await call(
                session,
                "cae_model_builder",
                name="cae_mb_smoke",
                geometry={"r_gauge": 2.0, "r_grip": 4.0, "l_gauge": 15.0, "l_trans": 20.0, "l_end": 45.0},
                mesh={"size": 1.0},
                template_deck=str(Path(r"D:/files/workProject/Explicit algorithm/WithH/S-explicit-H.inp")),
            )
            check("cae_model_builder ok", data.get("ok") is True, str(data)[:400])
            check("cae_model_builder wrote .cae", Path(data.get("cae", "")).is_file(), str(data.get("cae")))
            check("cae_model_builder wrote deck", Path(data.get("inp", "")).is_file(), str(data.get("inp")))
            check("cae_model_builder set sizes", all(v > 0 for v in (data.get("set_sizes") or {}).values()))
            check("cae_model_builder no tris", data.get("tri_count") == 0, str(data.get("tri_count")))

            if with_odb:
                data = await call(session, "odb_summary", odb=str(LIN24 / "check_diffusion.odb"))
                check("odb_summary ok", data.get("ok") is True, str(data)[:300])
                steps = data.get("steps") or []
                check("odb_summary has steps", bool(steps))
                insts = data.get("instances") or []
                check("odb_summary has instances", bool(insts))
                if steps:
                    print("   fields:", steps[-1].get("fields"))
                print("   instances:", insts)

                data = await call(session, "odb_field_extremes", odb=str(LIN24 / "check_diffusion.odb"), field="U", component="U2")
                check("odb_field_extremes ok", data.get("ok") is True, str(data)[:300])

                lab = (data.get("max") or {}).get("label")
                if lab:
                    node = int(lab)
                    data = await call(
                        session, "odb_node_series", odb=str(LIN24 / "check_diffusion.odb"), nodes=[node], field="U", component="U2"
                    )
                    check("odb_node_series ok", data.get("ok") is True, str(data)[:300])

                data = await call(session, "odb_history", odb=str(LIN24 / "check_diffusion.odb"), variable="")
                check("odb_history ok", data.get("ok") is True, str(data)[:300])

                data = await call(
                    session,
                    "odb_xy",
                    odb=str(LIN24 / "check_diffusion.odb"),
                    series=[{"label": "max SDV19", "x": {"kind": "time"}, "y": {"kind": "field_max", "field": "SDV19"}}],
                    xlabel="time (s)",
                    ylabel="max SDV19",
                    output="xy_test.png",
                    overwrite=True,
                )
                check("odb_xy ok", data.get("ok") is True, str(data)[:400])
                check("odb_xy wrote a curve png", (data.get("size_bytes") or 0) > 5000, str(data.get("size_bytes")))

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
