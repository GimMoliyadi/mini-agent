"""Exercise installed npm through the actual bounded process runner."""

import json
import io
import shutil
from pathlib import Path
import tarfile

import pytest

from mini_agent.dev_environment import _execute
from mini_agent.verifier import snapshot_workspace
from mini_agent.process_runner import run_process, node_package_command


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
def test_real_npm_script_preserves_arguments(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "process-regression", "version": "1.0.0",
        "scripts": {"inspect": "node inspect.cjs"},
    }), encoding="utf-8")
    (tmp_path / "inspect.cjs").write_text(
        "console.log(JSON.stringify(process.argv.slice(2)));\n", encoding="utf-8",
    )
    argument = "spaces & echo injected > unexpected.txt"
    result = _execute(
        ["npm", "run", "inspect", "--", argument], tmp_path, tmp_path, timeout=20,
    )
    assert result.status == "success", result.text
    assert json.dumps([argument]) in result.text
    assert not (tmp_path / "unexpected.txt").exists()
    assert set(snapshot_workspace(tmp_path)) == {"package.json", "inspect.cjs"}


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm is not installed")
def test_real_pnpm_store_stays_out_of_source_snapshot(tmp_path):
    result = run_process(["pnpm", "store", "path"], tmp_path, workspace=tmp_path, timeout_seconds=20)
    assert result.returncode == 0, result.stderr
    store = Path(result.stdout.decode().strip()).resolve()
    assert store.is_relative_to(tmp_path / "node_modules"), store
    with tarfile.open(tmp_path / "dependency.tgz", "w:gz") as archive:
        for name, data in {
            "package/package.json": b'{"name":"local-dependency","version":"1.0.0"}',
            "package/index.js": b"module.exports = 1;\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "pnpm-snapshot-regression", "version": "1.0.0",
        "dependencies": {"local-dependency": "file:dependency.tgz"},
    }), encoding="utf-8")
    installed = run_process(["pnpm", "install", "--offline", "--ignore-scripts"],
                            tmp_path, workspace=tmp_path, timeout_seconds=30)
    assert installed.returncode == 0, installed.stderr
    snapshot = snapshot_workspace(tmp_path)
    assert "pnpm-lock.yaml" in snapshot
    assert not any(path.startswith(".pnpm/") for path in snapshot)


def test_windows_corepack_shim_uses_corepack_entry(tmp_path, monkeypatch):
    from mini_agent import process_runner

    shims = tmp_path / "node_modules/corepack/shims"
    shims.mkdir(parents=True)
    manager = shims / "pnpm.cmd"
    manager.write_text("Corepack shim", encoding="utf-8")
    entry = shims.parent / "dist/pnpm.js"
    entry.parent.mkdir()
    entry.write_text("// Corepack entry", encoding="utf-8")
    monkeypatch.setattr(process_runner.sys, "platform", "win32")
    monkeypatch.setattr(process_runner.shutil, "which", lambda name, **kwargs:
                        str(manager) if name == "pnpm" else str(tmp_path / "node.exe"))
    assert node_package_command(["pnpm", "--version"], {"PATH": ""}) == [
        str(tmp_path / "node.exe"), str(entry), "--version",
    ]


def test_windows_enabled_corepack_npm_preserves_shim_selection(tmp_path, monkeypatch):
    from mini_agent import process_runner

    manager = tmp_path / "npm.cmd"
    manager.write_text('"%dp0%\\node_modules\\corepack\\dist\\npm.js"', encoding="utf-8")
    entry = tmp_path / "node_modules/corepack/dist/npm.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// Corepack entry", encoding="utf-8")
    normal_entry = tmp_path / "node_modules/npm/bin/npm-cli.js"
    normal_entry.parent.mkdir(parents=True)
    normal_entry.write_text("// Ordinary npm entry", encoding="utf-8")
    monkeypatch.setattr(process_runner.sys, "platform", "win32")
    monkeypatch.setattr(process_runner.shutil, "which", lambda name, **kwargs:
                        str(manager) if name == "npm" else str(tmp_path / "node.exe"))
    assert node_package_command(["npm", "--version"], {"PATH": ""})[1] == str(entry)
