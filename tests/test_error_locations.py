"""Failure-location hints extracted from real-looking Python and JS/TS logs."""

from mini_agent import dev_environment
from mini_agent.error_locations import extract_failure_locations
from mini_agent.process_runner import ProcessResult


def test_python_traceback_and_pytest_locations(tmp_path):
    root = tmp_path / "api"
    root.mkdir()
    stderr = (
        'Traceback (most recent call last):\n'
        '  File "src/service.py", line 23, in load\n'
        '    raise ValueError("missing item")\n'
        'ValueError: missing item\n'
    )
    stdout = "tests/test_service.py:11: AssertionError: expected 2, got 1\n"
    found = extract_failure_locations(stdout, stderr, root)
    assert [(x["file"], x["line"]) for x in found] == [
        ("src/service.py", 23), ("tests/test_service.py", 11),
    ]
    assert found[0]["kind"] == "traceback"
    assert found[1]["kind"] == "source"


def test_typescript_and_vite_locations(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    stderr = (
        "src/store.ts(8,12): error TS2322: Type string is not assignable to number\n"
        "src/components/App.tsx:20:5: Unexpected token\n"
    )
    found = extract_failure_locations("", stderr, root)
    assert len(found) == 2
    assert found[0]["file"] == "src/store.ts"
    assert found[0]["line"] == 8
    assert found[0]["column"] == 12
    assert "TS2322" in found[0]["message"]
    assert found[1]["file"] == "src/components/App.tsx"
    assert found[1]["column"] == 5


def test_ignore_duplicate_and_out_of_project_locations(tmp_path):
    root = tmp_path / "api"
    root.mkdir()
    logs = (
        "src/service.py:3: syntax error\n"
        "src/service.py:3: repeated hint\n"
        "../outside.py:9: ignore\n"
        ".venv/lib/dep.py:10: ignore generated code\n"
    )
    found = extract_failure_locations("", logs, root)
    assert len(found) == 1
    assert found[0]["file"] == "src/service.py"


def test_failed_dev_command_returns_structured_locations(tmp_path, monkeypatch):
    root = tmp_path / "backend"
    root.mkdir()
    def fake_runner(argv, cwd, **kwargs):
        return ProcessResult(returncode=1, stdout=b"",
                             stderr=b"app.py:7: NameError: undefined_symbol\n")
    monkeypatch.setattr(dev_environment, "run_process", fake_runner)
    result = dev_environment._execute(["python", "-m", "unittest"], root, root, timeout=60)
    assert result.status == "command_failed"
    assert result.metadata["diagnostics"][0]["file"] == "app.py"
    assert result.metadata["diagnostics"][0]["line"] == 7
    assert "ERROR_LOCATIONS" in result.text


def test_successful_command_reports_empty_diagnostics(tmp_path, monkeypatch):
    root = tmp_path / "backend"
    root.mkdir()
    monkeypatch.setattr(dev_environment, "run_process", lambda *a, **k: ProcessResult(
        returncode=0, stdout=b"tests passed\n", stderr=b"",
    ))
    result = dev_environment._execute(["python", "-m", "unittest"], root, root, timeout=60)
    assert result.status == "success"
    assert result.metadata["diagnostics"] == []
    assert "ERROR_LOCATIONS" not in result.text
