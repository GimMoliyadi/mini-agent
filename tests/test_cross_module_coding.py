"""Real cross-module navigation and coordinated multi-file coding tests."""

import json

import pytest

from helpers import ScriptedModel, call
from mini_agent import multi_file
from mini_agent.multi_file import (
    apply_patch_batch, patch_batch_precondition, prepare_patch_batch,
)
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.tools.module_map import inspect_module_dependencies
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredTestCommand


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "logic.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8",
    )
    (root / "src" / "service.py").write_text(
        "from src.logic import add\n\n"
        "def total(a, b):\n    return add(a, b) * 2\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_service.py").write_text(
        "import unittest\nfrom src.service import total\n\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_total(self):\n        self.assertEqual(total(2, 3), 10)\n",
        encoding="utf-8",
    )
    return root


def _patches():
    return [
        {"path": "src/logic.py", "old_text": "return a - b", "new_text": "return a + b"},
        {"path": "src/service.py", "old_text": "return add(a, b) * 2", "new_text": "return add(a, b) * 2 + 0"},
    ]


def _settings(root, tmp_path, mode="ALLOW"):
    return Settings(workspace=root, state_dir=tmp_path / "state", approval_mode=mode)


def test_python_reverse_impact_and_graph_closure(repository):
    graph = json.loads(inspect_module_dependencies(workspace=repository, focus="src/logic.py"))
    assert graph["focus"] == "src/logic.py"
    assert "src/service.py" in graph["direct_dependents"]
    assert "tests/test_service.py" in graph["transitive_dependents"]
    assert graph["truncated"] is False
    assert any(edge["from"] == "src/service.py" and edge["to"] == "src/logic.py"
               for edge in graph["edges"])


def test_js_ts_dependency_graph_handles_relative_imports(repository):
    (repository / "web").mkdir()
    (repository / "web" / "util.ts").write_text(
        "export const format = (value: number) => String(value)\n", encoding="utf-8",
    )
    (repository / "web" / "store.ts").write_text(
        'import { format } from "./util"\nexport const store = format(2)\n', encoding="utf-8",
    )
    (repository / "web" / "app.tsx").write_text(
        'import { store } from "./store"\nexport default store\n', encoding="utf-8",
    )
    graph = json.loads(inspect_module_dependencies(workspace=repository, focus="web/util.ts"))
    assert graph["direct_dependents"] == ["web/store.ts"]
    assert "web/app.tsx" in graph["transitive_dependents"]


def test_batch_validates_all_files_before_first_write(repository):
    patches = _patches()
    patches[1]["old_text"] = "does not exist"
    with pytest.raises(ValueError):
        apply_patch_batch(patches, workspace=repository)
    assert "return a - b" in (repository / "src" / "logic.py").read_text()
    assert "return add(a, b) * 2" in (repository / "src" / "service.py").read_text()


def test_atomic_rollback_after_second_file_write_failure(repository, monkeypatch):
    original_write = multi_file.sandbox.atomic_write_bytes
    count = [0]

    def failing_write(path, content):
        count[0] += 1
        if count[0] == 2:
            raise OSError("simulated disk error")
        return original_write(path, content)

    monkeypatch.setattr(multi_file.sandbox, "atomic_write_bytes", failing_write)
    with pytest.raises(OSError, match="simulated disk error"):
        apply_patch_batch(_patches(), workspace=repository)
    assert "return a - b" in (repository / "src" / "logic.py").read_text()
    assert "return add(a, b) * 2 + 0" not in (repository / "src" / "service.py").read_text()



@pytest.mark.parametrize("failure_on", [1, 2])
def test_batch_rolls_back_write_that_succeeded_before_reporting_error(
    repository, monkeypatch, failure_on,
):
    """A late fsync/close failure must undo even the in-flight write."""
    paths = [repository / "src" / "logic.py", repository / "src" / "service.py"]
    original = {path: path.read_bytes() for path in paths}
    real_atomic_write = multi_file.sandbox.atomic_write_bytes
    count = [0]

    def late_write_failure(path, content):
        count[0] += 1
        real_atomic_write(path, content)
        if count[0] == failure_on:
            raise OSError("simulated post-replace I/O error")

    monkeypatch.setattr(multi_file.sandbox, "atomic_write_bytes", late_write_failure)
    with pytest.raises(OSError, match="post-replace I/O error"):
        apply_patch_batch(_patches(), workspace=repository)
    assert {path: path.read_bytes() for path in paths} == original


def test_path_escape_duplicate_and_virtualenv_are_rejected(repository):
    for edits in [
        [_patches()[0], _patches()[0]],
        [_patches()[0], {"path": "../secrets.py", "old_text": "a", "new_text": "b"}],
    ]:
        with pytest.raises((ValueError, PermissionError)):
            prepare_patch_batch(edits, repository)
    (repository / ".venv").mkdir()
    (repository / ".venv" / "internal.py").write_text("foo\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        prepare_patch_batch([_patches()[0], {
            "path": ".venv/internal.py", "old_text": "foo", "new_text": "bar",
        }], repository)


def test_precondition_changes_when_any_file_changes(repository):
    original = patch_batch_precondition(_patches(), repository)
    (repository / "src" / "service.py").write_text(
        "from src.logic import add\n\ndef total(a, b):\n    return add(a, b) * 2\n# unrelated\n",
        encoding="utf-8",
    )
    updated = patch_batch_precondition(_patches(), repository)
    assert original != updated


def test_real_agent_repairs_two_modules_then_runs_unittest(repository, tmp_path):
    model = ScriptedModel(replies=[
        call("inspect_module_dependencies", path=".", focus="src/logic.py"),
        call("apply_patch_batch", patches=_patches()),
        call("run_command", command="python",
             args=["-m", "unittest", "discover", "-s", "tests", "-q"]),
        "Implemented and verified the cross-module change.",
    ])
    with AgentApplication(_settings(repository, tmp_path), model=model) as app:
        result = app.run("Fix addition in logic and update service consistently")
    assert result["status"] == "completed", result
    assert "return a + b" in (repository / "src" / "logic.py").read_text()
    assert "return add(a, b) * 2 + 0" in (repository / "src" / "service.py").read_text()
    assert any(event["event"] == "tool" and event["name"] == "apply_patch_batch"
               and event["status"] == "success" for event in result["trace"])
    assert any(event["event"] == "tool" and event["name"] == "run_command"
               and event["status"] == "success" for event in result["trace"])


def test_contract_rejects_batch_if_one_path_is_outside_allowlist(repository, tmp_path):
    contract = CodingTaskContract(
        "bounded", "Fix logic only", ("src/logic.py",),
        RequiredTestCommand("python", ("-m", "unittest", "discover", "-s", "tests", "-q")),
    )
    model = ScriptedModel(replies=[
        call("apply_patch_batch", patches=_patches()),
        "Not permitted to edit service.",
    ])
    with AgentApplication(_settings(repository, tmp_path), model=model) as app:
        result = app.run("Fix logic only", contract=contract)
    assert result["status"] != "completed", result
    assert "return a - b" in (repository / "src" / "logic.py").read_text()
    assert "return add(a, b) * 2 + 0" not in (repository / "src" / "service.py").read_text()


def test_hitl_requests_single_batch_approval(repository, tmp_path):
    model = ScriptedModel(replies=[
        call("apply_patch_batch", patches=_patches()),
        "Approved change completed.",
    ])
    with AgentApplication(_settings(repository, tmp_path, mode="ASK"), model=model) as app:
        pause = app.run("Update both modules")
        assert pause["status"] == "interrupted"
        assert "return a - b" in (repository / "src" / "logic.py").read_text()
        approved = app.run(None, thread_id=pause["session_id"],
                           decisions=[{"type": "approve"}])
    assert approved["status"] == "completed", approved
    assert "return a + b" in (repository / "src" / "logic.py").read_text()



def test_hitl_rejects_stale_second_file_without_partial_changes(repository, tmp_path):
    model = ScriptedModel(replies=[
        call("apply_patch_batch", patches=_patches()),
        "Changes were applied.",
    ])
    with AgentApplication(_settings(repository, tmp_path, mode="ASK"), model=model) as app:
        pause = app.run("Fix both source modules")
        assert pause["status"] == "interrupted"
        service = repository / "src" / "service.py"
        service.write_text(service.read_text(encoding="utf-8") + "# external edit\\n", encoding="utf-8")
        result = app.run(None, thread_id=pause["session_id"], decisions=[{"type": "approve"}])
    assert result["status"] != "completed", result
    assert "return a - b" in (repository / "src" / "logic.py").read_text()
    assert "# external edit\\n" in service.read_text(encoding="utf-8")
    assert "return add(a, b) * 2 + 0" not in service.read_text(encoding="utf-8")


def test_batch_tool_must_be_only_call_in_model_batch(repository, tmp_path):
    initial = call("apply_patch_batch", patches=_patches())
    initial.tool_calls.append({
        "name": "read_file", "args": {"path": "src/logic.py"},
        "id": "extra-read", "type": "tool_call",
    })
    with AgentApplication(_settings(repository, tmp_path), model=ScriptedModel(replies=[initial])) as app:
        result = app.run("Do the updates simultaneously")
    assert result["status"] == "failed"
    assert "单独" in result["error"] or "冲突" in result["error"]


def test_navigation_bounds_and_missing_focus(repository):
    with pytest.raises(ValueError):
        inspect_module_dependencies(workspace=repository, max_files=0)
    with pytest.raises(PermissionError):
        inspect_module_dependencies(workspace=repository, path="../")
    with pytest.raises(FileNotFoundError):
        inspect_module_dependencies(workspace=repository, focus="src/missing.py")
