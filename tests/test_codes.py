import re

import pytest

from otpguard import (
    DIGITS,
    UPPERCASE_UNAMBIGUOUS,
    CodePolicy,
    HashedCode,
    generate_code,
    hash_code,
    verify_code,
)


def test_default_policy_generates_six_digits():
    assert re.fullmatch(r"\d{6}", generate_code())


def test_generated_code_follows_policy():
    policy = CodePolicy(length=8, alphabet=UPPERCASE_UNAMBIGUOUS)
    code = generate_code(policy)
    assert len(code) == 8
    assert set(code) <= set(UPPERCASE_UNAMBIGUOUS)


def test_generated_codes_vary():
    assert len({generate_code() for _ in range(50)}) > 1


def test_stored_value_does_not_contain_the_code():
    code = "482913"
    assert code not in hash_code(code).encode()


def test_verify_accepts_the_correct_code():
    code = generate_code()
    assert verify_code(code, hash_code(code))


def test_verify_rejects_a_wrong_code():
    assert not verify_code("123457", hash_code("123456"))


def test_same_code_hashes_differently_each_time():
    first = hash_code("123456")
    second = hash_code("123456")
    assert first.digest != second.digest
    assert verify_code("123456", first)
    assert verify_code("123456", second)


def test_pepper_is_required_to_verify():
    hashed = hash_code("123456", pepper=b"server-secret")
    assert verify_code("123456", hashed, pepper=b"server-secret")
    assert not verify_code("123456", hashed)
    assert not verify_code("123456", hashed, pepper=b"other-secret")


def test_verification_ignores_separators_and_case():
    policy = CodePolicy(length=6, alphabet=UPPERCASE_UNAMBIGUOUS)
    hashed = hash_code("AB2CD3", policy)
    assert verify_code("ab2-cd3", hashed, policy)
    assert verify_code(" AB2 CD3 ", hashed, policy)


def test_case_sensitive_policy_rejects_the_wrong_case():
    policy = CodePolicy(length=6, alphabet="abcdefABCDEF", case_sensitive=True)
    hashed = hash_code("aBcDeF", policy)
    assert verify_code("aBcDeF", hashed, policy)
    assert not verify_code("abcdef", hashed, policy)


def test_encoded_form_round_trips():
    hashed = hash_code("123456")
    assert HashedCode.decode(hashed.encode()) == hashed
    assert verify_code("123456", hashed.encode())


def test_decode_rejects_a_malformed_value():
    with pytest.raises(ValueError):
        HashedCode.decode("nonsense")


def test_unknown_algorithm_never_verifies():
    hashed = hash_code("123456")
    foreign = HashedCode("sha1", hashed.salt, hashed.digest)
    assert not verify_code("123456", foreign)


def test_entropy_bits():
    policy = CodePolicy(length=6, alphabet=DIGITS)
    assert policy.entropy_bits == pytest.approx(19.93, abs=0.01)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"length": 3},
        {"alphabet": "A"},
        {"alphabet": "AAB"},
        {"alphabet": "aA"},
        {"alphabet": "AB-CD"},
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        CodePolicy(**kwargs)


def test_empty_salt_is_rejected():
    with pytest.raises(ValueError):
        hash_code("123456", salt=b"")
