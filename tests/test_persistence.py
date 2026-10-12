import os
from pathlib import Path
import subprocess

import pytest

from mini_agent.persistence import checkpoint_store


def test_checkpoint_store_rejects_private_root_symlink(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    private_root = state_dir / ".mini-agent"
    try:
        private_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating directory symlinks is unavailable")

    with pytest.raises(PermissionError):
        with checkpoint_store(state_dir):
            pass

    assert not (outside / "checkpoints").exists()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_checkpoint_store_rejects_private_root_junction(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    private_root = state_dir / ".mini-agent"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(private_root), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"creating directory junction is unavailable: {result.stderr.strip()}")

    try:
        with pytest.raises(PermissionError):
            with checkpoint_store(state_dir):
                pass
        assert not (outside / "checkpoints").exists()
    finally:
        if private_root.exists():
            private_root.rmdir()
