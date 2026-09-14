#!/usr/bin/env python3
"""Dependency-free test runner for the demo sandbox.

The demo must work from any interpreter the runner happens to be installed
under — a `uv tool install` venv has the runtime deps only, no pytest. When
pytest is importable the demo uses it; otherwise it falls back to this.

Output is TAP (the protocol the harness documents for custom test commands), so
the runner can tell "one test executed and failed" from "nothing ran at all"
instead of guessing from an exit code. Exit codes are kept as they were: 0
green, 1 a real test failure, 2 nothing executable.

A directory argument runs every `test_*.py` beneath it, which is what a
whole-suite regression check needs.
"""

import importlib.util
import sys
import traceback
from pathlib import Path


def _collect(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("test_*.py") if p.is_file())
    return [path]


def _load(path: Path):
    """Import a test file; returns (module, error_message)."""
    spec = importlib.util.spec_from_file_location(f"minitest_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None, f"cannot import {path}"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — a test runner reports any failure, never raises
        traceback.print_exc()
        return None, f"collection error in {path}"
    return module, None


def run(path: Path) -> int:
    sys.path.insert(0, str(Path.cwd()))
    files = _collect(path)
    print("TAP version 13")
    if not files:
        print(f"1..0 # no test file under {path}")
        print(f"# no test file under {path}")
        return 2

    cases: list[tuple[str, object]] = []
    for file in files:
        if not file.is_file():
            print("1..0 # cannot read " + str(file))
            print(f"# cannot read {file}")
            return 2
        module, error = _load(file)
        if module is None:
            print(f"1..0 # {error}")
            print(f"# {error}")
            return 2
        cases += [
            (f"{file}::{name}", obj)
            for name, obj in sorted(vars(module).items())
            if name.startswith("test_") and callable(obj)
        ]

    if not cases:
        print(f"1..0 # no test function found in {path}")
        print(f"# no test function found in {path}")
        return 2

    print(f"1..{len(cases)}")
    failed = 0
    for number, (name, test) in enumerate(cases, start=1):
        try:
            test()
        except Exception:  # noqa: BLE001 — a failing test is data, not an error here
            failed += 1
            print(f"not ok {number} - {name}")
            for line in traceback.format_exc().splitlines():
                print(f"  # {line}")
        else:
            print(f"ok {number} - {name}")
    print(f"# tests {len(cases)}")
    print(f"# pass {len(cases) - failed}")
    print(f"# fail {failed}")
    return 1 if failed else 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: minitest.py <test_file_or_directory>")
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    sys.exit(main())
