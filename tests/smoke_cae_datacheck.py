"""One-shot: datacheck the cae_model_builder smoke deck through the MCP tool."""
from __future__ import annotations
import asyncio, json, os, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
DECK = HERE / "abq_jobs" / "models" / "cae_mb_smoke" / "cae_mb_smoke.inp"
SUB = HERE / "abq_jobs" / "models" / "cae_mb_smoke" / "VUMATCGMAXH.f"

async def main() -> int:
    env = dict(os.environ)
    env["ABAQUS_MCP_ROOTS"] = os.pathsep.join([str(HERE / "abq_jobs"), str(Path.home())])
    env["ABAQUS_MCP_WORKDIR"] = str(HERE / "abq_jobs")
    params = StdioServerParameters(command=str(PY), args=["-m", "mcp_abaqus"], env=env, cwd=str(HERE))
    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(
                "datacheck",
                {"inp": str(DECK), "user_subroutine": str(SUB), "options": {"double": "both"}},
                read_timeout_seconds=600,
            )
            data = json.loads("".join(getattr(c, "text", "") for c in res.content))
            print(json.dumps(data, ensure_ascii=False, indent=1)[:1500])
            return 0 if data.get("verdict") == "passed" else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))