from datetime import timedelta

import pytest

from otpguard import DEFAULT_SCOPE, MemoryStorage, Quota, QuotaExceeded

SUBJECT = "alice"
WINDOW = timedelta(days=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return MemoryStorage(clock=clock)


@pytest.fixture
def quota():
    return Quota(limit=3, window=WINDOW)


def test_the_counter_lives_under_the_scope(quota):
    assert quota.scope == DEFAULT_SCOPE
    assert quota.key(SUBJECT) == f"{DEFAULT_SCOPE}:{SUBJECT}"


def test_an_unseen_subject_has_the_whole_allowance(quota, store):
    assert quota.used(store, SUBJECT) == 0
    assert quota.remaining(store, SUBJECT) == 3
    assert quota.allows(store, SUBJECT)
    quota.check(store, SUBJECT)


def test_claims_are_counted_until_the_allowance_runs_out(quota, store):
    assert [quota.claim(store, SUBJECT) for _ in range(3)] == [1, 2, 3]
    assert quota.used(store, SUBJECT) == 3
    assert quota.remaining(store, SUBJECT) == 0
    assert not quota.allows(store, SUBJECT)
    with pytest.raises(QuotaExceeded):
        quota.claim(store, SUBJECT)
    with pytest.raises(QuotaExceeded):
        quota.check(store, SUBJECT)


def test_the_refusal_reports_the_cap_it_hit(quota, store):
    for _ in range(3):
        quota.claim(store, SUBJECT)
    with pytest.raises(QuotaExceeded) as excinfo:
        quota.claim(store, SUBJECT)
    assert excinfo.value.scope == DEFAULT_SCOPE
    assert excinfo.value.subject == SUBJECT
    assert excinfo.value.limit == 3
    assert excinfo.value.window == WINDOW
    assert excinfo.value.retry_after == WINDOW
    assert excinfo.value.retry_after_seconds == pytest.approx(86_400.0)


def test_check_spends_nothing(quota, store):
    quota.check(store, SUBJECT)
    quota.check(store, SUBJECT)
    assert quota.used(store, SUBJECT) == 0
    assert quota.claim(store, SUBJECT) == 1


def test_a_refused_claim_does_not_push_the_reset_out(quota, store, clock):
    for _ in range(3):
        quota.claim(store, SUBJECT)

    clock.advance(86_399)
    with pytest.raises(QuotaExceeded):
        quota.claim(store, SUBJECT)

    clock.advance(1)
    assert quota.remaining(store, SUBJECT) == 3
    assert quota.claim(store, SUBJECT) == 1


def test_a_single_claim_quota_refuses_the_second(store):
    quota = Quota(limit=1, window=timedelta(hours=1))
    assert quota.claim(store, SUBJECT) == 1
    with pytest.raises(QuotaExceeded):
        quota.claim(store, SUBJECT)


def test_subjects_and_scopes_are_counted_apart(store):
    daily = Quota(limit=2, window=WINDOW, scope="sends:day")
    hourly = Quota(limit=1, window=timedelta(hours=1), scope="sends:hour")

    assert daily.claim(store, "alice") == 1
    assert daily.claim(store, "bob") == 1
    assert hourly.claim(store, "alice") == 1

    assert daily.used(store, "alice") == 1
    assert daily.remaining(store, "bob") == 1
    with pytest.raises(QuotaExceeded):
        hourly.claim(store, "alice")


def test_a_value_that_is_not_a_counter_is_a_fault(quota, store):
    store.set(quota.key(SUBJECT), "lots")
    with pytest.raises(ValueError):
        quota.used(store, SUBJECT)
    with pytest.raises(ValueError):
        quota.claim(store, SUBJECT)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": -1},
        {"window": timedelta(0)},
        {"window": timedelta(seconds=-1)},
        {"scope": ""},
        {"scope": "   "},
    ],
)
def test_invalid_quotas_are_rejected(kwargs):
    fields = {"limit": 3, "window": WINDOW, **kwargs}
    with pytest.raises(ValueError):
        Quota(**fields)


@pytest.mark.parametrize("subject", ["", "   "])
def test_an_empty_subject_is_rejected(quota, store, subject):
    with pytest.raises(ValueError):
        quota.used(store, subject)
    with pytest.raises(ValueError):
        quota.claim(store, subject)
