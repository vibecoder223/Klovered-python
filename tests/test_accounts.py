"""Unit tests for account auth primitives (hashing, validation, tokens) —
no database, always run."""

import pytest

from app import auth
from app.auth import (
    AuthError,
    hash_password,
    mint_account_token,
    mint_guest_token,
    normalize_email,
    validate_email,
    validate_password,
    verify_password,
    verify_token,
)


# ---------- email ----------
def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  Foo@Bar.COM ") == "foo@bar.com"


def test_validate_email_accepts_normal_address():
    assert validate_email("Someone@Company.io") == "someone@company.io"


@pytest.mark.parametrize("bad", ["", "nope", "no@domain", "@no.local", "a b@c.com", "two@@at.com"])
def test_validate_email_rejects_junk(bad):
    with pytest.raises(AuthError) as e:
        validate_email(bad)
    assert e.value.status == 400


# ---------- password ----------
def test_validate_password_accepts_min_length():
    assert validate_password("12345678") == "12345678"


def test_validate_password_rejects_short():
    with pytest.raises(AuthError) as e:
        validate_password("short")
    assert e.value.status == 400


def test_validate_password_rejects_over_bcrypt_72_byte_limit():
    # bcrypt silently truncates past 72 bytes — we reject instead of letting a
    # user think the tail of their passphrase counts.
    with pytest.raises(AuthError):
        validate_password("a" * 73)


def test_validate_password_counts_bytes_not_characters():
    # 4-byte emoji * 20 = 80 bytes, though only 20 characters.
    with pytest.raises(AuthError):
        validate_password("😀" * 20)


def test_hash_password_roundtrips():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) is True
    assert verify_password("wrong password", h) is False


def test_hash_password_is_salted_so_same_input_differs():
    assert hash_password("samepassword") != hash_password("samepassword")


def test_verify_password_rejects_missing_hash():
    # Guests have password_hash = NULL — must never match anything.
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False


def test_verify_password_rejects_malformed_hash_without_raising():
    # A junk value in the column is a failed login, not a 500.
    assert verify_password("anything", "not-a-bcrypt-hash") is False


# ---------- tokens ----------
def test_account_token_is_not_anonymous():
    uid = auth.new_user_id()
    claims = verify_token(mint_account_token(uid))
    assert claims["sub"] == uid
    assert claims["is_anonymous"] is False


def test_guest_token_is_anonymous():
    uid = auth.new_user_id()
    claims = verify_token(mint_guest_token(uid))
    assert claims["is_anonymous"] is True


def test_account_token_outlives_guest_token():
    uid = auth.new_user_id()
    guest = verify_token(mint_guest_token(uid))
    account = verify_token(mint_account_token(uid))
    assert account["exp"] > guest["exp"]
