"""Result storage helpers."""


def save_result(data: str, path: str) -> None:
    """Persist a result string to disk."""
    with open(path, "a", encoding="utf-8") as file:
        file.write(f"{data}\n")
