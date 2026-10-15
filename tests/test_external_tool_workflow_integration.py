"""End-to-end LangGraph regression: explicit extension stays advertised in a workflow."""

from langchain_core.tools import tool
from pydantic import Field

from helpers import ScriptedModel, call
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


class RecordingModel(ScriptedModel):
    advertised_tools: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.advertised_tools.append([item.name for item in tools])
        return super().bind_tools(tools, **kwargs)


def test_langgraph_keeps_explicit_extra_tool_during_inspect_stage(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    @tool
    def custom_repo_probe() -> str:
        """Return an external, explicitly provided project clue."""
        return "remote-project-available"

    custom_repo_probe.metadata = {"risk": "read", "mutation": False}
    model = RecordingModel(replies=[
        call("start_development_workflow", goal="Analyze the external project clue",
             profile="general"),
        call("custom_repo_probe"),
        call("list_files"),
        "Discovered the external project clue; development is still incomplete.",
    ])
    settings = Settings(
        workspace=workspace, state_dir=tmp_path / "state",
        approval_mode="ALLOW", max_model_calls=12,
    )
    with AgentApplication(settings, model=model, extra_tools=[custom_repo_probe]) as app:
        result = app.run("Analyze the project with the supplied custom tool")
        messages = app.graph.get_state(
            app.config(result["session_id"]),
        ).values["messages"]
    assert result["status"] == "incomplete", result
    assert any(
        message.name == "custom_repo_probe" and "remote-project-available" in str(message.content)
        for message in messages if message.type == "tool"
    )
    assert len(model.advertised_tools) >= 3
    # First request has the full registry. Later calls are stage-filtered,
    # while explicitly injected extension remains advertised to the model.
    assert "start_development_workflow" in model.advertised_tools[0]
    assert "custom_repo_probe" in model.advertised_tools[1]
    assert "github_push_branch" not in model.advertised_tools[1]
