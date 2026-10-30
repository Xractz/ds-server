"""Unit tests for request pacing and retry backoff.

Run from the repository root:

    python3 -m unittest discover -s tests -v

No network access is used: HTTP attempts are stubbed callables and the
clock and sleep functions are fakes.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from deepseek_web import core
from deepseek_web.core import DeepSeekClient, RequestPacer


class FakeClock:
    """Monotonic clock that only advances when sleep() is called."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class PacerTest(unittest.TestCase):
    def test_session_interval_is_enforced(self):
        clock = FakeClock()
        pacer = RequestPacer(completion_interval=0.0,
                             session_interval=2.0,
                             clock=clock, sleep=clock.sleep)
        pacer.wait_for_session()
        self.assertEqual(clock.sleeps, [])
        pacer.wait_for_session()
        self.assertEqual(clock.sleeps, [2.0])
        pacer.wait_for_session()
        self.assertEqual(clock.sleeps, [2.0, 2.0])

    def test_completion_starts_are_spaced(self):
        clock = FakeClock()
        pacer = RequestPacer(completion_interval=0.5,
                             session_interval=0.0,
                             clock=clock, sleep=clock.sleep)
        with pacer.completion_slot():
            pass
        self.assertEqual(clock.sleeps, [])
        with pacer.completion_slot():
            pass
        self.assertEqual(clock.sleeps, [0.5])

    def test_completions_are_serialized(self):
        pacer = RequestPacer(completion_interval=0.0,
                             session_interval=0.0)
        inside = []
        peak = []
        entered = threading.Event()
        release = threading.Event()

        def worker(name, hold):
            with pacer.completion_slot():
                inside.append(name)
                peak.append(len(inside))
                if hold:
                    entered.set()
                    release.wait(5)
                inside.remove(name)

        first = threading.Thread(target=worker, args=("a", True))
        second = threading.Thread(target=worker, args=("b", False))
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        time.sleep(0.3)
        # The second worker must still be waiting for the slot.
        self.assertEqual(inside, ["a"])
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(peak, [1, 1])

    def test_disabled_pacer_never_waits(self):
        clock = FakeClock()
        pacer = RequestPacer(completion_interval=5.0,
                             session_interval=5.0, enabled=False,
                             clock=clock, sleep=clock.sleep)
        pacer.wait_for_session()
        pacer.wait_for_session()
        with pacer.completion_slot():
            pass
        with pacer.completion_slot():
            pass
        self.assertEqual(clock.sleeps, [])


class BackoffTest(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekClient(
            "test-token", pacer=RequestPacer(enabled=False))
        patcher = mock.patch.object(core.time, "sleep")
        self.fake_sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_retryable_status_then_success(self):
        attempts = []

        def send():
            attempts.append(1)
            return FakeResponse(429 if len(attempts) == 1 else 200)

        response = self.client._request("test", send)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 2)
        self.fake_sleep.assert_called_once_with(1.0)

    def test_retry_after_header_wins(self):
        attempts = []

        def send():
            attempts.append(1)
            if len(attempts) == 1:
                return FakeResponse(503, {"Retry-After": "5"})
            return FakeResponse(200)

        self.client._request("test", send)
        self.fake_sleep.assert_called_once_with(5.0)

    def test_connection_error_then_success(self):
        attempts = []

        def send():
            attempts.append(1)
            if len(attempts) < 3:
                raise requests.ConnectionError("boom")
            return FakeResponse(200)

        response = self.client._request("test", send)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.fake_sleep.call_count, 2)

    def test_non_retryable_status_passes_through(self):
        attempts = []

        def send():
            attempts.append(1)
            return FakeResponse(400)

        response = self.client._request("test", send)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(attempts), 1)
        self.fake_sleep.assert_not_called()

    def test_exhaustion_returns_last_response(self):
        attempts = []

        def send():
            attempts.append(1)
            return FakeResponse(503)

        response = self.client._request("test", send)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(attempts), 1 + DeepSeekClient.MAX_RETRIES)
        delays = [call.args[0] for call in self.fake_sleep.call_args_list]
        self.assertEqual(delays, [1.0, 2.0, 4.0])


class AdaptivePacingTest(unittest.TestCase):
    """The penalty widens on 429s and relaxes on its own."""

    def make_pacer(self, **kwargs):
        clock = FakeClock()
        pacer = RequestPacer(clock=clock, sleep=clock.sleep, **kwargs)
        return pacer, clock

    def test_penalty_starts_at_zero_and_floors(self):
        pacer, _clock = self.make_pacer()
        self.assertEqual(pacer.current_penalty, 0.0)
        pacer.note_rate_limited()
        self.assertEqual(pacer.current_penalty, RequestPacer.PENALTY_FLOOR)

    def test_penalty_doubles_and_caps(self):
        pacer, _clock = self.make_pacer()
        seen = []
        for _ in range(6):
            pacer.note_rate_limited()
            seen.append(pacer.current_penalty)
        self.assertEqual(seen, [2.0, 4.0, 8.0, 16.0, 30.0, 30.0])

    def test_penalty_decays_with_half_life(self):
        pacer, clock = self.make_pacer()
        pacer.note_rate_limited()
        clock.now += RequestPacer.PENALTY_HALF_LIFE
        self.assertAlmostEqual(pacer.current_penalty, 1.0)
        clock.now += RequestPacer.PENALTY_HALF_LIFE
        self.assertAlmostEqual(pacer.current_penalty, 0.5)

    def test_penalty_widens_completion_spacing(self):
        pacer, clock = self.make_pacer(completion_interval=0.5)
        with pacer.completion_slot():
            pass
        pacer.note_rate_limited()
        with pacer.completion_slot():
            pass
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 0.5 + 2.0)

    def test_adaptive_disabled_ignores_notes(self):
        pacer, _clock = self.make_pacer(adaptive=False)
        pacer.note_rate_limited()
        self.assertEqual(pacer.current_penalty, 0.0)

    def test_pacing_disabled_ignores_notes(self):
        pacer, _clock = self.make_pacer(enabled=False)
        pacer.note_rate_limited()
        self.assertEqual(pacer.current_penalty, 0.0)


if __name__ == "__main__":
    unittest.main()
