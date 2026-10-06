"""Parse comma-separated integers."""


def parse_numbers(text: str) -> list[int]:
    return [int(piece.strip()) for piece in text.split(",")]


def within_range(text: str, lower: int, upper: int) -> list[int]:
    return [number for number in parse_numbers(text) if lower <= number <= upper]


def summarize_range(text: str, lower: int, upper: int) -> dict:
    selected = within_range(text, lower, upper)
    return {"values": selected, "count": len(selected), "sum": sum(selected)}
