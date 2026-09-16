import pytest

from issue_runner.demo.range_demo_issue_72 import parse_numbers


def test_parse_numbers_with_comma_separated_integers():
    assert parse_numbers('1, 3, 4, 5') == [1, 3, 4, 5]
