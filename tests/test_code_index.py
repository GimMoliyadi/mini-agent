"""Cross-language Tree-sitter navigation (optional parser tests)."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
from scripts.check import isolated_environment

from mini_agent.code_index import build_code_index_tools


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "workspace"
    backend = workspace / "backend"
    frontend = workspace / "frontend" / "src"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (backend / "main.py").write_text(
        "class UserStore:\n"
        "    def lookup(self, user_id):\n"
        "        return user_id\n\n"
        "def list_users():\n"
        "    value = 'lookup'\n"
        "    # lookup is only a comment\n"
        "    return UserStore().lookup(4)\n",
        encoding="utf-8",
    )
    (frontend / "client.ts").write_text(
        "export class UserClient { lookup() { return 2; } }\n"
        "export function listUsers() { return new UserClient().lookup(); }\n"
        'const fake = "lookup"; // lookup in comment\n',
        encoding="utf-8",
    )
    return workspace


def test_tools_register_read_only_without_optional_package(tmp_path):
    tools = {tool.name: tool for tool in build_code_index_tools(tmp_path)}
    assert set(tools) == {"inspect_code_symbols", "find_code_references"}
    assert all(tool.metadata["risk"] == "read" for tool in tools.values())


def test_python_typescript_declarations_and_identifier_occurrences(project):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    pytest.importorskip("tree_sitter_typescript")
    tools = {tool.name: tool for tool in build_code_index_tools(project)}
    indexed = json.loads(tools["inspect_code_symbols"].invoke({
        "path": ".", "query": "User", "max_results": 40,
    }))
    assert {"UserStore", "UserClient"} <= {entry["name"] for entry in indexed["items"]}
    occurrences = json.loads(tools["find_code_references"].invoke({
        "symbol": "lookup", "path": ".", "max_results": 40,
    }))
    assert len(occurrences["items"]) >= 3
    assert all(
        "comment" not in entry["source"] or "lookup" in entry["source"]
        for entry in occurrences["items"]
    )
    # Only actual syntax identifiers should match, not the quoted literal.
    assert not any(entry["source"].strip() == 'const fake = "lookup"; // lookup in comment'
                   for entry in occurrences["items"])


def test_reference_search_rejects_non_identifier_and_bad_scope(project):
    tools = {tool.name: tool for tool in build_code_index_tools(project)}
    with pytest.raises(Exception):
        tools["find_code_references"].invoke({"symbol": "bad.name", "path": "."})
    with pytest.raises(Exception):
        tools["inspect_code_symbols"].invoke({"path": "../escape"})


def test_installed_grammars_work_in_isolated_environment_without_cache(tmp_path):
    for module in ("tree_sitter", "tree_sitter_python", "tree_sitter_javascript", "tree_sitter_typescript"):
        pytest.importorskip(module)
    environment = isolated_environment(tmp_path)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    source = (
        "import socket\n"
        "def forbidden(*args, **kwargs): raise AssertionError('Unexpected network access')\n"
        "socket.create_connection = forbidden\n"
        "from mini_agent.code_index import _parser\n"
        "for language in ['python', 'javascript', 'typescript', 'tsx']:\n"
        "    assert not _parser(language).parse(b'').root_node.has_error\n"
        "print('4 offline grammars loaded')\n"
    )
    result = subprocess.run([sys.executable, "-c", source], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "4 offline grammars loaded" in result.stdout


def test_javascript_and_tsx_definitions_and_real_references(project):
    for module in ("tree_sitter", "tree_sitter_javascript", "tree_sitter_typescript"):
        pytest.importorskip(module)
    frontend = project / "frontend/src"
    (frontend / "client.js").write_text(
        'export class UserJS { lookup() { return 2; } }\n'
        'const fake = "lookup"; // lookup in comment\n', encoding="utf-8",
    )
    (frontend / "card.tsx").write_text(
        'function lookup() { return 2; }\n'
        'export function UserCard() { return <button onClick={() => lookup()}>lookup</button>; }\n'
        'const fake = "lookup"; // lookup in comment\n', encoding="utf-8",
    )
    tools = {tool.name: tool for tool in build_code_index_tools(project)}
    symbols = json.loads(tools["inspect_code_symbols"].invoke({"path": "frontend", "query": "User"}))
    assert {"UserJS", "UserCard"} <= {item["name"] for item in symbols["items"]}
    references = json.loads(tools["find_code_references"].invoke({"path": "frontend", "symbol": "lookup"}))
    assert {"frontend/src/client.js", "frontend/src/card.tsx"} <= {item["file"] for item in references["items"]}
    assert not any(item["source"].startswith("const fake") for item in references["items"])
