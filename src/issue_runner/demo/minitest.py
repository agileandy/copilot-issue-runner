#!/usr/bin/env python3
"""Dependency-free test runner for the demo sandbox.

The demo must work from any interpreter the runner happens to be installed
under — a `uv tool install` venv has the runtime deps only, no pytest. When
pytest is importable the demo uses it; otherwise it falls back to this, which
collects `test_*` functions from one file and reports the same way: exit 0 for
green, non-zero and a traceback for red.
"""

import importlib.util
import sys
import traceback
from pathlib import Path


def run(path: Path) -> int:
    sys.path.insert(0, str(Path.cwd()))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        print(f"cannot import {path}")
        return 2
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — a test runner reports any failure, never raises
        traceback.print_exc()
        print(f"collection error in {path}")
        return 2

    tests = [
        (name, obj)
        for name, obj in sorted(vars(module).items())
        if name.startswith("test_") and callable(obj)
    ]
    if not tests:
        print(f"no tests found in {path}")
        return 2

    failed = 0
    for name, test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 — a failing test is data, not an error here
            failed += 1
            traceback.print_exc()
            print(f"FAILED {path}::{name}")
        else:
            print(f"passed {path}::{name}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: minitest.py <test_file>")
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    sys.exit(main())
