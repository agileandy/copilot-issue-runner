import json
import sys

import pytest

from issue_runner.config import load_config
from issue_runner.testcmd import DEFAULT_TEST_CMD, GO_TEST_CMD, detect_test_cmd


def test_pyproject_detects_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    found = detect_test_cmd(tmp_path)
    assert found.marker == "pyproject.toml"
    assert found.test_cmd == DEFAULT_TEST_CMD
    assert sys.executable in found.test_cmd


def test_setup_py_detects_pytest(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert detect_test_cmd(tmp_path).marker == "setup.py"


def test_package_json_with_test_script_detects_npm(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    found = detect_test_cmd(tmp_path)
    assert found.marker == "package.json"
    assert found.test_cmd == "npm test -- {test_path}"


def test_package_json_without_test_script_is_not_a_marker(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    assert detect_test_cmd(tmp_path).marker is None


def test_unparseable_package_json_is_not_a_marker(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert detect_test_cmd(tmp_path).marker is None


def test_go_mod_detects_go_test(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    found = detect_test_cmd(tmp_path)
    assert found.marker == "go.mod"
    assert found.test_cmd == GO_TEST_CMD


def test_go_command_prints_per_case_evidence_and_skips_the_cache(tmp_path):
    """Plain `go test ./...` prints only "ok pkg", which proves nothing ran."""
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    cmd = detect_test_cmd(tmp_path).test_cmd
    assert " -v" in cmd, "without -v go names no test case"
    assert "-count=1" in cmd, "without -count=1 go replays a cached result"


def test_cargo_toml_detects_cargo_test(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    found = detect_test_cmd(tmp_path)
    assert found.marker == "Cargo.toml"
    assert found.test_cmd == "cargo test"


def test_no_marker_falls_back_to_pytest(tmp_path):
    found = detect_test_cmd(tmp_path)
    assert found.marker is None
    assert found.test_cmd == DEFAULT_TEST_CMD


def test_no_marker_warns(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="issue_runner"):
        load_config(tmp_path)
    assert any("test_cmd" in r.message for r in caplog.records)


def test_python_marker_wins_over_node_in_polyglot_repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    assert detect_test_cmd(tmp_path).marker == "pyproject.toml"


@pytest.mark.parametrize(
    "marker,content",
    [
        ("go.mod", "module x\n"),
        ("Cargo.toml", "[package]\n"),
        ("package.json", json.dumps({"scripts": {"test": "vitest"}})),
    ],
)
def test_load_config_uses_detection_when_no_runner_toml(tmp_path, marker, content):
    (tmp_path / marker).write_text(content)
    cfg = load_config(tmp_path)
    assert cfg.test_cmd == detect_test_cmd(tmp_path).test_cmd
    assert cfg.test_cmd != DEFAULT_TEST_CMD


def test_runner_toml_test_cmd_beats_detection(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "runner.toml").write_text('test_cmd = "mytest {test_path}"\n')
    assert load_config(tmp_path).test_cmd == "mytest {test_path}"


def test_runner_toml_without_test_cmd_still_detects(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "runner.toml").write_text("max_rounds = 5\n")
    cfg = load_config(tmp_path)
    assert cfg.max_rounds == 5
    assert cfg.test_cmd == GO_TEST_CMD


def test_detected_command_is_logged(tmp_path, caplog):
    (tmp_path / "go.mod").write_text("module x\n")
    with caplog.at_level("INFO", logger="issue_runner"):
        load_config(tmp_path)
    assert any(GO_TEST_CMD in r.message for r in caplog.records)


def test_this_repo_still_uses_pytest():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    assert detect_test_cmd(repo_root).test_cmd == DEFAULT_TEST_CMD
