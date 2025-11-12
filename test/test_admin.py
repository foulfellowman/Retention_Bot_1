import pytest

pytest.importorskip("flask_login")

from admin import Admin


def test_admin_hashes_plain_password():
    admin = Admin("user", "secret")

    assert admin.password_hash is not None
    assert admin.password_hash != "secret"
    assert admin.check_password("secret")
    assert not admin.check_password("other")


def test_admin_accepts_prehashed_password():
    admin = Admin("user", "secret")
    hashed = admin.password_hash

    cloned = Admin("user", hashed)

    assert cloned.password_hash == hashed
    assert cloned.check_password("secret")
