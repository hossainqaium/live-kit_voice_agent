#!/usr/bin/env python3
"""Pre-flight port conflict check.

Reads the ports Docker Compose will actually publish and reports which are
already bound on the host. Run this before `compose up`: a collision otherwise
surfaces as "Bind for 0.0.0.0:5432 failed: port is already allocated" after
some containers have already started, leaving the stack half-up.

Usage:
    python3 scripts/preflight.py                 # check
    python3 scripts/preflight.py --suggest       # check and propose free ports

Exit status is 1 when a conflict is found, so it can gate a Make target.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"

# Ports whose value is dictated by an external system rather than our
# preference. Moving these breaks the thing that connects to them, so a
# conflict here needs a human decision, not an automatic reassignment.
PROTOCOL_PINNED = {
    5060: "SIP signalling — PBXs are conventionally configured to reach 5060",
}


def compose_published_ports() -> list[tuple[str, int, str]]:
    """Return (service, host_port, protocol) for every published port."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            *(["--env-file", str(ENV_FILE)] if ENV_FILE.exists() else []),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        print(f"error: docker compose config failed\n{result.stderr}", file=sys.stderr)
        raise SystemExit(2)

    config = json.loads(result.stdout)
    published: list[tuple[str, int, str]] = []

    for service, definition in (config.get("services") or {}).items():
        for port in definition.get("ports") or []:
            protocol = port.get("protocol", "tcp")
            published_spec = port.get("published")
            if published_spec is None:
                continue
            # A range arrives as "50000-50049"; expand it so every port is
            # checked, since a single busy port in the range fails the bind.
            spec = str(published_spec)
            if "-" in spec:
                start, end = (int(part) for part in spec.split("-", 1))
                published.extend((service, p, protocol) for p in range(start, end + 1))
            else:
                published.append((service, int(spec), protocol))

    return published


def is_bound(port: int, protocol: str) -> bool:
    """Whether binding this port on all interfaces would fail."""
    family = socket.AF_INET
    kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    with socket.socket(family, kind) as sock:
        # No SO_REUSEADDR: we want to know whether Docker's bind would fail,
        # and Docker does not set it either.
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def owner(port: int, protocol: str) -> str:
    """Best-effort identification of what holds a port."""
    flag = "-iUDP" if protocol == "udp" else "-iTCP"
    args = ["lsof", "-nP", f"{flag}:{port}"]
    if protocol == "tcp":
        args.append("-sTCP:LISTEN")
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines()[1:] if line.strip()]
    if not lines:
        return "unknown"
    fields = lines[0].split()
    process = fields[0] if fields else "unknown"

    if process.startswith("com.docke"):
        # docker-proxy hides the real owner; ask Docker which container it is.
        ps = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in ps.stdout.splitlines():
            name, _, ports = line.partition("|")
            if f":{port}->" in ports:
                return f"container {name}"
        return "docker (container unknown)"
    return process


def find_free(preferred: int, protocol: str, taken: set[int]) -> int:
    """Next free port at or above ``preferred``, skipping ``taken``."""
    candidate = preferred
    while candidate < 65535:
        if candidate not in taken and not is_bound(candidate, protocol):
            return candidate
        candidate += 1
    raise RuntimeError("no free port found")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suggest", action="store_true", help="propose a free port for each conflict"
    )
    args = parser.parse_args()

    published = compose_published_ports()
    if not published:
        print("no published ports found in the compose file")
        return 0

    # Only the containers of *this* project may legitimately hold our ports;
    # a running stack would otherwise report every one of its own ports as a
    # conflict.
    ps = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "--format", "{{.Service}}"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    ours_running = {line.strip() for line in ps.stdout.splitlines() if line.strip()}

    conflicts: list[tuple[str, int, str, str]] = []
    checked = 0

    for service, port, protocol in published:
        checked += 1
        if not is_bound(port, protocol):
            continue
        held_by = owner(port, protocol)
        if held_by.startswith("container voice-agent-platform") or service in ours_running:
            continue  # our own already-running stack
        conflicts.append((service, port, protocol, held_by))

    print(
        f"checked {checked} published port(s) across {len({s for s, _, _ in published})} service(s)"
    )

    if not conflicts:
        print("no conflicts — every published port is free")
        return 0

    print(f"\n{len(conflicts)} conflict(s):\n")
    taken: set[int] = {p for _, p, _ in published}
    for service, port, protocol, held_by in conflicts:
        print(f"  {service}: {port}/{protocol} is held by {held_by}")
        if port in PROTOCOL_PINNED:
            print(f"      NOTE: {PROTOCOL_PINNED[port]}")
            print("      Moving it means reconfiguring the PBX to match.")
        elif args.suggest:
            free = find_free(port + 1, protocol, taken)
            taken.add(free)
            print(f"      suggestion: use {free} instead")

    if not args.suggest:
        print("\nRe-run with --suggest to get free alternatives.")
    print("\nSet host ports in .env — see the 'Ports on the host' block.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
