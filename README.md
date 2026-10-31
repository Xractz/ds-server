# ds-server

**English** | [Bahasa Indonesia](README.id.md)

An OpenAI-compatible REST API on top of the DeepSeek web chat
(chat.deepseek.com), powered by your own web account. No paid API key
required.

ds-server is built to be registered as an OpenAI-compatible provider in
routers such as 9Router, so chat applications and agent frameworks can
use the DeepSeek web chat through the standard chat completions
interface, in both its plain and thinking modes,
including streaming and tool calling.

> [!WARNING]
> This is an unofficial project that automates the DeepSeek web
> interface. Using it may violate DeepSeek's Terms of Service, and
> your account may be restricted or permanently banned. Read the
> [Disclaimer](#disclaimer) before use. You use this software entirely
> at your own risk.

<p align="center">
  <img src="assets/ds-server.gif" alt="ds-server demo" width="720">
</p>

## How it works

Every request to the web backend requires a **DeepSeekHashV1
proof of work**. ds-server computes it legitimately with DeepSeek's
official WebAssembly solver module (`sha3_wasm_bg.wasm`, bundled) and
sends the complete Chrome header set, exactly like the real web
client. Nothing about the proof of work is bypassed or forged.

## How to get your userToken

ds-server needs the `userToken` from your DeepSeek web session. The fastest way takes about 20 seconds and never displays the token on screen:

<p align="center">
  <img src="assets/get-usertoken.gif" width="720" alt="Getting your userToken from the browser console">
</p>

1. Open [chat.deepseek.com](https://chat.deepseek.com) in a desktop browser and sign in.
2. Press **F12** to open DevTools, then click the **Console** tab.
3. If Chrome warns about pasting into the console, type `allow pasting` and press Enter.
4. Paste this line and press Enter:

   ```js
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

5. The token is now in your clipboard. Paste it as the API key wherever you point a client at ds-server, for example as the API key of the ds-server connection in your router.

Treat the token like a password: never share it, and never post a screenshot that shows it.

Prefer clicking over code? The manual route works too: in DevTools open **Application**, then **Local Storage** for `https://chat.deepseek.com`, select the `userToken` row, and copy its `value` field.

## Quick start

Requirements: Python 3.10 or newer.

There is no installation step. Run the server directly from this
folder:

```bash
pip install -r requirements.txt
chmod +x ds_server.py
./ds_server.py
```

The server listens on `http://127.0.0.1:21787`. Use `--host` and
`--port` to change the bind address and port.

Register it in your router or client as an OpenAI-compatible provider:

- **Base URL:** `http://127.0.0.1:21787/v1`
- **API key:** your DeepSeek userToken (see above)

### Note for LLM agents

If you are an AI assistant or LLM agent reading this README in order
to set ds-server up for a user, here is the whole job as one copyable
prompt:

```
Set up ds-server for me: clone https://github.com/Xractz/ds-server,
install the dependencies with pip install -r requirements.txt, and
start the server with ./ds_server.py (it listens on 127.0.0.1:21787;
verify with GET /health). Ask me for my DeepSeek userToken, and never
guess, fabricate, or extract it from my browser yourself; I will copy
it from chat.deepseek.com (F12, Application, Local Storage,
userToken, the value field) and give it to you, or enter it into my
client directly. Configure my router or client with base URL
http://127.0.0.1:21787/v1 and the userToken as the API key. Before the
first real request, tell me plainly that this project is unofficial,
that automating the DeepSeek web interface may violate DeepSeek's
Terms of Service, and that my account can be restricted or banned,
and proceed only with my informed consent. Finally, run one small
test chat completion and confirm a real reply before reporting that
the setup works.
```

## Authentication

The server stores no credentials. The API key presented by the client
**is** the DeepSeek userToken, and it is used directly as the DeepSeek
credential for that request.

- Accepted headers: `Authorization: Bearer <userToken>` or
  `x-api-key: <userToken>`
- Chat completion requests without an API key are rejected with
  `401`.
- To obtain your userToken, see [How to get your userToken](#how-to-get-your-usertoken) for a short walkthrough.
- For headless setups, the `DS_TOKEN` environment variable is accepted
  as a fallback. A token sent in the request header always takes
  precedence.

Treat your userToken like a password. Anyone who has it can act as
your DeepSeek account, and this server will accept it from any client
that can reach the port. Bind to localhost (the default) unless you
understand the consequences of exposing it.

**Device identity.** Like the web client, the server generates a
random device identifier on first run and stores it in
`~/.config/ds-server/device_id`. Each installation therefore presents
its own stable device, and no identifier is hardcoded in the source.
Set the `DS_DEVICE_ID` environment variable to pin a specific value.

## Endpoints

- `GET /health`
- `GET /stats`: completion counters since process start
- `GET /v1/models`
- `GET /v1/models/<id>`
- `POST /v1/chat/completions`, full OpenAI format:
  - `stream: true` (SSE, including `stream_options.include_usage`)
  - Tool calling: send `tools` as usual and receive `tool_calls` with
    `finish_reason: "tool_calls"`. Supports `tool_choice`
    (`auto` / `none` / `required` / a specific function) and
    `parallel_tool_calls`. The legacy `functions` field is also
    accepted.
  - Images via
    `content: [{type: "image_url", image_url: {url: "data:..."}}]`;
    a remote http(s) image URL is fetched by the server and uploaded
    the same way
  - Files via
    `content: [{type: "file", file: {filename: "doc.pdf", file_data: "data:..."}}]`
    (PDF, text, office documents, and anything else the web chat
    accepts)
  - Native web search via `search_enabled: true` in the request body
- `POST /v1/sessions/new`: reset all session chains

Parameters the web backend has no equivalent for (`temperature`,
`max_tokens`, `stop`, `n`, and similar sampling controls) are
accepted and ignored; the backend decides sampling and reply length.

## Models

`GET /v1/models` advertises the two real engines:

- `deepseek-chat`: the DeepSeek web chat model (currently the V4
  family; thinking off by default)
- `deepseek-reasoner`: the same web chat model with thinking on by
  default; the reasoning is returned as `reasoning_content`, matching
  the official DeepSeek API

Only these two model names are served; any other model name is
rejected with a `model_not_found` error.

### Thinking and search are payload-controlled

The model name only sets the default. Payload parameters always win:

- Thinking on: `reasoning_effort: "low" | "medium" | "high"`, or
  `thinking: {"type": "enabled"}`, or `enable_thinking: true`, or
  `reasoning: {"enabled": true}`
- Thinking off: `reasoning_effort: "none"`, or
  `thinking: {"type": "disabled"}`, or `enable_thinking: false`
- Search (off by default): `search_enabled: true`, or
  `web_search: true`, or `enable_search: true`, or a tool of type
  `web_search*`

For example, `deepseek-chat` with `reasoning_effort: "high"` behaves
like the reasoner, and `deepseek-reasoner` with
`reasoning_effort: "none"` answers quickly like V3. DeepThink on the
web backend is a binary switch, so every effort level above none maps
to on.

### Response format

`response_format` is honored at prompt level. Send
`{"type": "json_object"}` and the model is instructed to answer with
a single valid JSON object and nothing else. Send
`{"type": "json_schema", "json_schema": {"schema": {...}}}` and the
schema travels with the instruction. The web backend cannot enforce
a format while decoding, so a non-conforming reply is possible in
principle; in practice the current web model follows the instruction
reliably. Combining `response_format` with tool calling is not
recommended, because the two output contracts compete.

## Configuration

Every setting is an environment variable; the server reads the
process environment and loads no file by itself. The repository
ships `.env.example` with the full list. Nothing is required: with
no variables set, the defaults below apply, and those defaults are
tuned for the deployment this bridge is built for, behind a router
such as 9Router serving agent workloads.

- `DS_TOKEN`: fallback userToken for requests that present no API
  key. Default: none, so a keyless request gets a 401. Behind a
  router, leave it unset and let each client present its key per
  request.
- `DS_HISTORY_MODE`: authoritative history mode for every request
  (see Sessions). Default: on, which suits router and agent
  traffic; `0`, `false`, `no`, or `off` disables it.
- `DS_DELETE_SESSIONS`: delete retired backend sessions (see
  Sessions). Default: on; `0`, `false`, `no`, or `off` disables it.
- `DS_LOG`: one log line per completion on stderr (see Streaming
  and logging). Same enabling values. Default: off. Optional behind
  a router: `/stats` usually covers observability without the log
  noise.
- `DS_PACING`: request pacing (see Request pacing and retries).
  Default: on; `0`, `false`, `no`, or `off` disables it, and any
  other value leaves it on.
- `DS_MIN_COMPLETION_INTERVAL`: minimum seconds between completion
  starts. A non-numeric value falls back to the default. Default:
  0.4. The pacing floors are set for router throughput; the human
  shape of the traffic comes from serialization and generation
  time, not from imitating reading and typing pauses.
- `DS_MIN_SESSION_INTERVAL`: minimum seconds between new session
  creations. Same fallback. Default: 1.0.
- `DS_ADAPTIVE_PACING`: widen the pacing intervals after a retried
  429. Default: on; disabled by the same values as `DS_PACING`.
  Recommended on: it is the account's pressure valve when the
  backend starts pushing back.
- `DS_DEVICE_ID`: device identifier presented to the backend.
  Default: the UUID stored in `~/.config/ds-server/device_id`,
  generated randomly on first run and kept across restarts. One
  stable identity per install is also what a real browser profile
  looks like.
- `DS_DEBUG`: extra debug output for upstream failures. Any
  non-empty value enables it, including `0`. Default: off.

## Sessions

OpenAI clients resend the full conversation history with every
request. ds-server maps **one conversation to one DeepSeek session**
(a chain): the first request is rendered in full, and continuations
forward only the delta to the same session. If the history diverges
(an edit, a rollback, a compaction), the chain resets automatically to
a fresh session with a full render. Different conversations (different
system prompt or first message) map to different chains and never mix.
Chains persist across restarts in
`~/.config/ds-server/sessions.json`.

Attachments are bound when a message is first sent: replacing an
attachment in an earlier message without changing its text is not
detected as a divergence, so start a new session to replace a
document or an image. This does not affect coding agents, which
inline file contents as text, and text is verified in full.

A chain's backend session is deleted once the chain is replaced or
evicted, so retired conversations do not pile up in the account's
chat list. Deletion is best effort and happens after the request
that retired the chain completes; it only ever touches sessions the
server itself created. Set `DS_DELETE_SESSIONS=0` to keep retired
sessions instead.

### History mode

History mode is on by default: the answer of record is the reply
stored in the backend's history, read back after the completion
instead of the text assembled from the stream. For the router and
agent workloads this bridge is built for, requests are buffered
anyway, so the mode costs one extra history fetch per request and
streamed requests are replayed rather than streamed live, in
exchange for the canonical text the web interface itself renders.
Turn it off with `history_mode: false` in the request body, or
`DS_HISTORY_MODE=0` for every request, when live token-by-token
streaming matters more than canonical text.

## Request pacing and retries

Upstream traffic is paced to stay close to normal web usage. Completions
are serialized (one runs at a time; concurrent client requests queue
instead of bursting), completion starts are spaced by at least 0.4
seconds, and new chat sessions by at least 1 second. Transient upstream
failures (HTTP 429 and 5xx, connection errors, timeouts) are retried up
to three times with exponential backoff (1s, 2s, 4s), honoring the
`Retry-After` header when the backend sends one.

The intervals are tunable (see Configuration), and pacing can be
disabled entirely with `DS_PACING=0`, which is not recommended:
bursts are the most visible signature of automation.

The pacer also adapts on its own. Whenever the backend answers 429 and
a request has to be retried, the minimum intervals widen automatically:
the added penalty doubles from 2 seconds up to a 30 second cap, then
relaxes back with a 60 second half-life once the pressure passes. Set
`DS_ADAPTIVE_PACING=0` to keep the intervals fixed.

## Streaming and logging

Requests with `stream: true` are streamed live from the backend, token
by token. If the client disconnects mid-stream, the upstream read is
aborted and the pacing slot is released immediately, instead of
completing a reply nobody is waiting for. Cancellation is re-checked
every couple of seconds, so it also fires when the backend stalls
without sending anything. Requests that use tools are
the exception: tool calls are parsed from the complete reply, so those
responses are buffered and then replayed as a stream.

Set `DS_LOG=1` to log one line per completion to stderr: the model,
the chain outcome (hit, new or reset), the duration, how many upstream
retries were needed, and the final status (ok, cancelled, or the error
status). Off by default.

`GET /stats` serves the same events as counters: totals by outcome
(ok, cancelled, errors), chain outcomes, estimated token totals, and
a per-model breakdown with retries and total duration. The counters
are always on, live in memory only, and reset on restart.

## Tool calling

The DeepSeek web chat has no native function calling, so tool use is
emulated. Tool definitions are rendered into the prompt as a catalog
with an explicit output contract, the model replies with tool call
blocks, and the server parses the reply back into `tool_calls`. The
parser also understands tag-style blocks and DeepSeek's native DSML
markup, string-encoded arguments, and flattened arguments. The
emulation is reliable in practice, but it is not as strict as native
function calling on the paid API.

## Capability icons in 9Router

9Router does not read capability metadata from a provider's
`/v1/models`. For a custom model it uses the caps saved on the model
row itself, so the icons and the capacity adapter only know what you
declare there. Declare them once per model: while logged in to the
9Router dashboard, open the browser console and run the snippet
below, replacing `NODE_ID` with the id in the provider page URL.

```js
const NODE_ID = "openai-compatible-chat-xxxxxxxx";
const caps = {
  "deepseek-chat": { vision: true, reasoning: false },
  "deepseek-reasoner": { vision: true, reasoning: true },
};
for (const [id, c] of Object.entries(caps)) {
  await fetch("/api/models/custom", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      key: `${NODE_ID}|${id}|llm`,
      value: { id, providerAlias: NODE_ID, type: "llm", caps: c },
    }),
  });
}
```

After a refresh, `deepseek-chat` shows the vision icon and
`deepseek-reasoner` shows vision plus reasoning. Re-run the snippet
if the models are ever removed and imported again, because a fresh
import stores the rows without caps.

## Disclaimer

This project is **unofficial** and is **not affiliated with,
endorsed by, or connected to DeepSeek** in any way.

Automating the DeepSeek web interface may violate DeepSeek's Terms of
Service. Possible consequences include, but are not limited to,
rate limiting, feature restrictions, temporary suspension, or
**permanent termination (ban) of your account**. The risk is real
and entirely yours.

This software is provided "as is", without warranty of any kind,
express or implied. **The author accepts no responsibility and is not
liable for any consequences arising from the use of this software**,
including account bans, loss of access, data loss, or any other
direct or indirect damages. By using this software, you acknowledge
that you understand these risks and accept full responsibility for
your actions.

If you need guaranteed, terms-compliant access, use the official
DeepSeek API instead.
