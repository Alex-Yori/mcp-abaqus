"""Verify batch lifecycle and audit fields over real MCP stdio without Abaqus."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parents[1]


class BatchStdio(unittest.IsolatedAsyncioTestCase):
    async def test_batch_contracts_over_stdio(self):
        with tempfile.TemporaryDirectory(prefix="abaqus-batch-stdio-") as temporary:
            root = Path(temporary).resolve()
            self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
            metadata = root / ".mcp-batches"
            metadata.mkdir()
            template = root / "template.inp"
            template.write_text("{{E}}\n", encoding="utf-8")
            for name, state, started, content in (
                ("missing", "finished", True, None),
                ("cancelled_bad", "cancelled", True,
                 "***WARNING: DATA CARDS AFTER *FIXED MASS SCALING ARE IGNORED\n"),
                ("cancelled_missing", "cancelled", True, None),
                ("cancelled_unstarted", "cancelled", False, None),
                ("queued", "pending", False, None),
            ):
                case_dir = root / name
                case_dir.mkdir()
                (case_dir / "S01.inp").write_text("*Heading\n", encoding="utf-8")
                if content is not None:
                    (case_dir / "S01.dat").write_text(content, encoding="utf-8")
                record = {
                    "batch_id": name, "root": str(case_dir), "max_parallel": 1,
                    "cases": [{
                        "name": "S01", "job": "S01", "dir": str(case_dir), "state": state,
                        "pid": 0, "submitted_at": "2026-11-12T00:00:00" if started else None,
                    }],
                    "render_warnings": [
                        {"case": "S01", "kind": "long_line", "line": index, "chars": 300}
                        for index in range(11)
                    ] + [{"case": "S01", "kind": "unused_param", "keys": ["TEMP"]}],
                }
                (metadata / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")

            environment = dict(os.environ)
            environment["ABAQUS_MCP_ROOTS"] = str(root)
            environment["ABAQUS_MCP_WORKDIR"] = str(root)
            environment["ABAQUS_ENV_BAT"] = ""
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "mcp_abaqus"], env=environment, cwd=str(HERE)
            )
            async with stdio_client(parameters, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(name, **arguments):
                        response = await session.call_tool(name, arguments, read_timeout_seconds=30)
                        text = "".join(getattr(content, "text", "") for content in response.content)
                        return json.loads(text)

                    tools = await session.list_tools()
                    self.assertIn("batch_warnings", {tool.name for tool in tools.tools})
                    for names in (("S01", "S01"), ("S01", "s01")):
                        rejected = await call(
                            "submit_batch", template_inp=str(template), cpus=1, batch_id="duplicates",
                            cases=[{"name": names[0], "E": 100}, {"name": names[1], "E": 200}],
                        )
                        self.assertFalse(rejected["ok"])
                    self.assertFalse((root / "_batch_duplicates").exists())

                    missing = await call("batch_status", batch_id="missing")
                    self.assertTrue(missing["done"])
                    self.assertEqual(missing["stalled"], ["S01"])
                    self.assertEqual(missing["datalint_unchecked"], ["S01"])
                    self.assertTrue(missing["render_warnings_truncated"])
                    self.assertEqual(missing["render_warnings_total"], 12)
                    self.assertEqual(missing["render_warnings"][0]["kind"], "unused_param")

                    bad = await call("batch_status", batch_id="cancelled_bad")
                    self.assertTrue(bad["done"])
                    self.assertEqual(bad["suspicious_cases"], ["S01"])
                    absent = await call("batch_status", batch_id="cancelled_missing")
                    self.assertEqual(absent["datalint_unchecked"], ["S01"])
                    unstarted = await call("batch_status", batch_id="cancelled_unstarted")
                    self.assertEqual(unstarted["datalint_unchecked"], [])
                    self.assertFalse(unstarted["cases"][0]["audit_required"])

                    queue_path = metadata / "queued.json"
                    original = queue_path.read_bytes()
                    first = await call("batch_warnings", batch_id="queued", limit=10)
                    second = await call("batch_warnings", batch_id="queued", offset=first["next_offset"])
                    self.assertEqual(first["total"], 12)
                    self.assertEqual(second["warnings"][-1]["keys"], ["TEMP"])
                    self.assertIsNone(second["next_offset"])
                    self.assertEqual(queue_path.read_bytes(), original)
                    self.assertEqual(list(root.rglob("*.mcp-job.json")), [])
            self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))


if __name__ == "__main__":
    unittest.main()
