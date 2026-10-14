"""Reversible Alembic migration scripts without applying database changes."""

import ast
import json
import runpy

import pytest

from mini_agent import alembic_migrations as migration
from mini_agent.result import ToolResult


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "workspace"
    backend = root / "backend"
    versions = backend / "alembic" / "versions"
    versions.mkdir(parents=True)
    (backend / "requirements.txt").write_text(
        "sqlalchemy\nalembic\n", encoding="utf-8",
    )
    (backend / "alembic.ini").write_text(
        "[alembic]\nscript_location = alembic\n", encoding="utf-8",
    )
    return root


def test_generates_valid_upgrade_and_reverse_order_downgrade(project):
    result = migration.generate_alembic_migration(
        workspace=project, backend_path="backend",
        output_path="backend/alembic/versions/000002_users.py",
        revision="000002", down_revision="000001",
        message="Add users table and email index",
        changes=[
            {"action": "create_table", "table": "users", "columns": [
                {"name": "id", "type": "Integer", "primary_key": True, "nullable": False},
                {"name": "email", "type": "String", "length": 255, "nullable": False},
            ]},
            {"action": "create_index", "name": "ix_users_email", "table": "users",
             "columns": ["email"], "unique": True},
        ],
    )
    assert result.status == "success"
    assert result.metadata["revision"] == "000002"
    source = (project / "backend/alembic/versions/000002_users.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert "op.create_table('users'" in source
    assert "op.create_index('ix_users_email'" in source
    assert source.index("op.drop_index(") < source.index("op.drop_table(")
    assert "down_revision = '000001'" in source
    assert "applied_to_database" in json.loads(result.text)
    assert json.loads(result.text)["applied_to_database"] is False


def test_add_column_renders_sqlalchemy_and_downgrade(project):
    migration.generate_alembic_migration(
        workspace=project, backend_path="backend",
        output_path="backend/alembic/versions/abcdef_status.py",
        revision="abcdef", down_revision=None,
        message="Add status",
        changes=[{"action": "add_column", "table": "users", "column": {
            "name": "status", "type": "String", "length": 64,
            "nullable": True,
        }}],
    )
    source = (project / "backend/alembic/versions/abcdef_status.py").read_text(encoding="utf-8")
    assert "sa.Column('status', sa.String(64), nullable=True)" in source
    assert "op.drop_column('users', 'status')" in source


def test_revision_will_not_overwrite_existing_file_or_allow_code_injection(project):
    output = project / "backend/alembic/versions/aabbcc_demo.py"
    output.write_text("original", encoding="utf-8")
    common = {
        "workspace": project, "backend_path": "backend",
        "output_path": "backend/alembic/versions/aabbcc_demo.py",
        "revision": "aabbcc", "down_revision": None, "message": "demo",
    }
    with pytest.raises(FileExistsError):
        migration.generate_alembic_migration(
            **common, changes=[{"action": "add_column", "table": "users",
                                "column": {"name": "name", "type": "String"}}],
        )
    assert output.read_text(encoding="utf-8") == "original"
    common["output_path"] = "backend/alembic/versions/aabbcc_new.py"
    with pytest.raises(ValueError, match="SQL 标识符"):
        migration.generate_alembic_migration(
            **common, changes=[{"action": "add_column", "table": "users; DROP TABLE users",
                                "column": {"name": "name", "type": "String"}}],
        )


def test_alembic_status_uses_backend_project_python_and_never_upgrades(project, monkeypatch):
    import os

    executable = project / "backend/.venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"dummy interpreter")
    calls = []

    def fake_execute(argv, cwd, workspace, *, timeout):
        calls.append(argv)
        return ToolResult("heads: 000001", "success", "command_succeeded", {"returncode": 0})

    monkeypatch.setattr(migration.dev, "_execute", fake_execute)
    status = migration.inspect_alembic_status(workspace=project, action="heads")
    assert status.status == "success"
    assert calls[0][1:] == ["-m", "alembic", "-c", "alembic.ini", "heads"]
    assert "upgrade" not in calls[0]


def test_generated_revision_executes_and_rolls_back_real_sqlite(project):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("alembic")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    output = "backend/alembic/versions/abcdef_tasks.py"
    result = migration.generate_alembic_migration(
        workspace=project, backend_path="backend", output_path=output,
        revision="abcdef", down_revision=None, message="Create tasks",
        changes=[{"action": "create_table", "table": "tasks", "columns": [
            {"name": "id", "type": "Integer", "primary_key": True, "nullable": False},
            {"name": "title", "type": "String", "length": 100, "nullable": False},
        ]}, {"action": "create_index", "name": "ix_tasks_title", "table": "tasks",
             "columns": ["title"]}],
    )
    assert result.status == "success"
    revision = runpy.run_path(str(project / output))
    engine = sqlalchemy.create_engine(f"sqlite:///{project / 'migration.db'}")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision["upgrade"]()
                connection.exec_driver_sql("INSERT INTO tasks (title) VALUES ('verified')")
                assert connection.exec_driver_sql("SELECT title FROM tasks").scalar_one() == "verified"
                assert sqlalchemy.inspect(connection).get_indexes("tasks")[0]["name"] == "ix_tasks_title"
                revision["downgrade"]()
                assert "tasks" not in sqlalchemy.inspect(connection).get_table_names()
    finally:
        engine.dispose()
