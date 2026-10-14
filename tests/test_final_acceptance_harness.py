"""Offline acceptance-report classification and checkpoint resume regression."""

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

import final_system_acceptance as acceptance
from helpers import ScriptedModel, call
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.mark.parametrize("final_status", ["completed", "failed"])
def test_resume_preserves_write_and_usage_and_requires_agent_completion(tmp_path, monkeypatch, final_status):
    workspace = tmp_path / "scenario-C"
    workspace.mkdir()
    settings = Settings(workspace=workspace, state_dir=tmp_path / "state-C", approval_mode="ALLOW")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="ready\n"),
    ]), interrupt_after=["tools"]) as app:
        original = app.run("Create note.txt with ready then finish")
    assert original["status"] == "interrupted"
    timestamp = (workspace / "note.txt").stat().st_mtime_ns
    previous = {
        "trace": original["trace"], "duration_seconds": 1.0, "model_calls": 1,
        "evidence": {"baseline_typecheck": "command_failed"},
    }
    (tmp_path / "result-C.json").write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(acceptance.Settings, "load", lambda *args, **kwargs: settings)
    monkeypatch.setattr(acceptance, "prepare", lambda *args: pytest.fail("Resume recreated the workspace"))
    monkeypatch.setattr(acceptance, "verify", lambda *args: {"process_cleanup": True})

    class ResumeApplication(AgentApplication):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            return {**result, "status": final_status}

    monkeypatch.setattr(acceptance, "AgentApplication", lambda configured:
                        ResumeApplication(replace(configured, trace_path=None),
                                          model=ScriptedModel(replies=["Finished."])))
    report = acceptance.run_scenario("C", tmp_path, tmp_path, 100000, 20, 16000, original["session_id"])
    assert report["independent_passed"] is True
    assert report["passed"] is (final_status == "completed")
    assert report["model_calls"] == 2
    assert report["input_tokens"] == 20 and report["output_tokens"] == 10
    assert report["duration_seconds"] >= previous["duration_seconds"]
    assert report["manual_resumes"] == 1
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "ready\n"
    assert (workspace / "note.txt").stat().st_mtime_ns == timestamp
    archived = json.loads((tmp_path / "result-C-before-resume-1.json").read_text(encoding="utf-8"))
    assert archived == previous


def test_cleanup_runs_when_browser_shutdown_fails():
    class Browser:
        def shutdown(self):
            raise RuntimeError("Browser close failed")

    class Servers:
        closed = False

        def close(self):
            self.closed = True

    servers = Servers()
    with pytest.raises(RuntimeError, match="Browser close failed"):
        acceptance.close_resources(Browser(), servers)
    assert servers.closed


def test_typed_hooks_reject_comment_and_string_spoofing():
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_typescript")
    with pytest.raises(AssertionError):
        acceptance.verify_typed_hooks('''
        // import {$api} from './api-hooks'; $api.useQuery(); $api.useMutation();
        const text = "$api.useQuery( $api.useMutation(";
        ''')
    acceptance.verify_typed_hooks('''
        import {$api} from './api-hooks';
        const tasks = $api.useQuery('get', '/api/tasks');
        const create = $api.useMutation('post', '/api/tasks');
        ''')
    with pytest.raises(AssertionError, match="not imported"):
        acceptance.verify_typed_hooks('''
            import { other } from './api-hooks';
            const $api = {useQuery() {}, useMutation() {}};
            $api.useQuery(); $api.useMutation();
        ''')
