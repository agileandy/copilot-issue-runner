import pytest

from issue_runner.jsonx import JsonExtractError, extract_json


def test_parses_bare_json_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_parses_fenced_json():
    text = 'Here is the plan:\n```json\n{"tickets": []}\n```\nDone.'
    assert extract_json(text) == {"tickets": []}


def test_parses_json_embedded_in_prose():
    text = 'Sure! The verdict follows. {"verdict": "pass", "reasons": ["ok"]} Hope that helps.'
    assert extract_json(text) == {"verdict": "pass", "reasons": ["ok"]}


def test_ignores_think_blocks():
    text = '<think>{"not": "this"}</think>\nAnswer:\n{"verdict": "rework_code"}'
    assert extract_json(text) == {"verdict": "rework_code"}


def test_nested_braces_survive():
    text = 'x {"a": {"b": [1, 2, {"c": "}"}]}} y'
    assert extract_json(text) == {"a": {"b": [1, 2, {"c": "}"}]}}


def test_no_json_raises():
    with pytest.raises(JsonExtractError):
        extract_json("no json here at all")
