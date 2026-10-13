"""Multi-file scaffolding: real source creation and LangGraph integration."""

import json
from pathlib import Path

import pytest

from helpers import ScriptedModel, call
from mini_agent.runtime import AgentApplication
from mini_agent.scaffolding import (
    create_files_batch, creation_precondition, prepare_file_creations,
    preview_file_creations,
)
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredTestCommand


def _files():
    return [
        {"path": "src/__init__.py", "content": ""},
        {"path": "src/service.py", "content": "def double(value):\n    return value * 2\n"},
        {"path": "tests/test_service.py", "content": (
            "import unittest\nfrom src.service import double\n\n"
            "class ServiceTests(unittest.TestCase):\n"
            "    def test_double(self):\n        self.assertEqual(double(4), 8)\n"
        )},
    ]


def _settings(root, tmp_path, mode="ALLOW"):
    return Settings(workspace=root, state_dir=tmp_path / "state", approval_mode=mode)


def test_creates_nested_files_and_returns_created_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    result = json.loads(create_files_batch(_files(), workspace=root))
    assert result["count"] == 3
    assert result["created"] == [item["path"] for item in _files()]
    assert (root / "src" / "service.py").read_text(encoding="utf-8").startswith("def double")
    assert (root / "src" / "__init__.py").read_bytes() == b""
    assert (root / "tests" / "test_service.py").is_file()
    assert "+def double(value)" in preview_file_creations(
        [{"path": "other/a.py", "content": "def double(value):\n"},
         {"path": "other/b.py", "content": ""}], root,
    )


def test_preflight_rejects_existing_and_duplicate_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "exists.py").write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_files_batch([{"path": "new.py", "content": "new"},
                            {"path": "exists.py", "content": "override"}], workspace=root)
    assert not (root / "new.py").exists()
    assert (root / "exists.py").read_text(encoding="utf-8") == "original"
    invalid = [
        [_files()[0], _files()[0]],
        [{"path": "pkg", "content": "x"}, {"path": "pkg/routes.py", "content": "y"}],
        [{"path": "ok.py", "content": "x"}, {"path": "../outside.py", "content": "y"}],
    ]
    for files in invalid:
        with pytest.raises((OSError, ValueError)):
            prepare_file_creations(files, root)
    assert not (root / "pkg").exists()


def test_rollback_removes_created_files_and_directories(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    original = Path.open

    def fail_second(self, mode="r", *args, **kwargs):
        if self == root / "tests" / "test_service.py" and mode == "xb":
            raise OSError("injected creation failure")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="injected"):
        create_files_batch(_files(), workspace=root)
    assert not (root / "src").exists()
    assert not (root / "tests").exists()


def test_stale_precondition_after_external_create(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    before = creation_precondition(_files(), root)
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("# external", encoding="utf-8")
    with pytest.raises(FileExistsError):
        creation_precondition(_files(), root)
    assert before["new_paths"] == sorted(x["path"] for x in _files())


def test_agent_creates_new_module_and_runs_actual_unittest(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    model = ScriptedModel(replies=[
        call("create_files_batch", files=_files()),
        call("run_command", command="python", args=[
            "-m", "unittest", "discover", "-s", "tests", "-q",
        ]),
        "Created the module and tested it.",
    ])
    with AgentApplication(_settings(root, tmp_path), model=model) as app:
        result = app.run("Build a new service module with a regression test")
    assert result["status"] == "completed", result
    assert (root / "src" / "service.py").is_file()
    assert any(x["event"] == "tool" and x["name"] == "create_files_batch"
               and x["status"] == "success" for x in result["trace"])
    assert any(x["event"] == "tool" and x["name"] == "run_command"
               and x["status"] == "success" for x in result["trace"])


def test_approval_pauses_then_creates_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    model = ScriptedModel(replies=[
        call("create_files_batch", files=_files()),
        "Created the requested files.",
    ])
    with AgentApplication(_settings(root, tmp_path, "ASK"), model=model) as app:
        interrupted = app.run("Create source and tests")
        assert interrupted["status"] == "interrupted"
        assert not (root / "src").exists()
        completed = app.run(None, thread_id=interrupted["session_id"],
                            decisions=[{"type": "approve"}])
    assert completed["status"] == "completed", completed
    assert (root / "src" / "service.py").is_file()


def test_contract_restricts_all_new_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    contract = CodingTaskContract(
        "build", "Create a service only", ("src/service.py",),
        RequiredTestCommand("python", ("-m", "unittest", "discover", "-s", "tests", "-q")),
    )
    model = ScriptedModel(replies=[
        call("create_files_batch", files=_files()),
        "Cannot create files outside allowed paths.",
    ])
    with AgentApplication(_settings(root, tmp_path), model=model) as app:
        result = app.run("Create the service only", contract=contract)
    assert result["status"] != "completed", result
    assert not (root / "src").exists()


def test_create_batch_must_be_called_alone(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    message = call("create_files_batch", files=_files())
    message.tool_calls.append({
        "name": "list_files", "args": {"path": "."},
        "id": "extra-call", "type": "tool_call",
    })
    with AgentApplication(_settings(root, tmp_path), model=ScriptedModel(replies=[message])) as app:
        result = app.run("Create everything in one call")
    assert result["status"] == "failed", result
    assert not (root / "src").exists()
