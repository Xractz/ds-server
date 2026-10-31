"""Unit tests for the v1.2.0 minor package: response_format honored
at prompt level, the opt-in authoritative history mode, upstream
session auto-delete, the /stats endpoint, and coverage for
stream_options.include_usage (implemented earlier, previously
untested).

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web.core import ChatResult, DeepSeekClient
from deepseek_web.sessions import ChainStore
from deepseek_web.tools import PromptBuilder

try:
    import ds_server
    from ds_server import (ChatRequest, CompletionService,
                           ResponseFactory, ServerStats)
    HAS_SERVER = True
except Exception:  # flask not installed in this environment
    HAS_SERVER = False


MESSAGES = [{"role": "user", "content": "halo"}]


class FakeChains:
    def __init__(self, record=None, retired=()):
        self._record = record
        self._retired = list(retired)
        self.updated = None

    def get(self, key):
        return self._record

    def is_continuation(self, record, messages):
        return record is not None

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        self.updated = kwargs

    def drain_retired(self):
        retired, self._retired = self._retired, []
        return retired

    @staticmethod
    def fingerprint(message):
        return "fp"


class FakeClient:
    def __init__(self, content="teks-stream", history=""):
        self.content = content
        self.history = history
        self.deleted = []
        self.prompts = []
        self.retries = 0

    def create_session(self):
        return "session-1"

    def chat_completion(self, session_id, parent, prompt, file_ids,
                        **kwargs):
        self.prompts.append(prompt)
        return ChatResult(content=self.content, response_id=42)

    def fetch_history_text(self, session_id):
        return self.history

    def delete_session(self, session_id):
        self.deleted.append(session_id)


def make_request(body):
    return ChatRequest.from_body({"model": "deepseek-chat",
                                  "messages": MESSAGES, **body})


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class ResponseFormatTest(unittest.TestCase):
    def test_clause_shapes(self):
        self.assertEqual(PromptBuilder._format_clause(None), "")
        self.assertEqual(
            PromptBuilder._format_clause({"type": "text"}), "")
        clause = PromptBuilder._format_clause({"type": "json_object"})
        self.assertIn("single valid JSON object", clause)
        schema = {"type": "json_schema",
                  "json_schema": {"name": "orang",
                                  "schema": {"type": "object",
                                             "properties": {
                                                 "umur": {
                                                     "type": "integer"}}}}}
        clause = PromptBuilder._format_clause(schema)
        self.assertIn("JSON Schema", clause)
        self.assertIn('"umur"', clause)

    def test_full_and_delta_carry_the_clause(self):
        fmt = {"type": "json_object"}
        full = PromptBuilder.full(MESSAGES, [], response_format=fmt)
        self.assertIn("single valid JSON object", full)
        delta = PromptBuilder.delta(MESSAGES, [], response_format=fmt)
        self.assertIn("single valid JSON object", delta)
        plain = PromptBuilder.full(MESSAGES, [])
        self.assertNotIn("single valid JSON object", plain)

    def test_request_resolution(self):
        self.assertIsNone(ChatRequest.resolve_response_format({}))
        self.assertIsNone(ChatRequest.resolve_response_format(
            {"response_format": {"type": "text"}}))
        self.assertIsNone(ChatRequest.resolve_response_format(
            {"response_format": {"type": "bogus"}}))
        kept = ChatRequest.resolve_response_format(
            {"response_format": {"type": "json_object"}})
        self.assertEqual(kept, {"type": "json_object"})

    def test_service_sends_the_clause_upstream(self):
        client = FakeClient()
        service = CompletionService(FakeChains())
        service.run(client, make_request(
            {"response_format": {"type": "json_object"}}), "key")
        self.assertIn("single valid JSON object", client.prompts[0])


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class HistoryModeTest(unittest.TestCase):
    def test_history_wins_when_enabled(self):
        client = FakeClient(content="teks-stream",
                            history="teks-history")
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(
            client, make_request({"history_mode": True}), "key")
        self.assertEqual(answer, "teks-history")

    def test_stream_text_wins_when_disabled(self):
        client = FakeClient(content="teks-stream",
                            history="teks-history")
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(
            client, make_request({"history_mode": False}), "key")
        self.assertEqual(answer, "teks-stream")

    def test_empty_history_falls_back_to_stream_text(self):
        client = FakeClient(content="teks-stream", history="")
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(
            client, make_request({"history_mode": True}), "key")
        self.assertEqual(answer, "teks-stream")

    def test_resolution_precedence(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DS_HISTORY_MODE", None)
            self.assertTrue(make_request({}).history_mode)
        with mock.patch.dict(os.environ, {"DS_HISTORY_MODE": "0"}):
            self.assertFalse(make_request({}).history_mode)
            self.assertTrue(
                make_request({"history_mode": True}).history_mode)
        with mock.patch.dict(os.environ, {"DS_HISTORY_MODE": "1"}):
            self.assertTrue(make_request({}).history_mode)
            self.assertFalse(
                make_request({"history_mode": False}).history_mode)


class DeleteSessionTest(unittest.TestCase):
    def test_client_uses_the_chat_session_id_field(self):
        client = DeepSeekClient("test-token")
        with mock.patch.object(client, "_post") as post:
            client.delete_session("abc-123")
        post.assert_called_once_with(
            "/chat_session/delete", {"chat_session_id": "abc-123"})

    def test_store_retires_replaced_and_evicted_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChainStore(path=Path(tmp) / "s.json", max_entries=2)
            store.put("a", "s1")
            store.put("b", "s2")
            store.put("c", "s3")
            self.assertEqual(store.drain_retired(), ["s1"])
            store.put("c", "s3-baru")
            self.assertEqual(store.drain_retired(), ["s3"])
            self.assertEqual(store.drain_retired(), [])

    @unittest.skipUnless(HAS_SERVER, "ds_server not available")
    def test_service_deletes_retired_when_enabled(self):
        chains = FakeChains(retired=["old-1", "old-2"])
        client = FakeClient()
        service = CompletionService(chains, delete_sessions=True)
        service.run(client, make_request({}), "key")
        self.assertEqual(client.deleted, ["old-1", "old-2"])

    @unittest.skipUnless(HAS_SERVER, "ds_server not available")
    def test_service_keeps_sessions_by_default(self):
        chains = FakeChains(retired=["old-1"])
        client = FakeClient()
        service = CompletionService(chains)
        service.run(client, make_request({}), "key")
        self.assertEqual(client.deleted, [])


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class EnvDefaultTest(unittest.TestCase):
    def test_unless_disabled_defaults_on(self):
        resolve = ChatRequest._env_flag_unless_disabled
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DS_DELETE_SESSIONS", None)
            self.assertTrue(resolve("DS_DELETE_SESSIONS"))
        for value in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ,
                                 {"DS_DELETE_SESSIONS": value}):
                self.assertFalse(resolve("DS_DELETE_SESSIONS"))
        with mock.patch.dict(os.environ, {"DS_DELETE_SESSIONS": "1"}):
            self.assertTrue(resolve("DS_DELETE_SESSIONS"))


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class IncludeUsageTest(unittest.TestCase):
    USAGE = {"prompt_tokens": 10, "completion_tokens": 5,
             "total_tokens": 15}

    def _chunks(self, include_usage):
        stream = ResponseFactory.stream(
            "chatcmpl-x", "deepseek-chat", "halo dunia", "", [],
            "stop", self.USAGE, include_usage)
        payloads = []
        for raw in stream:
            if raw.startswith("data: {"):
                payloads.append(json.loads(raw[len("data: "):]))
        return payloads

    def test_usage_chunk_emitted_when_requested(self):
        payloads = self._chunks(True)
        usage_chunks = [p for p in payloads if "usage" in p]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0]["usage"], self.USAGE)
        self.assertEqual(usage_chunks[0]["choices"], [])

    def test_no_usage_chunk_by_default(self):
        payloads = self._chunks(False)
        self.assertFalse(any("usage" in p for p in payloads))

    def test_buffered_completion_carries_usage(self):
        body = ResponseFactory.completion(
            "chatcmpl-x", "deepseek-chat", "halo", "", [], "stop",
            self.USAGE)
        self.assertEqual(body["usage"], self.USAGE)


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class StatsTest(unittest.TestCase):
    def test_counters(self):
        stats = ServerStats()
        usage = {"prompt_tokens": 5, "completion_tokens": 7}
        stats.record("deepseek-chat", "hit", 1.5, 2, "ok", usage)
        stats.record("deepseek-chat", "-", 0.5, 0, "error-502")
        stats.record("deepseek-reasoner", "-", 0.2, 0, "cancelled")
        snap = stats.snapshot()
        self.assertEqual(snap["totals"], {"requests": 3, "ok": 1,
                                          "cancelled": 1, "errors": 1})
        self.assertEqual(snap["chains"]["hit"], 1)
        self.assertEqual(snap["tokens"], {"prompt": 5, "completion": 7})
        chat = snap["by_model"]["deepseek-chat"]
        self.assertEqual(chat["requests"], 2)
        self.assertEqual(chat["retries"], 2)
        self.assertAlmostEqual(chat["total_duration"], 2.0)

    def test_stats_endpoint_starts_empty(self):
        client = ds_server.create_app().test_client()
        response = client.get("/stats")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["version"], "1.2.0")
        self.assertEqual(body["totals"]["requests"], 0)

    def test_completions_feed_the_endpoint(self):
        stub = FakeClient(history="teks-history")
        with tempfile.TemporaryDirectory() as tmp:
            store = ChainStore(path=Path(tmp) / "s.json")
            with mock.patch.object(ds_server, "DeepSeekClient",
                                   lambda token: stub), \
                    mock.patch.object(ds_server, "ChainStore",
                                      lambda: store):
                app = ds_server.create_app()
                client = app.test_client()
                headers = {"Authorization": "Bearer test-token"}

                plain = client.post(
                    "/v1/chat/completions", headers=headers,
                    json={"model": "deepseek-chat",
                          "messages": MESSAGES,
                          "history_mode": False})
                self.assertEqual(plain.status_code, 200)
                self.assertEqual(
                    plain.get_json()["choices"][0]["message"]["content"],
                    "teks-stream")

                streamed = client.post(
                    "/v1/chat/completions", headers=headers,
                    json={"model": "deepseek-chat",
                          "messages": MESSAGES,
                          "stream": True, "history_mode": True})
                self.assertEqual(streamed.status_code, 200)
                body = streamed.get_data(as_text=True)
                self.assertIn("teks-history", body)
                self.assertNotIn("teks-stream", body)

                stats = client.get("/stats").get_json()
                self.assertEqual(stats["totals"]["requests"], 2)
                self.assertEqual(stats["totals"]["ok"], 2)
                self.assertEqual(
                    stats["by_model"]["deepseek-chat"]["requests"], 2)
                self.assertGreater(stats["tokens"]["completion"], 0)


if __name__ == "__main__":
    unittest.main()
