import numpy as np
import pytest

from unreliable.cache import MemoryBackend, RedisBackend, SemanticCache, entry_key, tprefix


def vec(seed):
    v = np.random.default_rng(seed).normal(size=16).astype(np.float32)
    return v / np.linalg.norm(v)


def backends():
    import fakeredis.aioredis
    return [MemoryBackend(), RedisBackend(fakeredis.aioredis.FakeRedis())]


@pytest.mark.parametrize("idx", [0, 1])
async def test_tenant_and_user_isolation(idx):
    c = SemanticCache(backends()[idx], 0.95)
    await c.store("tA", "u1", "What is X?", {"answer": "secret-A"}, vec(1))
    assert (await c.lookup("tA", "u1", "what is x ?"))[0]["answer"] == "secret-A"       # normalised exact hit
    assert (await c.lookup("tB", "u1", "What is X?"))[0] is None                          # other tenant, same user & query
    assert (await c.lookup("tA", "u2", "What is X?"))[0] is None                          # same tenant, other user (not shareable)


async def test_shared_partition_only_within_tenant():
    c = SemanticCache(MemoryBackend(), 0.95)
    await c.store("tA", "u1", "public q", {"answer": "pub"}, vec(2), shareable=True)
    assert (await c.lookup("tA", "u9", "public q"))[1] == "shared"
    assert (await c.lookup("tB", "u9", "public q"))[0] is None


async def test_semantic_hit_and_threshold():
    c = SemanticCache(MemoryBackend(), 0.95)
    v = vec(3)
    await c.store("t", "u", "original question", {"answer": "A"}, v)
    async def same(_): return v
    async def other(_): return vec(4)
    assert (await c.lookup("t", "u", "paraphrase", same))[1] == "semantic"
    assert (await c.lookup("t", "u", "paraphrase", other))[0] is None


async def test_poisoned_entry_is_purged_not_served():
    b = MemoryBackend(); c = SemanticCache(b, 0.95)
    import json, time
    k = entry_key("tB", "u", "q")                       # right key prefix for tB ...
    await b.set(k, json.dumps({"tenant_id": "tA", "response": {"answer": "LEAK"}, "vec": []}).encode(), 60)   # ... but wrong stored tenant
    assert (await c.lookup("tB", "u", "q"))[0] is None
    assert k not in b.kv


def test_key_format_is_hashed_and_prefixed():
    k = entry_key("tenant_a", "u", "q")
    assert k.startswith(tprefix("tenant_a")) and "tenant_a" not in k
    assert not entry_key("tenant_a1", "u", "q").startswith(tprefix("tenant_a"))      # prefix-collision safe
