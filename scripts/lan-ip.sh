#!/usr/bin/env sh
# Print this machine's primary LAN IPv4 address.
#
# livekit-sip must advertise an address the PBX can actually route to. Inside
# Docker it only knows its bridge address (172.x.y.z), which a PBX on another
# host cannot reach — signalling would succeed and audio would silently fail.
set -eu

case "$(uname -s)" in
  Darwin)
    iface=$(route get default 2>/dev/null | awk '/interface:/ {print $2; exit}')
    [ -n "${iface:-}" ] && ipconfig getifaddr "$iface" 2>/dev/null && exit 0
    # Fall back to the first non-loopback address with a default route.
    ifconfig 2>/dev/null | awk '/inet /{if ($2 != "127.0.0.1") {print $2; exit}}'
    ;;
  Linux)
    ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}'
    ;;
  *)
    echo "unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac
