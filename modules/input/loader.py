"""Input loader utilities."""


def load_accounts(path: str) -> list[str]:
    """Load account lines from a text file."""
    with open(path, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]
