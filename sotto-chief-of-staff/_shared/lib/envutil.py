"""Integer environment values with the skills' existing empty/default policy."""
import os


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
