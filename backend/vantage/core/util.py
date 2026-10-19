from __future__ import annotations

import math
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import orjson


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def iso_to_ms(s: str | None) -> int | None:
    """Parse RFC-3339 (with optional nanoseconds / Z) to epoch ms."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # trim sub-microsecond digits that fromisoformat can't handle
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                frac, tz = rest[:i], rest[i:]
                break
        else:
            frac = rest
        s = f"{head}.{frac[:6]}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def clean_float(x: Any) -> float | None:
    """Finite float or None (for JSON)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _default(obj: Any) -> Any:
    if hasattr(obj, "to_wire"):
        return obj.to_wire()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:  # pragma: no cover
        pass
    raise TypeError(f"not serialisable: {type(obj)}")


def dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, default=_default, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)


def loads(b: bytes | str) -> Any:
    return orjson.loads(b)


def sanitize(obj: Any) -> Any:
    """Recursively replace NaN/inf with None so payloads are valid JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj
