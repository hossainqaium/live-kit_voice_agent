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

import asyncio
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
    update_usage,
)
from worker.db import get_session_factory
from worker.health import state as worker_state
from worker.pipeline.observer import CallObserver
from worker.providers.registry import build_llm, build_stt, build_tts
from worker.resilience import ATTEMPT_TIMEOUT
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


async def _await_caller(ctx: JobContext) -> tuple[rtc.RemoteParticipant | None, bool]:
    """Wait for whoever is calling: a SIP participant, or a browser test one.

    Returns ``(participant, is_browser_test)``.

    **Both are awaited together, not one after the other.** They used to be
    sequential — fifteen seconds for SIP, then five for a browser — so every
    browser test call sat in silence for fifteen seconds before the agent
    started, greeted into a conversation the caller had already begun, and
    ignored everything said in the meantime. The caller experiences that as an
    agent that does not answer, which is indistinguishable from the defect this
    path was built to investigate.

    A real call is unaffected: no browser participant arrives, so the SIP
    branch resolves exactly as before.
    """
    import asyncio

    settings = get_settings()
    accept_browser = settings.allow_browser_test_participant and (
        settings.environment == "development"
    )

    def classify(participant: rtc.RemoteParticipant) -> bool | None:
        """True for SIP, False for an acceptable browser caller, None to ignore."""
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return True
        if accept_browser and _browser_test_did(participant, ctx.room):
            return False
        return None

    for participant in ctx.room.remote_participants.values():
        kind = classify(participant)
        if kind is not None:
            return participant, not kind

    joined: asyncio.Future[tuple[rtc.RemoteParticipant, bool]] = (
        asyncio.get_running_loop().create_future()
    )

    def _on_join(participant: rtc.RemoteParticipant) -> None:
        if joined.done():
            return
        kind = classify(participant)
        if kind is not None:
            joined.set_result((participant, not kind))

    ctx.room.on("participant_connected", _on_join)
    try:
        return await asyncio.wait_for(joined, timeout=_PARTICIPANT_TIMEOUT_SECONDS)
    except TimeoutError:
        return None, False
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

    Call-policy options (spec 18, 29) are forwarded to the session rather than
    reimplemented. LiveKit's own option names are used; the mapping is:
      silence_timeout_seconds → user_away_timeout
      interruption_enabled    → allow_interruptions
      interruption_min_words  → min_interruption_words
    max_call_duration_seconds is handled by a separate watchdog task in
    ``_run_call`` because no session option covers hard wall-clock limits.
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

    stt = (
        stt_chain[0]
        if len(stt_chain) == 1
        else stt_api.FallbackAdapter(stt_chain, attempt_timeout=ATTEMPT_TIMEOUT["stt"])
    )
    llm = (
        llm_chain[0]
        if len(llm_chain) == 1
        else llm_api.FallbackAdapter(llm_chain, attempt_timeout=ATTEMPT_TIMEOUT["llm"])
    )
    tts = (
        tts_chain[0]
        if len(tts_chain) == 1
        else tts_api.FallbackAdapter(tts_chain, attempt_timeout=ATTEMPT_TIMEOUT["tts"])
    )

    if len(stt_chain) > 1 or len(llm_chain) > 1 or len(tts_chain) > 1:
        logger.info(
            "provider_fallback_configured",
            extra={
                "stt_tiers": len(stt_chain),
                "llm_tiers": len(llm_chain),
                "tts_tiers": len(tts_chain),
            },
        )

    policy = context.call_policy

    # Voice activity detection drives turn-taking and barge-in (spec 29).
    # Silero runs locally, so it adds no network latency to the turn decision —
    # but loading it is synchronous, which is why it arrives here already
    # loaded by ``prewarm`` rather than being loaded per call.
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        # Spec 29 — barge-in / interruption policy (2b.4).
        allow_interruptions=policy.interruption_enabled,
        min_interruption_words=policy.interruption_min_words,
        # Spec 18 — hang up when the caller goes silent for too long (2b.1).
        # None keeps the session alive indefinitely (the default behaviour).
        user_away_timeout=(
            float(policy.silence_timeout_seconds)
            if policy.silence_timeout_seconds
            else None
        ),
    )


async def entrypoint(ctx: JobContext) -> None:
    """Handle one dispatched call."""
    settings = get_settings()
    call_id = _generate_call_id()
    worker_id = f"{settings.worker_agent_name}-{uuid.uuid4().hex[:6]}"

    with log_context(call_id=call_id, room_id=ctx.room.name):
        logger.info("job_received", extra={"room": ctx.room.name})

        await ctx.connect()

        participant, browser_test = await _await_caller(ctx)

        if participant is None:
            # Nothing to serve. Recorded as a log event rather than a call row,
            # because without a DID there is no tenant to attribute it to — and
            # a call row with a null tenant would violate spec 6.
            logger.warning("no_caller", extra={"room": ctx.room.name})
            return

        if browser_test:
            logger.warning(
                "browser_test_participant_accepted",
                extra={
                    "identity": participant.identity,
                    "did": _browser_test_did(participant, ctx.room),
                },
            )

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


async def _max_duration_watchdog(
    ctx: JobContext, max_seconds: int, call_id: str
) -> HangupReason:
    """Sleep for ``max_seconds``, then close the room (spec 18, 2b.2).

    Returns ``MAX_DURATION`` so the caller can record why the call ended.
    The room deletion is what actually causes ``_wait_for_disconnect`` to
    resolve, so both tasks always finish cleanly.
    """
    import asyncio

    await asyncio.sleep(max_seconds)
    logger.warning(
        "call_max_duration_reached",
        extra={"call_id": call_id, "max_seconds": max_seconds},
    )
    await ctx.delete_room()
    return HangupReason.MAX_DURATION


async def _wait_for_call_end(
    ctx: JobContext, context: CallContext
) -> HangupReason:
    """Wait for the room to close, enforcing a hard wall-clock limit if set.

    When ``max_call_duration_seconds`` is configured, two tasks race:
    - ``_wait_for_disconnect`` resolves on caller hangup (normal path).
    - ``_max_duration_watchdog`` fires after the limit, deletes the room, and
      resolves ``_wait_for_disconnect`` as a side-effect.

    The task that did not win is cancelled so it does not linger.
    """
    import asyncio

    max_seconds = context.call_policy.max_call_duration_seconds
    if max_seconds is None:
        await _wait_for_disconnect(ctx)
        return HangupReason.CALLER_HANGUP

    disconnect_task = asyncio.create_task(_wait_for_disconnect(ctx))
    watchdog_task = asyncio.create_task(
        _max_duration_watchdog(ctx, max_seconds, context.call_id)
    )

    done, pending = await asyncio.wait(
        {disconnect_task, watchdog_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if watchdog_task in done:
        return HangupReason.MAX_DURATION
    return HangupReason.CALLER_HANGUP


async def _start_recording(ctx: JobContext, context: CallContext) -> str | None:
    """Start a LiveKit room-composite egress to S3/MinIO (spec 39, 2b.3).

    Non-fatal: a recording failure must never silence the caller. The egress ID
    is logged so it can be retrieved from LiveKit's own records if needed.
    """
    settings = get_settings()
    if not settings.s3_bucket_recordings:
        return None
    try:
        from livekit.api import LiveKitAPI
        from livekit.api import (  # livekit-api ≥ 1.0  # noqa: PLC0415
            EncodedFileOutput,
            RoomCompositeEgressRequest,
            S3Upload,
        )

        s3 = S3Upload(
            access_key=settings.s3_access_key_id.get_secret_value(),
            secret=settings.s3_secret_access_key.get_secret_value(),
            bucket=settings.s3_bucket_recordings,
            region=settings.s3_region,
            endpoint=settings.s3_endpoint_url or "",
            force_path_style=bool(settings.s3_endpoint_url),
        )
        egress_path = f"calls/{context.tenant_id}/{context.call_id}.mp4"
        req = RoomCompositeEgressRequest(
            room_name=context.room_name,
            file_outputs=[EncodedFileOutput(filepath=egress_path, s3=s3)],
        )
        lk_url = (
            settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
        )
        async with LiveKitAPI(
            url=lk_url,
            api_key=settings.livekit_api_key.get_secret_value(),
            api_secret=settings.livekit_api_secret.get_secret_value(),
        ) as lk:
            info = await lk.egress.start_room_composite_egress(req)
        egress_id: str = info.egress_id
        logger.info(
            "recording_started",
            extra={"egress_id": egress_id, "s3_path": egress_path},
        )
        return egress_id
    except Exception:
        logger.exception("recording_start_failed_call_continues")
        return None


async def _stop_recording(egress_id: str) -> None:
    """Stop the LiveKit egress started by ``_start_recording``."""
    settings = get_settings()
    try:
        from livekit.api import LiveKitAPI  # noqa: PLC0415

        lk_url = (
            settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")
        )
        async with LiveKitAPI(
            url=lk_url,
            api_key=settings.livekit_api_key.get_secret_value(),
            api_secret=settings.livekit_api_secret.get_secret_value(),
        ) as lk:
            await lk.egress.stop_egress(egress_id)
        logger.info("recording_stopped", extra={"egress_id": egress_id})
    except Exception:
        logger.exception("recording_stop_failed", extra={"egress_id": egress_id})


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
        egress_id: str | None = None
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

            # Start recording before any audio so the greeting is captured
            # (spec 39, 2b.3). Non-fatal: failure is logged, call continues.
            if context.call_policy.recording_enabled:
                egress_id = await _start_recording(ctx, context)

            if context.greeting:
                # Spoken first so the caller is not met with silence while the
                # model warms up.
                await session.say(context.greeting, allow_interruptions=True)

            # Wait for the call to end — either the caller hangs up, or the
            # max-duration watchdog fires (spec 18, 2b.2).
            hangup_reason = await _wait_for_call_end(ctx, context)

            await tracker.transition(CallState.COMPLETED, hangup_reason=hangup_reason)
            logger.info("conversation_ended", extra={"hangup_reason": hangup_reason})
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
            # Stop recording before closing the observer so the egress
            # has a chance to flush its last segment.
            if egress_id:
                await _stop_recording(egress_id)

            # Flushed before the counters drop, so a transcript is complete
            # even when the call ended badly.
            await observer.aclose()

            # Upsert the daily usage row so the monthly-minutes limit check
            # stays accurate (spec 47, 61).  Non-fatal.
            try:
                duration = tracker.duration_seconds or 0
                succeeded = tracker.state == CallState.COMPLETED
                async with factory() as usage_session:
                    await update_usage(
                        usage_session,
                        context=context,
                        duration_seconds=int(duration),
                        succeeded=succeeded,
                    )
                    await usage_session.commit()
            except Exception:
                logger.exception("usage_update_failed")

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
