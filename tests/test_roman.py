import pytest

from issue_runner.demo.roman import from_roman, to_roman


def test_converts_1994_with_subtractive_notation():
    assert to_roman(1994) == "MCMXCIV"


def test_converts_lower_bound():
    assert to_roman(1) == "I"


def test_converts_upper_bound():
    assert to_roman(3999) == "MMMCMXCIX"


@pytest.mark.parametrize(
    ("number", "expected"),
    [(4, "IV"), (9, "IX"), (40, "XL"), (90, "XC"), (400, "CD"), (900, "CM")],
)
def test_subtractive_pairs(number, expected):
    assert to_roman(number) == expected


@pytest.mark.parametrize("number", [0, 4000])
def test_out_of_range_raises(number):
    with pytest.raises(ValueError):
        to_roman(number)


def test_bool_is_rejected():
    with pytest.raises(ValueError):
        to_roman(True)


@pytest.mark.parametrize("number", ["5", 3.0])
def test_non_int_raises(number):
    with pytest.raises(ValueError):
        to_roman(number)


def test_from_roman_round_trips_to_roman():
    assert from_roman(to_roman(2024)) == 2024


def test_from_roman_is_case_insensitive():
    assert from_roman("mcmxciv") == 1994


@pytest.mark.parametrize("numeral", ["IIII", "IC", "VX", "MMMM"])
def test_from_roman_rejects_non_canonical(numeral):
    with pytest.raises(ValueError):
        from_roman(numeral)


@pytest.mark.parametrize("numeral", ["", "   ", "ABC", "MC!", 5, None])
def test_from_roman_rejects_bad_input(numeral):
    with pytest.raises(ValueError):
        from_roman(numeral)
