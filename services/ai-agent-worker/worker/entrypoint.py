"""LiveKit Agents job handler — the per-call execution path (spec 22, 23).

The sequence, and why it is this order:

1. Wait for the SIP participant, because its attributes are the only reliable
   source of the dialled number (spec 20 routing depends on it).
2. Resolve configuration and create the call record before any audio, so a
   call that cannot be routed is recorded as such rather than vanishing.
3. Build the pipeline from that configuration alone. Nothing tenant-specific
   is compiled in (spec 23, 79).
4. Run until the caller hangs up, a policy limit fires, or the worker drains.

The worker holds no tenant logic. Everything above step 3 is data.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions

from shared.logging import get_logger, log_context
from shared.models import CallState, HangupReason
from shared.telemetry import VoiceMetrics
from worker.call_state import CallStateTracker
from worker.config_loader import (
    CallConfigLoader,
    CallContext,
    ConfigurationError,
    NoRouteError,
    NotPublishedError,
    SipCallInfo,
    TenantLimitExceededError,
)
from worker.db import get_session_factory
from worker.health import state as worker_state
from worker.pipeline.observer import CallObserver
from worker.providers.registry import build_llm, build_stt, build_tts
from worker.settings import get_settings

logger = get_logger(__name__)

#: One instance per process. Prometheus collectors are registered on
#: creation, so building these per call would raise on the second call.
voice_metrics = VoiceMetrics()

#: How long to wait for the SIP participant to appear. LiveKit creates the room
#: and dispatches the agent before the participant is fully joined, so a short
#: wait is normal; exceeding this means the call never really arrived.
_PARTICIPANT_TIMEOUT_SECONDS = 15.0


def _generate_call_id() -> str:
    """A readable, unique correlation ID (spec 43).

    Time-prefixed so that sorting log lines by call_id groups a conversation
    together, and a support ticket quoting one is easy to place in time.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"call_{stamp}_{uuid.uuid4().hex[:8]}"


async def _await_sip_participant(ctx: JobContext) -> rtc.RemoteParticipant | None:
    """Return the SIP participant, or None if none arrives in time."""
    for participant in ctx.room.remote_participants.values():
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant

    import asyncio

    joined: asyncio.Future[rtc.RemoteParticipant] = asyncio.get_running_loop().create_future()

    def _on_join(participant: rtc.RemoteParticipant) -> None:
        if not joined.done() and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            joined.set_result(participant)

    ctx.room.on("participant_connected", _on_join)
    try:
        return await asyncio.wait_for(joined, timeout=_PARTICIPANT_TIMEOUT_SECONDS)
    except TimeoutError:
        return None
    finally:
        ctx.room.off("participant_connected", _on_join)


#: Where a browser test client may declare the DID it is calling. Checked in
#: order; participant attributes first because a client can set them per
#: participant without re-creating the room.
_TEST_DID_KEYS = ("did", "test.did", "sip.trunkPhoneNumber")


def _browser_test_did(participant: rtc.RemoteParticipant, room: rtc.Room) -> str | None:
    """The DID a browser participant claims, from its attributes or the room.

    Returns None when nothing declares one, which is the correct outcome: a
    participant with no DID cannot be attributed to a tenant, and this path
    exists to relax where the DID comes from, not whether there is one.
    """
    attributes = dict(participant.attributes)
    for key in _TEST_DID_KEYS:
        value = attributes.get(key)
        if value:
            return str(value).strip()

    # Room metadata, for a client that cannot set participant attributes. The
    # Playground can set room metadata at token-mint time, so this is the path
    # that needs no fork of it.
    raw = (room.metadata or "").strip()
    if not raw:
        return None
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError:
        # Metadata is free-form and may legitimately hold something else.
        # Treating that as an error would turn an unrelated convention into a
        # failed call.
        return None
    if isinstance(metadata, dict):
        for key in _TEST_DID_KEYS:
            value = metadata.get(key)
            if value:
                return str(value).strip()
    return None


async def _await_browser_test_participant(ctx: JobContext) -> rtc.RemoteParticipant | None:
    """A non-SIP participant, for the development test path (Plan 2b.10).

    Only reached when the setting is on; see ``allow_browser_test_participant``
    for why that default matters.
    """
    import asyncio

    settings = get_settings()
    deadline = settings.browser_test_participant_timeout_seconds

    for participant in ctx.room.remote_participants.values():
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant

    joined: asyncio.Future[rtc.RemoteParticipant] = asyncio.get_running_loop().create_future()

    def _on_join(participant: rtc.RemoteParticipant) -> None:
        if not joined.done() and participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            joined.set_result(participant)

    ctx.room.on("participant_connected", _on_join)
    try:
        return await asyncio.wait_for(joined, timeout=deadline)
    except TimeoutError:
        return None
    finally:
        ctx.room.off("participant_connected", _on_join)


def _build_agent(context: CallContext) -> Agent:
    """Construct the agent from configuration only.

    The system prompt and greeting come from the published agent version, so
    two tenants running the same worker behave completely differently without
    a line of tenant-specific code (spec 9, 23).
    """
    instructions = context.system_prompt or (
        "You are a helpful voice assistant answering a phone call. "
        "Keep replies brief, since they are spoken aloud."
    )
    return Agent(instructions=instructions)


def _build_session(context: CallContext, vad: Any) -> AgentSession:
    """Assemble the STT, LLM and TTS pipeline for this call (spec 28, 55).

    When a version configures fallback or local tiers, each stage becomes a
    ``FallbackAdapter`` over the chain. LiveKit's own adapters are used rather
    than a retry wrapper of our own: they already know which errors are worth
    failing over for, they recover to the primary when it comes back, and they
    do it inside one turn — which is the only place a caller would tolerate it.

    A single-tier chain is passed through unwrapped. Wrapping one provider adds
    a layer that can only ever fail the same way, and it would show up in every
    trace for no reason.
    """
    from livekit.agents import llm as llm_api
    from livekit.agents import stt as stt_api
    from livekit.agents import tts as tts_api

    stt_chain = [
        build_stt(config).build_livekit_component()
        for config in (context.stt, *context.stt_fallbacks)
    ]
    llm_chain = [
        build_llm(config).build_livekit_component()
        for config in (context.llm, *context.llm_fallbacks)
    ]
    tts_chain = [
        build_tts(config).build_livekit_component()
        for config in (context.tts, *context.tts_fallbacks)
    ]

    stt = stt_chain[0] if len(stt_chain) == 1 else stt_api.FallbackAdapter(stt_chain)
    llm = llm_chain[0] if len(llm_chain) == 1 else llm_api.FallbackAdapter(llm_chain)
    tts = tts_chain[0] if len(tts_chain) == 1 else tts_api.FallbackAdapter(tts_chain)

    if len(stt_chain) > 1 or len(llm_chain) > 1 or len(tts_chain) > 1:
        logger.info(
            "provider_fallback_configured",
            extra={
                "stt_tiers": len(stt_chain),
                "llm_tiers": len(llm_chain),
                "tts_tiers": len(tts_chain),
            },
        )

    # Voice activity detection drives turn-taking and barge-in (spec 29).
    # Silero runs locally, so it adds no network latency to the turn decision —
    # but loading it is synchronous, which is why it arrives here already
    # loaded by ``prewarm`` rather than being loaded per call.
    return AgentSession(stt=stt, llm=llm, tts=tts, vad=vad)


async def entrypoint(ctx: JobContext) -> None:
    """Handle one dispatched call."""
    settings = get_settings()
    call_id = _generate_call_id()
    worker_id = f"{settings.worker_agent_name}-{uuid.uuid4().hex[:6]}"

    with log_context(call_id=call_id, room_id=ctx.room.name):
        logger.info("job_received", extra={"room": ctx.room.name})

        await ctx.connect()

        participant = await _await_sip_participant(ctx)
        browser_test = False

        if participant is None and settings.allow_browser_test_participant:
            # Development test path (Plan 2b.10): accept a browser participant
            # that declares the DID it is calling, so the pipeline can be
            # exercised without a PBX. Everything downstream is unchanged — the
            # DID still resolves the tenant, agent and providers, and the call
            # still gets a row.
            if settings.environment != "development":
                logger.error(
                    "browser_test_participant_refused_outside_development",
                    extra={"environment": settings.environment},
                )
            else:
                candidate = await _await_browser_test_participant(ctx)
                did = _browser_test_did(candidate, ctx.room) if candidate else None
                if candidate is not None and did:
                    participant = candidate
                    browser_test = True
                    logger.warning(
                        "browser_test_participant_accepted",
                        extra={"identity": candidate.identity, "did": did},
                    )
                elif candidate is not None:
                    # Joined but declared nothing. Naming the keys is the whole
                    # difference between a five-minute fix and an afternoon:
                    # the failure is silent audio either way.
                    logger.warning(
                        "browser_test_participant_has_no_did",
                        extra={
                            "identity": candidate.identity,
                            "checked_attributes": list(_TEST_DID_KEYS),
                            "hint": (
                                "set a participant attribute or room metadata JSON "
                                "naming the DID to call"
                            ),
                        },
                    )

        if participant is None:
            # Nothing to serve. Recorded as a log event rather than a call row,
            # because without SIP attributes there is no tenant to attribute it
            # to — and a call row with a null tenant would violate spec 6.
            logger.warning("no_sip_participant", extra={"room": ctx.room.name})
            return

        attributes = dict(participant.attributes)
        if browser_test:
            # Present the declared DID through the same attribute the SIP stack
            # would have used, so SipCallInfo and everything after it takes one
            # code path. A second parsing route for test calls would be a
            # second thing to keep correct.
            attributes["sip.trunkPhoneNumber"] = _browser_test_did(participant, ctx.room) or ""
            attributes.setdefault("sip.phoneNumber", f"browser:{participant.identity}")

        # The full attribute set at debug level. LiveKit's SIP attribute names
        # have changed across versions, and a routing failure caused by a
        # renamed key is otherwise indistinguishable from a misconfigured DID.
        logger.debug("sip_participant_attributes", extra={"attributes": attributes})

        sip = SipCallInfo.from_attributes(attributes)
        logger.info(
            "sip_participant_joined",
            extra={
                "sip_call_id": sip.livekit_call_id,
                "called_number": sip.called_number,
                "caller_number": sip.caller_number,
                "livekit_trunk_id": sip.livekit_trunk_id,
            },
        )

        factory = get_session_factory()
        loader = CallConfigLoader()

        try:
            async with factory() as session:
                context = await loader.load(
                    session,
                    call_id=call_id,
                    room_name=ctx.room.name,
                    sip=sip,
                    worker_id=worker_id,
                )
        except TenantLimitExceededError as exc:
            # Spec 47: enforced before the call is accepted. The caller is
            # released rather than answered and dropped.
            logger.warning("call_rejected_tenant_limit", extra={"limit": exc.limit})
            await ctx.delete_room()
            return
        except (NoRouteError, NotPublishedError) as exc:
            logger.warning("call_not_routable", extra={"reason": str(exc)})
            await ctx.delete_room()
            return
        except ConfigurationError as exc:
            logger.error("call_configuration_failed", extra={"reason": str(exc)})
            await ctx.delete_room()
            return

        await _run_call(ctx, context, factory)


async def _run_call(ctx: JobContext, context: CallContext, factory) -> None:
    """Run the conversation for a fully resolved call."""
    tracker = CallStateTracker(
        factory,
        call_row_id=context.call_row_id,
        tenant_id=context.tenant_id,
        call_id=context.call_id,
        initial_state=CallState.ANSWERED,
    )

    worker_state.active_calls += 1
    voice_metrics.active_calls.labels(tenant_id=str(context.tenant_id)).inc()
    _update_utilization()

    observer = CallObserver(
        factory,
        call_row_id=context.call_row_id,
        tenant_id=context.tenant_id,
        call_id=context.call_id,
        agent_id=context.agent_name,
        metrics=voice_metrics,
        transcription_enabled=context.call_policy.transcription_enabled,
    )

    with log_context(**context.log_fields()):
        try:
            session = _build_session(context, ctx.proc.userdata["vad"])

            # Attached before the session starts so the first turn is not
            # missed. Wrapped because observability must never be able to end
            # a call: a failure here should cost visibility, not the
            # conversation. This bit me immediately — a wrong attribute name
            # left a call stuck in ANSWERED.
            try:
                observer.attach(session)
                await observer.start()
            except Exception:
                logger.exception("observer_start_failed_continuing_without_it")

            await tracker.transition(CallState.AI_CONNECTED)

            await session.start(
                agent=_build_agent(context),
                room=ctx.room,
                room_input_options=RoomInputOptions(
                    # Telephony audio arrives already narrowband and
                    # noise-suppressed by the carrier; a second pass adds
                    # latency for no gain.
                    noise_cancellation=None,
                ),
            )

            await tracker.transition(CallState.IN_PROGRESS)
            logger.info("conversation_started")

            if context.greeting:
                # Spoken first so the caller is not met with silence while the
                # model warms up.
                await session.say(context.greeting, allow_interruptions=True)

            # The session ends when the caller hangs up, or when a policy limit
            # closes it. Both surface as the room disconnecting.
            await _wait_for_disconnect(ctx)

            await tracker.transition(CallState.COMPLETED, hangup_reason=HangupReason.CALLER_HANGUP)
            logger.info("conversation_ended")
            _record_completion(context, tracker, CallState.COMPLETED)

        except Exception as exc:
            logger.exception("call_failed")
            if not tracker.is_terminal:
                await tracker.transition(
                    CallState.FAILED,
                    hangup_reason=HangupReason.SYSTEM_ERROR,
                    detail=str(exc)[:500],
                )
            _record_completion(context, tracker, CallState.FAILED)
            raise
        finally:
            # Flushed before the counters drop, so a transcript is complete
            # even when the call ended badly.
            await observer.aclose()
            worker_state.active_calls = max(0, worker_state.active_calls - 1)
            voice_metrics.active_calls.labels(tenant_id=str(context.tenant_id)).dec()
            _update_utilization()


def _update_utilization() -> None:
    """Publish this worker's load as a ratio (spec 48, 50).

    A ratio rather than a count, because it is what an autoscaler can act on
    without knowing each worker's configured capacity.
    """
    settings = get_settings()
    capacity = max(1, settings.worker_max_concurrent_calls)
    voice_metrics.worker_utilization.set(worker_state.active_calls / capacity)


def _record_completion(context: CallContext, tracker: CallStateTracker, state: CallState) -> None:
    """Record the call's outcome and duration (spec 57 business metrics)."""
    labels = {"tenant_id": str(context.tenant_id), "agent_id": context.agent_name or "unknown"}
    voice_metrics.calls_total.labels(**labels, state=state.value).inc()
    duration = tracker.duration_seconds
    if duration is not None:
        voice_metrics.call_duration.labels(**labels).observe(duration)


async def _wait_for_disconnect(ctx: JobContext) -> None:
    """Block until the room closes."""
    import asyncio

    closed = asyncio.get_running_loop().create_future()

    def _on_disconnect(*_: object) -> None:
        if not closed.done():
            closed.set_result(None)

    ctx.room.on("disconnected", _on_disconnect)
    ctx.room.on("participant_disconnected", _on_disconnect)
    try:
        await closed
    finally:
        ctx.room.off("disconnected", _on_disconnect)
        ctx.room.off("participant_disconnected", _on_disconnect)


def prewarm(proc: agents.JobProcess) -> None:
    """Load the VAD model once per process, before any job arrives.

    Silero is an ONNX model and loading it is synchronous. Doing it inside the
    entrypoint blocked the job's event loop for about a second on every call,
    measured between ``call_configuration_loaded`` and the session starting —
    a second of silence the caller hears before the greeting.

    It is process-wide rather than per-call because the model is stateless and
    identical for every tenant: nothing about it is configuration (spec 23), so
    sharing it changes no behaviour.
    """
    from livekit.plugins import silero

    proc.userdata["vad"] = silero.VAD.load()


def worker_options() -> agents.WorkerOptions:
    """Register this worker for agent dispatch.

    ``agent_name`` is what LiveKit dispatch rules target (spec 21), so it must
    match ``agent_dispatch_name`` on the rule the Control Plane created.
    """
    settings = get_settings()
    return agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=settings.worker_agent_name,
        ws_url=settings.livekit_url,
        api_key=settings.livekit_api_key.get_secret_value(),
        api_secret=settings.livekit_api_secret.get_secret_value(),
        # Must exceed the longest expected call so a deploy drains rather than
        # cuts conversations off (spec 51).
        drain_timeout=settings.worker_drain_timeout_seconds,
        prewarm_fnc=prewarm,
    )
