"""Which platform this process runs on: the one selection point of command search, of where the
Claude ACP adapter is installed and of how its launcher starts it. Each of them takes the answer
as a value, so its decisions are tested for both platforms on either."""
import os


def is_windows() -> bool:
    return os.name == "nt"
