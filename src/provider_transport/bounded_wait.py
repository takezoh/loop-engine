"""Waits whose bound is measured on the monotonic clock.

`Event.wait` and `Thread.join` with a timeout can return before the timeout has
passed: a CPython that measures the timeout against the wall clock (a build
without `sem_clockwait`) ends the wait when the wall clock steps. These waits
wait again after such a return and report a timeout only once the bound has
passed on the monotonic clock.
"""
from __future__ import annotations

from threading import Event, Thread
from time import monotonic
from typing import Callable


def set_within(event: Event, seconds: float) -> bool:
    """Whether `event` is set within `seconds`."""
    return _within(seconds, event.is_set, event.wait)


def ended_within(thread: Thread, seconds: float) -> bool:
    """Whether `thread` has ended within `seconds`."""
    return _within(seconds, lambda: not thread.is_alive(), thread.join)


def _within(seconds: float, holds: Callable[[], bool], wait: Callable[[float], object]) -> bool:
    deadline = monotonic() + seconds
    while not holds():
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        wait(remaining)
    return True
