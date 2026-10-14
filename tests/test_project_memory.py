"""Project memory persists across sessions and isolates different workspaces."""

from helpers import ScriptedModel, call
from mini_agent.project_memory import ProjectMemoryStore
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


def test_project_facts_survive_store_recreation(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    state = tmp_path / "state"
    first = ProjectMemoryStore(root, state)
    assert first.remember("architecture", "api", "FastAPI backend")["saved"]
    assert first.remember("todo", "frontend", "Connect user list")["saved"]
    second = ProjectMemoryStore(root, state)
    all_facts = second.recall()["facts"]
    assert len(all_facts) == 2
    assert second.recall("user list")["facts"][0]["key"] == "frontend"
    assert second.remember("todo", "frontend", "Finished frontend")["saved"]
    assert second.recall("Finished frontend")["returned"] == 1
    assert second.delete("todo", "frontend")["deleted"] is True
    assert second.recall("frontend")["returned"] == 0


def test_facts_do_not_leak_into_unrelated_project(tmp_path):
    a = tmp_path / "project_a"
    b = tmp_path / "project_b"
    a.mkdir()
    b.mkdir()
    state = tmp_path / "state"
    ProjectMemoryStore(a, state).remember("decision", "storage", "SQLite")
    assert ProjectMemoryStore(b, state).recall()["facts"] == []


def test_registered_agent_tools_store_and_recall_between_sessions(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    settings = Settings(
        workspace=root, state_dir=tmp_path / "state", approval_mode="ALLOW",
    )
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("remember_project_fact", category="feature", key="typed-api",
             content="React Query types generated"),
        "Recorded the project's API feature.",
    ])) as app:
        outcome = app.run("Save the new feature")
    assert outcome["status"] == "completed", outcome
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("recall_project_facts", query="typed-api"),
        "Found the previously saved API feature.",
    ])) as app:
        recall = app.run("What did I implement last session?")
    assert recall["status"] == "completed", recall
    assert ProjectMemoryStore(root, settings.state_dir).recall("typed-api")["returned"] == 1
