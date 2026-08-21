import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from agenttrace import TraceManager
from agenttrace.trajectory import (
    DynamicRedactor,
    GitVersionManager,
    PackagingApprovalRequired,
    TrajectoryBuilder,
)


def run_git(repo_path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


class TestTrajectoryCapture(unittest.TestCase):
    def setUp(self):
        TraceManager._instance = None
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "traces.db"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init")
        run_git(self.repo, "config", "user.email", "agenttrace@example.test")
        run_git(self.repo, "config", "user.name", "AgentTrace")

    def tearDown(self):
        instance = TraceManager._instance
        if instance is not None and getattr(instance, "conn", None):
            instance.save_traces()
            instance.conn.close()
            instance.conn = None
        TraceManager._instance = None
        self.temp_dir.cleanup()

    def test_records_artifact_snapshot_and_token_usage(self):
        source = self.repo / "input.txt"
        source.write_text("customer ACME used 42 tokens", encoding="utf-8")
        tracer = TraceManager(db_path=str(self.db_path), colored_logging=False)

        artifact_id = tracer.capture_artifact(
            source,
            artifact_type="input",
            session_id="task-1",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        tracer.record_token_usage(
            session_id="task-1",
            prompt_tokens=3,
            completion_tokens=2,
            model="gpt-test",
            source="manual-note",
        )

        artifact_rows = tracer.get_artifacts(session_id="task-1")
        token_totals = tracer.get_token_usage(session_id="task-1")

        self.assertEqual(artifact_rows[0]["id"], artifact_id)
        self.assertEqual(artifact_rows[0]["artifact_type"], "input")
        self.assertEqual(artifact_rows[0]["token_usage"]["total_tokens"], 15)
        self.assertEqual(token_totals["total_tokens"], 20)

    def test_add_trace_extracts_token_usage_from_model_response(self):
        tracer = TraceManager(db_path=str(self.db_path), colored_logging=False)
        session_id = tracer.add_trace("START", "model_call", session_id="task-token")
        tracer.add_trace(
            "END",
            "model_call",
            result={"usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}},
            session_id=session_id,
        )

        token_totals = tracer.get_token_usage(session_id="task-token")

        self.assertEqual(token_totals["prompt_tokens"], 7)
        self.assertEqual(token_totals["completion_tokens"], 4)
        self.assertEqual(token_totals["total_tokens"], 11)

    def test_extracts_multi_version_file_trajectory_from_git(self):
        file_path = self.repo / "answer.md"
        file_path.write_text("draft for Vendor Alpha\n", encoding="utf-8")
        run_git(self.repo, "add", "answer.md")
        first_commit = run_git(self.repo, "commit", "-m", "draft")
        file_path.write_text("final for Vendor Alpha with price 1200\n", encoding="utf-8")
        run_git(self.repo, "add", "answer.md")
        run_git(self.repo, "commit", "-m", "final")

        tracer = TraceManager(db_path=str(self.db_path), colored_logging=False)
        manager = GitVersionManager(self.repo, tracer=tracer, session_id="task-2")
        trajectory = manager.extract_trajectory(paths=["answer.md"])

        versions = trajectory["files"]["answer.md"]["versions"]
        self.assertEqual(len(versions), 2)
        self.assertIn("draft for Vendor Alpha", versions[0]["content"])
        self.assertIn("final for Vendor Alpha", versions[1]["content"])
        self.assertEqual(trajectory["session_id"], "task-2")
        self.assertTrue(trajectory["git"]["head"])
        self.assertIn("draft", first_commit)

    def test_packaging_requires_confirmed_redaction_preview(self):
        sensitive = self.repo / "supplier.txt"
        sensitive.write_text(
            "Vendor Alpha offered price 1200 to customer Beta.",
            encoding="utf-8",
        )
        run_git(self.repo, "add", "supplier.txt")
        run_git(self.repo, "commit", "-m", "supplier note")

        tracer = TraceManager(db_path=str(self.db_path), colored_logging=False)
        builder = TrajectoryBuilder(
            repo_path=self.repo,
            tracer=tracer,
            session_id="task-3",
            task_context="supplier sourcing workflow with commercial terms",
        )
        preview = builder.preview_package(paths=["supplier.txt"])
        redacted_text = preview["files"]["supplier.txt"]["versions"][0]["content"]

        self.assertNotIn("Vendor Alpha", redacted_text)
        self.assertNotIn("1200", redacted_text)
        self.assertFalse(preview["approval"]["confirmed"])
        with self.assertRaises(PackagingApprovalRequired):
            builder.export_package(paths=["supplier.txt"])

        output_path = self.root / "package.zip"
        result = builder.export_package(
            paths=["supplier.txt"],
            output_path=output_path,
            confirmed_preview_id=preview["approval"]["preview_id"],
        )
        package_dir = Path(result["package_dir"])
        trajectory_path = package_dir / "trajectory.json"
        redacted_file = package_dir / "files" / "redacted" / "latest" / "supplier.txt"
        version_file = package_dir / "files" / "redacted" / "versions" / preview["git"]["head"][:12] / "supplier.txt"
        exported = json.loads(trajectory_path.read_text(encoding="utf-8"))

        self.assertEqual(result["output_path"], str(output_path))
        self.assertEqual(result["package_type"], "zip")
        self.assertTrue(output_path.exists())
        self.assertTrue(trajectory_path.exists())
        self.assertTrue(redacted_file.exists())
        self.assertTrue(version_file.exists())
        self.assertNotIn("Vendor Alpha", redacted_file.read_text(encoding="utf-8"))
        self.assertNotIn("1200", redacted_file.read_text(encoding="utf-8"))
        self.assertEqual(exported["approval"]["confirmed_preview_id"], preview["approval"]["preview_id"])
        self.assertEqual(exported["files"]["supplier.txt"]["versions"][0]["package_file"], "files/redacted/versions/" + preview["git"]["head"][:12] + "/supplier.txt")
        with zipfile.ZipFile(output_path) as package_zip:
            names = set(package_zip.namelist())
        self.assertIn("trajectory.json", names)
        self.assertIn("files/redacted/latest/supplier.txt", names)
        self.assertIn("files/redacted/versions/" + preview["git"]["head"][:12] + "/supplier.txt", names)

    def test_preview_writes_redacted_directory_for_expert_review(self):
        sensitive = self.repo / "supplier.txt"
        sensitive.write_text(
            "Vendor Alpha offered price 1200 to customer Beta.",
            encoding="utf-8",
        )
        run_git(self.repo, "add", "supplier.txt")
        run_git(self.repo, "commit", "-m", "supplier note")

        tracer = TraceManager(db_path=str(self.db_path), colored_logging=False)
        builder = TrajectoryBuilder(
            repo_path=self.repo,
            tracer=tracer,
            session_id="task-preview",
            task_context="supplier sourcing workflow with commercial terms",
        )
        preview_dir = self.root / "preview"

        result = builder.write_preview_package(paths=["supplier.txt"], output_dir=preview_dir)
        trajectory_path = preview_dir / "trajectory-preview.json"
        redacted_file = preview_dir / "files" / "redacted" / "latest" / "supplier.txt"
        preview = json.loads(trajectory_path.read_text(encoding="utf-8"))

        self.assertEqual(result["preview_dir"], str(preview_dir))
        self.assertEqual(result["package_type"], "preview-directory")
        self.assertFalse(preview["approval"]["confirmed"])
        self.assertTrue(trajectory_path.exists())
        self.assertTrue(redacted_file.exists())
        self.assertNotIn("Vendor Alpha", redacted_file.read_text(encoding="utf-8"))
        self.assertNotIn("1200", redacted_file.read_text(encoding="utf-8"))
        self.assertEqual(
            preview["files"]["supplier.txt"]["latest_package_file"],
            "files/redacted/latest/supplier.txt",
        )

    def test_dynamic_redactor_uses_task_context_without_fixed_examples(self):
        redactor = DynamicRedactor(
            task_context="analyze supplier quote documents and customer-specific terms"
        )
        result = redactor.redact_text("Vendor Alpha quoted $900 for Customer Beta")

        self.assertNotIn("Vendor Alpha", result.text)
        self.assertNotIn("Customer Beta", result.text)
        self.assertNotIn("$900", result.text)
        self.assertTrue(result.findings)


if __name__ == "__main__":
    unittest.main()
