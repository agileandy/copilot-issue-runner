def parse_numbers(text: str) -> list[int]:
    """Parse a comma-separated string of integers and return them as a list.
    
    Args:
        text: A comma-separated string of integers (e.g., '1, 3, 4, 5')
    
    Returns:
        A list of integers in the same order as the input string.
    """
    return [int(num.strip()) for num in text.split(',')]


def within_range(text: str, lower: int, upper: int) -> list[int]:
    """Filter numbers within a range.
    
    Args:
        text: A comma-separated string of integers
        lower: Lower bound (inclusive)
        upper: Upper bound (inclusive)
    
    Returns:
        A list of numbers from text that fall within the range.
    """
    numbers = parse_numbers(text)
    return [num for num in numbers if lower <= num < upper]
