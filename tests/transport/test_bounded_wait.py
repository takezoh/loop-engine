"""A bounded wait ends only once its bound has passed on the monotonic clock."""
from threading import Event, Timer
import time
import unittest

from provider_transport.bounded_wait import ended_within, set_within
from .wall_clock_step import SteppedEvent, SteppedThread


class EventTests(unittest.TestCase):
    def test_an_event_set_after_the_wait_returned_early_is_set_within_the_bound(self):
        event = SteppedEvent()
        Timer(0.1, event.set).start()
        self.assertTrue(set_within(event, 5))

    def test_an_event_that_is_not_set_is_not_set_within_the_bound_only_once_the_bound_passed(self):
        began = time.monotonic()
        self.assertFalse(set_within(SteppedEvent(), 0.1))
        self.assertGreaterEqual(time.monotonic() - began, 0.1)

    def test_a_set_event_is_set_within_no_time(self):
        event = SteppedEvent()
        event.set()
        self.assertTrue(set_within(event, 0))


class ThreadTests(unittest.TestCase):
    def test_a_thread_that_ends_after_the_join_returned_early_ends_within_the_bound(self):
        thread = SteppedThread(target=time.sleep, args=(0.1,), daemon=True)
        thread.start()
        self.assertTrue(ended_within(thread, 5))
        self.assertFalse(thread.is_alive())

    def test_a_thread_that_runs_on_has_not_ended_within_the_bound_only_once_the_bound_passed(self):
        release = Event()
        thread = SteppedThread(target=release.wait, daemon=True)
        thread.start()
        try:
            began = time.monotonic()
            self.assertFalse(ended_within(thread, 0.1))
            self.assertGreaterEqual(time.monotonic() - began, 0.1)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
