"""SIP connection testing (spec 14, 15).

The important property is honest classification: a "PASSED" that overstates
what was proven is worse than no test at all, and an auth challenge from a
correctly-secured trunk must not read as a failure.
"""

from __future__ import annotations

import pytest

from app.services.connection_test import (
    _classify,
    _options_request,
    probe_sip_endpoint,
)
from shared.models import ConnectionTestResult, SipTransport


class TestResponseClassification:
    @pytest.mark.parametrize(
        "status_line",
        [
            b"SIP/2.0 200 OK\r\n\r\n",
            b"SIP/2.0 404 Not Found\r\n\r\n",
            b"SIP/2.0 407 Proxy Authentication Required\r\n\r\n",
            b"SIP/2.0 403 Forbidden\r\n\r\n",
            b"SIP/2.0 486 Busy Here\r\n\r\n",
        ],
    )
    def test_any_sip_response_is_a_pass(self, status_line: bytes) -> None:
        """Reachability is the question being asked.

        A 404 or a 407 both prove a SIP stack answered. Treating an auth
        challenge as failure would mark a correctly-secured trunk as broken and
        send the operator looking for a network problem that does not exist.
        """
        assert _classify(status_line).result is ConnectionTestResult.PASSED

    def test_the_status_code_is_reported(self) -> None:
        detail = _classify(b"SIP/2.0 407 Proxy Authentication Required\r\n\r\n").detail
        assert "407" in detail
        assert "Proxy Authentication Required" in detail

    def test_a_non_sip_reply_is_a_failure(self) -> None:
        """Something is listening, but it is not SIP — worth saying precisely,
        since it usually means the wrong port."""
        outcome = _classify(b"HTTP/1.1 200 OK\r\n\r\n")
        assert outcome.result is ConnectionTestResult.FAILED
        assert "not SIP" in outcome.detail

    def test_binary_noise_does_not_raise(self) -> None:
        assert _classify(b"\x00\xff\xfe garbage").result is ConnectionTestResult.FAILED


class TestRequestConstruction:
    def test_the_request_is_a_well_formed_options(self) -> None:
        """A truncated request can be dropped silently, which looks identical
        to an unreachable host."""
        raw = _options_request("10.0.0.1", 5060, "192.168.1.2", "UDP").decode()
        assert raw.startswith("OPTIONS sip:10.0.0.1:5060 SIP/2.0\r\n")
        for header in ("Via:", "From:", "To:", "Call-ID:", "CSeq:", "Max-Forwards:"):
            assert header in raw
        assert raw.endswith("\r\n\r\n")
        assert "Content-Length: 0" in raw

    def test_the_branch_is_unique_per_request(self) -> None:
        """RFC 3261 requires a new branch per transaction; a repeated one can
        be treated as a retransmission and ignored."""
        first = _options_request("h", 1, "l", "UDP").decode()
        second = _options_request("h", 1, "l", "UDP").decode()
        assert first != second

    def test_the_branch_carries_the_required_magic_cookie(self) -> None:
        raw = _options_request("h", 1, "l", "UDP").decode()
        assert "branch=z9hG4bK" in raw


class TestFailureModes:
    @pytest.mark.asyncio
    async def test_an_unresolvable_host_says_so(self) -> None:
        """DNS is the most common cause and the easiest to fix once named, so
        it must not be reported as a generic timeout."""
        outcome = await probe_sip_endpoint(
            host="no-such-host.invalid", port=5060, transport=SipTransport.UDP
        )
        assert outcome.result is ConnectionTestResult.FAILED
        assert "resolve" in outcome.detail

    @pytest.mark.asyncio
    async def test_a_refused_tcp_port_says_refused(self) -> None:
        outcome = await probe_sip_endpoint(host="127.0.0.1", port=1, transport=SipTransport.TCP)
        assert outcome.result is ConnectionTestResult.FAILED
        assert "refused" in outcome.detail or "timed out" in outcome.detail

    @pytest.mark.asyncio
    async def test_it_never_raises(self) -> None:
        """A diagnostic must always return a readable result: a 500 tells the
        operator nothing about their configuration."""
        for host in ("", "999.999.999.999", "::not-an-address"):
            outcome = await probe_sip_endpoint(host=host, port=5060, transport=SipTransport.UDP)
            assert outcome.result is ConnectionTestResult.FAILED
            assert outcome.detail
