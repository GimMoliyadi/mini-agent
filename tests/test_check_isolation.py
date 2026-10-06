from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check import ignored_entries, isolated_project


class OfflineCopyIsolationTests(unittest.TestCase):
    def test_temp_path_alias_is_canonical_before_building_child_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            source.mkdir()
            (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "alias").mkdir()
            check_root = root / "check"
            check_root.mkdir()
            alias = root / "alias" / ".." / "check"
            with patch("scripts.check.tempfile.mkdtemp", return_value=str(alias)):
                with isolated_project(source) as (project, environment):
                    self.assertEqual(project, check_root / "project")
                    self.assertEqual(environment["TMP"], str(project / ".tmp"))
                    self.assertEqual(environment["MINI_AGENT_STATE_DIR"], str(project / ".state"))
                    self.assertEqual((project / "module.py").read_text(), "VALUE = 1\n")
            self.assertFalse(check_root.exists())
            self.assertTrue((source / "module.py").is_file())

    def test_undo_backups_are_never_copied_into_test_workspace(self):
        for name in ("journals", "JOURNALS"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                backup = source / name
                backup.mkdir()
                (backup / "dummy-run.json").write_text("fictional original file contents", encoding="utf-8")
                (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
                destination = root / "copy"
                shutil.copytree(source, destination, ignore=ignored_entries)
                self.assertFalse((destination / name).exists())
                self.assertEqual((destination / "module.py").read_text(), "VALUE = 1\n")
                self.assertTrue((backup / "dummy-run.json").is_file())


if __name__ == "__main__":
    unittest.main()
