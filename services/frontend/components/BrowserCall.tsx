"use client";

/**
 * Browser test call (Plan 2b.10).
 *
 * Talks to an agent over WebRTC with no PBX in the path, so an operator can
 * hear their own configuration before pointing a phone number at it — and so
 * latency work does not need a phone call per iteration.
 *
 * Two things it deliberately does not do. It never stores the join token: the
 * token lives in this component's closure for the life of the call and goes
 * with it. And it does not claim to replace a real call — telephony audio is
 * 8 kHz and a browser is not, so a model that sounds fine here can still be
 * wrong on the phone, which the panel says on screen rather than in a comment.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, Button, Dialog, Notice } from "@/components/ui";
import { ApiError, api, type BrowserTestSession } from "@/lib/api";

type Phase = "idle" | "connecting" | "live" | "ended" | "failed";

interface Line {
  who: "you" | "agent";
  text: string;
  final: boolean;
}

export function BrowserCall({
  did,
  agentName,
  onClose,
}: {
  did: string;
  agentName: string;
  onClose(): void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState<BrowserTestSession | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [muted, setMuted] = useState(false);

  // The Room instance and the audio elements it creates live outside React
  // state: they are not rendered, and putting them in state would re-run
  // effects on every transcript line.
  const roomRef = useRef<unknown>(null);
  const audioRef = useRef<HTMLDivElement | null>(null);

  const hangUp = useCallback(async () => {
    const room = roomRef.current as { disconnect?: () => Promise<void> } | null;
    roomRef.current = null;

    // No room means no call to end, so the phase must not move. React runs
    // effects twice in development — mount, unmount, mount — and the unmount
    // cleanup calls this. Without the guard the panel opened already reading
    // "ended", offering to "Call again" before the first call.
    if (!room) return;

    if (room.disconnect) {
      try {
        await room.disconnect();
      } catch {
        // Already gone. Nothing to recover and nothing worth telling anyone.
      }
    }
    setPhase((p) => (p === "failed" ? p : "ended"));
  }, []);

  useEffect(() => {
    // Hanging up on unmount matters more than it looks: without it, closing
    // the dialog leaves a participant in the room and the agent talking to
    // nobody until its own timeout.
    return () => {
      void hangUp();
    };
  }, [hangUp]);

  async function start() {
    setPhase("connecting");
    setError(null);
    setLines([]);

    let issued: BrowserTestSession;
    try {
      issued = await api.browserTest.session(did);
      setSession(issued);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not open a test call");
      setPhase("failed");
      return;
    }

    try {
      // Imported here rather than at module scope so the SDK is only fetched
      // when somebody actually places a call. It is by far the largest
      // dependency in the console.
      const { Room, RoomEvent, Track } = await import("livekit-client");

      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;

      room.on(RoomEvent.TrackSubscribed, (track: { kind: string; attach(): HTMLMediaElement }) => {
        if (track.kind === Track.Kind.Audio && audioRef.current) {
          audioRef.current.appendChild(track.attach());
        }
      });

      room.on(
        RoomEvent.TranscriptionReceived,
        (
          segments: { text: string; final: boolean }[],
          participant?: { isLocal?: boolean },
        ) => {
          const who: Line["who"] = participant?.isLocal ? "you" : "agent";
          setLines((prev) => {
            const next = [...prev];
            for (const segment of segments) {
              // Interim results replace the previous interim from the same
              // speaker; a final one is appended. Without this the panel fills
              // with half-sentences and reads as though the agent is stuttering.
              const last = next[next.length - 1];
              if (last && last.who === who && !last.final) {
                next[next.length - 1] = { who, text: segment.text, final: segment.final };
              } else {
                next.push({ who, text: segment.text, final: segment.final });
              }
            }
            return next;
          });
        },
      );

      room.on(RoomEvent.Disconnected, () => {
        roomRef.current = null;
        setPhase((p) => (p === "failed" ? p : "ended"));
      });

      await room.connect(issued.url, issued.token, {
        // The SDK default is 15 s, and the first measurement of this path was
        // 14,992 ms — negotiation was losing a race with its own timeout, and
        // reported it as "negotiation timed out" with no mention of time.
        //
        // Muxing LiveKit onto one UDP port brought it to ~7 s; this is the
        // headroom, not the fix. A developer machine running a Kubernetes
        // cluster and a second application stack is slower again, and a call
        // that takes twelve seconds to connect is worth waiting for when the
        // alternative is an error that explains nothing.
        peerConnectionTimeout: 45_000,
      });

      // Development aid: lets the media path be exercised from the browser
      // console without a microphone, which is the only way to reproduce a
      // publish failure on a machine that has no audio input.
      if (process.env.NODE_ENV !== "production") {
        (window as unknown as { __voiceAgentRoom?: unknown }).__voiceAgentRoom = room;
      }

      await room.localParticipant.setMicrophoneEnabled(true);
      setPhase("live");
    } catch (err) {
      // The overwhelmingly common cause is a denied microphone prompt, and
      // the raw error says "NotAllowedError", which helps nobody.
      const message =
        err instanceof Error && err.name === "NotAllowedError"
          ? "the browser refused microphone access — allow it and try again"
          : err instanceof Error
            ? err.message
            : "could not connect to the call";
      setError(message);
      setPhase("failed");
    }
  }

  async function toggleMute() {
    const room = roomRef.current as {
      localParticipant?: { setMicrophoneEnabled(on: boolean): Promise<unknown> };
    } | null;
    if (!room?.localParticipant) return;
    await room.localParticipant.setMicrophoneEnabled(muted);
    setMuted(!muted);
  }

  return (
    <Dialog
      title={`Test call to ${agentName}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Close</Button>
          {phase === "live" ? (
            <>
              <Button variant="ghost" onClick={toggleMute}>
                {muted ? "Unmute" : "Mute"}
              </Button>
              <Button onClick={hangUp}>Hang up</Button>
            </>
          ) : (
            <Button onClick={start} disabled={phase === "connecting"}>
              {phase === "connecting"
                ? "Connecting…"
                : phase === "ended" || phase === "failed"
                  ? "Call again"
                  : "Start call"}
            </Button>
          )}
        </>
      }
    >
      <div className="row" style={{ gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
        <Badge tone={phase === "live" ? "ok" : phase === "failed" ? "err" : "neutral"} dot>
          {phase === "live" ? "live" : phase}
        </Badge>
        <span className="small subtle mono">dialling {did}</span>
        {session && <span className="small subtle mono">room {session.room}</span>}
      </div>

      {error && <Notice tone="err">{error}</Notice>}

      {phase === "idle" && (
        <Notice tone="info">
          This calls <strong>{agentName}</strong> the way {did} would, resolving
          the same published version, providers and voice — with no PBX in the
          path. Your browser will ask for the microphone.
        </Notice>
      )}

      <div
        className="card"
        style={{ minHeight: 180, maxHeight: 320, overflowY: "auto", marginTop: 10 }}
      >
        {lines.length === 0 ? (
          <p className="subtle small" style={{ margin: 0 }}>
            {phase === "live"
              ? "Connected. Say something — the agent speaks first."
              : "The conversation appears here as it happens."}
          </p>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {lines.map((line, index) => (
              <div key={index}>
                <span
                  className="small"
                  style={{ fontWeight: 600, color: line.who === "you" ? "var(--accent)" : undefined }}
                >
                  {line.who === "you" ? "You" : agentName}
                </span>
                <div className={line.final ? undefined : "subtle"} style={{ fontSize: 13 }}>
                  {line.text}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Where the agent's audio elements are attached. */}
      <div ref={audioRef} />

      <p className="subtle small" style={{ marginTop: 10, marginBottom: 0 }}>
        A browser sends wideband audio; a phone call is 8 kHz. Speech recognition
        that sounds fine here can still be wrong on a real call, so this checks
        configuration and latency — not transcription quality.
      </p>
    </Dialog>
  );
}
