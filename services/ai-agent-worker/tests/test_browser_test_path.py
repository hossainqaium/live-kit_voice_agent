"""The development browser test path (Plan 2b.10).

The worker's SIP gate is what guarantees every call has a tenant: SIP
attributes carry the DID, the DID is the only route to a tenant, and a call row
with a null tenant would violate spec 6. This path relaxes *where the DID comes
from*, not whether there is one — so these tests are mostly about the things
that must remain true.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from worker.entrypoint import _browser_test_did
from worker.settings import WorkerSettings


def participant(**attributes: str) -> SimpleNamespace:
    return SimpleNamespace(identity="browser-1", attributes=dict(attributes))


def room(metadata: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name="browser-test", metadata=metadata)


class TestTheGateIsOffByDefault:
    """The property that matters most, asserted rather than assumed.

    Read from the field definitions rather than from ``WorkerSettings()``.
    Instantiating reads the environment, so on any machine where the gate is
    switched on for testing — which is the machine most likely to be running
    these tests — an instance-based assertion tests the current environment and
    not the default. It failed exactly that way the first time it ran.
    """

    def test_the_setting_defaults_to_false(self) -> None:
        field = WorkerSettings.model_fields["allow_browser_test_participant"]
        assert field.default is False

    def test_the_default_environment_is_development(self) -> None:
        """So the environment check is not the only guard standing between a
        production deployment and an unauthenticated participant."""
        assert WorkerSettings.model_fields["environment"].default == "development"

    def test_both_the_setting_and_the_environment_are_checked(self) -> None:
        """Either guard alone is one configuration mistake from being wrong.

        Read from the source because the branch needs a live JobContext to
        exercise: this asserts the guards are present and paired, which is the
        part a refactor would silently drop — and did. The guards moved from
        ``entrypoint`` into ``_await_caller`` when the two waits were made
        concurrent, and this test caught that it had to follow them.
        """
        import inspect

        from worker import entrypoint

        source = inspect.getsource(entrypoint._await_caller)
        assert "settings.allow_browser_test_participant" in source
        assert 'settings.environment == "development"' in source

    def test_a_real_call_does_not_wait_for_a_browser_participant(self) -> None:
        """The two waits are concurrent, not sequential.

        They were sequential first: fifteen seconds for SIP, then five for a
        browser. Every browser test call therefore sat silent for fifteen
        seconds before the agent started, greeted over a caller who had already
        spoken, and dropped what was said in between — which presents exactly
        as an agent that does not answer.
        """
        import inspect

        from worker import entrypoint

        source = inspect.getsource(entrypoint)
        assert "_await_browser_test_participant" not in source, (
            "a second sequential wait has come back"
        )
        assert "async def _await_caller" in source


class TestDidResolution:
    def test_a_participant_attribute_is_read(self) -> None:
        assert _browser_test_did(participant(did="1001"), room()) == "1001"

    @pytest.mark.parametrize("key", ["did", "test.did", "sip.trunkPhoneNumber"])
    def test_every_documented_key_works(self, key: str) -> None:
        assert _browser_test_did(participant(**{key: "1801"}), room()) == "1801"

    def test_room_metadata_is_read_when_the_participant_says_nothing(self) -> None:
        """The path that needs no fork of the playground.

        A browser client mints its own token from its own configuration and
        cannot set participant attributes for us, but the room it joins can
        carry the DID — which is what ``make test-room`` writes.
        """
        assert _browser_test_did(participant(), room(json.dumps({"did": "1001"}))) == "1001"

    def test_the_participant_wins_over_the_room(self) -> None:
        found = _browser_test_did(participant(did="1001"), room(json.dumps({"did": "1801"})))
        assert found == "1001"

    def test_no_did_anywhere_returns_none(self) -> None:
        """Which makes the call be refused, exactly as a SIP-less call is.

        This is the case that must not fall through to "pick a tenant": a
        participant with no DID cannot be attributed to one.
        """
        assert _browser_test_did(participant(), room()) is None

    def test_unparseable_room_metadata_is_not_an_error(self) -> None:
        """Metadata is free-form and may hold something else entirely.

        Raising here would turn an unrelated convention into a failed call.
        """
        assert _browser_test_did(participant(), room("not json at all")) is None

    def test_metadata_that_is_not_an_object_is_ignored(self) -> None:
        assert _browser_test_did(participant(), room(json.dumps(["1001"]))) is None

    def test_a_blank_attribute_does_not_count_as_a_did(self) -> None:
        """An empty string is how a client that tried and failed shows up."""
        assert _browser_test_did(participant(did=""), room()) is None

    def test_whitespace_is_trimmed(self) -> None:
        assert _browser_test_did(participant(did=" 1001 "), room()) == "1001"


class TestTheDidTakesTheNormalPath:
    """A test call must resolve configuration the way a real one does."""

    def test_the_did_is_presented_as_a_sip_attribute(self) -> None:
        """So ``SipCallInfo`` and everything after it has one code path.

        A second parsing route for test calls would be a second thing to keep
        correct, and the two would drift in exactly the way that makes a test
        call stop resembling a real one.
        """
        import inspect

        from worker import entrypoint

        source = inspect.getsource(entrypoint.entrypoint)
        assert 'attributes["sip.trunkPhoneNumber"]' in source

    def test_sip_call_info_reads_that_attribute(self) -> None:
        from worker.config_loader import SipCallInfo

        info = SipCallInfo.from_attributes({"sip.trunkPhoneNumber": "1001"})
        assert info.called_number == "1001"
