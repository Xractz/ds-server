"""Session chain store: one client conversation maps to one DeepSeek
session.

OpenAI-compatible clients resend the full conversation history with
every request. Re-rendering everything each time would waste the
stateful web session, so each conversation gets a chain record:

- ``ds_session``: the DeepSeek session identifier,
- ``parent``: the last message identifier returned by DeepSeek,
- ``count``: how many client messages have been forwarded (watermark),
- ``last_fp``: fingerprint of the last forwarded client message.

On the next request, if the history extends the watermark and the
fingerprint at the watermark still matches, the chain continues and
only the delta is sent. If the history diverged (edit, rollback, or a
new conversation that happens to share the key), the chain resets to a
fresh session with a full render.

Records persist to disk so chains survive a server restart. The store
keeps at most ``max_entries`` chains and evicts the least recently
used ones.
"""
import hashlib
import json
import time
from pathlib import Path

from .tools import MessageView

DEFAULT_PATH = Path.home() / ".config" / "ds-server" / "sessions.json"


class ChainStore:
    """Persistent store of conversation chains."""

    def __init__(self, path=DEFAULT_PATH, max_entries=50):
        self._retired = []
        self._path = Path(path)
        self._max_entries = max_entries

    # -- keying ------------------------------------------------------------

    @staticmethod
    def key_for(messages, model):
        """Derive the chain key from the model and the conversation
        opening (system prompt plus first user message)."""
        seed = f"{model}::{MessageView.fingerprint_hint(messages)}"
        return hashlib.sha256(seed.encode()).hexdigest()[:16]

    @staticmethod
    def fingerprint(message):
        """Fingerprint one client message; used to detect divergence."""
        try:
            serialized = json.dumps(message, sort_keys=True,
                                    ensure_ascii=False, default=str)
        except Exception:
            serialized = str(message)
        return hashlib.md5(serialized.encode()).hexdigest()[:10]

    @staticmethod
    def is_continuation(record, messages):
        """Whether ``messages`` exactly extends the recorded chain.

        The whole recorded prefix is verified, not only the boundary
        message: an edit anywhere in the recorded history (an edit, a
        rollback, a head-preserving compaction) must reset the chain,
        because the backend would otherwise keep answering from the
        pre-edit context. Records written before prefix fingerprints
        existed carry no ``prefix`` and never continue; they reset
        once and are rewritten in the new shape.
        """
        if not record or not record.get("count"):
            return False
        count = record["count"]
        if len(messages) <= count:
            return False
        if ChainStore.fingerprint(messages[count - 1]) \
                != record.get("last_fp", ""):
            return False
        prefix = record.get("prefix")
        if prefix is None or len(prefix) != count:
            return False
        return all(ChainStore.fingerprint(message) == stored
                   for message, stored in zip(messages, prefix))

    # -- persistence ---------------------------------------------------------

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if isinstance(data, dict) \
                        and isinstance(data.get("chains"), dict):
                    return data
            except Exception:
                pass
        return {"chains": {}}

    def _save(self, data):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data))
        self._path.chmod(0o600)

    # -- operations ------------------------------------------------------------

    def get(self, key):
        """Return the chain record for ``key``, or None."""
        return self._load()["chains"].get(key)

    def put(self, key, ds_session, model=""):
        """Create or replace a chain record bound to a DeepSeek session."""
        data = self._load()
        chains = data["chains"]
        previous = chains.get(key)
        if previous is not None \
                and previous.get("ds_session") not in (None, ds_session):
            self._retired.append(previous["ds_session"])
        chains[key] = {
            "ds_session": ds_session,
            "parent": None,
            "count": 0,
            "last_fp": "",
            "prefix": [],
            "model": model,
            "last_used": int(time.time()),
            "total_input": 0,
            "total_output": 0,
        }
        if len(chains) > self._max_entries:
            oldest = sorted(chains.items(),
                            key=lambda item: item[1].get("last_used", 0))
            for old_key, _record in oldest[:len(chains)
                                            - self._max_entries]:
                if _record.get("ds_session"):
                    self._retired.append(_record["ds_session"])
                del chains[old_key]
        self._save(data)
        return chains[key]

    def drain_retired(self):
        """Return the session ids retired since the last drain.

        A session retires when its chain record is replaced by a
        fresh session or evicted by the LRU trim. The caller decides
        what to do with them (the completion service deletes them on
        the backend when session auto-delete is enabled).
        """
        retired, self._retired = self._retired, []
        return retired

    def update(self, key, parent=None, count=None, last_fp=None,
               prefix=None, input_tokens=0, output_tokens=0):
        """Advance a chain record after a successful completion."""
        data = self._load()
        record = data["chains"].get(key)
        if record is None:
            return
        if parent is not None:
            record["parent"] = parent
        if count is not None:
            record["count"] = count
        if last_fp is not None:
            record["last_fp"] = last_fp
        if prefix is not None:
            record["prefix"] = list(prefix)
        record["last_used"] = int(time.time())
        record["total_input"] = record.get("total_input", 0) + input_tokens
        record["total_output"] = record.get("total_output", 0) \
            + output_tokens
        self._save(data)

    def drop(self, key):
        """Remove one chain record."""
        data = self._load()
        if key in data["chains"]:
            del data["chains"][key]
            self._save(data)

    def clear(self):
        """Remove all chain records."""
        self._save({"chains": {}})
