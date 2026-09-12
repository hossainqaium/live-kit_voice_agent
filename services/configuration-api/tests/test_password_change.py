"""Self-service password change (spec 53, Plan 3b.3a).

Admin reset already exists. This suite covers the signed-in user's own
rotation: current password required, new password hashed, sessions revoked
in the same change, audit row written, no secret in the audit payload.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.core.security import hash_password, verify_password
from app.schemas.auth import PasswordChange
from app.services import auth_service


_SHELL = (
    Path(__file__).parent.parent.parent.parent
    / "services"
    / "frontend"
    / "components"
    / "Shell.tsx"
)


class TestPasswordChangeSchema:
    def test_accepts_a_valid_pair(self) -> None:
        payload = PasswordChange(
            current_password="CorrectHorse1",
            new_password="CorrectHorse2x",
        )
        assert payload.new_password == "CorrectHorse2x"

    def test_rejects_identical_passwords(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PasswordChange(
                current_password="CorrectHorse1",
                new_password="CorrectHorse1",
            )
        assert "different" in str(exc.value).lower()

    def test_rejects_a_short_new_password(self) -> None:
        with pytest.raises(ValidationError):
            PasswordChange(current_password="CorrectHorse1", new_password="short")

    def test_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            PasswordChange.model_validate(
                {
                    "current_password": "CorrectHorse1",
                    "new_password": "CorrectHorse2x",
                    "hint": "extra",
                }
            )


class TestChangeOwnPassword:
    def _user(self, password: str = "CorrectHorse1") -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            is_active=True,
            password_hash=hash_password(password),
            tokens_valid_from=None,
        )

    def _session(self, user: object | None) -> AsyncMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        return session

    @pytest.mark.asyncio
    async def test_wrong_current_password_is_refused(self) -> None:
        user = self._user("CorrectHorse1")
        session = self._session(user)
        with pytest.raises(auth_service.PasswordMismatchError):
            await auth_service.change_own_password(
                session,
                user_id=user.id,
                current_password="WrongPassword99",
                new_password="CorrectHorse2x",
            )
        assert verify_password("CorrectHorse1", user.password_hash)

    @pytest.mark.asyncio
    async def test_missing_user_is_refused(self) -> None:
        session = self._session(None)
        with pytest.raises(auth_service.InvalidCredentialsError):
            await auth_service.change_own_password(
                session,
                user_id=uuid.uuid4(),
                current_password="CorrectHorse1",
                new_password="CorrectHorse2x",
            )

    @pytest.mark.asyncio
    async def test_disabled_account_is_refused(self) -> None:
        user = self._user()
        user.is_active = False
        session = self._session(user)
        with pytest.raises(auth_service.InvalidCredentialsError):
            await auth_service.change_own_password(
                session,
                user_id=user.id,
                current_password="CorrectHorse1",
                new_password="CorrectHorse2x",
            )

    @pytest.mark.asyncio
    async def test_success_hashes_and_revokes_sessions(self) -> None:
        user = self._user("CorrectHorse1")
        session = self._session(user)
        before = datetime.now(UTC)
        returned = await auth_service.change_own_password(
            session,
            user_id=user.id,
            current_password="CorrectHorse1",
            new_password="CorrectHorse2x",
        )
        assert returned is user
        assert verify_password("CorrectHorse2x", user.password_hash)
        assert not verify_password("CorrectHorse1", user.password_hash)
        assert user.tokens_valid_from is not None
        assert user.tokens_valid_from >= before


class TestChangePasswordEndpoint:
    def test_handler_calls_audit_record(self) -> None:
        from app.api.v1 import auth as auth_api

        src = inspect.getsource(auth_api.change_password)
        assert "audit.record" in src
        assert "user.password_changed" in src

    def test_audit_payload_does_not_include_the_password(self) -> None:
        from app.api.v1 import auth as auth_api

        src = inspect.getsource(auth_api.change_password)
        assert "sessions_revoked" in src
        assert "new_password" not in src or "payload.new_password" in src
        # The audit new_value must not pass the plaintext through.
        assert 'new_value={"sessions_revoked": True}' in src.replace(" ", "") or (
            '"sessions_revoked"' in src and "payload.new_password" not in src.split("audit.record")[1]
        )


class TestChangePasswordUi:
    def test_shell_offers_change_password(self) -> None:
        if not _SHELL.exists():
            pytest.skip("services/frontend is not mounted in this container")
        src = _SHELL.read_text()
        assert "Change password" in src
        assert "changePassword" in src
        assert "current-password" in src
        assert "signOutAfterPasswordChange" in src
