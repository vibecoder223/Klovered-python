import pytest

from app.auth import AuthError, verify_jwt


def test_valid_token_returns_claims(make_token):
    token = make_token(sub="guest-abc", is_anonymous=True, email="")
    claims = verify_jwt(token)
    assert claims["sub"] == "guest-abc"
    assert claims["is_anonymous"] is True


def test_expired_token_raises_401(make_token):
    token = make_token(expired=True)
    with pytest.raises(AuthError) as exc:
        verify_jwt(token)
    assert exc.value.status == 401


def test_wrong_audience_raises_401(make_token):
    token = make_token(aud="wrong-aud")
    with pytest.raises(AuthError) as exc:
        verify_jwt(token)
    assert exc.value.status == 401
