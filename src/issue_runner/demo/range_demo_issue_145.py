"""Parse comma-separated integers."""


def parse_numbers(text: str) -> list[int]:
    return [int(piece.strip()) for piece in text.split(",")]
