"""Cross-language Tree-sitter navigation (optional parser tests)."""

import json

import pytest

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
    pytest.importorskip("tree_sitter_language_pack")
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
