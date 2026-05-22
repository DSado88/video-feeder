import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import server


class ConfigTests(unittest.TestCase):
    def test_normalizes_mapping_config(self):
        apps = server._normalize_apps_config(
            {
                "apps": {
                    "demo": {
                        "cwd": "/tmp/demo",
                        "command": "npm run dev",
                        "window_match": "Demo",
                    }
                }
            }
        )

        self.assertEqual(apps["demo"]["name"], "demo")
        self.assertEqual(apps["demo"]["command"], "npm run dev")

    def test_rejects_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, "missing window_match"):
            server._normalize_apps_config(
                {"apps": {"demo": {"cwd": "/tmp/demo", "command": "npm run dev"}}}
            )


class LogSlicingTests(unittest.TestCase):
    def test_slices_file_from_byte_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app.log"
            dest = Path(tmp) / "capture.log"
            source.write_text("before\n", encoding="utf-8")
            offset = source.stat().st_size
            source.write_text("before\nduring\nlater\n", encoding="utf-8")

            bytes_written = server._slice_file_by_offset(source, dest, offset)

            self.assertEqual(dest.read_text(encoding="utf-8"), "during\nlater\n")
            self.assertEqual(bytes_written, len("during\nlater\n"))

    def test_offset_beyond_current_size_treats_file_as_rotated(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app.log"
            dest = Path(tmp) / "capture.log"
            source.write_text("new file after rotation\n", encoding="utf-8")

            bytes_written = server._slice_file_by_offset(source, dest, 10_000)

            self.assertEqual(dest.read_text(encoding="utf-8"), "new file after rotation\n")
            self.assertEqual(bytes_written, len("new file after rotation\n"))

    def test_timestamp_filtered_jsonl_accepts_iso_and_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "events.ndjson"
            dest = Path(tmp) / "filtered.ndjson"
            start = 1_700_000_000.0
            stop = start + 10
            source.write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": start - 1, "message": "before"}),
                        json.dumps({"ts": start + 1, "message": "during-epoch"}),
                        json.dumps({"time": "2023-11-14T22:13:25+00:00", "message": "during-iso"}),
                        "not json",
                        json.dumps({"timestamp": stop + 1, "message": "after"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            count = server._write_timestamp_filtered_jsonl(source, dest, start, stop)
            rows = [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(count, 2)
            self.assertEqual([row["message"] for row in rows], ["during-epoch", "during-iso"])


class ManifestAndPromptTests(unittest.TestCase):
    def test_empty_run_id_does_not_pick_stale_saved_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                stale = server._run_dir("20260101T000000Z-stale")
                stale.mkdir(parents=True)

                with self.assertRaises(FileNotFoundError):
                    server._resolve_run_id("")
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_rejects_path_traversal_run_id(self):
        with self.assertRaises(ValueError):
            server._resolve_run_id("../outside")

    def test_start_capture_refuses_terminal_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                manifest = {
                    "run_id": "20260101T000000Z_done",
                    "status": "analyzed",
                    "app": {"cwd": tmp, "command": "true", "window_match": "Test"},
                }
                server._save_manifest(manifest)

                result = server.start_capture("20260101T000000Z_done")

                self.assertIn("only prepared runs can start capture", result)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_stop_prepared_run_does_not_stop_obs(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                manifest = {
                    "run_id": "20260101T000000Z_prepared",
                    "status": "prepared",
                    "app": {"cwd": tmp, "command": "true", "window_match": "Test"},
                }
                server._save_manifest(manifest)

                result = server.stop_and_analyze("20260101T000000Z_prepared")

                self.assertIn("no recording has started", result)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_missing_generated_run_id_errors_instead_of_raw_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                result = server.stop_and_analyze("20260101T000000Z-missing")

                self.assertIn("was not found", result)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_manifest_round_trip_and_prompt_includes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-test"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                scoped_log = logs_dir / "app.full.log.capture.log"
                scoped_log.write_text("visible bug log line\n", encoding="utf-8")
                context = run_dir / "context.md"
                context.write_text("working on a QA harness\n", encoding="utf-8")

                manifest = {
                    "run_id": run_id,
                    "status": "stopped",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "context_path": str(context),
                    "capture_start_epoch": time.time() - 1,
                    "capture_stop_epoch": time.time(),
                    "scoped_logs": [
                        {
                            "label": "app.full.log",
                            "path": str(scoped_log),
                            "mode": "byte_offset",
                        }
                    ],
                }
                server._save_manifest(manifest)
                loaded = server._load_manifest(run_id)
                prompt = server._build_evidence_prompt(loaded)

                self.assertEqual(loaded["run_id"], run_id)
                self.assertIn("working on a QA harness", prompt)
                self.assertIn("visible bug log line", prompt)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_analysis_failure_sets_failed_status_and_releases_active_session(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_fail"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("during capture\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("not real video", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"}
                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", return_value="Gemini error: no key"):
                    result = server.stop_and_analyze(run_id)

                loaded = server._load_manifest(run_id)
                self.assertIn("witness query failed", result)
                self.assertEqual(loaded["status"], "analysis_failed")
                self.assertTrue(loaded["app_process_left_running"])
                self.assertNotIn(run_id, server._active_sessions)
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)

    def test_analysis_exception_sets_failed_status_and_releases_active_session(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_exception"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("during capture\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("not real video", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"}
                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", side_effect=RuntimeError("gemini exploded")):
                    result = server.stop_and_analyze(run_id)

                loaded = server._load_manifest(run_id)
                self.assertIn("witness query failed", result)
                self.assertEqual(loaded["status"], "analysis_failed")
                self.assertIn("witness query failed", Path(loaded["analysis_path"]).read_text())
                self.assertNotIn(run_id, server._active_sessions)
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)


class WitnessTests(unittest.TestCase):
    def test_ask_witness_queries_gemini_with_previous_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-witness"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                report = run_dir / "witness-report.md"
                report.write_text("# Witness Report\n\nButton clicked at 0:12.\n", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "reported",
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "gemini_file_ref": {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"},
                    "witness_report_path": str(report),
                }
                server._save_manifest(manifest)

                with mock.patch.object(
                    server, "_query_gemini_with_ref", return_value="The button was gray at 0:12."
                ) as mock_query:
                    result = server.ask_witness("What color was the button at 0:12?", run_id=run_id)

                self.assertIn("The button was gray at 0:12.", result)
                self.assertIn("follow-up #1", result.split("\n")[0].lower())
                call_args = mock_query.call_args
                self.assertIn("What color was the button at 0:12?", call_args[0][1])
                self.assertIn("Button clicked at 0:12", call_args[0][1])

                followup_path = run_dir / "followups" / "followup-001.md"
                self.assertTrue(followup_path.exists())
                content = followup_path.read_text(encoding="utf-8")
                self.assertIn("What color was the button", content)
                self.assertIn("gray at 0:12", content)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_ask_witness_sequential_followups_numbered_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-seq"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                report = run_dir / "witness-report.md"
                report.write_text("# Report\n\nStuff.\n", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "reported",
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "gemini_file_ref": {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"},
                    "witness_report_path": str(report),
                }
                server._save_manifest(manifest)

                with mock.patch.object(server, "_query_gemini_with_ref", return_value="Answer 1"):
                    server.ask_witness("Q1?", run_id=run_id)
                with mock.patch.object(server, "_query_gemini_with_ref", return_value="Answer 2"):
                    result = server.ask_witness("Q2?", run_id=run_id)

                self.assertIn("follow-up #2", result.lower())
                followups = sorted((run_dir / "followups").glob("followup-*.md"))
                self.assertEqual(len(followups), 2)
                self.assertEqual(followups[0].name, "followup-001.md")
                self.assertEqual(followups[1].name, "followup-002.md")
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_ask_witness_concurrent_followups_no_overwrite(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-race"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                report = run_dir / "witness-report.md"
                report.write_text("# Report\n\nStuff.\n", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "reported",
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "gemini_file_ref": {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"},
                    "witness_report_path": str(report),
                }
                server._save_manifest(manifest)

                call_counter = {"n": 0}
                counter_lock = threading.Lock()

                def fake_query(_ref, _prompt):
                    with counter_lock:
                        call_counter["n"] += 1
                        n = call_counter["n"]
                    return f"Answer {n}"

                with mock.patch.object(server, "_query_gemini_with_ref", side_effect=fake_query):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [pool.submit(server.ask_witness, f"Q{i}?", run_id) for i in range(4)]
                        [f.result() for f in futures]

                followups = sorted((run_dir / "followups").glob("followup-*.md"))
                self.assertEqual(len(followups), 4)
                names = {f.name for f in followups}
                self.assertEqual(names, {"followup-001.md", "followup-002.md", "followup-003.md", "followup-004.md"})
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_ask_witness_finds_reported_run_after_stop_and_analyze(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-e2e"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("log\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("fake", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"}
                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", return_value="## Transcript\nOK"):
                    server.stop_and_analyze(run_id)

                self.assertNotIn(run_id, server._active_sessions)

                with mock.patch.object(server, "_query_gemini_with_ref", return_value="Yes."):
                    result = server.ask_witness("Was there an error?")

                self.assertIn("Yes.", result)
                self.assertNotIn("Error", result.split("\n")[0])
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)

    def test_ask_witness_rejects_path_traversal_run_id(self):
        result = server.ask_witness("What happened?", run_id="../outside")
        self.assertIn("Error", result)
        self.assertNotIn("Traceback", result)

    def test_ask_witness_errors_without_file_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-noref"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                manifest = {
                    "run_id": run_id,
                    "status": "stopped",
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                }
                server._save_manifest(manifest)

                result = server.ask_witness("What happened?", run_id=run_id)
                self.assertIn("no uploaded video reference", result)
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_stop_and_analyze_stores_file_ref_and_sets_reported(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_reported"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("log line\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("not real video", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "files/abc", "uri": "gs://abc", "mime_type": "video/mp4"}
                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", return_value="## Transcript\nUser said hello."):
                    result = server.stop_and_analyze(run_id)

                loaded = server._load_manifest(run_id)
                self.assertEqual(loaded["status"], "reported")
                self.assertEqual(loaded["gemini_file_ref"], fake_ref)
                self.assertIn("witness report ready", result)
                self.assertIn("ask_witness", result)
                self.assertIn("User said hello", result)
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)

    def test_stop_and_analyze_success_releases_active_session(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_release"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("log\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("fake", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "f", "uri": "gs://f", "mime_type": "video/mp4"}
                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", return_value="## Transcript\nOK"):
                    server.stop_and_analyze(run_id)

                self.assertNotIn(run_id, server._active_sessions)
                loaded = server._load_manifest(run_id)
                self.assertIn("app_process_left_running", loaded)
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)

    def test_stop_and_analyze_persists_file_ref_before_query(self):
        class FakeProcess:
            def poll(self):
                return None

        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            original_sessions = dict(server._active_sessions)
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_persist"
                run_dir = server._run_dir(run_id)
                logs_dir = run_dir / "logs"
                logs_dir.mkdir(parents=True)
                full_log = logs_dir / "app.full.log"
                full_log.write_text("log\n", encoding="utf-8")
                recording = run_dir / "obs.mov"
                recording.write_text("fake", encoding="utf-8")
                manifest = {
                    "run_id": run_id,
                    "status": "recording",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "logs": [{"label": "app.full.log", "path": str(full_log)}],
                    "log_offsets": {"app.full.log": 0},
                    "capture_start_epoch": time.time() - 1,
                    "obs_output_path": str(recording),
                }
                server._save_manifest(manifest)
                server._active_sessions[run_id] = {"process": FakeProcess(), "status": "recording"}

                fake_ref = {"name": "files/abc", "uri": "gs://abc", "mime_type": "video/mp4"}
                ref_saved_before_query = {}

                def check_ref_persisted(_ref, _prompt):
                    loaded = server._load_manifest(run_id)
                    ref_saved_before_query["ref"] = loaded.get("gemini_file_ref")
                    return "## Transcript\nOK"

                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video", return_value=(fake_ref, "")), \
                     mock.patch.object(server, "_query_gemini_with_ref", side_effect=check_ref_persisted):
                    server.stop_and_analyze(run_id)

                self.assertEqual(ref_saved_before_query["ref"], fake_ref)
            finally:
                server.RUNS_DIR = original_runs_dir
                server._active_sessions.clear()
                server._active_sessions.update(original_sessions)

    def test_stop_and_analyze_reuses_existing_file_ref_on_retry(self):
        class FakeObs:
            def remove_input(self, _name):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z_retry"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                recording = run_dir / "recording.mov"
                recording.write_text("fake", encoding="utf-8")
                existing_ref = {"name": "files/existing", "uri": "gs://existing", "mime_type": "video/mp4"}
                manifest = {
                    "run_id": run_id,
                    "status": "analysis_failed",
                    "run_dir": str(run_dir),
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "recording_path": str(recording),
                    "capture_start_epoch": time.time() - 1,
                    "capture_stop_epoch": time.time(),
                    "obs_output_path": str(recording),
                    "gemini_file_ref": existing_ref,
                }
                server._save_manifest(manifest)

                with mock.patch.object(server, "_get_obs", return_value=FakeObs()), \
                     mock.patch.object(server, "_upload_video") as mock_upload, \
                     mock.patch.object(server, "_query_gemini_with_ref", return_value="## Transcript\nRetry OK"):
                    server.stop_and_analyze(run_id)

                mock_upload.assert_not_called()
                loaded = server._load_manifest(run_id)
                self.assertEqual(loaded["gemini_file_ref"], existing_ref)
                self.assertEqual(loaded["status"], "reported")
            finally:
                server.RUNS_DIR = original_runs_dir

    def test_witness_prompt_used_in_evidence_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_runs_dir = server.RUNS_DIR
            server.RUNS_DIR = Path(tmp)
            try:
                run_id = "20260101T000000Z-prompt"
                run_dir = server._run_dir(run_id)
                run_dir.mkdir(parents=True)
                manifest = {
                    "run_id": run_id,
                    "status": "stopped",
                    "app": {"cwd": str(run_dir), "command": "true", "window_match": "Test"},
                    "capture_start_epoch": time.time() - 1,
                    "capture_stop_epoch": time.time(),
                }
                server._save_manifest(manifest)
                prompt = server._build_evidence_prompt(manifest)
                self.assertIn("You are a witness", prompt)
                self.assertNotIn("Suggest likely root causes", prompt)
            finally:
                server.RUNS_DIR = original_runs_dir


class ObsReadinessTests(unittest.TestCase):
    def test_wait_for_obs_retries_until_ready(self):
        call_count = {"n": 0}

        class FakeObs:
            def get_version(self):
                call_count["n"] += 1
                if call_count["n"] < 3:
                    raise Exception("Request GetVersion returned code 207. OBS is not ready.")
                return mock.MagicMock(obs_version="32.0")

        fake = FakeObs()
        with mock.patch.object(server, "_get_obs", return_value=fake), \
             mock.patch.object(server, "_open_obs"):
            result = server._wait_for_obs(timeout_secs=5)

        self.assertEqual(result, fake)
        self.assertGreaterEqual(call_count["n"], 3)


class ObsCaptureTests(unittest.TestCase):
    def test_create_capture_source_recovers_from_already_exists(self):
        class FakeObs:
            def __init__(self):
                self.create_calls = 0
                self.settings_calls = 0

            def create_input(self, **kwargs):
                self.create_calls += 1
                raise Exception("Request CreateInput returned code 601. With message: A source already exists by that input name.")

            def set_input_settings(self, **kwargs):
                self.settings_calls += 1

        cl = FakeObs()
        warnings = []
        result = server._create_capture_source(cl, 12345, warnings)

        self.assertTrue(result)
        self.assertEqual(cl.settings_calls, 1)
        self.assertFalse(server._has_capture_source_failure(warnings))

    def test_create_capture_source_still_fails_on_other_errors(self):
        class FakeObs:
            def create_input(self, **kwargs):
                raise Exception("Request CreateInput returned code 500. Something else.")

        cl = FakeObs()
        warnings = []
        result = server._create_capture_source(cl, 12345, warnings)

        self.assertFalse(result)

    def test_remove_input_failure_logged_in_warnings(self):
        class FakeObs:
            def remove_input(self, name):
                raise Exception("Cannot remove: not found")

            def create_input(self, **kwargs):
                pass

            def create_scene(self, name):
                pass

            def set_current_program_scene(self, name):
                pass

            def get_input_list(self):
                return None

        cl = FakeObs()
        warnings = server._ensure_obs_capture(cl, 12345, manage_profile=False)

        remove_warnings = [w for w in warnings if "remove" in w.lower()]
        self.assertTrue(len(remove_warnings) > 0)


class WindowMatchTests(unittest.TestCase):
    def test_find_window_prefers_owner_match_over_title_match(self):
        fake_windows = [
            {"id": 100, "owner": "Google Chrome", "title": "OrchidStudio/orchid PR #42"},
            {"id": 200, "owner": "orchid", "title": "ORCHID"},
        ]
        with mock.patch.object(server, "_list_window_dicts", return_value=fake_windows):
            result = server._find_window("orchid")

        self.assertEqual(result["id"], 200)
        self.assertEqual(result["owner"], "orchid")

    def test_find_window_falls_back_to_title_match(self):
        fake_windows = [
            {"id": 100, "owner": "Google Chrome", "title": "Orchid Docs"},
        ]
        with mock.patch.object(server, "_list_window_dicts", return_value=fake_windows):
            result = server._find_window("orchid")

        self.assertEqual(result["id"], 100)

    def test_find_window_returns_none_when_no_match(self):
        fake_windows = [
            {"id": 100, "owner": "Firefox", "title": "Homepage"},
        ]
        with mock.patch.object(server, "_list_window_dicts", return_value=fake_windows):
            result = server._find_window("orchid")

        self.assertIsNone(result)


class LogPathTests(unittest.TestCase):
    def test_extra_logs_with_same_basename_get_unique_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_log = Path(tmp) / "app.full.log"
            extra1 = Path(tmp) / "subdir1" / "app.log"
            extra2 = Path(tmp) / "subdir2" / "app.log"
            extra1.parent.mkdir()
            extra2.parent.mkdir()

            app = {
                "cwd": tmp,
                "command": "true",
                "window_match": "Test",
                "extra_logs": [str(extra1), str(extra2)],
            }
            logs = server._configured_log_paths(app, app_log)
            labels = [e["label"] for e in logs]

            self.assertEqual(len(labels), len(set(labels)), f"Duplicate labels: {labels}")


class StartCaptureTests(unittest.TestCase):
    def test_start_capture_rejects_negative_max_seconds(self):
        result = server.start_capture("any", max_seconds=-5)
        self.assertIn("max_seconds must be positive", result)

    def test_start_capture_rejects_zero_max_seconds(self):
        result = server.start_capture("any", max_seconds=0)
        self.assertIn("max_seconds must be positive", result)


class AnalyzeBugTests(unittest.TestCase):
    def test_analyze_bug_image_handles_gemini_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "screenshot.png"
            img.write_bytes(b"\x89PNG\r\n")

            with mock.patch.object(server, "_get_gemini", return_value=(mock.MagicMock(), None)) as _, \
                 mock.patch("server.types") as mock_types:
                mock_types.Part.from_bytes.return_value = "fake_part"
                client = server._get_gemini()[0]
                client.models.generate_content.side_effect = RuntimeError("API down")
                with mock.patch.object(server, "_get_gemini", return_value=(client, None)):
                    result = server.analyze_bug(str(img))

            self.assertIn("Error", result)
            self.assertNotIn("Traceback", result)

    def test_analyze_bug_image_handles_none_response_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "screenshot.png"
            img.write_bytes(b"\x89PNG\r\n")

            with mock.patch.object(server, "_get_gemini") as mock_get:
                client = mock.MagicMock()
                client.models.generate_content.return_value.text = None
                mock_get.return_value = (client, None)
                with mock.patch("server.types") as mock_types:
                    mock_types.Part.from_bytes.return_value = "fake_part"
                    result = server.analyze_bug(str(img))

            self.assertIn("Error", result)


class CdpTests(unittest.TestCase):
    def test_runtime_console_api_called_is_captured(self):
        recorder = server.CdpRecorder("ws://example", Path("/tmp/unused.ndjson"))

        event = recorder._interesting_event(
            {"method": "Runtime.consoleAPICalled", "params": {"type": "log"}}
        )

        self.assertEqual(event["method"], "Runtime.consoleAPICalled")


if __name__ == "__main__":
    unittest.main()
