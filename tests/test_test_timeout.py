"""test_timeout_s: the per-run timeout for test_cmd and regression_cmd."""

from issue_runner.config import load_config


def test_test_timeout_s_loads_from_toml_and_defaults_to_1800(tmp_path):
    toml = tmp_path / "runner.toml"
    toml.write_text('test_cmd = "echo {test_path}"\ntest_timeout_s = 5\n')
    configured = load_config(tmp_path).test_timeout_s
    toml.write_text('test_cmd = "echo {test_path}"\n')
    default = load_config(tmp_path).test_timeout_s
    assert (configured, default) == (5, 1800)
