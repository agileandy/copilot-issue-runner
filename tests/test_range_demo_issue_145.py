def test_parse_numbers_splits_strips_and_converts_in_order():
    from issue_runner.demo.range_demo_issue_145 import parse_numbers

    assert parse_numbers("1, 3, 4, 5") == [1, 3, 4, 5]
