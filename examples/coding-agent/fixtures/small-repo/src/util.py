"""Small helpers shared across the project."""


def slugify(text: str) -> str:
    return text.strip().lower().replace(" ", "-")
