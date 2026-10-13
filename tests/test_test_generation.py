"""Structured, executable pytest regression generation tests."""

import json
import subprocess
import sys

import pytest

from mini_agent.test_generation import generate_pytest_regressions


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "calculator.py").write_text(
        "def add(x, y):\n    return x + y\n\n"
        "def safe_divide(x, y):\n    return x / y\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    return root


def test_generated_pytest_cases_are_executable(workspace):
    result = generate_pytest_regressions(
        workspace=workspace, source_path="calculator.py", function_name="add",
        output_path="tests/test_add_regression.py",
        cases=[
            {"id": "positive", "args": [2, 3], "expected": 5},
            {"id": "negative", "args": [-2, 2], "expected": 0},
            {"id": "type_error", "args": ["a", 1], "raises": "TypeError"},
        ],
    )
    assert result.status == "success"
    file = workspace / "tests" / "test_add_regression.py"
    text = file.read_text(encoding="utf-8")
    assert "pytest.mark.parametrize" in text
    assert "expected" in text
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_add_regression.py"],
        cwd=workspace, check=False, capture_output=True, text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_generator_avoids_overwriting_existing_tests(workspace):
    output = workspace / "tests" / "test_existing.py"
    output.write_text("original\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        generate_pytest_regressions(
            workspace=workspace, source_path="calculator.py",
            function_name="add", output_path="tests/test_existing.py",
            cases=[{"expected": 0, "args": [0, 0]}],
        )
    assert output.read_text(encoding="utf-8") == "original\n"


def test_invalid_cases_are_rejected(workspace):
    template = {
        "workspace": workspace, "source_path": "calculator.py",
        "function_name": "add", "output_path": "tests/test_invalid.py",
    }
    with pytest.raises(ValueError, match="expected 或 raises"):
        generate_pytest_regressions(cases=[{"args": [1, 2]}], **template)
    with pytest.raises(ValueError, match="支持的内置异常"):
        generate_pytest_regressions(cases=[{"raises": "SystemExit"}], **template)
    with pytest.raises(ValueError, match="JSON"):
        generate_pytest_regressions(
            cases=[{"args": [float("nan"), 0], "expected": 0}], **template,
        )
    assert not (workspace / "tests" / "test_invalid.py").exists()


def test_generated_parameters_are_data_not_executable_code(workspace):
    tricky = "__import__('os').system('echo NEVER_RUN')"
    result = generate_pytest_regressions(
        workspace=workspace, source_path="calculator.py",
        function_name="add", output_path="tests/test_safe_strings.py",
        cases=[{"id": "malicious-string-as-data", "args": [tricky, "suffix"], "expected": tricky + "suffix"}],
    )
    assert result.status == "success"
    assert tricky in json.dumps({"value": tricky})
    assert "json.loads(" in (workspace / "tests/test_safe_strings.py").read_text(encoding="utf-8")
