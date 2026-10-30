"""Unit tests for the v1.1.1 patch package: the parser final debris
sweep and fused-dialect closers, the long-code-block trim for
tool-carrying replies, cancellation hardening for stalled streams,
and the regression fixture corpus under tests/fixtures.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web import core
from deepseek_web.core import (DeepSeekClient, RequestPacer,
                               StreamCancelled, StreamParser)
from deepseek_web.tools import ToolCallParser

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DSML = "\uff5c\uff5cDSML\uff5c\uff5c"


def dsml_invoke(name, params, closer=None):
    """Build one DSML invoke block. ``closer`` selects the closing
    tag: the normal invoke closer, a fused tool_call closer, or the
    calls wrapper closer."""
    if closer is None:
        closer = "</" + DSML + "invoke>"
    body = "".join(
        "<" + DSML + "parameter name=\"" + key + "\" string=\"true\">"
        + value + "</" + DSML + "parameter>"
        for key, value in params.items())
    return ("<" + DSML + "invoke name=\"" + name + "\">" + body + closer)


class DebrisSweepTest(unittest.TestCase):
    def setUp(self):
        self.parser = ToolCallParser()

    def test_dsml_closed_by_tool_call_tag(self):
        reply = ("Working on it "
                 + dsml_invoke("bash", {"command": "ls -la"},
                               closer="</tool_call>")
                 + " done")
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "bash")
        self.assertEqual(calls[0].arguments, {"command": "ls -la"})
        self.assertNotIn("ls -la", text)
        self.assertNotIn("tool_call", text)
        self.assertNotIn("DSML", text)

    def test_dsml_closed_by_calls_wrapper(self):
        reply = ("Working on it "
                 + dsml_invoke("bash", {"command": "ls"},
                               closer="</" + DSML + "calls>")
                 + " done")
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arguments, {"command": "ls"})
        self.assertNotIn("DSML", text)

    def test_tool_call_closed_by_dsml_invoke(self):
        reply = ('Hi <tool_call>{"name": "bash", "arguments": '
                 '{"command": "ls"}}</' + DSML + 'invoke> done')
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "bash")
        self.assertNotIn("DSML", text)
        self.assertNotIn("tool_call", text)

    def test_mixed_dialect_reply(self):
        reply = ("First ```tool_call\n"
                 '{"name": "bash", "arguments": {"command": "ls"}}\n```'
                 " and " + dsml_invoke("read", {"path": "/tmp/x"})
                 + " trailing </tool_call>")
        text, calls = self.parser.parse(
            reply, valid_names={"bash", "read"})
        self.assertEqual([call.name for call in calls],
                         ["bash", "read"])
        self.assertEqual(text, "First  and  trailing")
        self.assertNotIn("DSML", text)
        self.assertNotIn("tool_call", text)

    def test_orphan_tags_stripped_but_words_kept(self):
        reply = ("Result ok </" + DSML + "invoke> real words "
                 "</" + DSML + "calls> </tool_call> tail")
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(calls, [])
        self.assertIn("real words", text)
        self.assertNotIn("DSML", text)
        self.assertNotIn("tool_call", text)

    def test_orphan_tool_call_opener_stripped(self):
        text, calls = self.parser.parse(
            "Hello <tool_call> not json here", valid_names={"bash"})
        self.assertEqual(calls, [])
        self.assertNotIn("tool_call", text)
        self.assertIn("not json here", text)

    def test_mojibake_debris_stripped_and_call_parsed(self):
        damaged = ("<�DSML�invoke name=\"bash\">"
                   "<�DSML�parameter name=\"command\" string=\"true\">"
                   "ls</�DSML�parameter></�DSML�invoke> ok")
        text, calls = self.parser.parse(damaged, valid_names={"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].arguments, {"command": "ls"})
        self.assertEqual(text, "ok")
        debris = ("Done <�DSML�invoke> stray words "
                  "</�DSML�calls> end")
        text, calls = self.parser.parse(debris, valid_names={"bash"})
        self.assertEqual(calls, [])
        self.assertIn("stray words", text)
        self.assertNotIn("DSML", text)

    def test_unknown_tool_block_removed_as_markup(self):
        reply = ("Hi " + dsml_invoke("nope", {"x": "y"}) + " done")
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(calls, [])
        self.assertNotIn("nope", text)
        self.assertNotIn("DSML", text)


def long_block(lines, language="python"):
    body = "\n".join("line%d" % index for index in range(lines))
    return "```" + language + "\n" + body + "\n```"


class TrimCodeBlocksTest(unittest.TestCase):
    def setUp(self):
        self.parser = ToolCallParser()
        self.call = ('<tool_call>{"name": "bash", "arguments": '
                     '{"command": "ls"}}</tool_call>')

    def test_long_block_trimmed_with_tool_calls(self):
        text, calls = self.parser.parse(
            self.call + "\nHere:\n" + long_block(20) + "\nend",
            valid_names={"bash"})
        self.assertEqual(len(calls), 1)
        self.assertIn("line9", text)
        self.assertNotIn("line10", text)
        self.assertIn("trimmed", text)
        self.assertIn("end", text)

    def test_twelve_line_block_kept_with_tool_calls(self):
        text, _calls = self.parser.parse(
            self.call + "\nHere:\n" + long_block(12) + "\nend",
            valid_names={"bash"})
        self.assertIn("line11", text)
        self.assertNotIn("trimmed", text)

    def test_thirteen_line_block_trimmed_with_tool_calls(self):
        text, _calls = self.parser.parse(
            self.call + "\nHere:\n" + long_block(13) + "\nend",
            valid_names={"bash"})
        self.assertIn("line9", text)
        self.assertNotIn("line10", text)
        self.assertIn("trimmed", text)

    def test_plain_reply_untouched(self):
        reply = "Here:\n" + long_block(20) + "\nend"
        text, calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(calls, [])
        self.assertEqual(text, reply)

    def test_multiple_blocks_only_long_one_trimmed(self):
        reply = (self.call + "\nShort:\n" + long_block(3)
                 + "\nLong:\n" + long_block(20))
        text, _calls = self.parser.parse(reply, valid_names={"bash"})
        self.assertEqual(text.count("trimmed"), 1)
        self.assertIn("line2", text)

    def test_unlabelled_fence_trimmed_with_tool_calls(self):
        block = "```\n" + "\n".join(
            "line%d" % index for index in range(20)) + "\n```"
        text, _calls = self.parser.parse(
            self.call + "\n" + block, valid_names={"bash"})
        self.assertIn("trimmed", text)
        self.assertNotIn("line15", text)


class StalledStreamResponse:
    """An SSE response that answers once and then stalls.

    After the first fragment the iterator sleeps for a minute, far
    longer than any cancel poll interval. It runs on the reader
    thread, which is a daemon, so the stall never blocks test exit.
    """

    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"request_message_id": 11, "response_message_id": 12}'
        event = {"v": {"response": {"fragments": [
            {"id": 1, "type": "RESPONSE", "content": "hello"}]}}}
        yield "data: " + json.dumps(event)
        time.sleep(60)
        yield "data: [DONE]"

    def close(self):
        self.closed = True


class FailingStreamResponse:
    """An SSE response whose read fails after the first fragment."""

    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"request_message_id": 11, "response_message_id": 12}'
        raise ValueError("read broke")

    def close(self):
        self.closed = True


class StalledStreamCancelTest(unittest.TestCase):
    def _run(self, response, cancel):
        client = DeepSeekClient("test-token",
                                pacer=RequestPacer(enabled=False))
        client._pow_headers = lambda target: {}
        with mock.patch.object(core.requests, "post",
                               return_value=response):
            return client.chat_completion(
                "session-1", None, "prompt",
                on_token=lambda kind, text: None,
                cancel_event=cancel)

    def test_cancel_aborts_a_fully_stalled_stream(self):
        response = StalledStreamResponse()
        cancel = threading.Event()

        def set_cancel_later():
            time.sleep(0.2)
            cancel.set()

        setter = threading.Thread(target=set_cancel_later)
        setter.start()
        started = time.monotonic()
        with self.assertRaises(StreamCancelled):
            self._run(response, cancel)
        elapsed = time.monotonic() - started
        setter.join(5)
        self.assertTrue(response.closed)
        # The stall lasts 60s; cancellation must land within one
        # poll interval plus scheduling slack, not after the stall.
        self.assertLess(elapsed, 5.0)

    def test_reader_error_reaches_the_caller(self):
        response = FailingStreamResponse()
        cancel = threading.Event()
        with self.assertRaises(ValueError):
            self._run(response, cancel)
        self.assertTrue(response.closed)


class FixtureCorpusTest(unittest.TestCase):
    """Every file under tests/fixtures must keep parsing stably.

    The corpus captures historical failure shapes, not just current
    behavior: SSE patch sequences for the fragment bugs and
    mixed-dialect tool replies, including mojibake-damaged DSML.
    Adding a new failure shape means adding a fixture here, not a
    one-off inline sample.
    """

    def test_sse_fixtures(self):
        files = sorted(FIXTURES.glob("sse_*.json"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            with self.subTest(fixture=path.name):
                case = json.loads(path.read_text())
                parser = StreamParser()
                for event in case["events"]:
                    parser.feed(event)
                self.assertEqual(parser.content,
                                 case["expected_content"])
                self.assertEqual(parser.thinking,
                                 case["expected_thinking"])

    def test_tool_fixtures(self):
        files = sorted(FIXTURES.glob("tool_*.json"))
        self.assertGreaterEqual(len(files), 2)
        parser = ToolCallParser()
        for path in files:
            with self.subTest(fixture=path.name):
                case = json.loads(path.read_text())
                text, calls = parser.parse(
                    case["reply"], valid_names=set(case["valid_names"]))
                self.assertEqual(
                    [(call.name, call.arguments)
                     for call in calls],
                    [(call["name"], call["arguments"])
                     for call in case["expected_calls"]])
                self.assertEqual(text, case["expected_text"])
                for forbidden in case["forbidden"]:
                    self.assertNotIn(forbidden, text)


try:
    import ds_server
    from ds_server import ChatRequest, CompletionService
    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False

import deepseek_web
from deepseek_web.core import ChatResult


class _FallbackChains:
    def __init__(self):
        self.updated = None

    def get(self, key):
        return None

    def is_continuation(self, record, messages):
        return False

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        self.updated = kwargs

    @staticmethod
    def fingerprint(message):
        return "fp"


class _EmptyClient:
    """Completion parses to nothing; the history holds the text."""

    def __init__(self, history="teks dari history"):
        self._history = history
        self.history_calls = 0

    def create_session(self):
        return "session-1"

    def chat_completion(self, session_id, parent, prompt, file_ids,
                        **kwargs):
        return ChatResult(content="", response_id=7)

    def fetch_history_text(self, session_id):
        self.history_calls += 1
        return self._history


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class HistoryFallbackStreamingTest(unittest.TestCase):
    """A stream that yields no tokens still delivers the fallback."""

    def make_request(self):
        return ChatRequest.from_body(
            {"model": "deepseek-chat",
             "messages": [{"role": "user", "content": "halo"}]})

    def test_fallback_text_is_emitted_through_on_token(self):
        service = CompletionService(_FallbackChains())
        client = _EmptyClient()
        emitted = []
        answer, _thinking, _status = service.run(
            client, self.make_request(), "key",
            on_token=lambda kind, text: emitted.append((kind, text)))
        self.assertEqual(answer, "teks dari history")
        self.assertEqual(emitted,
                         [(StreamParser.CONTENT, "teks dari history")])

    def test_history_not_called_when_answer_present(self):
        class FullClient(_EmptyClient):
            def chat_completion(self, session_id, parent, prompt,
                                file_ids, **kwargs):
                return ChatResult(content="ada jawaban", response_id=8)

        service = CompletionService(_FallbackChains())
        client = FullClient()
        emitted = []
        answer, _thinking, _status = service.run(
            client, self.make_request(), "key",
            on_token=lambda kind, text: emitted.append((kind, text)))
        self.assertEqual(answer, "ada jawaban")
        self.assertEqual(client.history_calls, 0)
        self.assertEqual(emitted, [])


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class HealthVersionTest(unittest.TestCase):
    def test_health_reports_the_package_version(self):
        client = ds_server.create_app().test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], deepseek_web.__version__)


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class ModelsMetadataTest(unittest.TestCase):
    """The /v1/models listing declares real capabilities."""

    def setUp(self):
        self.client = ds_server.create_app().test_client()

    def test_listing_declares_vision_and_reasoning(self):
        body = self.client.get("/v1/models").get_json()
        entries = {entry["id"]: entry for entry in body["data"]}
        self.assertEqual(set(entries),
                         {"deepseek-chat", "deepseek-reasoner"})
        for entry in entries.values():
            self.assertTrue(entry["capabilities"]["vision"])
            self.assertIn("image",
                          entry["architecture"]["input_modalities"])
        chat = entries["deepseek-chat"]["capabilities"]
        reasoner = entries["deepseek-reasoner"]["capabilities"]
        self.assertFalse(chat["reasoning"])
        self.assertTrue(reasoner["reasoning"])

    def test_alias_entry_carries_the_canonical_capabilities(self):
        body = self.client.get("/v1/models/deepseek-r1").get_json()
        self.assertEqual(body["id"], "deepseek-r1")
        self.assertTrue(body["capabilities"]["reasoning"])
        self.assertTrue(body["capabilities"]["vision"])


if __name__ == "__main__":
    unittest.main()
