from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check import ignored_entries


class OfflineCopyIsolationTests(unittest.TestCase):
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
