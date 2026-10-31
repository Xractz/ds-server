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
    RequestPacer:  process-wide pacing: completions are serialized and
                   spaced, session creations are spaced, and transient
                   upstream failures are retried with backoff.
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
import queue
import re
import struct
import threading
import time
import uuid
from contextlib import contextmanager
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
    """Raised when the DeepSeek backend rejects or fails a request.

    ``status`` carries the upstream HTTP status when one is known, so
    the server can map the failure to an appropriate client-facing
    status (for example 429 for rate limits) instead of a generic one.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class StreamCancelled(Exception):
    """Raised when the caller aborts a streaming completion.

    Deliberately not a DSError: a cancellation is not a backend
    failure and must never trigger the expired-session retry.
    """


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


class RequestPacer:
    """Process-wide pacing for upstream requests.

    Bursts are the most visible signature of automation, so upstream
    traffic is smoothed in two ways:

    - Completions are serialized: one completion runs at a time and
      concurrent client requests queue instead of bursting.
    - Minimum intervals are enforced between completion starts and
      between chat session creations.

    Pacing is enabled by default and is shared by every client in the
    process, however many client instances the server creates. The
    intervals can be tuned with the ``DS_MIN_COMPLETION_INTERVAL`` and
    ``DS_MIN_SESSION_INTERVAL`` environment variables (seconds), and
    pacing can be disabled entirely with ``DS_PACING=0``.

    The pacer is also adaptive: every upstream 429 that forces a retry
    widens the effective intervals (the penalty doubles from a floor,
    up to a cap) and the penalty then decays with a half-life, so the
    server backs off while the backend is under pressure and relaxes
    on its own afterwards. Adaptation can be switched off with
    ``DS_ADAPTIVE_PACING=0``.
    """

    DEFAULT_COMPLETION_INTERVAL = 0.4
    DEFAULT_SESSION_INTERVAL = 1.0
    PENALTY_FLOOR = 2.0
    PENALTY_CAP = 30.0
    PENALTY_HALF_LIFE = 60.0

    def __init__(self, completion_interval=DEFAULT_COMPLETION_INTERVAL,
                 session_interval=DEFAULT_SESSION_INTERVAL, enabled=True,
                 adaptive=True,
                 clock=time.monotonic, sleep=time.sleep):
        self._completion_interval = max(0.0, float(completion_interval))
        self._session_interval = max(0.0, float(session_interval))
        self._enabled = bool(enabled)
        self._adaptive = bool(adaptive)
        self._clock = clock
        self._sleep = sleep
        self._completion_lock = threading.Lock()
        self._stamp_lock = threading.Lock()
        self._last_completion_start = None
        self._last_session_start = None
        self._penalty = 0.0
        self._penalty_at = None

    @classmethod
    def from_env(cls):
        """Build a pacer from the DS_PACING configuration variables."""
        def _seconds(name, default):
            try:
                return float(os.environ[name])
            except (KeyError, ValueError):
                return default

        return cls(
            completion_interval=_seconds(
                "DS_MIN_COMPLETION_INTERVAL",
                cls.DEFAULT_COMPLETION_INTERVAL),
            session_interval=_seconds(
                "DS_MIN_SESSION_INTERVAL", cls.DEFAULT_SESSION_INTERVAL),
            enabled=os.environ.get("DS_PACING", "1").strip().lower()
            not in ("0", "false", "no", "off"),
            adaptive=os.environ.get(
                "DS_ADAPTIVE_PACING", "1").strip().lower()
            not in ("0", "false", "no", "off"),
        )

    def _penalty_locked(self):
        """The current penalty in seconds, decayed to now.

        The caller must hold ``_stamp_lock``.
        """
        if self._penalty_at is None or self._penalty <= 0:
            return 0.0
        elapsed = max(0.0, self._clock() - self._penalty_at)
        return self._penalty * (0.5 ** (elapsed / self.PENALTY_HALF_LIFE))

    @property
    def current_penalty(self):
        """Seconds currently added to the pacing intervals."""
        with self._stamp_lock:
            return self._penalty_locked()

    def note_rate_limited(self):
        """Widen the intervals after an upstream rate limit.

        The penalty doubles from ``PENALTY_FLOOR`` up to
        ``PENALTY_CAP`` on every call, measured from its current
        decayed value, and then relaxes with ``PENALTY_HALF_LIFE``.
        """
        if not self._enabled or not self._adaptive:
            return
        with self._stamp_lock:
            widened = min(max(self._penalty_locked() * 2.0,
                              self.PENALTY_FLOOR), self.PENALTY_CAP)
            self._penalty = widened
            self._penalty_at = self._clock()

    def _wait_since(self, last, interval):
        if not self._enabled or interval <= 0 or last is None:
            return
        remaining = interval - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)

    @contextmanager
    def completion_slot(self):
        """Run one completion at a time, spaced from the previous one."""
        if not self._enabled:
            yield
            return
        with self._completion_lock:
            with self._stamp_lock:
                last = self._last_completion_start
            self._wait_since(
                last, self._completion_interval + self.current_penalty)
            with self._stamp_lock:
                self._last_completion_start = self._clock()
            yield

    def wait_for_session(self):
        """Enforce the minimum interval before a session is created."""
        if not self._enabled:
            return
        with self._stamp_lock:
            last = self._last_session_start
        self._wait_since(
            last, self._session_interval + self.current_penalty)
        with self._stamp_lock:
            self._last_session_start = self._clock()


# One pacer for the whole process, configured from the environment at
# import time and shared by every DeepSeekClient instance.
SHARED_PACER = RequestPacer.from_env()


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
    registered fragment. Fragment updates may also arrive as lists
    appended or batched at the response level itself, rather than
    under ``response/fragments``. Structural citation markers of the
    form ``[reference:N]`` that arrive as standalone chunks are
    filtered out of every channel.
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
        # A standalone citation marker is structure, not text, in
        # every channel: it arrives as its own chunk from fragment
        # appends, batch updates, and fragment snapshots alike.
        if re.fullmatch(r"\[reference:\d+\]", text.strip()):
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
        # carry the first characters of a response. A sub-update
        # without a path is a fragment dict in its own right.
        if path == "response" and operation == "BATCH" \
                and isinstance(value, list):
            for sub in value:
                if not isinstance(sub, dict):
                    continue
                if "p" not in sub:
                    self._register(sub)
                elif sub.get("p") == "fragments" \
                        and sub.get("o") == "APPEND":
                    for fragment in sub.get("v") or []:
                        self._register(fragment)
                elif isinstance(sub.get("v"), str) \
                        and str(sub.get("p", "")).endswith("/content"):
                    self._emit(self._kind_at("response/" + str(sub["p"])),
                               sub["v"])
            return

        # Fragment append at response level: the same shapes as the
        # batch branch, but with an APPEND operation.
        if path == "response" and operation == "APPEND" \
                and isinstance(value, list):
            for sub in value:
                if not isinstance(sub, dict):
                    continue
                if "p" not in sub:
                    self._register(sub)
                elif sub.get("p") == "fragments" \
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

        # Batch update inside one fragment. Standalone citation
        # markers are dropped by _emit, like everywhere else.
        if path.startswith("response/fragments/") \
                and operation == "BATCH" and isinstance(value, list):
            for sub in value:
                if isinstance(sub, dict) and sub.get("p") == "content":
                    self._emit(self._kind_at(path + "/content"),
                               sub.get("v"))
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

    def __init__(self, token, pow_solver=None, device_id=None,
                 pacer=None):
        self._token = token
        self._pow = pow_solver or PowSolver()
        self._device_id = device_id or DeviceIdentity.resolve()
        self._pacer = pacer or SHARED_PACER
        self._retries = 0

    @property
    def retries(self):
        """How many upstream retries this client has performed."""
        return self._retries

    # -- request plumbing ----------------------------------------------

    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    MAX_RETRIES = 3

    # How often the streaming read re-checks the cancel event while
    # the upstream sends nothing. The read runs on a daemon thread
    # feeding a queue, so a stalled upstream cannot block the check:
    # the main loop wakes at least this often, sees the event, and
    # aborts. Normal traffic is unaffected, since lines are forwarded
    # as soon as they arrive.
    STREAM_CANCEL_POLL_SECONDS = 2.0

    @staticmethod
    def _iter_lines_cancellable(response, cancel_event,
                                poll_seconds=None):
        """Yield SSE lines, re-checking ``cancel_event`` while stalled.

        When no line arrives within ``poll_seconds``, the loop wakes,
        checks the event, and either keeps waiting or raises
        StreamCancelled. Reader errors are re-raised on the caller
        thread. The reader thread is a daemon, so a stalled read can
        never block process exit after the response is closed.
        """
        if poll_seconds is None:
            poll_seconds = DeepSeekClient.STREAM_CANCEL_POLL_SECONDS
        delivered = queue.Queue()

        def feed():
            try:
                for line in response.iter_lines(decode_unicode=True):
                    delivered.put(("line", line))
            except Exception as error:
                delivered.put(("error", error))
            finally:
                delivered.put(("end", None))

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        while True:
            try:
                kind, payload = delivered.get(timeout=poll_seconds)
            except queue.Empty:
                if cancel_event.is_set():
                    raise StreamCancelled(
                        "stream cancelled by the caller")
                continue
            if kind == "end":
                return
            if kind == "error":
                raise payload
            yield payload

    @staticmethod
    def _retry_delay(attempt, response=None):
        """Exponential backoff (1s, 2s, 4s), honoring Retry-After."""
        delay = float(2 ** attempt)
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    delay = max(delay, min(float(raw), 30.0))
                except ValueError:
                    pass
        return delay

    @staticmethod
    def _business_error(context, detail):
        """Build a DSError for a business-level rejection.

        The backend reports some failures, including rejected tokens,
        as business errors over HTTP 200 rather than with an HTTP
        status. Recognizable authorization failures are tagged with
        status 401 so they surface to clients as authentication
        errors instead of generic gateway errors.
        """
        lowered = str(detail).lower()
        markers = ("authorization failed", "invalid token",
                   "unauthorized", "token expired", "token invalid")
        status = 401 if any(marker in lowered for marker in markers) \
            else None
        return DSError(f"{context}: {detail}", status=status)

    def _request(self, description, send):
        """Execute one HTTP request, retrying transient failures.

        ``send`` performs a single attempt and returns the response.
        Rate limits (429), server errors (5xx), connection errors and
        timeouts are retried with exponential backoff. Any other
        response is returned to the caller for its own handling, so a
        permanently failing request surfaces the usual error.
        """
        attempt = 0
        while True:
            try:
                response = send()
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt >= self.MAX_RETRIES:
                    raise DSError(
                        f"{description}: connection failed after "
                        f"{attempt + 1} attempts") from error
                self._retries += 1
                time.sleep(self._retry_delay(attempt))
                attempt += 1
                continue
            if response.status_code not in self.RETRYABLE_STATUS \
                    or attempt >= self.MAX_RETRIES:
                return response
            if response.status_code == 429:
                self._pacer.note_rate_limited()
            self._retries += 1
            delay = self._retry_delay(attempt, response)
            try:
                response.close()
            except Exception:
                pass
            time.sleep(delay)
            attempt += 1

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
        response = self._request(
            "create_pow_challenge",
            lambda: requests.post(
                API_BASE + "/chat/create_pow_challenge", headers=headers,
                json={"target_path": target_path}, timeout=30))
        if response.status_code != 200:
            raise DSError("create_pow_challenge: "
                          f"HTTP {response.status_code}",
                          status=response.status_code)
        payload = response.json()
        if payload.get("code") != 0:
            raise self._business_error("create_pow_challenge",
                                       payload.get("msg"))
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
        response = self._request(
            path,
            lambda: requests.post(
                API_BASE + path, headers=headers or self.headers(),
                json=body, timeout=30))
        if response.status_code != 200:
            raise DSError(f"{path}: HTTP {response.status_code}",
                          status=response.status_code)
        payload = response.json()
        if payload.get("code") != 0 \
                or payload.get("data", {}).get("biz_code") != 0:
            raise self._business_error(
                path, f"{payload.get('msg')}/"
                      f"{payload.get('data', {}).get('biz_msg')}")
        return payload["data"]["biz_data"]

    # -- sessions --------------------------------------------------------

    def create_session(self):
        """Create a new chat session and return its identifier."""
        self._pacer.wait_for_session()
        headers = self._pow_headers("/api/v0/chat/completion")
        data = self._post("/chat_session/create", {}, headers=headers)
        return data["chat_session"]["id"]

    def delete_session(self, session_id):
        """Delete a chat session on the backend.

        The identifier field is ``chat_session_id``; the endpoint
        silently accepts and ignores a ``session_id`` field instead,
        which deletes nothing (verified live 2026-10-31: only the
        ``chat_session_id`` form removes the session from the list
        and empties its history).
        """
        self._post("/chat_session/delete",
                   {"chat_session_id": session_id})

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
        response = self._request(
            f"upload {filename}",
            lambda: requests.post(
                API_BASE + "/file/upload_file", headers=headers,
                files={"file": (filename, data, mime)}, timeout=120))
        if response.status_code != 200:
            raise DSError(f"upload {filename}: HTTP {response.status_code}",
                          status=response.status_code)
        payload = response.json()
        if payload.get("code") != 0 \
                or payload.get("data", {}).get("biz_code") != 0:
            raise self._business_error(f"upload {filename}",
                                       payload.get("msg"))
        file_id = payload["data"]["biz_data"]["id"]

        for _ in range(20):
            time.sleep(3)
            status_response = self._request(
                "fetch_files",
                lambda: requests.get(
                    API_BASE + "/file/fetch_files",
                    headers=self.headers(),
                    params={"file_ids": file_id}, timeout=30))
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
        response = self._request(
            "history_messages",
            lambda: requests.get(
                API_BASE + "/chat/history_messages",
                headers=self.headers(),
                params={"chat_session_id": session_id}, timeout=30))
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
                        model_type="default", on_token=None,
                        cancel_event=None):
        """Send one message and stream the reply.

        ``on_token`` is invoked as ``on_token(kind, text)`` for every
        streamed fragment, where kind is ``"content"`` or ``"thinking"``.
        Returns a :class:`ChatResult`.

        The call runs inside the shared pacer slot: completions are
        serialized process-wide and spaced out, so concurrent clients
        queue instead of bursting against the backend.

        When ``cancel_event`` (a threading.Event) becomes set, the
        upstream read is aborted, the connection is closed, and
        :class:`StreamCancelled` is raised, which releases the pacer
        slot immediately instead of letting the completion run to the
        end for a caller that has already gone away. The check also
        runs while the upstream is fully stalled: the stream read
        wakes at least every ``STREAM_CANCEL_POLL_SECONDS`` to test
        the event, instead of waiting for the next SSE line.
        """
        with self._pacer.completion_slot():
            return self._run_completion(
                session_id, parent_id, prompt,
                ref_file_ids=ref_file_ids, thinking=thinking,
                search=search, model_type=model_type, on_token=on_token,
                cancel_event=cancel_event)

    def _run_completion(self, session_id, parent_id, prompt,
                        ref_file_ids=None, thinking=False, search=False,
                        model_type="default", on_token=None,
                        cancel_event=None):
        """Execute one completion inside the pacer slot."""
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
        response = self._request(
            "completion",
            lambda: requests.post(API_BASE + "/chat/completion",
                                  headers=headers, json=body,
                                  timeout=300, stream=True))
        if response.status_code != 200:
            raise DSError(f"completion: HTTP {response.status_code}",
                          status=response.status_code)
        if "application/json" in response.headers.get("content-type", ""):
            raise DSError("completion rejected: "
                          f"{response.json().get('msg')}")

        def guarded_on_token(kind, text):
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled("stream cancelled by the caller")
            if on_token:
                on_token(kind, text)

        parser = StreamParser(on_token=guarded_on_token)
        result = ChatResult()
        debug = bool(os.environ.get("DS_DEBUG"))
        debug_lines = []

        # Without a cancel event the plain iterator is kept, so the
        # normal path is byte-for-byte unchanged. With one, the
        # polling reader guarantees the cancel check runs even when
        # the upstream sends nothing for a long time.
        if cancel_event is not None:
            lines = self._iter_lines_cancellable(response, cancel_event)
        else:
            lines = response.iter_lines(decode_unicode=True)

        try:
            for line in lines:
                if cancel_event is not None and cancel_event.is_set():
                    raise StreamCancelled(
                        "stream cancelled by the caller")
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
        finally:
            response.close()

        if debug and debug_lines:
            with open("/tmp/ds-debug.log", "a") as handle:
                handle.write("\n".join(debug_lines) + "\n---\n")

        result.content = parser.content
        result.thinking = parser.thinking
        return result
