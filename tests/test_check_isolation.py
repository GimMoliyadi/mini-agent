import shutil
from unittest.mock import patch

from scripts.check import ignored_entries, isolated_project


def test_temp_path_alias_is_canonical_before_building_child_environment(tmp_path):
    root = tmp_path.resolve()
    source = root / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "alias").mkdir()
    check_root = root / "check"
    check_root.mkdir()
    alias = root / "alias" / ".." / "check"
    with patch("scripts.check.tempfile.mkdtemp", return_value=str(alias)):
        with isolated_project(source) as (project, environment):
            assert project == check_root / "project"
            assert environment["TMP"] == str(project / ".tmp")
            assert environment["MINI_AGENT_STATE_DIR"] == str(project / ".state")
            assert (project / "module.py").read_text() == "VALUE = 1\n"
    assert not check_root.exists()
    assert (source / "module.py").is_file()


def test_private_state_and_undo_backups_are_never_copied(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    excluded = ("journals", "JOURNALS", "sessions", "checkpoints", ".mini-agent", ".state")
    for name in excluded:
        folder = source / name
        folder.mkdir(exist_ok=True)
        (folder / "private.json").write_text("fictional private content", encoding="utf-8")
    (source / ".env").write_text("DUMMY_KEY=fictional", encoding="utf-8")
    (source / ".env.example").write_text("DUMMY_KEY=placeholder", encoding="utf-8")
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    destination = tmp_path / "copy"
    shutil.copytree(source, destination, ignore=ignored_entries)
    assert {path.name for path in destination.iterdir()} == {".env.example", "module.py"}
    assert (source / ".env").is_file()
    assert all((source / name / "private.json").is_file() for name in excluded)
