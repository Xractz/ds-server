"""Unit tests for the v1.2.4 patch package: an answer that is still
empty after the fresh-session retry becomes a retryable 502 instead
of an empty 200 (delta UAT F1), the empty-retry no longer relabels a
continued chain as reset (F2), and [citation:N] markers are stripped
from answers like [reference:N] (O1).
Findings: ~/workspace/ds-server-uat/FINDINGS-v123.md.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web.core import ChatResult, DSError

try:
    import ds_server
    from ds_server import ChatRequest, CompletionService
    HAS_SERVER = True
except Exception:  # flask not installed in this environment
    HAS_SERVER = False


def msg(role, content):
    return {"role": role, "content": content}


class FakeChains:
    def __init__(self, record=None, continuation=False):
        self._record = record
        self._continuation = continuation
        self.updated = None

    def get(self, key):
        return self._record

    def is_continuation(self, record, messages):
        return self._continuation

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        self.updated = kwargs

    def drain_retired(self):
        return []

    @staticmethod
    def fingerprint(message):
        return "fp"


class ScriptedClient:
    def __init__(self, answers, history=""):
        self.answers = list(answers)
        self.history = history
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
        return self.history

    def delete_session(self, session_id):
        pass


def make_request(messages=None):
    return ChatRequest.from_body(
        {"model": "deepseek-chat",
         "messages": messages or [msg("user", "Berapa 2 + 2?")]})


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class EmptyBecomes502Test(unittest.TestCase):
    def test_still_empty_after_retry_raises_502(self):
        client = ScriptedClient(["", ""])
        service = CompletionService(FakeChains())
        with self.assertRaises(DSError) as caught:
            service.run(client, make_request(), "k")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(client.completions, 2)

    def test_retry_rescue_still_returns_the_answer(self):
        client = ScriptedClient(["", "4"])
        service = CompletionService(FakeChains())
        answer, _t, status = service.run(client, make_request(), "k")
        self.assertEqual(answer, "4")
        self.assertEqual(status, "new")


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class EmptyRetryLabelTest(unittest.TestCase):
    def test_continuation_keeps_hit_label_after_empty_retry(self):
        record = {"ds_session": "s0", "parent": 3, "count": 1}
        chains = FakeChains(record, continuation=True)
        client = ScriptedClient(["", "jawaban"])
        service = CompletionService(chains)
        messages = [msg("user", "satu"), msg("assistant", "dua"),
                    msg("user", "tiga")]
        answer, _t, status = service.run(
            client, make_request(messages), "k")
        self.assertEqual(answer, "jawaban")
        self.assertEqual(status, "hit")


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class CitationStripTest(unittest.TestCase):
    def run_content(self, content):
        client = ScriptedClient([content])
        service = CompletionService(FakeChains())
        answer, _t, _s = service.run(client, make_request(), "k")
        return answer

    def test_citation_and_reference_markers_are_stripped(self):
        answer = self.run_content(
            "Andrea Hirata[citation:1][citation:2] menulis. "
            "[reference:3] Selesai.")
        self.assertNotIn("[citation:", answer)
        self.assertNotIn("[reference:", answer)
        self.assertIn("Andrea Hirata", answer)
        self.assertIn("Selesai.", answer)

    def test_unrelated_brackets_survive(self):
        answer = self.run_content("Lihat [catatan: 1] dan [lampiran].")
        self.assertIn("[catatan: 1]", answer)
        self.assertIn("[lampiran]", answer)


if __name__ == "__main__":
    unittest.main()
