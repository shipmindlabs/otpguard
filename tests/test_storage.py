from datetime import timedelta

import pytest

from otpguard import DEFAULT_PREFIX, MemoryStorage, RedisStorage, Storage


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    """The handful of commands the adapter is allowed to use.

    Every command is recorded in order, and ``fail_after`` makes the connection
    drop once that many commands have been accepted, so a half-finished
    sequence can be inspected.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.commands: list[str] = []
        self._fail_after = fail_after

    def get(self, name):
        self._record("get")
        value = self.values.get(name)
        return None if value is None else value.encode("utf-8")

    def set(self, name, value, px=None, nx=False):
        self._record("set")
        if nx and name in self.values:
            return None
        self.values[name] = value
        if px is None:
            self.expiries.pop(name, None)
        else:
            self.expiries[name] = px
        return True

    def incr(self, name):
        self._record("incr")
        counter = int(self.values.get(name, 0)) + 1
        self.values[name] = str(counter)
        return counter

    def delete(self, name):
        self._record("delete")
        self.values.pop(name, None)
        self.expiries.pop(name, None)

    def _record(self, command: str) -> None:
        self.commands.append(command)
        if self._fail_after is not None and len(self.commands) > self._fail_after:
            raise ConnectionError("the connection dropped")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return MemoryStorage(clock=clock)


@pytest.fixture
def client():
    return FakeRedis()


def test_adapters_satisfy_the_protocol(client):
    assert isinstance(MemoryStorage(), Storage)
    assert isinstance(RedisStorage(client), Storage)


def test_a_missing_key_reads_as_none(store):
    assert store.get("code:alice") is None


def test_values_round_trip_and_are_replaced(store):
    store.set("code:alice", "first")
    assert store.get("code:alice") == "first"
    store.set("code:alice", "second")
    assert store.get("code:alice") == "second"


def test_a_value_disappears_once_its_lifetime_runs_out(store, clock):
    store.set("code:alice", "digest", timedelta(minutes=5))
    clock.advance(299)
    assert store.get("code:alice") == "digest"
    clock.advance(1)
    assert store.get("code:alice") is None


def test_a_value_without_a_lifetime_stays(store, clock):
    store.set("code:alice", "digest")
    clock.advance(86_400)
    assert store.get("code:alice") == "digest"


def test_a_new_lifetime_replaces_the_old_one(store, clock):
    store.set("code:alice", "digest", timedelta(seconds=10))
    clock.advance(9)
    store.set("code:alice", "digest", timedelta(seconds=60))
    clock.advance(30)
    assert store.get("code:alice") == "digest"


def test_counters_start_at_one(store):
    assert store.incr("fails:alice") == 1
    assert store.incr("fails:alice") == 2
    assert store.get("fails:alice") == "2"


def test_later_increments_do_not_extend_the_window(store, clock):
    store.incr("fails:alice", timedelta(seconds=60))
    clock.advance(59)
    assert store.incr("fails:alice", timedelta(seconds=60)) == 2
    clock.advance(1)
    assert store.get("fails:alice") is None


def test_the_tally_starts_over_after_the_window(store, clock):
    store.incr("fails:alice", timedelta(seconds=60))
    store.incr("fails:alice", timedelta(seconds=60))
    clock.advance(60)
    assert store.incr("fails:alice", timedelta(seconds=60)) == 1


def test_incr_refuses_a_value_that_is_not_a_counter(store):
    store.set("code:alice", "digest")
    with pytest.raises(ValueError):
        store.incr("code:alice")


def test_delete_removes_a_key_and_forgives_a_missing_one(store):
    store.set("code:alice", "digest")
    store.delete("code:alice")
    assert store.get("code:alice") is None
    store.delete("code:alice")


def test_purge_drops_expired_entries(store, clock):
    store.set("code:alice", "digest", timedelta(seconds=10))
    store.set("code:bob", "digest")
    clock.advance(10)
    store.purge()
    assert store._entries.keys() == {"code:bob"}


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_a_non_positive_lifetime_is_rejected(store, ttl):
    with pytest.raises(ValueError):
        store.set("code:alice", "digest", ttl)
    with pytest.raises(ValueError):
        store.incr("fails:alice", ttl)


def test_redis_keys_are_prefixed(client):
    RedisStorage(client).set("code:alice", "digest")
    assert client.values == {DEFAULT_PREFIX + "code:alice": "digest"}
    assert RedisStorage(client).get("code:alice") == "digest"


def test_redis_prefix_is_configurable(client):
    RedisStorage(client, prefix="auth/").set("code:alice", "digest")
    assert "auth/code:alice" in client.values


def test_redis_reads_decode_bytes(client):
    client.values[DEFAULT_PREFIX + "code:alice"] = "digest"
    assert RedisStorage(client).get("code:alice") == "digest"
    assert RedisStorage(client).get("code:bob") is None


def test_redis_lifetimes_are_passed_in_milliseconds(client):
    store = RedisStorage(client)
    store.set("code:alice", "digest", timedelta(minutes=5))
    assert client.expiries[DEFAULT_PREFIX + "code:alice"] == 300_000
    store.set("code:bob", "digest")
    assert DEFAULT_PREFIX + "code:bob" not in client.expiries


def test_redis_attaches_the_lifetime_before_the_counter_exists(client):
    store = RedisStorage(client)
    assert store.incr("fails:alice", timedelta(seconds=60)) == 1
    assert client.commands == ["set", "incr"]
    assert client.expiries[DEFAULT_PREFIX + "fails:alice"] == 60_000


def test_redis_counts_without_a_lifetime_in_one_command(client):
    store = RedisStorage(client)
    assert store.incr("fails:alice") == 1
    assert client.commands == ["incr"]
    assert DEFAULT_PREFIX + "fails:alice" not in client.expiries


def test_a_counter_left_half_created_still_expires():
    client = FakeRedis(fail_after=1)
    store = RedisStorage(client)
    with pytest.raises(ConnectionError):
        store.incr("fails:alice", timedelta(seconds=60))
    key = DEFAULT_PREFIX + "fails:alice"
    assert client.commands == ["set", "incr"]
    assert client.values[key] == "0"
    assert client.expiries[key] == 60_000


def test_redis_expires_a_counter_only_when_it_is_created(client):
    store = RedisStorage(client)
    assert store.incr("fails:alice", timedelta(seconds=60)) == 1
    assert client.expiries[DEFAULT_PREFIX + "fails:alice"] == 60_000

    client.expiries[DEFAULT_PREFIX + "fails:alice"] = 1_000
    assert store.incr("fails:alice", timedelta(seconds=60)) == 2
    assert client.expiries[DEFAULT_PREFIX + "fails:alice"] == 1_000


def test_redis_delete(client):
    store = RedisStorage(client)
    store.set("code:alice", "digest", timedelta(seconds=60))
    store.delete("code:alice")
    assert client.values == {}
    assert client.expiries == {}


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_redis_rejects_a_non_positive_lifetime(client, ttl):
    store = RedisStorage(client)
    with pytest.raises(ValueError):
        store.set("code:alice", "digest", ttl)
    with pytest.raises(ValueError):
        store.incr("fails:alice", ttl)
    assert client.commands == []
