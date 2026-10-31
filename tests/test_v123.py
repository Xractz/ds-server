"""Unit tests for the v1.2.3 attachment patch: OpenAI ``file``
content parts and remote http(s) image URLs, both uploaded to the
backend like data-URL images.

Run from the repository root (with dependencies installed):

    python3 -m unittest discover -s tests -v
"""
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepseek_web.core import ChatResult
from deepseek_web.tools import MessageView

try:
    import ds_server
    from ds_server import ChatRequest, CompletionService
    HAS_SERVER = True
except Exception:  # flask not installed in this environment
    HAS_SERVER = False


def b64(data):
    return base64.b64encode(data).decode()


def user_with(parts):
    return {"role": "user", "content": parts}


class FilePartsTest(unittest.TestCase):
    def test_nested_file_part_decodes(self):
        message = user_with([{
            "type": "file",
            "file": {"filename": "catatan.txt",
                     "file_data": "data:text/plain;base64,"
                                  + b64(b"isi dokumen")}}])
        found = MessageView.files(message)
        self.assertEqual(len(found), 1)
        data, filename, mime = found[0]
        self.assertEqual(data, b"isi dokumen")
        self.assertEqual(filename, "catatan.txt")
        self.assertEqual(mime, "text/plain")

    def test_flat_file_part_with_bare_base64(self):
        message = user_with([{
            "type": "file", "filename": "a.bin",
            "file_data": b64(b"\x01\x02")}])
        found = MessageView.files(message)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], b"\x01\x02")
        self.assertEqual(found[0][1], "a.bin")

    def test_undecodable_file_part_is_skipped(self):
        message = user_with([{
            "type": "file",
            "file": {"filename": "x.txt",
                     "file_data": "data:text/plain;base64,###"}}])
        self.assertEqual(MessageView.files(message), [])

    def test_file_part_renders_a_placeholder(self):
        message = user_with([
            {"type": "text", "text": "lihat ini"},
            {"type": "file",
             "file": {"filename": "x.txt", "file_data": b64(b"y")}}])
        self.assertEqual(MessageView.text(message),
                         "lihat ini\n[File attached]")


class ImageUrlsTest(unittest.TestCase):
    def test_remote_urls_collected_data_urls_not(self):
        message = user_with([
            {"type": "image_url",
             "image_url": {"url": "https://example.com/a.png"}},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + b64(b"p")}},
            {"type": "image_url",
             "image_url": {"url": "http://example.com/b.jpg"}}])
        self.assertEqual(
            MessageView.image_urls(message),
            ["https://example.com/a.png", "http://example.com/b.jpg"])


class UploadClient:
    def __init__(self):
        self.uploads = []
        self.completions = []

    def upload_file(self, data, filename=None):
        self.uploads.append((data, filename))
        return f"file-{len(self.uploads)}"

    def create_session(self):
        return "s1"

    def chat_completion(self, session_id, parent, prompt, file_ids,
                        **kwargs):
        self.completions.append(list(file_ids))
        return ChatResult(content="baik", response_id=1)

    def fetch_history_text(self, session_id):
        return ""

    def delete_session(self, session_id):
        pass


class FakeChains:
    def get(self, key):
        return None

    def is_continuation(self, record, messages):
        return False

    def put(self, key, session_id, model):
        pass

    def update(self, key, **kwargs):
        pass

    def drain_retired(self):
        return []

    @staticmethod
    def fingerprint(message):
        return "fp"


class FakeResponse:
    def __init__(self, content, ctype="image/png", status=200):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": ctype}


@unittest.skipUnless(HAS_SERVER, "ds_server dependencies not installed")
class AttachmentServiceTest(unittest.TestCase):
    def run_with(self, parts):
        client = UploadClient()
        service = CompletionService(FakeChains())
        request = ChatRequest.from_body(
            {"model": "deepseek-chat",
             "messages": [user_with(parts)]})
        service.run(client, request, "k")
        return client

    def test_file_part_is_uploaded_and_attached(self):
        client = self.run_with([{
            "type": "file",
            "file": {"filename": "doc.txt",
                     "file_data": "data:text/plain;base64,"
                                  + b64(b"halo")}}])
        self.assertEqual(client.uploads, [(b"halo", "doc.txt")])
        self.assertEqual(client.completions, [["file-1"]])

    def test_remote_image_is_fetched_and_uploaded(self):
        parts = [{"type": "image_url",
                  "image_url": {"url": "https://example.com/merah.png"}}]
        fake_requests = mock.Mock()
        fake_requests.get = mock.Mock(
            return_value=FakeResponse(b"PNGDATA"))
        with mock.patch.object(ds_server, "requests", fake_requests):
            client = self.run_with(parts)
        self.assertEqual(client.uploads, [(b"PNGDATA", "image.png")])
        self.assertEqual(client.completions, [["file-1"]])

    def test_unreachable_remote_image_is_skipped(self):
        parts = [{"type": "text", "text": "halo"},
                 {"type": "image_url",
                  "image_url": {"url": "https://example.com/x.png"}}]
        fake_requests = mock.Mock()
        fake_requests.get = mock.Mock(side_effect=OSError("down"))
        with mock.patch.object(ds_server, "requests", fake_requests):
            client = self.run_with(parts)
        self.assertEqual(client.uploads, [])
        self.assertEqual(client.completions, [[]])


if __name__ == "__main__":
    unittest.main()
