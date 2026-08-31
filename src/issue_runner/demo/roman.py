"""Integer to Roman numeral conversion using standard subtractive notation."""

_VALUES = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def to_roman(number: int) -> str:
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError(f"number must be an int, got {type(number).__name__}")  # noqa: TRY004
    if not 1 <= number <= 3999:
        raise ValueError(f"number must be between 1 and 3999, got {number}")

    parts = []
    for value, symbol in _VALUES:
        count, number = divmod(number, value)
        parts.append(symbol * count)
    return "".join(parts)
