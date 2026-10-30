"""Core client for the DeepSeek web API (chat.deepseek.com).

This module is the only layer that communicates with the DeepSeek web
backend. It backs the REST server (``ds_server``) and can also be used
directly as a library.

Components:
    TokenStore:    resolves the account token from the request or the
                   environment. The API key presented by the client is
                   the DeepSeek userToken itself; the server keeps no
                   credential of its own.
    DeviceIdentity: resolves the per-installation device identifier.
    PowSolver:     solves the DeepSeekHashV1 proof-of-work challenge with
                   the official WebAssembly module shipped by DeepSeek.
    StreamParser:  incremental parser for the completion SSE stream.
    DeepSeekClient: session management, file upload and chat completion.

Every request carries the complete Chrome header set and a valid
proof-of-work response, exactly as the official web client does.
"""
import base64
import json
import mimetypes
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
WASM_PATH = BASE_DIR / "sha3_wasm_bg.wasm"
CONFIG_DIR = Path.home() / ".config" / "ds-server"
API_BASE = "https://chat.deepseek.com/api/v0"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/154.0.0.0 Safari/537.36")


class DSError(Exception):
    """Raised when the DeepSeek backend rejects or fails a request."""


class TokenStore:
    """Resolves the DeepSeek account token for one request.

    The token is the account's userToken, presented by the client as
    its API key. An explicitly supplied value wins; otherwise the
    ``DS_TOKEN`` environment variable is used, which is convenient for
    headless deployments. Nothing is persisted to disk.
    """

    @staticmethod
    def resolve(explicit=None):
        if explicit:
            return explicit
        if os.environ.get("DS_TOKEN"):
            return os.environ["DS_TOKEN"]
        return None


class DeviceIdentity:
    """Resolves the device identifier presented to the backend.

    The web client generates a random device identifier once and
    stores it locally; this class does the same. A random identifier
    is generated on first use and persisted in the configuration
    directory, so one installation presents one stable device. The
    ``DS_DEVICE_ID`` environment variable overrides the stored value.
    No identifier is hardcoded in the source code.
    """

    DEVICE_ID_PATH = CONFIG_DIR / "device_id"

    @classmethod
    def resolve(cls):
        override = os.environ.get("DS_DEVICE_ID")
        if override:
            return override.strip()
        if cls.DEVICE_ID_PATH.exists():
            stored = cls.DEVICE_ID_PATH.read_text().strip()
            if stored:
                return stored
        generated = str(uuid.uuid4())
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cls.DEVICE_ID_PATH.write_text(generated)
        cls.DEVICE_ID_PATH.chmod(0o600)
        return generated


class PowSolver:
    """Solves DeepSeekHashV1 proof-of-work challenges.

    The solver runs the official ``sha3_wasm_bg.wasm`` module published
    by DeepSeek (a SHA3-256 variant) through wasmtime. The module is
    loaded lazily on first use and reused for the process lifetime.
    """

    def __init__(self, wasm_path=WASM_PATH):
        self._wasm_path = wasm_path
        self._runtime = None

    def _load(self):
        if self._runtime is None:
            import wasmtime
            store = wasmtime.Store()
            module = wasmtime.Module.from_file(store.engine,
                                               str(self._wasm_path))
            instance = wasmtime.Instance(store, module, [])
            self._runtime = (store, instance)
        return self._runtime

    def solve(self, challenge):
        """Return the numeric answer for one challenge dictionary."""
        store, instance = self._load()
        exports = instance.exports(store)
        memory = exports["memory"]
        wasm_solve = exports["wasm_solve"]
        stack_pointer = exports["__wbindgen_add_to_stack_pointer"]
        malloc = exports["__wbindgen_export_0"]

        def alloc(data: bytes):
            ptr = malloc(store, len(data), 1)
            memory.write(store, data, ptr)
            return ptr, len(data)

        prefix = f"{challenge['salt']}_{challenge['expire_at']}_".encode()
        challenge_ptr, challenge_len = alloc(challenge["challenge"].encode())
        prefix_ptr, prefix_len = alloc(prefix)
        ret = stack_pointer(store, -16)
        wasm_solve(store, ret, challenge_ptr, challenge_len,
                   prefix_ptr, prefix_len, float(challenge["difficulty"]))
        raw = bytes(memory.read(store, ret, ret + 16))
        status = struct.unpack("<i", raw[0:4])[0]
        answer = struct.unpack("<d", raw[8:16])[0]
        stack_pointer(store, 16)
        if status == 0:
            raise DSError("Proof of work failed; the challenge may "
                          "have expired.")
        return int(answer)


@dataclass
class ChatResult:
    """The outcome of one chat completion."""

    content: str = ""
    thinking: str = ""
    request_id: object = None
    response_id: object = None


class StreamParser:
    """Incremental parser for the completion event stream.

    The backend streams a response as a sequence of fragments. Each
    fragment has a type (response text, thinking, search) and receives
    incremental appends addressed by path. The parser keeps a registry
    of fragments in arrival order so that every append is routed to the
    correct output channel:

    - thinking fragments accumulate into the reasoning text,
    - response fragments accumulate into the answer text,
    - search fragments contain user interface labels only and are
      discarded.

    An index of ``-1`` in a path always refers to the most recently
    registered fragment. Structural citation markers of the form
    ``[reference:N]`` are filtered out of the answer text.
    """

    CONTENT = "content"
    THINKING = "thinking"
    IGNORE = "ignore"

    def __init__(self, on_token=None):
        self._on_token = on_token
        self._order = []
        self._kinds = {}
        self._registered = set()
        self._content_parts = []
        self._thinking_parts = []

    @property
    def content(self):
        return "".join(self._content_parts)

    @property
    def thinking(self):
        return "".join(self._thinking_parts)

    @staticmethod
    def _classify(fragment_type):
        kind = str(fragment_type or "").upper()
        if "THINK" in kind:
            return StreamParser.THINKING
        if "SEARCH" in kind:
            return StreamParser.IGNORE
        return StreamParser.CONTENT

    def _emit(self, kind, text):
        if kind == self.IGNORE or not isinstance(text, str) or not text:
            return
        if kind == self.THINKING:
            self._thinking_parts.append(text)
        else:
            self._content_parts.append(text)
        if self._on_token:
            self._on_token(kind, text)

    def _register(self, fragment):
        """Register a newly announced fragment and emit its initial
        content exactly once."""
        if not isinstance(fragment, dict):
            return
        fragment_id = fragment.get("id")
        if fragment_id is not None and fragment_id not in self._kinds:
            self._kinds[fragment_id] = self._classify(fragment.get("type"))
            self._order.append(fragment_id)
        if fragment_id in self._registered:
            return
        self._registered.add(fragment_id)
        self._emit(self._kinds.get(fragment_id, self.CONTENT),
                   fragment.get("content"))

    def _last_kind(self):
        if self._order:
            return self._kinds.get(self._order[-1], self.CONTENT)
        return self.CONTENT

    def _kind_at(self, path):
        """Resolve the channel for a fragment content path."""
        parts = (path or "").split("/")
        if len(parts) >= 3:
            selector = parts[2]
            if selector == "-1" and self._order:
                return self._kinds.get(self._order[-1], self.CONTENT)
            try:
                index = int(selector)
            except ValueError:
                index = -1
            if 0 <= index < len(self._order):
                return self._kinds.get(self._order[index], self.CONTENT)
        return self._last_kind()

    def feed(self, event):
        """Consume one parsed stream event."""
        path = event.get("p", "") or ""
        operation = event.get("o")
        value = event.get("v")

        # Full fragment snapshot.
        if isinstance(value, dict) and isinstance(value.get("response"), dict):
            for fragment in value["response"].get("fragments", []):
                self._register(fragment)
            return

        # Batch update at response level; fragment appends inside it
        # carry the first characters of a response.
        if path == "response" and operation == "BATCH" \
                and isinstance(value, list):
            for sub in value:
                if not isinstance(sub, dict):
                    continue
                if sub.get("p") == "fragments" \
                        and sub.get("o") == "APPEND":
                    for fragment in sub.get("v") or []:
                        self._register(fragment)
                elif isinstance(sub.get("v"), str) \
                        and str(sub.get("p", "")).endswith("/content"):
                    self._emit(self._kind_at("response/" + str(sub["p"])),
                               sub["v"])
            return

        # Fragment append at response level.
        if path == "response/fragments" and operation == "APPEND" \
                and isinstance(value, list):
            for fragment in value:
                self._register(fragment)
            return

        # Batch update inside one fragment.
        if path.startswith("response/fragments/") \
                and operation == "BATCH" and isinstance(value, list):
            for sub in value:
                if isinstance(sub, dict) and sub.get("p") == "content":
                    text = sub.get("v")
                    if isinstance(text, str) and re.fullmatch(
                            r"\[reference:\d+\]", text.strip()):
                        continue
                    self._emit(self._kind_at(path + "/content"), text)
            return

        # Text append to one fragment. The operation may be absent at a
        # fragment transition; the chunk still belongs to that fragment.
        if isinstance(value, str) \
                and path.startswith("response/fragments/") \
                and path.endswith("/content") \
                and operation in ("APPEND", None):
            self._emit(self._kind_at(path), value)
            return

        if operation == "APPEND" and isinstance(value, str) \
                and "/thinking" in path:
            self._emit(self.THINKING, value)
            return

        # Unaddressed continuation: belongs to the active fragment.
        if operation is None and path == "" and isinstance(value, str):
            self._emit(self._last_kind(), value)


class DeepSeekClient:
    """Client for one DeepSeek account, identified by its token."""

    def __init__(self, token, pow_solver=None, device_id=None):
        self._token = token
        self._pow = pow_solver or PowSolver()
        self._device_id = device_id or DeviceIdentity.resolve()

    # -- request plumbing ----------------------------------------------

    def headers(self, referer="https://chat.deepseek.com/",
                content_type="application/json"):
        """The complete Chrome header set used by the web client."""
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9,id;q=0.8,ms;q=0.7",
            "authorization": f"Bearer {self._token}",
            "cache-control": "no-cache",
            "dnt": "1",
            "origin": "https://chat.deepseek.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": referer,
            "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", '
                         '"Not A(Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-timezone-offset": "25200",
            "x-client-version": "2.5.0",
            "x-device-id": self._device_id,
        }
        if content_type:
            headers["content-type"] = content_type
        return headers

    def _pow_headers(self, target_path):
        """Base headers extended with a solved proof-of-work response."""
        headers = self.headers()
        response = requests.post(
            API_BASE + "/chat/create_pow_challenge", headers=headers,
            json={"target_path": target_path}, timeout=30)
        if response.status_code != 200:
            raise DSError("create_pow_challenge: "
                          f"HTTP {response.status_code}")
        payload = response.json()
        if payload.get("code") != 0:
            raise DSError(f"create_pow_challenge: {payload.get('msg')}")
        challenge = payload["data"]["biz_data"]["challenge"]
        proof = {
            "algorithm": "DeepSeekHashV1",
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": self._pow.solve(challenge),
            "signature": challenge["signature"],
            "target_path": target_path,
        }
        headers["x-ds-pow-response"] = base64.b64encode(
            json.dumps(proof).encode()).decode()
        return headers

    def _post(self, path, body, headers=None):
        """POST a JSON request and return the business payload."""
        response = requests.post(
            API_BASE + path, headers=headers or self.headers(),
            json=body, timeout=30)
        if response.status_code != 200:
            raise DSError(f"{path}: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("code") != 0 \
                or payload.get("data", {}).get("biz_code") != 0:
            raise DSError(f"{path}: {payload.get('msg')}/"
                          f"{payload.get('data', {}).get('biz_msg')}")
        return payload["data"]["biz_data"]

    # -- sessions --------------------------------------------------------

    def create_session(self):
        """Create a new chat session and return its identifier."""
        headers = self._pow_headers("/api/v0/chat/completion")
        data = self._post("/chat_session/create", {}, headers=headers)
        return data["chat_session"]["id"]

    # -- files -----------------------------------------------------------

    def upload_file(self, source, filename=None):
        """Upload a file (path or bytes) and wait until the backend has
        parsed it. Returns the file identifier."""
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.exists():
                raise DSError(f"File not found: {source}")
            data = path.read_bytes()
            filename = filename or path.name
        else:
            data = source
            filename = filename or "file"
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"

        headers = self._pow_headers("/api/v0/file/upload_file")
        headers.pop("content-type", None)
        response = requests.post(
            API_BASE + "/file/upload_file", headers=headers,
            files={"file": (filename, data, mime)}, timeout=120)
        if response.status_code != 200:
            raise DSError(f"upload {filename}: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("code") != 0 \
                or payload.get("data", {}).get("biz_code") != 0:
            raise DSError(f"upload {filename}: {payload.get('msg')}")
        file_id = payload["data"]["biz_data"]["id"]

        for _ in range(20):
            time.sleep(3)
            status_response = requests.get(
                API_BASE + "/file/fetch_files", headers=self.headers(),
                params={"file_ids": file_id}, timeout=30)
            try:
                entry = status_response.json()["data"]["biz_data"] \
                    ["files"][0]
            except Exception:
                continue
            status = entry.get("status")
            if status == "SUCCESS":
                return file_id
            if status in ("failed", "content_filter", "content_empty"):
                raise DSError(f"upload {filename}: parsing failed "
                              f"({status})")
        raise DSError(f"upload {filename}: parsing timed out")

    # -- history ---------------------------------------------------------

    def fetch_history_text(self, session_id):
        """Return the text of the latest assistant reply in a session.

        This is a fallback for cases where stream parsing yields an
        empty answer; the stored history is the authoritative record.
        """
        response = requests.get(
            API_BASE + "/chat/history_messages", headers=self.headers(),
            params={"chat_session_id": session_id}, timeout=30)
        if response.status_code != 200:
            return ""
        try:
            messages = response.json()["data"]["biz_data"]["chat_messages"]
        except Exception:
            return ""
        assistants = [m for m in messages if m.get("role") == "ASSISTANT"]
        if not assistants:
            return ""
        latest = assistants[-1]
        return "".join(fragment.get("content", "")
                       for fragment in latest.get("fragments", [])
                       if fragment.get("type") == "RESPONSE")

    # -- completion ------------------------------------------------------

    def chat_completion(self, session_id, parent_id, prompt,
                        ref_file_ids=None, thinking=False, search=False,
                        model_type="default", on_token=None):
        """Send one message and stream the reply.

        ``on_token`` is invoked as ``on_token(kind, text)`` for every
        streamed fragment, where kind is ``"content"`` or ``"thinking"``.
        Returns a :class:`ChatResult`.
        """
        headers = self._pow_headers("/api/v0/chat/completion")
        headers["referer"] = \
            f"https://chat.deepseek.com/a/chat/s/{session_id}"
        ref_file_ids = ref_file_ids or []
        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": ref_file_ids,
            "thinking_enabled": thinking,
            "search_enabled": bool(search) and not ref_file_ids,
            "action": None,
            "preempt": False,
        }
        response = requests.post(API_BASE + "/chat/completion",
                                 headers=headers, json=body,
                                 timeout=300, stream=True)
        if response.status_code != 200:
            raise DSError(f"completion: HTTP {response.status_code}")
        if "application/json" in response.headers.get("content-type", ""):
            raise DSError("completion rejected: "
                          f"{response.json().get('msg')}")

        parser = StreamParser(on_token=on_token)
        result = ChatResult()
        debug = bool(os.environ.get("DS_DEBUG"))
        debug_lines = []

        for line in response.iter_lines(decode_unicode=True):
            if debug and len(debug_lines) < 15:
                debug_lines.append(repr(line))
            if not line:
                continue
            line = line.lstrip("\ufeff").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            if "request_message_id" in event:
                result.request_id = event.get("request_message_id")
                result.response_id = event.get("response_message_id")
                continue
            parser.feed(event)

        if debug and debug_lines:
            with open("/tmp/ds-debug.log", "a") as handle:
                handle.write("\n".join(debug_lines) + "\n---\n")

        result.content = parser.content
        result.thinking = parser.thinking
        return result
