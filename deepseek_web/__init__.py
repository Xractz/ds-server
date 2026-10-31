"""deepseek_web: access to the DeepSeek web chat as a Python package.

The package wraps chat.deepseek.com (sessions, file upload, streaming
completions, proof of work) and adds prompt-based tool calling plus a
session chain store for OpenAI-compatible clients. The REST server in
``ds_server.py`` is a thin entry point on top of this package.
"""
__version__ = "1.2.1"

from .core import (ChatResult, DeepSeekClient, DeviceIdentity, DSError,
                   PowSolver, RequestPacer, StreamCancelled, StreamParser,
                   TokenStore)
from .sessions import ChainStore
from .tools import (MessageView, PromptBuilder, ToolCall, ToolCallParser,
                    ToolCatalog, ToolDef, estimate_tokens)

__all__ = [
    "ChatResult",
    "ChainStore",
    "DeepSeekClient",
    "DeviceIdentity",
    "DSError",
    "MessageView",
    "PowSolver",
    "PromptBuilder",
    "RequestPacer",
    "StreamCancelled",
    "StreamParser",
    "TokenStore",
    "ToolCall",
    "ToolCallParser",
    "ToolCatalog",
    "ToolDef",
    "estimate_tokens",
]
