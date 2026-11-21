from proto_performance import _cache_classmethod


def test_type_hint_cache_resolves_once_per_concrete_message_class():
    calls = []

    class ProtoMessage:
        @classmethod
        def _type_hints(cls):
            calls.append(cls)
            return {"message": cls.__name__}

    cache = _cache_classmethod(ProtoMessage)

    class NestedMessage(ProtoMessage):
        pass

    first = NestedMessage._type_hints()
    second = NestedMessage._type_hints()

    assert first is second
    assert first == {"message": "NestedMessage"}
    assert calls == [NestedMessage]
    assert cache.cache_info().misses == 1
    assert cache.cache_info().hits == 1


def test_type_hint_cache_is_separate_for_each_message_class():
    calls = []

    class ProtoMessage:
        @classmethod
        def _type_hints(cls):
            calls.append(cls)
            return {"message": cls.__name__}

    _cache_classmethod(ProtoMessage)

    class FirstMessage(ProtoMessage):
        pass

    class SecondMessage(ProtoMessage):
        pass

    assert FirstMessage._type_hints() == {"message": "FirstMessage"}
    assert SecondMessage._type_hints() == {"message": "SecondMessage"}
    assert FirstMessage._type_hints() == {"message": "FirstMessage"}
    assert calls == [FirstMessage, SecondMessage]


def test_type_hint_cache_installation_is_idempotent():
    class ProtoMessage:
        @classmethod
        def _type_hints(cls):
            return {"message": cls.__name__}

    first_cache = _cache_classmethod(ProtoMessage)
    second_cache = _cache_classmethod(ProtoMessage)

    assert first_cache is second_cache


def test_installation_caches_real_tiktok_and_betterproto_resolvers():
    from betterproto2 import Message
    from TikTokLive.events.base_event import BaseEvent

    from proto_performance import install_proto_type_hint_caches

    caches = install_proto_type_hint_caches()

    assert Message._type_hints.__func__ is caches["betterproto2"]
    assert BaseEvent._type_hints.__func__ is caches["tiktoklive"]
    assert Message._type_hints() == Message._type_hints()
