import hashlib
import json
from typing import Any


def stable_cache_key(prefix: str, payload: dict[str, Any]) -> str:
    """
    Build compact deterministic cache keys from structured payloads.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"

