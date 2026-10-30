"""Unit tests for upstream error mapping.

Covers the DSError status attribute, the status preserved through the
retry helper, and the client-facing mapping in ds_server.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web import core
from deepseek_web.core import DeepSeekClient, DSError, RequestPacer

try:
    import ds_server
    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.headers = {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def close(self):
        pass


class DSErrorStatusTest(unittest.TestCase):
    def test_status_defaults_to_none(self):
        self.assertIsNone(DSError("boom").status)

    def test_status_is_carried(self):
        self.assertEqual(DSError("boom", status=429).status, 429)

    def test_post_preserves_status_after_retries(self):
        client = DeepSeekClient("test-token",
                                pacer=RequestPacer(enabled=False))
        attempts = []

        def fake_post(*args, **kwargs):
            attempts.append(1)
            return FakeResponse(429)

        with mock.patch.object(core.time, "sleep"), \
                mock.patch.object(core.requests, "post", fake_post):
            with self.assertRaises(DSError) as raised:
                client._post("/chat_session/create", {})
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(attempts), 1 + DeepSeekClient.MAX_RETRIES)

    def test_business_auth_failure_tagged_401(self):
        client = DeepSeekClient("test-token",
                                pacer=RequestPacer(enabled=False))
        payload = {"code": 1, "msg": "Authorization Failed (invalid token)",
                   "data": {"biz_code": 1, "biz_msg": None}}

        def fake_post(*args, **kwargs):
            return FakeResponse(200, payload)

        with mock.patch.object(core.requests, "post", fake_post):
            with self.assertRaises(DSError) as raised:
                client._post("/chat_session/create", {})
        self.assertEqual(raised.exception.status, 401)

    def test_business_other_failure_untagged(self):
        error = DeepSeekClient._business_error("ctx", "model is busy")
        self.assertIsNone(error.status)
        error = DeepSeekClient._business_error("ctx", "Token expired")
        self.assertEqual(error.status, 401)


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class MappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = ds_server.create_app()

    def map_error(self, exc):
        with self.app.app_context():
            response, status = ds_server.upstream_error_response(exc)
            return status, response.get_json()["error"]

    def test_rate_limit_stays_429(self):
        status, error = self.map_error(DSError("slow down", status=429))
        self.assertEqual(status, 429)
        self.assertEqual(error["type"], "rate_limit_error")
        self.assertEqual(error["code"], "rate_limit_exceeded")

    def test_rejected_token_becomes_401(self):
        for upstream in (401, 403):
            status, error = self.map_error(
                DSError("nope", status=upstream))
            self.assertEqual(status, 401)
            self.assertEqual(error["type"], "authentication_error")
            self.assertEqual(error["code"], "invalid_api_key")

    def test_other_4xx_passes_through(self):
        status, error = self.map_error(DSError("bad", status=404))
        self.assertEqual(status, 404)
        self.assertEqual(error["type"], "invalid_request_error")

    def test_5xx_becomes_502(self):
        status, error = self.map_error(DSError("dead", status=503))
        self.assertEqual(status, 502)
        self.assertEqual(error["type"], "upstream_error")

    def test_unknown_status_becomes_502(self):
        status, error = self.map_error(DSError("connection failed"))
        self.assertEqual(status, 502)
        self.assertEqual(error["type"], "upstream_error")


if __name__ == "__main__":
    unittest.main()
