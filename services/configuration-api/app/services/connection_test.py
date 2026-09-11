"""SIP connectivity testing (spec 14, 15).

A tenant administrator needs to know whether a PBX or trunk address is
reachable *before* routing a call to it. Without this, a typo in a hostname
surfaces as a failed call minutes or days later, with nothing to distinguish it
from a routing or credential problem.

What this does and does not prove is worth being precise about, because a
"PASSED" that overstates itself is worse than no test:

* It sends a real SIP ``OPTIONS`` request and waits for any SIP response.
  A response — including ``404`` or ``407`` — proves a SIP stack is listening
  and reachable.
* It does **not** prove the far end will accept a call, that credentials are
  right, or that media will flow. Those need an actual call.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import socket
import string
import time
from dataclasses import dataclass

from shared.logging import get_logger
from shared.models import ConnectionTestResult, SipTransport

logger = get_logger(__name__)

#: Short enough that a wrong address fails while the administrator is still
#: looking at the form, long enough for a WAN round trip.
_TIMEOUT_SECONDS = 4.0

#: A SIP stack that ignores OPTIONS is unusual but legal, so a silent timeout
#: is reported as FAILED with that nuance stated rather than as "unreachable".
_SILENT_HINT = (
    "no SIP response within {timeout:g}s. The address may be unreachable or "
    "filtered, or the far end may be configured not to answer OPTIONS"
)


@dataclass(frozen=True, slots=True)
class TestOutcome:
    result: ConnectionTestResult
    detail: str
    latency_ms: float | None = None


def _branch() -> str:
    """A unique Via branch, as RFC 3261 requires for a new transaction."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))  # noqa: S311
    return f"z9hG4bK-probe-{suffix}"


def _options_request(host: str, port: int, local_host: str, transport: str) -> bytes:
    """Build a minimal but well-formed SIP OPTIONS request.

    Deliberately complete enough that a strict stack answers it: a truncated
    request can be dropped silently, which would look identical to an
    unreachable host.
    """
    tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))  # noqa: S311
    call_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))  # noqa: S311
    lines = [
        f"OPTIONS sip:{host}:{port} SIP/2.0",
        f"Via: SIP/2.0/{transport} {local_host}:0;branch={_branch()};rport",
        "Max-Forwards: 70",
        f"From: <sip:probe@{local_host}>;tag={tag}",
        f"To: <sip:{host}:{port}>",
        f"Call-ID: {call_id}@{local_host}",
        "CSeq: 1 OPTIONS",
        f"Contact: <sip:probe@{local_host}>",
        "User-Agent: AI-Voice-Agent-Platform/connection-test",
        "Accept: application/sdp",
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _classify(response: bytes) -> TestOutcome:
    """Interpret a SIP response.

    Any SIP status line is a pass: the point of the test is reachability, and a
    ``404 Not Found`` or ``407 Proxy Authentication Required`` both prove a SIP
    stack answered. Treating an auth challenge as failure would mark a
    correctly-secured trunk as broken.
    """
    text = response.decode("utf-8", errors="replace")
    first_line = text.split("\r\n", 1)[0].strip()

    if not first_line.startswith("SIP/2.0"):
        return TestOutcome(
            result=ConnectionTestResult.FAILED,
            detail=f"reachable, but the reply was not SIP: {first_line[:120]!r}",
        )

    parts = first_line.split(None, 2)
    code = parts[1] if len(parts) > 1 else "?"
    reason = parts[2] if len(parts) > 2 else ""

    return TestOutcome(
        result=ConnectionTestResult.PASSED,
        detail=f"SIP {code} {reason}".strip() + " — a SIP stack is listening and reachable",
    )


async def _probe_udp(host: str, port: int, local_host: str) -> TestOutcome:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    started = time.perf_counter()
    try:
        await loop.sock_connect(sock, (host, port))
        await loop.sock_sendall(sock, _options_request(host, port, local_host, "UDP"))
        response = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=_TIMEOUT_SECONDS)
        outcome = _classify(response)
        return TestOutcome(
            result=outcome.result,
            detail=outcome.detail,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except TimeoutError:
        return TestOutcome(
            result=ConnectionTestResult.FAILED,
            detail=_SILENT_HINT.format(timeout=_TIMEOUT_SECONDS),
        )
    finally:
        sock.close()


async def _probe_stream(host: str, port: int, local_host: str, transport: str) -> TestOutcome:
    """TCP or TLS.

    A completed handshake is itself meaningful — more than UDP gives us — so a
    connection that opens and then goes quiet still counts as reachable.
    """
    started = time.perf_counter()
    ssl_context = None
    if transport == "TLS":
        import ssl

        # Reachability is the question, not certificate validity: a
        # self-signed certificate on a lab PBX should not read as "unreachable".
        # Certificate verification belongs to the trunk's own configuration.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context),
            timeout=_TIMEOUT_SECONDS,
        )
        writer.write(_options_request(host, port, local_host, transport))
        await writer.drain()

        try:
            response = await asyncio.wait_for(reader.read(4096), timeout=_TIMEOUT_SECONDS)
        except TimeoutError:
            response = b""

        elapsed = round((time.perf_counter() - started) * 1000, 2)

        if not response:
            return TestOutcome(
                result=ConnectionTestResult.PASSED,
                detail=(
                    f"{transport} connection established, but no SIP reply. "
                    "The port is open and reachable; the far end may not answer OPTIONS"
                ),
                latency_ms=elapsed,
            )

        outcome = _classify(response)
        return TestOutcome(outcome.result, outcome.detail, elapsed)

    except TimeoutError:
        return TestOutcome(
            result=ConnectionTestResult.FAILED,
            detail=f"{transport} connection timed out after {_TIMEOUT_SECONDS:g}s",
        )
    except ConnectionRefusedError:
        return TestOutcome(
            result=ConnectionTestResult.FAILED,
            detail=f"connection refused on {transport} {port} — nothing is listening",
        )
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def probe_sip_endpoint(
    *, host: str, port: int, transport: SipTransport, advertise_host: str | None = None
) -> TestOutcome:
    """Probe a SIP endpoint and report what was learned.

    Never raises: a connection test is diagnostic, so every failure mode has to
    come back as a readable result rather than a 500 that tells the operator
    nothing.
    """
    local_host = advertise_host or "0.0.0.0"

    try:
        # Resolve first, so a DNS mistake is reported as a DNS mistake rather
        # than as a timeout. This is the single most common cause and the
        # easiest to fix once named.
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM),
                timeout=_TIMEOUT_SECONDS,
            )
        except (TimeoutError, socket.gaierror) as exc:
            return TestOutcome(
                result=ConnectionTestResult.FAILED,
                detail=f"could not resolve {host!r}: {exc}",
            )

        if transport is SipTransport.UDP:
            return await _probe_udp(host, port, local_host)
        return await _probe_stream(host, port, local_host, transport.value)

    except Exception as exc:
        logger.exception("connection_test_error", extra={"sip_host": host, "sip_port": port})
        return TestOutcome(
            result=ConnectionTestResult.FAILED,
            detail=f"test could not be completed: {type(exc).__name__}: {exc}",
        )
