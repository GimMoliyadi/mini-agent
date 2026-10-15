"""Stage-aware tool schema exposure with the same underlying registered tools."""

from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from mini_agent.development_workflow import (
    DevelopmentWorkflowMiddleware, focus_model_tools,
)


def _tool(name):
    return SimpleNamespace(name=name)


def test_stage_focus_shows_only_relevant_tool_schemas():
    names = [
        "read_file", "search_text", "set_task_plan", "update_task_step",
        "create_files_batch", "generate_openapi_types", "run_fullstack_checks",
        "browser_action", "review_code_changes", "github_push_branch",
    ]
    tools = [_tool(name) for name in names]
    inspect = focus_model_tools(tools, "inspect")
    assert "read_file" in [tool.name for tool in inspect]
    assert "set_task_plan" in [tool.name for tool in inspect]
    assert "github_push_branch" not in [tool.name for tool in inspect]
    assert "browser_action" not in [tool.name for tool in inspect]
    implement = focus_model_tools(tools, "implement")
    assert "generate_openapi_types" in [tool.name for tool in implement]
    assert "create_files_batch" in [tool.name for tool in implement]
    assert "github_push_branch" not in [tool.name for tool in implement]
    testing = focus_model_tools(tools, "test")
    assert "run_fullstack_checks" in [tool.name for tool in testing]
    delivery = focus_model_tools(tools, "deliver")
    assert "github_push_branch" in [tool.name for tool in delivery]
    assert "generate_openapi_types" not in [tool.name for tool in delivery]
    assert len(tools) == len(names)  # the registry remains untouched


def test_unconfigured_workflow_preserves_full_tool_set():
    tools = [_tool("custom_extra_tool"), _tool("read_file")]
    assert focus_model_tools(tools, None) == tools
    assert focus_model_tools(tools, "unknown") == tools


def test_workflow_model_middleware_filters_without_modifying_request():
    class Request:
        def __init__(self):
            self.state = {
                "task_start": 0, "messages": [],
                "development_workflow": {
                    "goal": "Fix an existing React backend feature",
                    "profile": "web", "require_browser": True,
                    "require_migration": False, "deliver_pr": False,
                },
            }
            self.system_message = SystemMessage(content="System instruction")
            self.tools = [
                _tool("read_file"), _tool("list_files"), _tool("browser_action"),
                _tool("write_file"), _tool("github_push_branch"),
            ]

        def override(self, **kwargs):
            clone = SimpleNamespace(**self.__dict__)
            for key, value in kwargs.items():
                setattr(clone, key, value)
            return clone

    request = Request()
    middleware = DevelopmentWorkflowMiddleware()
    received = []
    middleware.wrap_model_call(request, lambda updated: received.append(updated))
    assert [tool.name for tool in received[0].tools] == ["read_file", "list_files"]
    assert "下一项尚未完成：inspect" in received[0].system_message.content
    assert len(request.tools) == 5
    assert request.system_message.content == "System instruction"
