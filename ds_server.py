#!/usr/bin/env python3
"""ds-server: an OpenAI-compatible REST API over the DeepSeek web chat.

The server is designed to be registered as an OpenAI-compatible
provider in a router such as 9Router:

    base_url: http://127.0.0.1:21787/v1
    api_key:  the DeepSeek userToken

Authentication: the API key presented by the client IS the account's
userToken. It is read from the ``Authorization: Bearer`` header (or
``x-api-key``) and used directly as the DeepSeek credential for that
request. The server stores no credential of its own; the ``DS_TOKEN``
environment variable is accepted as a fallback for headless setups.

Models: ``/v1/models`` advertises the two modes of the DeepSeek web
chat model (currently the V4 family): ``deepseek-chat`` with thinking
off by default and ``deepseek-reasoner`` with thinking on. Only these
two model names are served; any other name is rejected as an unknown
model. Thinking and web search are controlled per request
through the payload (see :meth:`ChatRequest.resolve_thinking`), with
the model name acting only as the default.

Sessions: clients resend the full history with every request. The
server maps one conversation to one DeepSeek session (see the
``deepseek_web.sessions`` module) and forwards only the delta on
continuing chains.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from flask import Flask, Response, jsonify, request

from deepseek_web import (ChainStore, DeepSeekClient, DSError,
                          MessageView, PromptBuilder, StreamCancelled,
                          StreamParser, TokenStore, ToolCatalog,
                          ToolCallParser, estimate_tokens)
from deepseek_web import __version__

MODELS = ["deepseek-chat", "deepseek-reasoner"]


def model_entry(model_id):
    """One /v1/models entry."""
    return {"id": model_id, "object": "model", "created": 1700000000,
            "owned_by": "deepseek"}
REASONER_MODELS = {"deepseek-reasoner"}


def error_response(message, status=500, error_type="upstream_error",
                   code=None):
    """Build an OpenAI-style error body."""
    return jsonify({"error": {"message": message, "type": error_type,
                              "code": code}}), status


def map_upstream_error(exc):
    """Map a backend failure to (status, message, error_type, code).

    The upstream HTTP status carried by the DSError drives the mapping,
    so routers and agents above this server can react correctly instead
    of treating every failure as a generic gateway error:

    - 429 stays 429 (``rate_limit_error``): clients back off.
    - 401/403 become 401 (``authentication_error``): the supplied
      userToken was rejected by DeepSeek.
    - Other upstream 4xx pass through as ``invalid_request_error``.
    - Everything else (upstream 5xx, connection failures, business
      errors reported over HTTP 200) becomes 502 ``upstream_error``.
    """
    status = getattr(exc, "status", None)
    message = str(exc)
    if status == 429:
        return 429, message, "rate_limit_error", "rate_limit_exceeded"
    if status in (401, 403):
        return (401,
                "DeepSeek rejected the supplied userToken "
                f"(HTTP {status}). Check that the token is valid.",
                "authentication_error", "invalid_api_key")
    if status is not None and 400 <= status < 500:
        return status, message, "invalid_request_error", None
    return 502, message, "upstream_error", None


def upstream_error_response(exc):
    """Build the client-facing OpenAI-style error for a DSError."""
    status, message, error_type, code = map_upstream_error(exc)
    return error_response(message, status, error_type, code)


class RequestLog:
    """Optional one-line-per-completion logging, enabled with DS_LOG=1.

    Lines go to stderr so they never mix with API responses. Each line
    carries the model, the chain outcome (hit, new or reset), the wall
    duration, how many upstream retries were performed, and the final
    status of the request.
    """

    @staticmethod
    def enabled():
        return os.environ.get("DS_LOG", "").strip().lower() \
            in ("1", "true", "yes", "on")

    @classmethod
    def completion(cls, model, chain, duration, retries, status):
        if not cls.enabled():
            return
        print(f"[ds-server] completion model={model} chain={chain} "
              f"duration={duration:.2f}s retries={retries} "
              f"status={status}", file=sys.stderr, flush=True)


class ServerStats:
    """In-process completion counters, served by ``GET /stats``.

    Always on, unlike the DS_LOG stderr lines: every completed,
    cancelled, or failed completion is recorded with its model, chain
    outcome, duration, retry count, and estimated token usage when
    the caller computed it. Counters live in memory only and reset
    when the process restarts; durations are stored as totals per
    model so callers can derive averages.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self._totals = {"requests": 0, "ok": 0, "cancelled": 0,
                        "errors": 0}
        self._chains = {"hit": 0, "new": 0, "reset": 0}
        self._tokens = {"prompt": 0, "completion": 0}
        self._models = {}

    def record(self, model, chain, duration, retries, status,
               usage=None):
        with self._lock:
            self._totals["requests"] += 1
            bucket = self._models.setdefault(model, {
                "requests": 0, "ok": 0, "cancelled": 0, "errors": 0,
                "retries": 0, "total_duration": 0.0,
                "prompt_tokens": 0, "completion_tokens": 0})
            bucket["requests"] += 1
            if status == "ok":
                self._totals["ok"] += 1
                bucket["ok"] += 1
            elif status == "cancelled":
                self._totals["cancelled"] += 1
                bucket["cancelled"] += 1
            else:
                self._totals["errors"] += 1
                bucket["errors"] += 1
            if chain in self._chains:
                self._chains[chain] += 1
            bucket["retries"] += retries
            bucket["total_duration"] += duration
            if usage:
                prompt = usage.get("prompt_tokens", 0)
                completion = usage.get("completion_tokens", 0)
                bucket["prompt_tokens"] += prompt
                bucket["completion_tokens"] += completion
                self._tokens["prompt"] += prompt
                self._tokens["completion"] += completion

    def snapshot(self):
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self._started, 1),
                "totals": dict(self._totals),
                "chains": dict(self._chains),
                "tokens": dict(self._tokens),
                "by_model": {model: dict(entry)
                             for model, entry in self._models.items()},
            }


class ChainLocks:
    """One lock per session chain.

    Requests belonging to the same DeepSeek session must be processed
    sequentially, while different chains may proceed in parallel.
    """

    def __init__(self):
        self._locks = {}
        self._guard = threading.Lock()

    def lock_for(self, key):
        with self._guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]


@dataclass
class ChatRequest:
    """A chat completion request, normalized from the request body."""

    model: str
    messages: list
    stream: bool
    include_usage: bool
    thinking: bool
    search: bool
    tools: list
    tool_choice: object
    parallel_tool_calls: bool
    response_format: object = None
    history_mode: bool = False

    @property
    def parse_tools(self):
        """Whether the reply should be parsed for tool calls."""
        return bool(self.tools) and self.tool_choice != "none"

    @staticmethod
    def is_reasoner_model(model):
        return model in REASONER_MODELS

    @staticmethod
    def resolve_thinking(body, model):
        """Resolve the thinking flag from the payload.

        The model name only supplies the default. Recognized payload
        controls, in precedence order:

        - ``enable_thinking``: boolean
        - ``thinking``: boolean or ``{"type": "enabled"/"disabled"}``
        - ``reasoning``: ``{"enabled": boolean}``
        - ``reasoning_effort``: ``"none"``/``"minimal"`` disables,
          any other value enables

        DeepThink on the web backend is a binary switch, so effort
        levels above none all map to enabled.
        """
        if "enable_thinking" in body:
            return bool(body["enable_thinking"])
        thinking = body.get("thinking")
        if isinstance(thinking, bool):
            return thinking
        if isinstance(thinking, dict) \
                and thinking.get("type") in ("enabled", "disabled"):
            return thinking["type"] == "enabled"
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict) and "enabled" in reasoning:
            return bool(reasoning["enabled"])
        effort = body.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip():
            return effort.strip().lower() not in ("none", "minimal", "off")
        return ChatRequest.is_reasoner_model(model)

    @staticmethod
    def _env_flag(name):
        return os.environ.get(name, "").strip().lower() \
            in ("1", "true", "yes", "on")

    @classmethod
    def resolve_history_mode(cls, body):
        """Whether the stored history is the authoritative answer.

        Per-request ``history_mode`` wins; the ``DS_HISTORY_MODE``
        environment variable supplies the default.
        """
        if isinstance(body.get("history_mode"), bool):
            return body["history_mode"]
        return cls._env_flag("DS_HISTORY_MODE")

    @staticmethod
    def resolve_response_format(body):
        """Keep only the response_format shapes this bridge honors."""
        response_format = body.get("response_format")
        if isinstance(response_format, dict) \
                and response_format.get("type") \
                in ("json_object", "json_schema"):
            return response_format
        return None

    @classmethod
    def from_body(cls, body):
        model = body.get("model") or "deepseek-chat"
        tool_choice = body.get("tool_choice", body.get("function_call"))
        if isinstance(tool_choice, dict) \
                and "function" not in tool_choice \
                and tool_choice.get("name"):
            tool_choice = {"type": "function",
                           "function": {"name": tool_choice["name"]}}
        return cls(
            model=model,
            messages=body.get("messages") or [],
            stream=bool(body.get("stream")),
            include_usage=bool((body.get("stream_options") or {})
                               .get("include_usage")),
            thinking=cls.resolve_thinking(body, model),
            search=ToolCatalog.body_requests_search(body),
            tools=ToolCatalog.from_body(body),
            tool_choice=tool_choice,
            parallel_tool_calls=body.get("parallel_tool_calls", True),
            response_format=cls.resolve_response_format(body),
            history_mode=cls.resolve_history_mode(body),
        )


class CompletionService:
    """Executes chat completions against DeepSeek through the chain
    store: continuing chains forward only the delta, new or diverged
    conversations start a fresh session with a full render."""

    def __init__(self, chains, delete_sessions=False):
        self._chains = chains
        self._delete_sessions = delete_sessions

    def _start_fresh(self, client, key, chat_request, tools):
        session_id = client.create_session()
        self._chains.put(key, session_id, chat_request.model)
        prompt = PromptBuilder.full(chat_request.messages, tools,
                                    chat_request.tool_choice,
                                    chat_request.parallel_tool_calls,
                                    chat_request.response_format)
        return session_id, None, prompt, chat_request.messages

    @staticmethod
    def _upload_images(client, messages):
        file_ids = []
        for message in messages:
            if message.get("role") == "user":
                for data, filename, _mime in MessageView.images(message):
                    file_ids.append(client.upload_file(data, filename))
        return file_ids

    def run(self, client, chat_request, key, on_token=None,
            cancel_event=None):
        """Run one completion.

        Returns (answer_text, thinking_text, chain_status), where
        chain_status is "hit" for a continued chain, "new" for a fresh
        one, and "reset" when a chain had to be restarted. When
        ``on_token`` is given it is forwarded to the client for true
        streaming; ``cancel_event`` aborts the upstream read when set.
        A StreamCancelled raised through the token callback is never
        retried: the caller is gone.
        """
        chains = self._chains
        tools = chat_request.tools if chat_request.parse_tools else []
        record = chains.get(key)
        continuation = chains.is_continuation(record,
                                              chat_request.messages)
        chain_status = "hit" if continuation else "new"

        if continuation:
            session_id = record["ds_session"]
            parent = record.get("parent")
            delta = chat_request.messages[record["count"]:]
            prompt = PromptBuilder.delta(delta, tools,
                                         chat_request.tool_choice,
                                         chat_request.parallel_tool_calls,
                                         chat_request.response_format)
            sent_messages = delta
        else:
            session_id, parent, prompt, sent_messages = \
                self._start_fresh(client, key, chat_request, tools)

        if not prompt.strip():
            # An empty delta means the client resent the same history;
            # fall back to a full render in a new session.
            session_id, parent, prompt, sent_messages = \
                self._start_fresh(client, key, chat_request, tools)
            continuation = False
            chain_status = "reset"

        file_ids = self._upload_images(client, sent_messages)
        try:
            result = client.chat_completion(
                session_id, parent, prompt, file_ids,
                thinking=chat_request.thinking, search=chat_request.search,
                on_token=on_token, cancel_event=cancel_event)
        except DSError:
            if not continuation:
                raise
            # The stored session has likely expired: reset the chain,
            # render in full, and retry once.
            session_id, parent, prompt, sent_messages = \
                self._start_fresh(client, key, chat_request, tools)
            file_ids = self._upload_images(client, chat_request.messages)
            result = client.chat_completion(
                session_id, parent, prompt, file_ids,
                thinking=chat_request.thinking, search=chat_request.search,
                on_token=on_token, cancel_event=cancel_event)
            chain_status = "reset"

        answer = result.content
        if not answer:
            # The stored history is the authoritative record if stream
            # parsing produced no text.
            try:
                answer = client.fetch_history_text(session_id)
            except Exception:
                pass
            if answer and on_token is not None:
                # Nothing was streamed, so a streaming client would
                # otherwise see an empty reply: deliver the fallback
                # text through the token callback as one content piece.
                on_token(StreamParser.CONTENT, answer)

        if chat_request.history_mode:
            # Authoritative history mode: the stored history, not the
            # assembled stream, is the answer of record. This costs
            # one extra history fetch per request and is opt-in for
            # workloads where stream assembly fidelity matters more
            # than latency. Streaming requests are buffered by the
            # caller in this mode, so the token callback never sees
            # text the history would contradict.
            try:
                history = client.fetch_history_text(session_id)
            except Exception:
                history = ""
            if history:
                answer = history

        chains.update(
            key, parent=result.response_id,
            count=len(chat_request.messages),
            last_fp=chains.fingerprint(chat_request.messages[-1]),
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(answer))

        if self._delete_sessions:
            # Best effort: a failed delete leaves the session listed
            # on the account, nothing more. The current request's
            # client is used, so a retired session belonging to a
            # different account simply fails and is skipped.
            for retired_id in chains.drain_retired():
                try:
                    client.delete_session(retired_id)
                except Exception:
                    pass
        return answer, result.thinking, chain_status


class ResponseFactory:
    """Builds OpenAI-style completion payloads and stream chunks."""

    CHUNK_SIZE = 60

    @classmethod
    def _pieces(cls, text):
        return [text[start:start + cls.CHUNK_SIZE]
                for start in range(0, len(text), cls.CHUNK_SIZE)]

    @staticmethod
    def _chunk(completion_id, model, delta, finish=None, usage=None):
        chunk = {"id": completion_id, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta,
                              "finish_reason": finish}]}
        if usage is not None:
            chunk["choices"] = []
            chunk["usage"] = usage
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    @classmethod
    def stream(cls, completion_id, model, text, thinking, tool_calls,
               finish, usage, include_usage):
        """Yield the SSE chunks for one completion. The backend reply
        is already complete, so the buffered text is replayed in
        chunks."""
        yield cls._chunk(completion_id, model,
                         {"role": "assistant", "content": ""})
        if thinking:
            for piece in cls._pieces(thinking):
                yield cls._chunk(completion_id, model,
                                 {"reasoning_content": piece})
        if text:
            for piece in cls._pieces(text):
                yield cls._chunk(completion_id, model, {"content": piece})
        for index, call in enumerate(tool_calls):
            function = call["function"]
            yield cls._chunk(completion_id, model, {"tool_calls": [{
                "index": index, "id": call["id"], "type": "function",
                "function": {"name": function["name"], "arguments": ""}}]})
            for piece in cls._pieces(function["arguments"]):
                yield cls._chunk(completion_id, model, {"tool_calls": [{
                    "index": index, "function": {"arguments": piece}}]})
        yield cls._chunk(completion_id, model, {}, finish)
        if include_usage:
            yield cls._chunk(completion_id, model, {}, None, usage)
        yield "data: [DONE]\n\n"

    @staticmethod
    def completion(completion_id, model, text, thinking, tool_calls,
                   finish, usage):
        """Build the non-streaming completion response body."""
        message = {"role": "assistant",
                   "content": text if (text or not tool_calls) else None}
        if thinking:
            message["reasoning_content"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {"id": completion_id, "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish}],
                "usage": usage}


def create_app():
    """Create the Flask application with all routes wired."""
    app = Flask(__name__)
    chains = ChainStore()
    locks = ChainLocks()
    service = CompletionService(
        chains,
        delete_sessions=ChatRequest._env_flag("DS_DELETE_SESSIONS"))
    stats = ServerStats()

    def record_completion(model, chain, duration, retries, status,
                          usage=None):
        """Feed both the /stats counters and the DS_LOG line."""
        stats.record(model, chain, duration, retries, status, usage)
        RequestLog.completion(model, chain, duration, retries, status)

    def authenticate():
        """Resolve the userToken presented as the request's API key."""
        authorization = request.headers.get("Authorization", "")
        token = None
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            token = request.headers.get("x-api-key", "").strip() or None
        return TokenStore.resolve(token)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "version": __version__})

    @app.get("/stats")
    def stats_endpoint():
        """Completion counters since process start (see ServerStats)."""
        return jsonify({"ok": True, "version": __version__,
                        **stats.snapshot()})

    @app.get("/v1/models")
    def list_models():
        return jsonify({"object": "list",
                        "data": [model_entry(model) for model in MODELS]})

    @app.get("/v1/models/<model_id>")
    def get_model(model_id):
        if model_id in MODELS:
            return jsonify(model_entry(model_id))
        return error_response(f"model '{model_id}' not found", 404,
                              "invalid_request_error", "model_not_found")

    @app.post("/v1/sessions/new")
    def new_session():
        """Reset all chains; the next requests start new sessions."""
        chains.clear()
        return jsonify({"ok": True,
                        "note": "all chains have been reset; the next "
                                "requests will create new DeepSeek sessions"})

    def _stream_response(client, chat_request, key, started):
        """Stream one completion live from the backend.

        The completion runs on a worker thread and its tokens are
        relayed through a queue as they arrive. If the client
        disconnects, the response generator is closed by the server,
        which sets the cancel event: the upstream read aborts and the
        pacing slot is released instead of completing a reply nobody
        is waiting for. Errors that occur before or during the stream
        are reported as an error chunk inside the stream, since the
        response has already begun.
        """
        tokens = queue.Queue()
        cancel = threading.Event()
        outcome = {}

        def push(kind, text):
            tokens.put((kind, text))

        def produce():
            try:
                with locks.lock_for(key):
                    outcome["result"] = service.run(
                        client, chat_request, key, on_token=push,
                        cancel_event=cancel)
            except StreamCancelled:
                outcome["cancelled"] = True
            except Exception as exc:  # surfaced inside the stream
                outcome["error"] = exc
            finally:
                tokens.put(None)

        worker = threading.Thread(target=produce, daemon=True)
        worker.start()

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]
        model = chat_request.model

        def generate():
            status = "ok"
            try:
                yield ResponseFactory._chunk(
                    completion_id, model,
                    {"role": "assistant", "content": ""})
                while True:
                    item = tokens.get()
                    if item is None:
                        break
                    kind, text = item
                    delta = {"reasoning_content": text} \
                        if kind == StreamParser.THINKING \
                        else {"content": text}
                    yield ResponseFactory._chunk(completion_id, model,
                                                 delta)
                if "error" in outcome:
                    exc = outcome["error"]
                    if isinstance(exc, DSError):
                        _, message, error_type, code = \
                            map_upstream_error(exc)
                    else:
                        message, error_type, code = \
                            f"internal: {exc}", "internal_error", None
                    status = "error"
                    yield "data: " + json.dumps({"error": {
                        "message": message, "type": error_type,
                        "code": code}}) + "\n\n"
                elif outcome.get("cancelled"):
                    status = "cancelled"
                else:
                    answer, _thinking, chain_status = outcome["result"]
                    outcome["chain"] = chain_status
                    yield ResponseFactory._chunk(
                        completion_id, model, {}, "stop")
                    prompt_tokens = estimate_tokens(
                        PromptBuilder.full(chat_request.messages,
                                           chat_request.tools))
                    completion_tokens = estimate_tokens(answer)
                    outcome["usage"] = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens}
                    if chat_request.include_usage:
                        yield ResponseFactory._chunk(
                            completion_id, model, {}, None,
                            outcome["usage"])
                yield "data: [DONE]\n\n"
            finally:
                # On a normal completion the worker has already
                # finished; if the consumer went away early, setting
                # the event is what aborts the upstream read.
                cancel.set()
                worker.join(timeout=10)
                if outcome.get("cancelled"):
                    status = "cancelled"
                record_completion(model, outcome.get("chain", "-"),
                                  time.monotonic() - started,
                                  client.retries, status,
                                  outcome.get("usage"))

        return Response(generate(), mimetype="text/event-stream")

    @app.post("/v1/chat/completions")
    def chat_completions():
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return error_response("request body must be a JSON object", 400,
                                  "invalid_request_error")
        if not body.get("messages"):
            return error_response("messages must not be empty", 400,
                                  "invalid_request_error")

        token = authenticate()
        if not token:
            return error_response(
                "Missing API key. Present the DeepSeek userToken as the "
                "API key (Authorization: Bearer <userToken>).", 401,
                "authentication_error", "invalid_api_key")

        model = body.get("model") or "deepseek-chat"
        if model not in MODELS:
            return error_response(f"model '{model}' not found", 404,
                                  "invalid_request_error", "model_not_found")

        chat_request = ChatRequest.from_body(body)
        client = DeepSeekClient(token)
        key = chains.key_for(chat_request.messages, chat_request.model)
        started = time.monotonic()

        # True streaming requires that no tool parsing is needed: tool
        # calls are parsed from the complete reply, so requests with
        # tools keep the buffered path below. History mode also stays
        # buffered: the authoritative answer is only known once the
        # stored history can be read back.
        if chat_request.stream and not chat_request.parse_tools \
                and not chat_request.history_mode:
            return _stream_response(client, chat_request, key, started)

        with locks.lock_for(key):
            try:
                full_text, thinking_text, chain_status = service.run(
                    client, chat_request, key)
            except DSError as exc:
                response, status = upstream_error_response(exc)
                record_completion(
                    chat_request.model, "-", time.monotonic() - started,
                    client.retries, f"error-{status}")
                return response, status
            except Exception as exc:
                record_completion(
                    chat_request.model, "-", time.monotonic() - started,
                    client.retries, "error-500")
                return error_response(f"internal: {exc}", 500,
                                      "internal_error")
        prompt_tokens = estimate_tokens(PromptBuilder.full(
            chat_request.messages, chat_request.tools))
        completion_tokens = estimate_tokens(full_text)
        usage = {"prompt_tokens": prompt_tokens,
                 "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        record_completion(chat_request.model, chain_status,
                          time.monotonic() - started, client.retries,
                          "ok", usage)

        if chat_request.parse_tools:
            valid_names = {tool.name for tool in chat_request.tools}
            text, calls = ToolCallParser().parse(full_text, valid_names)
        else:
            text, calls = full_text, []
        tool_calls = ToolCallParser.to_openai(calls) if calls else []

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]
        finish = "tool_calls" if tool_calls else "stop"

        if chat_request.stream:
            return Response(
                ResponseFactory.stream(
                    completion_id, chat_request.model, text, thinking_text,
                    tool_calls, finish, usage, chat_request.include_usage),
                mimetype="text/event-stream")
        return jsonify(ResponseFactory.completion(
            completion_id, chat_request.model, text, thinking_text,
            tool_calls, finish, usage))

    return app


app = create_app()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ds-server")
    parser.add_argument("--port", type=int, default=21787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"ds-server {__version__} running at "
          f"http://{args.host}:{args.port}")
    print("OpenAI-compatible provider: base_url "
          f"http://{args.host}:{args.port}/v1")
    print("API key: the DeepSeek userToken (Authorization: Bearer).")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
