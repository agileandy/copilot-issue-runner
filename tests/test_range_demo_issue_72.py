import pytest

from issue_runner.demo.range_demo_issue_72 import parse_numbers, within_range


def test_parse_numbers_with_comma_separated_integers():
    assert parse_numbers('1, 3, 4, 5') == [1, 3, 4, 5]


def test_within_range_interior_case():
    assert within_range('1,3,5', 2, 4) == [3]
