#!/usr/bin/env python3
"""Scripted stand-in for the Copilot CLI, used by `--demo`.

It accepts the same argv the runner builds, works out which role is calling
from the prompt text, performs the file edits a real agent would have made, and
replies with the JSON that role's parser expects. Call counts are kept in the
sandbox's `.issue-runner/demo-state.json`, so the second builder.coder call on a
ticket can deliberately differ from the first — that is how the demo shows the
harness catching a failing implementation and the verifier handing work back.

Both transport modes are supported: plain text for `-s`, and copilot's JSONL
event stream for `--output-format json` (what the visual mode consumes).
"""

import json
import os
import sys
import time
from pathlib import Path

STATE_FILE = ".issue-runner/demo-state.json"

PLAN = {
    "summary": "add mean and median to demo_pkg.stats, one behaviour per ticket",
    "tickets": [
        {
            "title": "add mean to demo_pkg.stats",
            "description": "Add mean(values) to demo_pkg/stats.py returning the arithmetic mean.",
            "test_assertion": "mean([1, 2, 3]) == 2",
            "files_hint": ["demo_pkg/stats.py", "tests/test_mean.py"],
        },
        {
            "title": "add median to demo_pkg.stats",
            "description": (
                "Add median(values) to demo_pkg/stats.py returning the middle value of the "
                "sorted input, averaging the two middle values for even-length input."
            ),
            "test_assertion": "median([3, 1, 2]) == 2",
            "files_hint": ["demo_pkg/stats.py", "tests/test_median.py"],
        },
    ],
}

TEST_MEAN = """\
from demo_pkg.stats import mean


def test_mean_of_three_ints():
    assert mean([1, 2, 3]) == 2
"""

TEST_MEDIAN_FIRST = """\
from demo_pkg.stats import median


def test_median_of_odd_length():
    assert median([3, 1, 2]) == 2
"""

TEST_MEDIAN_REFINED = """\
from demo_pkg.stats import median


def test_median_of_even_length_averages_the_middle_pair():
    assert median([1, 2, 3, 4]) == 2.5
"""

STATS_HEADER = '"""Summary statistics."""\n'

STATS_MEAN = (
    STATS_HEADER
    + """

def mean(values):
    return sum(values) / len(values)
"""
)

# first median attempt: forgets to sort, so the test stays red on purpose
STATS_MEDIAN_UNSORTED = (
    STATS_MEAN
    + """

def median(values):
    return values[len(values) // 2]
"""
)

STATS_MEDIAN_ODD_ONLY = (
    STATS_MEAN
    + """

def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
"""
)

STATS_MEDIAN_FULL = (
    STATS_MEAN
    + """

def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
"""
)

VERDICT_REFINE = {
    "verdict": "refine_test",
    "reasons": ["the test only covers odd-length input"],
    "test_feedback": "cover the even-length case, where the two middle values are averaged",
    "code_feedback": "",
}

VERDICT_PASS_MEAN = {
    "verdict": "pass",
    "reasons": ["the test asserts real behaviour", "mean([1, 2, 3]) == 2 passes"],
    "test_feedback": "",
    "code_feedback": "",
}

VERDICT_PASS_MEDIAN = {
    "verdict": "pass",
    "reasons": ["even-length input is now covered", "the implementation passes the test"],
    "test_feedback": "",
    "code_feedback": "",
}


def parse_argv(argv):
    prompt, repo_dir, stream = "", Path.cwd(), False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-p" and i + 1 < len(argv):
            prompt = argv[i + 1]
            i += 2
            continue
        if arg == "-C" and i + 1 < len(argv):
            repo_dir = Path(argv[i + 1])
            i += 2
            continue
        if arg == "--output-format" and i + 1 < len(argv):
            stream = argv[i + 1] == "json"
            i += 2
            continue
        i += 1
    return prompt, repo_dir, stream


def role_of(prompt: str) -> str:
    for marker, role in (
        ("You are the planner", "planner"),
        ("You are builder.tester", "tester"),
        ("You are builder.coder", "coder"),
        ("You are the verifier", "verifier"),
    ):
        if marker in prompt:
            return role
    return "unknown"


def ticket_of(prompt: str) -> str:
    """Which demo ticket this call belongs to, read off the Sub-task line."""
    for line in prompt.splitlines():
        if line.startswith("Sub-task"):
            return "median" if "median" in line else "mean"
    return "none"


def bump(repo_dir: Path, key: str) -> int:
    """Increment and return the call count for `key` (1 on the first call)."""
    path = repo_dir / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    count = int(state.get(key, 0)) + 1
    state[key] = count
    path.write_text(json.dumps(state, indent=2))
    return count


def write(repo_dir: Path, rel: str, text: str) -> str:
    target = repo_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return rel


def respond(prompt: str, repo_dir: Path):
    """Return (reply, [edited paths], reasoning) for one scripted call."""
    role = role_of(prompt)
    ticket = ticket_of(prompt)
    attempt = bump(repo_dir, f"{role}:{ticket}")

    if role == "planner":
        return json.dumps(PLAN), [], "reading the issue and the repository layout"

    if role == "tester":
        if ticket == "mean":
            path = write(repo_dir, "tests/test_mean.py", TEST_MEAN)
            why = "writing a failing test for mean"
        elif attempt == 1:
            path = write(repo_dir, "tests/test_median.py", TEST_MEDIAN_FIRST)
            why = "writing a failing test for median"
        else:
            path = write(repo_dir, "tests/test_median.py", TEST_MEDIAN_REFINED)
            why = "refining the median test to cover even-length input"
        return json.dumps({"test_path": path}), [path], why

    if role == "coder":
        if ticket == "mean":
            path = write(repo_dir, "demo_pkg/stats.py", STATS_MEAN)
            why = "implementing mean"
        elif attempt == 1:
            path = write(repo_dir, "demo_pkg/stats.py", STATS_MEDIAN_UNSORTED)
            why = "implementing median (first attempt, unsorted)"
        elif attempt == 2:
            path = write(repo_dir, "demo_pkg/stats.py", STATS_MEDIAN_ODD_ONLY)
            why = "sorting before taking the middle value"
        else:
            path = write(repo_dir, "demo_pkg/stats.py", STATS_MEDIAN_FULL)
            why = "averaging the middle pair for even-length input"
        return (
            json.dumps({"changed_files": [path], "notes": why}),
            [path],
            why,
        )

    if role == "verifier":
        if ticket == "mean":
            verdict = VERDICT_PASS_MEAN
        elif attempt == 1:
            verdict = VERDICT_REFINE
        else:
            verdict = VERDICT_PASS_MEDIAN
        return json.dumps(verdict), [], "reviewing the test and running it"

    return "the demo responder was called with an unrecognised prompt", [], "confused"


def _delay() -> float:
    try:
        return max(0.0, float(os.environ.get("ISSUE_RUNNER_DEMO_DELAY", "0.15")))
    except ValueError:
        return 0.15


def emit_stream(reply: str, edited, reasoning: str) -> None:
    """Speak copilot's JSONL event dialect so the visual mode has something to show."""
    pause = _delay()

    def send(event):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
        time.sleep(pause)

    for word in reasoning.split(" "):
        send({"type": "assistant.reasoning_delta", "data": {"deltaContent": word + " "}})
    for path in edited:
        send(
            {
                "type": "tool.execution_start",
                "data": {"toolName": "str_replace_editor", "arguments": {"path": path}},
            }
        )
    send({"type": "assistant.message", "data": {"content": reply}})
    send(
        {
            "type": "model.model_call_success",
            "data": {
                "responseUsage": {
                    "prompt_tokens": len(reasoning) * 7,
                    "completion_tokens": len(reply),
                },
                "copilotUsage": {"total_nano_aiu": 0},
            },
        }
    )


def main(argv=None) -> int:
    prompt, repo_dir, stream = parse_argv(list(sys.argv[1:] if argv is None else argv))
    reply, edited, reasoning = respond(prompt, repo_dir)
    if stream:
        emit_stream(reply, edited, reasoning)
    else:
        sys.stdout.write(reply + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
