# Changelog

All notable changes to ds-server are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- The `capabilities` and `architecture` metadata on `/v1/models`
  entries is gone. It was added in 1.1.1, but the router this bridge
  serves does not read capability metadata from a provider's model
  list, so the declaration had no consumer.

## [1.1.1] - 2026-10-31

### Added

- Adaptive pacing: whenever the backend answers 429 and a request has
  to be retried, the pacer widens its minimum intervals automatically
  (the penalty doubles from 2 seconds up to a 30 second cap) and then
  relaxes with a 60 second half-life. Set `DS_ADAPTIVE_PACING=0` to
  keep the intervals fixed.
- The server version is now reported by the `/health` endpoint and in
  the startup line, from a single `__version__` constant in the
  package.
- `/v1/models` now declares each model's capabilities (`vision`,
  `reasoning`, `tools`, `search`) and its input and output modalities,
  so routers and clients can display and route on them instead of
  guessing from the model name. The declaration follows each model's
  own mode: `deepseek-chat` is the plain mode and declares no
  reasoning, `deepseek-reasoner` is the thinking mode.
- Regression fixture corpus under `tests/fixtures` covering the
  historical parser failure shapes: SSE patch sequences for thinking
  routed through the -1 fragment selector, fragment lists appended
  and batched at the response level, transition chunks with no
  operation plus standalone `[reference:N]` markers, and
  mixed-dialect tool replies including mojibake-damaged DSML.

### Changed

- The model labels in the README and the module docs no longer call
  the web chat models DeepSeek V3 and R1. The web chat has run the
  DeepSeek V4 family since April 2026 (the vendor replaces the web
  checkpoint without notice, and the web API does not report which
  one served a request), so the models are now described as the web
  chat model in its plain and thinking modes.

### Removed

- The legacy model aliases `deepseek-v3` and `deepseek-r1` are gone.
  Only `deepseek-chat` and `deepseek-reasoner` are served: lookups of
  any other model name return a `model_not_found` error, and chat
  completions reject unknown model names the same way instead of
  silently treating them as `deepseek-chat`.

### Fixed

- A streamed request whose parsing produced no text now delivers the
  history fallback through the stream as one content piece, matching
  the buffered path; before, streaming clients silently received an
  empty reply in that case.
- Tool replies with fused dialects no longer leak markup into the
  visible text: a DSML invoke block closed by a `tool_call` tag (or
  by its calls wrapper) is now parsed as a call, and any orphaned
  DSML or `tool_call` tag debris left after extraction is stripped
  by a final sweep. Long fenced code blocks in tool-carrying
  replies stay capped at 10 lines plus a trimmed note, while plain
  replies are untouched; both behaviors are now covered by tests.
- The stream parser now handles fragment updates that arrive at
  the response level itself: lists appended with `APPEND` and bare
  fragment dicts inside `BATCH` updates are routed to the correct
  channel instead of being dropped. Standalone `[reference:N]`
  markers are now filtered from every channel, not only from
  fragment batch updates.
- Streaming cancellation now reacts even when the upstream stalls
  completely: the completion stream read re-checks the cancel event
  at least every 2 seconds, so a disconnected client no longer
  waits out a silent upstream. Normal streaming behavior and the
  pacing design are unchanged.

## [1.1.0] - 2026-10-30

### Added

- True streaming for plain streaming requests: tokens are relayed live
  from the backend instead of being buffered and replayed. If the
  client disconnects mid-stream, the upstream read is aborted and the
  pacing slot is released immediately, instead of completing a reply
  nobody is waiting for. Requests with tools still use the buffered
  path, because tool calls are parsed from the complete reply.
- Optional request logging with `DS_LOG=1`: one line per completion on
  stderr carrying the model, the chain outcome (hit, new or reset),
  the duration, the upstream retry count, and the final status (ok,
  cancelled, or the mapped error status).

## [1.0.2] - 2026-10-30

### Fixed

- Upstream failures are now mapped to correct client-facing HTTP status
  codes and OpenAI-style error types instead of a generic 502: rate
  limits stay 429 (`rate_limit_error`), rejected userTokens become 401
  (`authentication_error`), other upstream 4xx pass through, and
  upstream 5xx or connection failures become 502 (`upstream_error`).
  Routers and agents can now back off or report credential problems
  correctly.

### Changed

- Dependencies in `requirements.txt` are pinned to the minimum versions
  this release was tested with (Flask 3.1, requests 2.34, wasmtime 50),
  so a fresh install can no longer silently pull an untested major
  version.

## [1.0.1] - 2026-10-30

### Added

- Request pacing: completions are serialized process-wide and spaced
  (0.4s minimum between completion starts, 1s between session
  creations), so client bursts no longer become bursts against
  DeepSeek. Tunable via `DS_MIN_COMPLETION_INTERVAL` and
  `DS_MIN_SESSION_INTERVAL`; disable with `DS_PACING=0`.
- Retry with exponential backoff for transient upstream failures
  (HTTP 429 and 5xx, connection errors, timeouts): up to three retries
  at 1s, 2s and 4s, honoring the `Retry-After` header.
- A "How to get your userToken" walkthrough in both READMEs, with an
  animated guide showing the browser console snippet.

### Changed

- README images moved into an `assets/` folder.

## [1.0.0] - 2026-10-30

### Added

- Initial release. ds-server exposes the DeepSeek web chat
  (chat.deepseek.com) as an OpenAI-compatible REST API using your own
  web account: streaming chat completions on port 21787, two models
  (`deepseek-chat` and `deepseek-reasoner`), thinking and web search
  controlled per request through payload parameters, prompt-based tool
  calling with a multi-dialect parser, and session chains that forward
  only the delta of each continuing conversation.
- Authentication by userToken: the client's API key is the DeepSeek
  userToken itself; the server stores no credentials.
- A random device identifier is generated per installation; nothing
  personal is hardcoded.
