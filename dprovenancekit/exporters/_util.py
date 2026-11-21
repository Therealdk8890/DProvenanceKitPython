"""Small helpers shared by the vendor adapters (standard library only)."""

from __future__ import annotations

import inspect
import uuid
from typing import Any, Callable, Dict, Optional


def missing_extra(package: str, extra: str) -> ImportError:
    return ImportError(
        f"{package} is not installed. Install it with: pip install \"dprovenancekit[{extra}]\" "
        "(or pass an already-configured client explicitly)."
    )


def call_supported(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` dropping ``None``-valued and unsupported keyword arguments.

    Vendor SDKs add optional keyword arguments over time (for example ``metadata`` on
    Langfuse scores, ``assessment`` on Datadog evaluations). Dropping keywords the
    installed version does not accept keeps one adapter working across SDK versions,
    without ever inventing parameters: only keywords the signature declares are sent.
    If the signature cannot be inspected, or the callable takes ``**kwargs``, all
    non-``None`` keywords are passed through.
    """
    supplied = {k: v for k, v in kwargs.items() if v is not None}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **supplied)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **supplied)
    return fn(*args, **{k: v for k, v in supplied.items() if k in params})


def uuid_or_none(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def hex32_or_none(value: Optional[str]) -> Optional[str]:
    parsed = uuid_or_none(value)
    return parsed.hex if parsed is not None else None


def json_metadata(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """A plain-JSON copy of verdict attributes for vendor ``metadata`` fields."""
    return {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in attrs.items()}
