def parse_numbers(text: str) -> list[int]:
    """Parse a comma-separated string of integers and return them as a list.
    
    Args:
        text: A comma-separated string of integers (e.g., '1, 3, 4, 5')
    
    Returns:
        A list of integers in the same order as the input string.
    """
    return [int(num.strip()) for num in text.split(',')]
