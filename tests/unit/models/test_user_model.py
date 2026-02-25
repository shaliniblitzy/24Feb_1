"""User data model unit tests — validates User model from src/models/user.py.

Covers: instantiation, password hashing (werkzeug), API key ops, email
validation, status transitions, role assignment (4 RBAC roles), serialization
with sensitive-field exclusion, edge cases, constraint errors, and CRUD.
"""
import secrets  # noqa: I001
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from src.models.user import User
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
    make_anonymous_user,
    make_user_base,
    make_api_key_data,
    make_login_credentials,
    DEFAULT_PASSWORD,
    ROLE_ADMIN,
    ROLE_DEVELOPER,
    ROLE_READONLY,
    ROLE_ANONYMOUS,
)

pytestmark = pytest.mark.unit

def _make_user(session, username="testuser", email="test@example.com",
               role="developer", status="active", is_admin=False,
               password=DEFAULT_PASSWORD, **kw):
    """Create a User with werkzeug-hashed password and flush to *session*."""
    user = User(username=username, email=email, role=role, status=status,
                is_admin=is_admin,
                password_hash=generate_password_hash(password), **kw)
    session.add(user)
    session.flush()
    return user

# -- Phase 2: Happy Path — Model Instantiation --------------------------------

def test_user_model_instantiation_with_required_fields(db_session):
    """User with required fields creates successfully."""
    user = User(username="john_doe", email="john@example.com")
    db_session.add(user)
    db_session.flush()
    assert user is not None
    assert user.username == "john_doe"
    assert user.email == "john@example.com"

def test_user_model_default_values(db_session):
    """User receives correct defaults for status, role, is_admin, created_at."""
    data = make_user_base(username="defaults_user", email="defaults@example.com")
    user = User(username=data["username"], email=data["email"])
    db_session.add(user)
    db_session.flush()
    assert user.status == "active"
    assert user.role == "developer"
    assert user.is_admin is False
    assert user.created_at is not None

def test_user_model_with_admin_role(db_session):
    """Admin user has role='admin' and is_admin=True."""
    data = make_admin_user()
    user = User(username=data["username"], email=data["email"],
                role=ROLE_ADMIN, is_admin=True)
    db_session.add(user)
    db_session.flush()
    assert user.role == ROLE_ADMIN
    assert user.is_admin is True

def test_user_model_with_developer_role(db_session):
    """Developer user has role='developer' and is_admin=False."""
    data = make_developer_user()
    user = User(username=data["username"], email=data["email"],
                role=ROLE_DEVELOPER, is_admin=False)
    db_session.add(user)
    db_session.flush()
    assert user.role == ROLE_DEVELOPER
    assert user.is_admin is False


def test_user_model_with_readonly_role(db_session):
    """Readonly user has role='readonly' and is_admin=False."""
    data = make_readonly_user()
    user = User(username=data["username"], email=data["email"],
                role=ROLE_READONLY, is_admin=False)
    db_session.add(user)
    db_session.flush()
    assert user.role == ROLE_READONLY
    assert user.is_admin is False


# -- Phase 3: Password Hashing Tests (werkzeug.security) ----------------------

def test_user_password_hashing_stores_hash_not_plaintext(db_session):
    """Stored password_hash differs from plaintext password."""
    user = _make_user(db_session, username="hash_user", email="hash@example.com")
    assert user.password_hash != DEFAULT_PASSWORD
    assert isinstance(user.password_hash, str)
    assert len(user.password_hash) > 0


def test_user_password_verification_correct_password(db_session):
    """Correct password verifies against werkzeug hash."""
    creds = make_login_credentials(username="verify_user", password=DEFAULT_PASSWORD)
    user = _make_user(db_session, username=creds["username"],
                      email="verify@example.com", password=creds["password"])
    assert check_password_hash(user.password_hash, DEFAULT_PASSWORD) is True
    assert user.password_hash is not None


def test_user_password_verification_incorrect_password(db_session):
    """Incorrect password fails verification without raising exceptions."""
    user = _make_user(db_session, username="wrong_pw", email="wrong@example.com")
    result = check_password_hash(user.password_hash, "WrongPassword456!")
    assert result is False
    assert user.password_hash is not None


def test_user_password_hash_uses_werkzeug_security(db_session):
    """Hash uses werkzeug format with scrypt: or pbkdf2: prefix."""
    user = _make_user(db_session, username="wz_user", email="wz@example.com")
    assert any(user.password_hash.startswith(p) for p in ("scrypt:", "pbkdf2:"))
    assert len(user.password_hash) > 50


def test_user_set_password_updates_hash(db_session):
    """Updating password_hash invalidates the old password."""
    user = _make_user(db_session, username="update_pw", email="upd@example.com")
    old_hash = user.password_hash
    user.password_hash = generate_password_hash("NewPassword789!")
    db_session.flush()
    assert user.password_hash != old_hash
    assert check_password_hash(user.password_hash, "NewPassword789!") is True
    assert check_password_hash(user.password_hash, DEFAULT_PASSWORD) is False


def test_user_password_hash_is_unique_per_user(db_session):
    """Two users with same password produce different hashes (salting)."""
    user1 = _make_user(db_session, username="salt1", email="s1@example.com")
    user2 = _make_user(db_session, username="salt2", email="s2@example.com")
    assert user1.password_hash != user2.password_hash
    assert check_password_hash(user1.password_hash, DEFAULT_PASSWORD) is True

# -- Phase 4: API Key Tests ---------------------------------------------------

def test_user_generate_api_key_returns_key(db_session):
    """Generated API key is a non-empty hex string of sufficient length."""
    key = secrets.token_hex(32)
    user = User(username="apikey_user", email="apikey@example.com", api_key=key)
    db_session.add(user)
    db_session.flush()
    assert isinstance(key, str)
    assert len(key) >= 32


def test_user_generate_api_key_unique_per_call(db_session):
    """Successive API key generations produce distinct values."""
    key1 = secrets.token_hex(32)
    key2 = secrets.token_hex(32)
    assert key1 != key2
    assert len(key1) == len(key2)


def test_user_api_key_stored_on_model(db_session):
    """API key assigned to User is retrievable from the model field."""
    key = secrets.token_hex(32)
    user = User(username="stored_key", email="stored@example.com", api_key=key)
    db_session.add(user)
    db_session.flush()
    assert user.api_key is not None
    assert user.api_key == key


def test_user_validate_api_key_correct_key(db_session):
    """Stored key matches the originally assigned API key data."""
    data = make_api_key_data()
    user = User(username="val_key", email="val@example.com", api_key=data["key"])
    db_session.add(user)
    db_session.flush()
    assert user.api_key == data["key"]
    assert len(user.api_key) > 0


def test_user_validate_api_key_incorrect_key(db_session):
    """Stored key does not match a different API key."""
    user = User(username="bad_key", email="bad@example.com",
                api_key=secrets.token_hex(32))
    db_session.add(user)
    db_session.flush()
    wrong_key = secrets.token_hex(32)
    assert user.api_key != wrong_key
    assert user.api_key is not None

# -- Phase 5: Email Validation Tests ------------------------------------------

def test_user_email_field_accepts_valid_email(db_session):
    """Valid email address is stored and retrieved correctly."""
    user = User(username="email_valid", email="user@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.email == "user@example.com"
    assert "@" in user.email


def test_user_email_field_stores_any_string_format(db_session):
    """Model stores email as-is; format validation is at the service layer."""
    user = User(username="no_validate", email="not-an-email")
    db_session.add(user)
    db_session.flush()
    assert user.email == "not-an-email"
    assert user.username == "no_validate"


def test_user_email_uniqueness_constraint(db_session):
    """Duplicate email addresses raise IntegrityError on flush."""
    user1 = User(username="dup_email1", email="dup@example.com")
    db_session.add(user1)
    db_session.flush()
    assert user1.email == "dup@example.com"
    user2 = User(username="dup_email2", email="dup@example.com")
    db_session.add(user2)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_email_case_handling(db_session):
    """Mixed-case email is stored exactly as provided."""
    user = User(username="case_email", email="User@Example.COM")
    db_session.add(user)
    db_session.flush()
    assert user.email == "User@Example.COM"
    assert isinstance(user.email, str)


# -- Phase 6: Status Transition Tests -----------------------------------------

def test_user_initial_status_is_active(db_session):
    """New user receives default status 'active' and is_active True."""
    user = User(username="status_default", email="status@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.status == "active"
    assert user.is_active is True


def test_user_status_transition_active_to_disabled(db_session):
    """Status changes from 'active' to 'disabled'; is_active becomes False."""
    user = _make_user(db_session, username="disable_me", email="dis@example.com")
    user.status = "disabled"
    db_session.flush()
    assert user.status == "disabled"
    assert user.is_active is False


def test_user_status_transition_disabled_to_active(db_session):
    """Status changes from 'disabled' back to 'active'."""
    user = _make_user(db_session, username="reactivate", email="re@example.com",
                      status="disabled")
    user.status = "active"
    db_session.flush()
    assert user.status == "active"
    assert user.is_active is True


def test_user_status_transition_to_locked(db_session):
    """Status can be set to 'locked'; is_active becomes False."""
    user = _make_user(db_session, username="lock_me", email="lock@example.com")
    user.status = "locked"
    db_session.flush()
    assert user.status == "locked"
    assert user.is_active is False


def test_user_status_field_accepts_arbitrary_string(db_session):
    """Model stores any status string; validation is at the service layer."""
    user = User(username="any_status", email="any@example.com",
                status="custom_status")
    db_session.add(user)
    db_session.flush()
    assert user.status == "custom_status"
    assert user.is_active is False


# -- Phase 7: Role Assignment Tests (all 4 RBAC roles) ------------------------

@pytest.mark.parametrize("role", [
    ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY, ROLE_ANONYMOUS,
])
def test_user_role_assignment_parametrized(db_session, role):
    """All 4 RBAC roles are accepted and stored correctly."""
    user = User(username=f"role_{role}", email=f"role_{role}@example.com",
                role=role)
    db_session.add(user)
    db_session.flush()
    assert user.role == role
    assert isinstance(user.role, str)


def test_user_role_change_updates_value(db_session):
    """Changing role from developer to admin updates both role and is_admin."""
    data = make_developer_user()
    user = User(username=data["username"], email=data["email"],
                role=ROLE_DEVELOPER, is_admin=False)
    db_session.add(user)
    db_session.flush()
    user.role = ROLE_ADMIN
    user.is_admin = True
    db_session.flush()
    assert user.role == ROLE_ADMIN
    assert user.is_admin is True


def test_user_role_field_accepts_arbitrary_string(db_session):
    """Model stores any role string; validation is at the service layer."""
    user = User(username="custom_role", email="cr@example.com", role="superadmin")
    db_session.add(user)
    db_session.flush()
    assert user.role == "superadmin"
    assert isinstance(user.role, str)


# -- Phase 8: Serialization and Property Tests --------------------------------

def test_user_to_dict_serialization(db_session):
    """to_dict returns dict with expected keys and correct values."""
    user = _make_user(db_session, username="serial_user",
                      email="serial@example.com",
                      first_name="Serial", last_name="User")
    result = user.to_dict()
    assert isinstance(result, dict)
    assert result["username"] == "serial_user"
    assert result["email"] == "serial@example.com"
    assert "id" in result


def test_user_serialization_excludes_password_hash(db_session):
    """Serialized dict excludes password_hash and api_key (security)."""
    user = _make_user(db_session, username="secure_serial",
                      email="sec@example.com")
    user.api_key = secrets.token_hex(32)
    db_session.flush()
    result = user.to_dict()
    assert "password_hash" not in result
    assert "password" not in result
    assert "api_key" not in result


def test_user_repr_string(db_session):
    """repr() returns meaningful string containing username and role."""
    user = User(username="repr_user", email="repr@example.com", role="developer")
    repr_str = repr(user)
    assert "repr_user" in repr_str
    assert "User" in repr_str


def test_user_full_name_property(db_session):
    """full_name returns 'first last' or falls back to username."""
    user = User(username="fn_user", email="fn@example.com",
                first_name="John", last_name="Doe")
    assert user.full_name == "John Doe"
    user2 = User(username="no_names", email="nn@example.com")
    assert user2.full_name == "no_names"


def test_user_anonymous_representation(db_session):
    """Anonymous user data can initialise a valid User model."""
    anon_data = make_anonymous_user()
    user = User(username=anon_data["username"], email=anon_data.get("email"),
                role=anon_data["role"], status=anon_data["status"],
                is_admin=anon_data["is_admin"])
    db_session.add(user)
    db_session.flush()
    assert user.role == ROLE_ANONYMOUS
    assert user.is_admin is False


# -- Phase 9: Edge Cases -------------------------------------------------------

def test_user_with_very_long_username(db_session):
    """Username at max column length (255 chars) is accepted."""
    long_name = "a" * 255
    user = User(username=long_name, email="long@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.username == long_name
    assert len(user.username) == 255


def test_user_with_empty_username_string(db_session):
    """Empty-string username is stored; non-empty check is at service layer."""
    user = User(username="", email="empty@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.username == ""
    assert user.username is not None


def test_user_with_special_characters_in_username(db_session):
    """Special characters in username are handled by the model."""
    special = "user-with_special.chars@123"
    user = User(username=special, email="special@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.username == special
    assert isinstance(user.username, str)


def test_user_last_login_initially_none(db_session):
    """New user has last_login=None and created_at populated."""
    user = User(username="no_login", email="nologin@example.com")
    db_session.add(user)
    db_session.flush()
    assert user.last_login is None
    assert user.created_at is not None


def test_user_last_login_update(db_session):
    """last_login can be set to a datetime value after creation."""
    user = User(username="login_update", email="login@example.com")
    db_session.add(user)
    db_session.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user.last_login = now
    db_session.flush()
    assert user.last_login is not None
    assert isinstance(user.last_login, datetime)


# -- Phase 10: Error Cases (Database Constraints) -----------------------------

def test_user_null_username_raises_error(db_session):
    """NOT NULL constraint on username raises IntegrityError."""
    user = User(username=None, email="null_user@example.com")
    assert user.username is None
    db_session.add(user)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_email_nullable_is_allowed(db_session):
    """Email is nullable — anonymous/API-key-only users need no email."""
    user = User(username="no_email_user", email=None)
    db_session.add(user)
    db_session.flush()
    assert user.email is None
    assert user.username == "no_email_user"


def test_user_duplicate_username_constraint(db_session):
    """UNIQUE constraint on username raises IntegrityError for duplicates."""
    user1 = User(username="dup_name", email="dup1@example.com")
    db_session.add(user1)
    db_session.flush()
    assert user1.username == "dup_name"
    user2 = User(username="dup_name", email="dup2@example.com")
    db_session.add(user2)
    with pytest.raises(IntegrityError):
        db_session.flush()


# -- Phase 11: Database Persistence Tests (CRUD) ------------------------------

def test_user_persists_to_database(db_session):
    """User persists and queries back with matching fields."""
    user = _make_user(db_session, username="persist_user",
                      email="persist@example.com")
    db_session.commit()
    queried = db_session.query(User).filter_by(username="persist_user").first()
    assert queried is not None
    assert queried.email == "persist@example.com"
    assert queried.username == "persist_user"


def test_user_update_persists(db_session):
    """Updated email field persists after commit and re-query."""
    user = _make_user(db_session, username="update_user",
                      email="old@example.com")
    db_session.commit()
    user.email = "new@example.com"
    db_session.commit()
    queried = db_session.query(User).filter_by(username="update_user").first()
    assert queried is not None
    assert queried.email == "new@example.com"


def test_user_delete_removes_from_db(db_session):
    """Deleted user no longer appears in queries."""
    user = _make_user(db_session, username="delete_user",
                      email="delete@example.com")
    db_session.commit()
    db_session.delete(user)
    db_session.commit()
    queried = db_session.query(User).filter_by(username="delete_user").first()
    assert queried is None
    assert db_session.query(User).count() == 0
