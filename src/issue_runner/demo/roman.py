"""Roman numeral conversion using standard subtractive notation."""

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


_SYMBOLS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def from_roman(numeral: str) -> int:
    if not isinstance(numeral, str):
        raise ValueError(f"numeral must be a str, got {type(numeral).__name__}")  # noqa: TRY004
    text = numeral.strip().upper()
    if not text:
        raise ValueError("numeral must not be empty")

    total = 0
    for index, char in enumerate(text):
        if char not in _SYMBOLS:
            raise ValueError(f"invalid Roman numeral character: {char!r}")
        value = _SYMBOLS[char]
        following = (_SYMBOLS.get(text[index + 1]) or 0) if index + 1 < len(text) else 0
        total += -value if value < following else value

    if not 1 <= total <= 3999 or to_roman(total) != text:
        raise ValueError(f"not a canonical Roman numeral: {numeral!r}")
    return total
