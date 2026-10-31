"""Unit tests for the v1.2.2 patch package: full-prefix chain
fingerprints (the C4 blind window), one retry on a completely empty
result (X1), upload business errors carrying biz_msg with rate
limits tagged 429 (X2), and the DS_LOG reset label for same-key
divergence (X3). Findings: ~/workspace/ds-server-uat/FINDINGS.md.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web import core
from deepseek_web.core import ChatResult, DeepSeekClient, DSError
from deepseek_web.sessions import ChainStore

try:
    import ds_server
    from ds_server import ChatRequest, CompletionService
    HAS_SERVER = True
except Exception:  # flask not installed in this environment
    HAS_SERVER = False


def msg(role, content):
    return {"role": role, "content": content}


class PrefixContinuationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.json"
        self.store = ChainStore(path=self.path)
        self.history = [msg("user", "nama kucing saya MOKA"),
                        msg("assistant", "baik"),
                        msg("user", "kucing saya suka IKAN"),
                        msg("assistant", "dicatat"),
                        msg("user", "Kucing saya suka apa?")]
        self.store.put("k", "s1")
        self.store.update(
            "k", parent=9, count=len(self.history),
            last_fp=ChainStore.fingerprint(self.history[-1]),
            prefix=[ChainStore.fingerprint(m) for m in self.history])
        self.record = self.store.get("k")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_extension_continues(self):
        extended = self.history + [msg("assistant", "IKAN"),
                                   msg("user", "dan minumnya?")]
        self.assertTrue(
            ChainStore.is_continuation(self.record, extended))

    def test_edit_behind_the_boundary_resets(self):
        edited = list(self.history)
        edited[2] = msg("user", "kucing saya suka AYAM")
        extended = edited + [msg("assistant", "AYAM"),
                             msg("user", "Kucing saya suka apa?")]
        self.assertFalse(
            ChainStore.is_continuation(self.record, extended))

    def test_legacy_record_without_prefix_resets_once(self):
        import json
        data = json.loads(self.path.read_text())
        del data["chains"]["k"]["prefix"]
        self.path.write_text(json.dumps(data))
        record = self.store.get("k")
        extended = self.history + [msg("assistant", "IKAN"),
                                   msg("user", "dan minumnya?")]
        self.assertFalse(
            ChainStore.is_continuation(record, extended))


class FakeChains:
    def __init__(self, record=None):
        self._record = record
        self.updated = None

    def get(self, key):
        return self._record

    def is_continuation(self, record, messages):
        return False

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        self.updated = kwargs

    def drain_retired(self):
        return []

    @staticmethod
    def fingerprint(message):
        return "fp"


class ContinuationChains(FakeChains):
    def is_continuation(self, record, messages):
        return record is not None


class FlakyClient:
    """First completion is empty (history too); later ones answer."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.completions = 0
        self.sessions = 0
        self.retries = 0

    def create_session(self):
        self.sessions += 1
        return f"s{self.sessions}"

    def chat_completion(self, session_id, parent, prompt, file_ids,
                        **kwargs):
        self.completions += 1
        content = self.answers.pop(0) if self.answers else ""
        return ChatResult(content=content,
                          response_id=self.completions)

    def fetch_history_text(self, session_id):
        return ""

    def delete_session(self, session_id):
        pass


def make_request(body=None):
    return ChatRequest.from_body(
        {"model": "deepseek-chat",
         "messages": [msg("user", "Berapa 2 + 2?")], **(body or {})})


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class EmptyRetryTest(unittest.TestCase):
    def test_empty_result_retries_once_on_a_fresh_session(self):
        client = FlakyClient(["", "4"])
        service = CompletionService(FakeChains())
        answer, _t, status = service.run(client, make_request(), "k")
        self.assertEqual(answer, "4")
        self.assertEqual(client.completions, 2)
        self.assertEqual(client.sessions, 2)
        self.assertEqual(status, "new")

    def test_persistent_empty_gives_up_after_one_retry(self):
        client = FlakyClient(["", "", ""])
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(client, make_request(), "k")
        self.assertEqual(answer, "")
        self.assertEqual(client.completions, 2)

    def test_non_empty_result_never_retries(self):
        client = FlakyClient(["halo"])
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(client, make_request(), "k")
        self.assertEqual(answer, "halo")
        self.assertEqual(client.completions, 1)

    def test_empty_continuation_retries_and_reports_reset(self):
        record = {"ds_session": "s0", "parent": 3, "count": 1}
        chains = ContinuationChains(record)
        client = FlakyClient(["", "jawaban"])
        service = CompletionService(chains)
        messages = [msg("user", "satu"), msg("assistant", "dua"),
                    msg("user", "tiga")]
        request = ChatRequest.from_body(
            {"model": "deepseek-chat", "messages": messages})
        answer, _t, status = service.run(client, request, "k")
        self.assertEqual(answer, "jawaban")
        self.assertEqual(status, "reset")


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class DivergenceLabelTest(unittest.TestCase):
    def test_existing_record_divergence_is_labeled_reset(self):
        record = {"ds_session": "s1", "parent": 1, "count": 2}
        service = CompletionService(FakeChains(record))
        client = FlakyClient(["jawaban"])
        _a, _t, status = service.run(client, make_request(), "k")
        self.assertEqual(status, "reset")

    def test_no_record_is_labeled_new(self):
        service = CompletionService(FakeChains())
        client = FlakyClient(["jawaban"])
        _a, _t, status = service.run(client, make_request(), "k")
        self.assertEqual(status, "new")


class FakeUploadResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class UploadBusinessErrorTest(unittest.TestCase):
    def run_upload(self, payload):
        client = DeepSeekClient("test-token")
        response = FakeUploadResponse(payload)
        with mock.patch.object(client, "_pow_headers",
                               lambda path: {}), \
                mock.patch.object(client, "_request",
                                  lambda desc, send: response):
            with self.assertRaises(DSError) as caught:
                client.upload_file(b"png-bytes", "image.png")
        return caught.exception

    def test_biz_msg_reaches_the_client(self):
        error = self.run_upload(
            {"code": 0, "msg": "",
             "data": {"biz_code": 3, "biz_msg": "file too large"}})
        self.assertIn("file too large", str(error))
        self.assertIsNone(error.status)

    def test_rate_limit_business_error_is_tagged_429(self):
        error = self.run_upload(
            {"code": 0, "msg": "",
             "data": {"biz_code": 7, "biz_msg": "rate limit reached"}})
        self.assertIn("rate limit reached", str(error))
        self.assertEqual(error.status, 429)


if __name__ == "__main__":
    unittest.main()
