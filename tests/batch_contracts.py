"""Exercise batch tool contracts with real files and a mocked solver launcher."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_abaqus import batch, server
from mcp_abaqus.config import Settings


class BatchContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="abaqus-batch-contracts-")
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.settings = Settings("", None, (self.root,), self.root, 1, 30, 30)
        self.template = self.root / "template.inp"
        self.template.write_text("*Elastic\n{{E}}, 0.3\n", encoding="utf-8")
        self.settings_patch = patch.object(server, "_settings", return_value=self.settings)
        self.settings_patch.start()
        self.launch_patch = patch.object(
            batch, "_submit", return_value={"pid": 0, "launched_at": "2026-11-12T00:00:00"}
        )
        self.launch = self.launch_patch.start()

    def tearDown(self):
        self.launch_patch.stop()
        self.settings_patch.stop()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.temporary.cleanup()

    def submit(self, cases=None, batch_id="review", **kwargs):
        return json.loads(server.submit_batch(
            str(self.template),
            cases if cases is not None else [{"name": "S01", "E": 100}],
            cpus=1,
            batch_id=batch_id,
            **kwargs,
        ))

    def save_case(self, state, started=False, dat=None, complete=False):
        case_dir = self.root / "case"
        case_dir.mkdir(exist_ok=True)
        (case_dir / "S01.inp").write_text("*Heading\n", encoding="utf-8")
        if dat is not None:
            (case_dir / "S01.dat").write_text(dat, encoding="utf-8")
        if complete:
            (case_dir / "S01.sta").write_text(
                "Abaqus/Explicit\nTHE ANALYSIS HAS COMPLETED SUCCESSFULLY\n", encoding="utf-8"
            )
        record = {
            "batch_id": "saved",
            "root": str(case_dir),
            "max_parallel": 1,
            "cases": [{
                "name": "S01", "job": "S01", "dir": str(case_dir), "state": state,
                "pid": 0, "submitted_at": "2026-11-12T00:00:00" if started else None,
            }],
        }
        batch._save(self.settings, record)
        return record

    def status(self):
        result = json.loads(server.batch_status("saved"))
        self.assertTrue(result["ok"], result)
        return result

    def test_duplicate_names_rejected_before_writes(self):
        for other in ("S01", "s01"):
            with self.subTest(other=other):
                result = self.submit([{"name": "S01", "E": 100}, {"name": other, "E": 200}])
                self.assertFalse(result["ok"], result)
                self.assertIn("unique", result["error"])
                self.assertFalse((self.root / "_batch_review").exists())
        self.launch.assert_not_called()

    def test_invalid_windows_names_rejected_before_writes(self):
        for name in (".", "..", "S01.", "NUL", "CON.txt", "COM1"):
            with self.subTest(name=name):
                self.assertFalse(self.submit([{"name": name, "E": 100}])["ok"])
                self.assertFalse((self.root / "_batch_review").exists())
        self.launch.assert_not_called()

    def test_later_invalid_params_leave_no_partial_decks(self):
        result = self.submit([{"name": "S01", "E": 100}, {"name": "S02", "E": "{{E}}"}])
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "_batch_review").exists())
        self.launch.assert_not_called()

    def test_name_is_not_a_render_parameter(self):
        self.template.write_text("{{name}}\n", encoding="utf-8")
        result = self.submit()
        self.assertFalse(result["ok"])
        self.assertIn("missing template placeholders", result["error"])
        self.launch.assert_not_called()

    def test_distinct_names_keep_their_parameters(self):
        self.assertTrue(self.submit([{"name": "S01", "E": 100}, {"name": "S02", "E": 200}])["ok"])
        saved = batch.load(self.settings, "review")
        for case, expected in zip(saved["cases"], (100, 200)):
            self.assertEqual(Path(case["inp"]).read_text(), f"*Elastic\n{expected}, 0.3\n")
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(saved["cases"][1]["state"], "pending")

    def test_existing_batch_id_preserves_inputs_and_queue(self):
        self.assertTrue(self.submit()["ok"])
        metadata = self.root / ".mcp-batches" / "review.json"
        original = metadata.read_bytes()
        result = self.submit([{"name": "S01", "E": 200}])
        self.assertFalse(result["ok"])
        self.assertEqual(metadata.read_bytes(), original)
        self.assertIn("100, 0.3", (self.root / "_batch_review/S01/S01.inp").read_text())
        self.assertEqual(self.launch.call_count, 1)

    def test_orphan_directory_is_not_overwritten(self):
        target = self.root / "_batch_review"
        target.mkdir()
        marker = target / "existing.txt"
        marker.write_text("keep")
        self.assertFalse(self.submit()["ok"])
        self.assertEqual(marker.read_text(), "keep")
        self.launch.assert_not_called()

    def test_invalid_batch_id_creates_no_case_directory(self):
        self.assertFalse(self.submit(batch_id="../bad")["ok"])
        self.assertEqual(list(self.root.glob("_batch_*")), [])
        self.launch.assert_not_called()

    def test_automatic_ids_differ_even_in_same_second(self):
        with patch.object(batch.time, "strftime", return_value="same-time"):
            first = self.submit(batch_id="")
            second = self.submit(batch_id="")
        self.assertTrue(first["ok"] and second["ok"])
        self.assertNotEqual(first["batch_id"], second["batch_id"])

    def test_finished_missing_outputs_settles_with_audit_gap(self):
        self.save_case("finished", started=True)
        result = self.status()
        self.assertEqual(result["summary"], {"unknown": 1})
        self.assertTrue(result["done"])
        self.assertEqual(result["stalled"], ["S01"])
        self.assertEqual(result["datalint_unchecked"], ["S01"])

    def test_finished_dat_only_is_stalled_not_pending(self):
        self.save_case("finished", dat="Preprocessing output\n")
        result = self.status()
        self.assertTrue(result["done"])
        self.assertEqual(result["stalled"], ["S01"])
        self.assertEqual(result["datalint_unchecked"], [])

    def test_running_process_without_outputs_remains_running(self):
        self.save_case("running", started=True)
        process = {"pid": 123, "pid_alive": True, "lck_present": False}
        with patch.object(batch, "process_state", return_value=process), patch.object(
            server, "process_state", return_value=process
        ):
            result = self.status()
        self.assertFalse(result["done"])
        self.assertEqual(result["summary"], {"running": 1})
        self.assertEqual(result["datalint_unchecked"], [])

    def test_cancelled_started_case_keeps_bad_dat_findings(self):
        fixture = Path(__file__).parent / "fixtures/step0_reference/S02/S02.dat"
        self.save_case("cancelled", started=True, dat=fixture.read_text(encoding="utf-8", errors="replace"))
        result = self.status()
        self.assertTrue(result["done"])
        self.assertEqual(result["summary"], {"cancelled": 1})
        self.assertEqual(result["suspicious_cases"], ["S01"])
        self.assertTrue(result["cases"][0]["dat_present"])

    def test_cancelled_started_case_without_dat_has_audit_gap(self):
        self.save_case("cancelled", started=True)
        self.assertEqual(self.status()["datalint_unchecked"], ["S01"])

    def test_cancelled_unstarted_case_has_no_audit_requirement(self):
        self.save_case("cancelled")
        result = self.status()
        self.assertTrue(result["done"])
        self.assertFalse(result["cases"][0]["started"])
        self.assertFalse(result["cases"][0]["audit_required"])
        self.assertEqual(result["datalint_unchecked"], [])

    def test_cancelled_legacy_case_is_audited_when_artifacts_exist(self):
        self.save_case("cancelled", dat="***WARNING: DATA CARDS AFTER *FIXED MASS SCALING ARE IGNORED\n")
        self.assertEqual(self.status()["suspicious_cases"], ["S01"])

    def test_failed_stop_keeps_running_state_and_blocks_queue(self):
        record = self.save_case("running", started=True)
        record["cases"].append({
            "name": "S02", "job": "S02", "dir": str(self.root / "pending"), "state": "pending",
        })
        batch._save(self.settings, record)
        with patch.object(batch, "_case_running", side_effect=lambda case: case["name"] == "S01"), patch.object(
            batch, "_stop_job", return_value={"stopped": False}
        ):
            stopped = batch.stop(self.settings, "saved")
        self.assertEqual([case["state"] for case in stopped["cases"]], ["running", "cancelled"])
        process = {"pid": 123, "pid_alive": True, "lck_present": False}
        with patch.object(server, "process_state", return_value=process):
            result = self.status()
        self.assertFalse(result["done"])
        self.launch.assert_not_called()

    def test_completed_fixture_reports_cleanly(self):
        self.save_case("finished", started=True, dat="No warnings\n", complete=True)
        result = self.status()
        self.assertEqual(result["summary"], {"completed": 1})
        self.assertTrue(result["done"])
        self.assertEqual(result["stalled"], [])
        self.assertEqual(result["datalint_unchecked"], [])

    def test_warning_summary_and_read_only_pagination(self):
        self.template.write_text(
            "\n".join("** " + str(index) + "X" * 260 for index in range(11)) + "\n{{E}}\n",
            encoding="utf-8",
        )
        submitted = self.submit([{"name": "S01", "E": 100}, {"name": "S02", "E": 200, "TEMP": 350}])
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["render_warnings_total"], 12)
        self.assertTrue(submitted["render_warnings_truncated"])
        self.assertEqual(submitted["render_warnings"][0]["kind"], "unused_param")
        saved = batch.load(self.settings, "review")
        metadata = self.root / ".mcp-batches/review.json"
        before = metadata.read_bytes()
        warnings = []
        offset = 0
        with patch.object(batch, "reconcile", side_effect=AssertionError("read-only query reconciled")):
            while offset is not None:
                page = json.loads(server.batch_warnings("review", offset=offset, limit=5))
                self.assertTrue(page["ok"])
                self.assertEqual(page["total"], 23)
                warnings.extend(page["warnings"])
                offset = page["next_offset"]
            filtered = json.loads(server.batch_warnings("review", case_name="S02"))
        self.assertEqual(warnings, saved["render_warnings"])
        self.assertEqual(len(filtered["warnings"]), 12)
        self.assertTrue(all(warning["case"] == "S02" for warning in filtered["warnings"]))
        self.assertEqual(metadata.read_bytes(), before)
        self.assertEqual(self.launch.call_count, 1)
        with patch.object(batch, "reconcile", side_effect=lambda settings, record: record):
            status = json.loads(server.batch_status("review"))
        self.assertEqual(status["render_warnings_total"], 12)
        self.assertTrue(status["render_warnings_truncated"])

    def test_warning_page_rejects_invalid_arguments(self):
        self.save_case("pending")
        for arguments in ({"offset": -1}, {"limit": 0}, {"limit": 101}, {"case_name": "missing"}):
            with self.subTest(arguments=arguments):
                self.assertFalse(json.loads(server.batch_warnings("saved", **arguments))["ok"])


if __name__ == "__main__":
    unittest.main()
