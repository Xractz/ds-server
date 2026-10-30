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

Models: ``/v1/models`` advertises the two real engines,
``deepseek-chat`` (V3) and ``deepseek-reasoner`` (R1). The legacy
aliases ``deepseek-v3`` and ``deepseek-r1`` remain accepted in chat
completions. Thinking and web search are controlled per request
through the payload (see :meth:`ChatRequest.resolve_thinking`), with
the model name acting only as the default.

Sessions: clients resend the full history with every request. The
server maps one conversation to one DeepSeek session (see the
``deepseek_web.sessions`` module) and forwards only the delta on
continuing chains.
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass

from flask import Flask, Response, jsonify, request

from deepseek_web import (ChainStore, DeepSeekClient, DSError,
                          MessageView, PromptBuilder, TokenStore,
                          ToolCatalog, ToolCallParser, estimate_tokens)

MODELS = ["deepseek-chat", "deepseek-reasoner"]
LEGACY_ALIASES = ("deepseek-v3", "deepseek-r1")
REASONER_MODELS = {"deepseek-reasoner", "deepseek-r1", "r1", "reasoner"}


def error_response(message, status=500, error_type="upstream_error",
                   code=None):
    """Build an OpenAI-style error body."""
    return jsonify({"error": {"message": message, "type": error_type,
                              "code": code}}), status


def upstream_error_response(exc):
    """Map a backend failure to a client-facing OpenAI-style error.

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
        return error_response(message, 429, "rate_limit_error",
                              "rate_limit_exceeded")
    if status in (401, 403):
        return error_response(
            "DeepSeek rejected the supplied userToken "
            f"(HTTP {status}). Check that the token is valid.",
            401, "authentication_error", "invalid_api_key")
    if status is not None and 400 <= status < 500:
        return error_response(message, status, "invalid_request_error")
    return error_response(message, 502, "upstream_error")


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

    @property
    def parse_tools(self):
        """Whether the reply should be parsed for tool calls."""
        return bool(self.tools) and self.tool_choice != "none"

    @staticmethod
    def is_reasoner_model(model):
        return model in REASONER_MODELS or "reasoner" in model \
            or "-r1" in model

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
        )


class CompletionService:
    """Executes chat completions against DeepSeek through the chain
    store: continuing chains forward only the delta, new or diverged
    conversations start a fresh session with a full render."""

    def __init__(self, chains):
        self._chains = chains

    def _start_fresh(self, client, key, chat_request, tools):
        session_id = client.create_session()
        self._chains.put(key, session_id, chat_request.model)
        prompt = PromptBuilder.full(chat_request.messages, tools,
                                    chat_request.tool_choice,
                                    chat_request.parallel_tool_calls)
        return session_id, None, prompt, chat_request.messages

    @staticmethod
    def _upload_images(client, messages):
        file_ids = []
        for message in messages:
            if message.get("role") == "user":
                for data, filename, _mime in MessageView.images(message):
                    file_ids.append(client.upload_file(data, filename))
        return file_ids

    def run(self, client, chat_request, key):
        """Run one completion. Returns (answer_text, thinking_text)."""
        chains = self._chains
        tools = chat_request.tools if chat_request.parse_tools else []
        record = chains.get(key)
        continuation = chains.is_continuation(record,
                                              chat_request.messages)

        if continuation:
            session_id = record["ds_session"]
            parent = record.get("parent")
            delta = chat_request.messages[record["count"]:]
            prompt = PromptBuilder.delta(delta, tools,
                                         chat_request.tool_choice,
                                         chat_request.parallel_tool_calls)
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

        file_ids = self._upload_images(client, sent_messages)
        try:
            result = client.chat_completion(
                session_id, parent, prompt, file_ids,
                thinking=chat_request.thinking, search=chat_request.search)
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
                thinking=chat_request.thinking, search=chat_request.search)

        answer = result.content
        if not answer:
            # The stored history is the authoritative record if stream
            # parsing produced no text.
            try:
                answer = client.fetch_history_text(session_id)
            except Exception:
                pass

        chains.update(
            key, parent=result.response_id,
            count=len(chat_request.messages),
            last_fp=chains.fingerprint(chat_request.messages[-1]),
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(answer))
        return answer, result.thinking


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
    service = CompletionService(chains)

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
        return jsonify({"ok": True})

    @app.get("/v1/models")
    def list_models():
        return jsonify({"object": "list", "data": [
            {"id": model, "object": "model", "created": 1700000000,
             "owned_by": "deepseek"} for model in MODELS]})

    @app.get("/v1/models/<model_id>")
    def get_model(model_id):
        if model_id in MODELS or model_id in LEGACY_ALIASES:
            return jsonify({"id": model_id, "object": "model",
                            "created": 1700000000, "owned_by": "deepseek"})
        return error_response(f"model '{model_id}' not found", 404,
                              "invalid_request_error", "model_not_found")

    @app.post("/v1/sessions/new")
    def new_session():
        """Reset all chains; the next requests start new sessions."""
        chains.clear()
        return jsonify({"ok": True,
                        "note": "all chains have been reset; the next "
                                "requests will create new DeepSeek sessions"})

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

        chat_request = ChatRequest.from_body(body)
        client = DeepSeekClient(token)
        key = chains.key_for(chat_request.messages, chat_request.model)

        with locks.lock_for(key):
            try:
                full_text, thinking_text = service.run(
                    client, chat_request, key)
            except DSError as exc:
                return upstream_error_response(exc)
            except Exception as exc:
                return error_response(f"internal: {exc}", 500,
                                      "internal_error")

        if chat_request.parse_tools:
            valid_names = {tool.name for tool in chat_request.tools}
            text, calls = ToolCallParser().parse(full_text, valid_names)
        else:
            text, calls = full_text, []
        tool_calls = ToolCallParser.to_openai(calls) if calls else []

        prompt_tokens = estimate_tokens(PromptBuilder.full(
            chat_request.messages, chat_request.tools))
        completion_tokens = estimate_tokens(full_text)
        usage = {"prompt_tokens": prompt_tokens,
                 "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
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

    print(f"ds-server running at http://{args.host}:{args.port}")
    print("OpenAI-compatible provider: base_url "
          f"http://{args.host}:{args.port}/v1")
    print("API key: the DeepSeek userToken (Authorization: Bearer).")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
