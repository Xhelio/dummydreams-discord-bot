"""Small, guarded optimization for repeated TikTok protobuf type-hint resolution."""

from functools import lru_cache


def _cache_classmethod(owner, method_name="_type_hints"):
    """Cache a classmethod's result once per concrete message class."""
    descriptor = owner.__dict__.get(method_name)
    if not isinstance(descriptor, classmethod):
        raise TypeError(f"{owner.__name__}.{method_name} must be a classmethod")

    original = descriptor.__func__
    existing_cache = getattr(original, "__proto_type_hints_cache__", None)
    if existing_cache is not None:
        return existing_cache

    cached = lru_cache(maxsize=4096)(original)
    cached.__proto_type_hints_cache__ = cached
    setattr(owner, method_name, classmethod(cached))
    return cached


def install_proto_type_hint_caches():
    """Cache type-hint resolution for both protobuf messages and TikTok events.

    TikTokLive's event base overrides betterproto2's method to resolve forward
    references in the generated schema module. Both methods therefore need
    their own cache; nested schema messages use betterproto2.Message directly.
    """
    from betterproto2 import Message
    from TikTokLive.events.base_event import BaseEvent

    return {
        "betterproto2": _cache_classmethod(Message),
        "tiktoklive": _cache_classmethod(BaseEvent),
    }
