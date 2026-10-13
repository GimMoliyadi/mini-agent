"""Review changed working-tree code with actual Git history (offline)."""

import json
import subprocess

from mini_agent.code_review import review_code_changes


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_review_detects_ast_and_json_regressions(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    source = root / "app.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial source")
    source.write_text(
        "def calc(a, seen=[]):\n    try:\n        return a\n    except:\n        pass\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "bad.json").write_text('{"not valid": ', encoding="utf-8")
    review = json.loads(review_code_changes(workspace=root))
    codes = {item["code"] for item in review["findings"]}
    assert {"MUTABLE_DEFAULT", "BARE_EXCEPT", "PY_SYNTAX", "INVALID_JSON", "TEST_GAP"} <= codes
    assert "app.py" in review["changed_files"]
    assert "broken.py" in review["changed_files"]
    assert "bad.json" in review["changed_files"]
    assert "def calc" in review["diff_excerpt"]


def test_review_does_not_modify_worktree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    file = root / "app.py"
    file.write_text("x = 2\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    before = file.read_bytes()
    report = json.loads(review_code_changes(workspace=root))
    assert report["changed_files"] == []
    assert report["findings"] == []
    assert file.read_bytes() == before
