"""Generate executable pytest regression cases from explicit expected behavior.

The existing LangGraph model selects edge/error cases using code inspection
and user requirements; this helper writes deterministic pytest code and does
not hallucinate expected outputs or create a second model/agent.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from typing import Any

from langchain.tools import tool

from . import sandbox
from . import dev_environment as dev
from .result import ToolResult

_MAX_CASES = 30
_MAX_PAYLOAD = 16000
_RAISES = frozenset({
    "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    "OverflowError", "AssertionError", "NotImplementedError", "LookupError",
})
_PY_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def _validate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise ValueError(f"测试案例数量必须为 1–{_MAX_CASES}")
    validated: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"案例 {index + 1} 不是对象")
        if set(case) - {"args", "kwargs", "expected", "raises", "id"}:
            raise ValueError("每个案例只允许 args、kwargs、expected、raises、id")
        args, kwargs = case.get("args", []), case.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("args 须为列表，kwargs 须为对象")
        if any(not isinstance(k, str) or not _PY_IDENTIFIER.fullmatch(k) for k in kwargs):
            raise ValueError("kwargs 键必须是 Python 标识符")
        has_expected = "expected" in case
        has_error = "raises" in case
        if has_expected == has_error:
            raise ValueError("每个案例必须提供 expected 或 raises，不能同时提供")
        if has_error and case["raises"] not in _RAISES:
            raise ValueError("raises 必须是受支持的内置异常类型")
        label = case.get("id", f"case-{index + 1}")
        if not isinstance(label, str) or not 1 <= len(label) <= 80:
            raise ValueError("案例 id 长度必须为 1–80")
        record = {"args": args, "kwargs": kwargs, "id": label}
        record["expected" if has_expected else "raises"] = (
            case["expected"] if has_expected else case["raises"]
        )
        validated.append(record)
    json.dumps(validated, ensure_ascii=False, allow_nan=False)
    return validated


def generate_pytest_regressions(
    *, workspace: Path,
    source_path: str,
    function_name: str,
    output_path: str,
    cases: list[dict[str, Any]],
) -> ToolResult:
    """Write a new parametric pytest file; never overwrite existing tests."""
    root = workspace.resolve()
    source = sandbox.resolve_path(source_path, root)
    output = sandbox.resolve_path(output_path, root)
    if not source.is_file() or source.suffix != ".py":
        raise ValueError("source_path 必须是工作区内已存在的 Python 源文件")
    if not _PY_IDENTIFIER.fullmatch(function_name):
        raise ValueError("function_name 必须是 Python 函数标识符")
    if not output.name.startswith("test_") or output.suffix != ".py":
        raise ValueError("output_path 必须是 test_*.py")
    if output.exists() or output.is_symlink():
        raise FileExistsError("目标测试文件已存在；请选择新文件名或用 apply_patch 修改")
    if not output.parent.is_dir():
        raise FileNotFoundError("输出目录尚不存在，请先用 create_files_batch 创建目录")
    source_bytes = sandbox.read_bounded_bytes(source)
    tree = ast.parse(source_bytes.decode("utf-8-sig"), filename=source_path)
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    if len(functions) != 1:
        raise ValueError("源码中未找到唯一的顶层函数；方法/嵌套函数暂不支持")
    if isinstance(functions[0], ast.AsyncFunctionDef):
        raise ValueError("当前生成器仅支持同步顶层函数；async 使用现有文件编辑工具创建测试")
    normalized = _validate_cases(cases)
    payload = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD:
        raise ValueError("测试案例数据过长，请拆分为多个测试文件")
    module = ".".join(source.relative_to(root).with_suffix("").parts)
    if not all(_PY_IDENTIFIER.fullmatch(part) for part in module.split(".")):
        raise ValueError("源码模块路径不能转换为 Python import 路径")
    # Use an encoded JSON string literal, not executable user-supplied Python.
    test_text = (
        '"""Regression cases generated from explicit expected outputs by mini-agent."""\n\n'
        'import builtins\nimport importlib\nimport json\n\nimport pytest\n\n'
        f"_CASES = json.loads({payload!r})\n"
        f"_MODULE = {module!r}\n"
        f"_FUNCTION = {function_name!r}\n\n\n"
        "@pytest.mark.parametrize('case', _CASES, ids=[case['id'] for case in _CASES])\n"
        "def test_generated_regression(case):\n"
        "    target = getattr(importlib.import_module(_MODULE), _FUNCTION)\n"
        "    if 'raises' in case:\n"
        "        with pytest.raises(getattr(builtins, case['raises'])):\n"
        "            target(*case['args'], **case['kwargs'])\n"
        "    else:\n"
        "        actual = target(*case['args'], **case['kwargs'])\n"
        "        assert actual == case['expected']\n"
    )
    binary = sandbox.encode_content(test_text)
    with sandbox.file_change({output: binary}, root):
        if output.exists():
            raise FileExistsError("批准后测试文件被其他程序创建，请重新生成")
        sandbox.atomic_write_bytes(output, binary)
    return ToolResult(
        sandbox.redact_text(json.dumps({
            "generated": output.relative_to(root).as_posix(),
            "source": source.relative_to(root).as_posix(),
            "function": function_name, "cases": len(normalized),
            "next_step": "Run run_dev_task(task='test') against this file; generated assertions are not proof of correct behavior.",
        }, ensure_ascii=False)),
        "success", "regression_tests_created",
        {"output": output.relative_to(root).as_posix(), "cases": len(normalized)},
    )


def build_test_generation_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool(response_format="content_and_artifact")
    def generate_pytest_regressions_tool(
        source_path: str, function_name: str, output_path: str,
        cases: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """Generate a new executable pytest test_*.py file from expected results.

        Inspect the target source and user requirements first; specify 1–30
        concrete cases with JSON args/kwargs and either expected or raises.
        Use normal code tools for complex fixtures, async, mocking, or tests
        of side effects. Generated tests still require execution.
        """
        return dev._outcome(generate_pytest_regressions(
            workspace=root, source_path=source_path,
            function_name=function_name, output_path=output_path,
            cases=cases,
        ))

    generate_pytest_regressions_tool.name = "generate_pytest_regressions"
    generate_pytest_regressions_tool.metadata = {
        "risk": "write", "mutation": True,
        "workspace_arguments": ("source_path", "output_path"),
        "mutation_paths": ("output_path",),
    }
    return [generate_pytest_regressions_tool]
