# Changelog

All notable changes to ds-server are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
