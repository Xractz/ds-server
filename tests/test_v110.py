"""Unit tests for the v1.1.0 additions: the retry counter, DS_LOG
request logging, stream cancellation in the core client, and the
CompletionService streaming pass-through.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from deepseek_web import core
from deepseek_web.core import (ChatResult, DeepSeekClient, RequestPacer,
                               StreamCancelled)

try:
    import ds_server
    from ds_server import ChatRequest, CompletionService, RequestLog
    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}

    def close(self):
        pass


class RetryCounterTest(unittest.TestCase):
    def test_retries_are_counted(self):
        client = DeepSeekClient("test-token",
                                pacer=RequestPacer(enabled=False))
        attempts = []

        def send():
            attempts.append(1)
            if len(attempts) < 3:
                raise requests.ConnectionError("boom")
            return FakeResponse(200)

        with mock.patch.object(core.time, "sleep"):
            response = client._request("test", send)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.retries, 2)


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class RequestLogTest(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("DS_LOG", None)

    def tearDown(self):
        os.environ.pop("DS_LOG", None)
        if self._old is not None:
            os.environ["DS_LOG"] = self._old

    def test_silent_by_default(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            RequestLog.completion("deepseek-chat", "hit", 1.25, 0, "ok")
        self.assertEqual(buffer.getvalue(), "")

    def test_one_line_when_enabled(self):
        os.environ["DS_LOG"] = "1"
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            RequestLog.completion("deepseek-chat", "hit", 1.25, 2, "ok")
        line = buffer.getvalue()
        self.assertIn("model=deepseek-chat", line)
        self.assertIn("chain=hit", line)
        self.assertIn("duration=1.25s", line)
        self.assertIn("retries=2", line)
        self.assertIn("status=ok", line)


class FakeStreamResponse:
    """A slow SSE response: one data line every 20 ms."""

    def __init__(self, lines=200):
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"request_message_id": 11, "response_message_id": 12}'
        event = {"v": {"response": {"fragments": [
            {"id": 1, "type": "RESPONSE", "content": "hello"}]}}}
        yield "data: " + json.dumps(event)
        for _ in range(self._lines):
            time.sleep(0.02)
            yield ('data: {"p": "response/fragments/-1/content", '
                   '"o": "APPEND", "v": "x"}')

    def close(self):
        self.closed = True


class StreamCancelTest(unittest.TestCase):
    def test_cancel_aborts_read_and_closes_response(self):
        client = DeepSeekClient("test-token",
                                pacer=RequestPacer(enabled=False))
        client._pow_headers = lambda target: {}
        response = FakeStreamResponse()
        cancel = threading.Event()
        tokens = []

        def set_cancel_later():
            time.sleep(0.15)
            cancel.set()

        with mock.patch.object(core.requests, "post",
                               return_value=response):
            setter = threading.Thread(target=set_cancel_later)
            setter.start()
            started = time.monotonic()
            with self.assertRaises(StreamCancelled):
                client.chat_completion(
                    "session-1", None, "prompt",
                    on_token=lambda kind, text: tokens.append(text),
                    cancel_event=cancel)
            elapsed = time.monotonic() - started
            setter.join(5)
        self.assertTrue(response.closed)
        self.assertLess(elapsed, 2.0)
        self.assertIn("hello", tokens)


class FakeChains:
    def __init__(self, record=None):
        self._record = record
        self.updated = None

    def get(self, key):
        return self._record

    def is_continuation(self, record, messages):
        return record is not None

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        self.updated = kwargs

    @staticmethod
    def fingerprint(message):
        return "fp"


class FakeClient:
    def __init__(self):
        self.seen = {}

    def create_session(self):
        return "session-1"

    def chat_completion(self, session_id, parent, prompt, file_ids,
                        **kwargs):
        self.seen = kwargs
        return ChatResult(content="jawaban", response_id=42)


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class ServiceStreamingTest(unittest.TestCase):
    def make_request(self, messages):
        return ChatRequest.from_body(
            {"model": "deepseek-chat", "messages": messages})

    def test_new_chain_passes_streaming_arguments(self):
        service = CompletionService(FakeChains())
        client = FakeClient()
        cancel = threading.Event()
        on_token = lambda kind, text: None
        answer, _thinking, status = service.run(
            client,
            self.make_request([{"role": "user", "content": "halo"}]),
            "key", on_token=on_token, cancel_event=cancel)
        self.assertEqual(answer, "jawaban")
        self.assertEqual(status, "new")
        self.assertIs(client.seen["on_token"], on_token)
        self.assertIs(client.seen["cancel_event"], cancel)

    def test_continuation_reports_hit(self):
        record = {"ds_session": "s1", "parent": 5, "count": 1}
        service = CompletionService(FakeChains(record))
        client = FakeClient()
        messages = [{"role": "user", "content": "satu"},
                    {"role": "assistant", "content": "dua"},
                    {"role": "user", "content": "tiga"}]
        _answer, _thinking, status = service.run(
            client, self.make_request(messages), "key")
        self.assertEqual(status, "hit")


if __name__ == "__main__":
    unittest.main()
