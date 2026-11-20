"""Which platform this process runs on: the one selection point of command search and of how the
Claude launcher starts the adapter. Each of them takes the answer as a value, so its decisions are
tested for both platforms on either."""
import os


def is_windows() -> bool:
    return os.name == "nt"
