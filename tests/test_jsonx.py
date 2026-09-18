import pytest

from issue_runner.jsonx import JsonExtractError, extract_json, extract_json_object


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


def test_object_extraction_unwraps_a_single_element_list():
    """Models often wrap the required object in an array."""
    assert extract_json_object('[{"verdict": "pass"}]') == {"verdict": "pass"}


def test_object_extraction_rejects_a_bare_list_of_several_objects():
    with pytest.raises(JsonExtractError) as excinfo:
        extract_json_object('[{"a": 1}, {"b": 2}]')
    assert "object" in str(excinfo.value).lower()


def test_object_extraction_rejects_a_scalar():
    with pytest.raises(JsonExtractError):
        extract_json_object("42")


def test_object_extraction_still_reads_a_plain_object():
    assert extract_json_object('prose {"verdict": "pass"} more') == {"verdict": "pass"}
