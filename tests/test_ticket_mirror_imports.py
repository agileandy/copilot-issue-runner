from pathlib import Path


def test_ticket_mirror_tests_import_github_io_only_as_module():
    source = Path(__file__).with_name("test_ticket_mirror.py").read_text(encoding="utf-8")
    offending = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(source.splitlines(), start=1)
        if "from issue_runner.github_io import" in line
    ]
    assert offending == []
