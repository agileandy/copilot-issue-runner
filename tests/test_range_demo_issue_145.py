import pytest


def test_parse_numbers_splits_strips_and_converts_in_order():
    from issue_runner.demo.range_demo_issue_145 import parse_numbers

    assert parse_numbers("1, 3, 4, 5") == [1, 3, 4, 5]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,3,5", [3]), ("2,4", [2, 4])],
)
def test_within_range_keeps_numbers_between_inclusive_bounds_in_order(text, expected):
    from issue_runner.demo.range_demo_issue_145 import within_range

    assert within_range(text, 2, 4) == expected
