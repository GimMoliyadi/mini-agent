import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import launcher

ROOT = Path(__file__).resolve().parents[1]


class EntrypointHardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / "install with spaces"
        self.install.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.process_environment = {
            name: os.environ[name] for name in ("PATH", "SystemRoot", "WINDIR", "LOCALAPPDATA", "APPDATA", "USERPROFILE")
            if name in os.environ
        }
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        project = patch.object(launcher, "PROJECT_ROOT", self.install)
        project.start()
        self.addCleanup(project.stop)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def test_source_checkout_keeps_existing_default_locations(self):
        demo = self.install / "demo_workspace"
        demo.mkdir()
        state, workspace = launcher.prepare_environment()
        self.assertEqual(state, self.install)
        self.assertEqual(workspace, demo)
        self.assertEqual(os.environ["MINI_AGENT_STATE_DIR"], str(state))
        self.assertEqual(os.environ["AGENT_WORKSPACE"], str(workspace))

    def test_installed_layout_does_not_use_install_directory_for_user_data(self):
        state, workspace = launcher.prepare_environment()
        self.assertEqual(state, self.home / ".mini-agent")
        self.assertEqual(workspace, state / "workspace")
        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(self.install.iterdir()), [])

    def test_explicit_paths_and_inherited_state_are_respected(self):
        workspace = self.root / "trusted project"
        workspace.mkdir()
        state = self.root / "custom state"
        os.environ["MINI_AGENT_STATE_DIR"] = str(state)
        selected_state, selected_workspace = launcher.prepare_environment(workspace=workspace)
        self.assertEqual(selected_state, state)
        self.assertEqual(selected_workspace, workspace)
        self.assertTrue(state.is_dir())

    def test_workspace_must_exist_and_desktop_is_mutually_exclusive(self):
        with self.assertRaises(NotADirectoryError):
            launcher.prepare_environment(workspace=self.root / "missing")
        with self.assertRaises(ValueError):
            launcher.prepare_environment(workspace=self.root, desktop=True)

    def test_desktop_uses_known_folder_result_not_userprofile_assumption(self):
        desktop = self.root / "redirected desktop"
        desktop.mkdir()
        with patch.object(launcher, "get_desktop_path", return_value=desktop) as finder:
            _, workspace = launcher.prepare_environment(desktop=True)
        self.assertEqual(workspace, desktop)
        finder.assert_called_once_with()

    def test_desktop_can_be_combined_with_resume(self):
        desktop = self.root / "desktop"
        desktop.mkdir()
        runtime = SimpleNamespace(configure_workspace=Mock(), main=Mock(return_value=7))
        with patch.object(launcher, "get_desktop_path", return_value=desktop), patch.dict(sys.modules, {"main": runtime}):
            code = launcher.main(["start", "--desktop", "--resume", "saved-session"])
        self.assertEqual(code, 7)
        runtime.configure_workspace.assert_called_once_with(desktop)
        runtime.main.assert_called_once_with(["--resume", "saved-session"])

    def test_start_configures_environment_before_runtime_import(self):
        workspace = self.root / "trusted"
        workspace.mkdir()
        runtime = SimpleNamespace(configure_workspace=Mock(), main=Mock(return_value=0))
        original_import = __import__
        def checked_import(name, *args, **kwargs):
            if name == "main":
                self.assertEqual(os.environ["AGENT_WORKSPACE"], str(workspace))
                self.assertEqual(os.environ["MINI_AGENT_STATE_DIR"], str(self.home / ".mini-agent"))
                return runtime
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=checked_import):
            self.assertEqual(launcher.main(["start", "--workspace", str(workspace)]), 0)

    def test_contract_and_resume_start_use_shared_runner(self):
        workspace = self.root / "trusted"
        workspace.mkdir()
        contract = self.root / "contract.json"
        contract.write_text(json.dumps({
            "task_id": "repair", "instruction": "修复", "allowed_paths": ["calculator.py"],
            "test_command": {"command": "python", "args": ["-m", "unittest", "test_calculator"]},
        }), encoding="utf-8")
        runtime = SimpleNamespace(configure_workspace=Mock(), run_task_with_session=Mock(return_value={"status": "incomplete", "error": "预算耗尽"}))
        with patch.dict(sys.modules, {"main": runtime}), patch.object(sys, "stderr", io.StringIO()):
            code = launcher.main(["start", "--workspace", str(workspace), "--resume", "saved", "--contract", str(contract)])
        self.assertEqual(code, 1)
        self.assertEqual(runtime.run_task_with_session.call_args.kwargs["resume_id"], "saved")
        self.assertEqual(runtime.run_task_with_session.call_args.kwargs["contract"].task_id, "repair")

    def test_task_command_delegates_and_preserves_exit_code(self):
        import cli
        with patch.object(cli, "cli", return_value=130) as task:
            code = launcher.main(["task", "--resume", "saved"])
        self.assertEqual(code, 130)
        task.assert_called_once_with(["--resume", "saved"])

    def test_sessions_command_delegates_after_path_setup(self):
        session_cli = SimpleNamespace(main=Mock(return_value=2))
        with patch.dict(sys.modules, {"session_cli": session_cli}):
            self.assertEqual(launcher.main(["sessions", "list"]), 2)
        session_cli.main.assert_called_once_with(["list"])
        self.assertEqual(os.environ["MINI_AGENT_STATE_DIR"], str(self.home / ".mini-agent"))

    def test_undo_requires_confirmation_before_calling_file_safety(self):
        import file_safety
        with patch.object(file_safety, "undo_task") as undo, patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                launcher.main(["undo", "example"])
        self.assertEqual(raised.exception.code, 2)
        undo.assert_not_called()

    def test_undo_calls_journal_api_and_preserves_conflict_failure(self):
        import file_safety
        workspace = self.root / "trusted"
        workspace.mkdir()
        output = io.StringIO()
        result = {"status": "conflict", "run_id": "example", "conflicts": ["file.py"]}
        with patch.object(file_safety, "undo_task", return_value=result) as undo, patch.object(sys, "stdout", output):
            code = launcher.main(["undo", "example", "--yes", "--workspace", str(workspace)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "conflict")
        undo.assert_called_once_with("example", workspace, self.home / ".mini-agent" / "journals")

    def test_undo_success_is_zero(self):
        import file_safety
        with patch.object(file_safety, "undo_task", return_value={"status": "undone"}), patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(launcher.main(["undo", "example", "--yes"]), 0)

    def test_doctor_does_not_create_user_directories_and_works_without_key(self):
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["OPENAI_BASE_URL"] = ""
        os.environ["OPENAI_MODEL"] = ""
        import config
        with patch.object(config, "load_env_file"), patch.object(diagnostics, "inspect_tools", return_value=(["read_file"], [])), patch.object(diagnostics, "installed_version", return_value="test-sdk"):
            report = diagnostics.build_report()
        self.assertFalse((self.home / ".mini-agent").exists())
        self.assertFalse(any(report["configuration"]["settings_present"].values()))
        self.assertIn("不构成操作系统沙盒", report["support_boundary"])
        self.assertEqual(report["issues"], [])
        self.assertFalse(report["permissions"]["workspace"]["exists"])

    def test_doctor_json_never_contains_key(self):
        import config
        secret = "doctor-secret-canary"
        os.environ["OPENAI_API_KEY"] = secret
        output = io.StringIO()
        with patch.object(config, "load_env_file"), patch.object(diagnostics, "inspect_tools", return_value=(["read_file"], [])), patch.object(diagnostics, "installed_version", return_value="test-sdk"), patch.object(sys, "stdout", output):
            self.assertEqual(launcher.main(["doctor"]), 0)
        report = json.loads(output.getvalue())
        self.assertNotIn(secret, output.getvalue())
        self.assertTrue(report["configuration"]["settings_present"]["OPENAI_API_KEY"])
        self.assertTrue(report["sdk"]["installed"])

    def test_doctor_reports_missing_sdk_without_installing_it(self):
        import config
        with patch.object(config, "load_env_file"), patch.object(diagnostics, "inspect_tools", return_value=(["read_file"], [])), patch.object(diagnostics, "installed_version", return_value=None):
            report = diagnostics.build_report()
        self.assertFalse(report["sdk"]["installed"])
        self.assertTrue(any("未安装" in issue for issue in report["issues"]))

    def test_batch_wrappers_are_ascii(self):
        for name in ("mini.cmd", "agent.cmd"):
            with self.subTest(name=name):
                content = (ROOT / name).read_bytes()
                self.assertTrue(all(byte < 128 for byte in content))
                self.assertNotIn(b"%USERPROFILE%", content)

    @unittest.skipUnless(os.name == "nt", "需要 Windows cmd")
    def test_batch_exit_codes_and_arguments_from_another_directory(self):
        for name in ("mini.cmd", "agent.cmd"):
            shutil.copy2(ROOT / name, self.install / name)
        (self.install / "launcher.py").write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(int(sys.argv[-1]))\n",
            encoding="ascii",
        )
        environment = {**self.process_environment, "TMP": str(self.root), "TEMP": str(self.root)}
        for name in ("mini.cmd", "agent.cmd"):
            for code in (0, 2, 7, 130):
                with self.subTest(name=name, code=code):
                    args = ["start", "--resume", "saved", str(code)] if name == "mini.cmd" else ["--resume", "saved", str(code)]
                    result = subprocess.run(["cmd", "/d", "/c", str(self.install / name), *args], cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(json.loads(result.stdout), ["start", "--resume", "saved", str(code)])


if __name__ == "__main__":
    unittest.main()
