"""An event and a thread whose timed waits return at once.

So a wait returns when the wall clock steps during it on a CPython that measures
the timeout against the wall clock (a build without `sem_clockwait`).
"""
from threading import Event, Thread


class SteppedEvent(Event):
    def wait(self, timeout=None):
        return super().wait(None if timeout is None else 0)


class SteppedThread(Thread):
    def join(self, timeout=None):
        super().join(None if timeout is None else 0)
