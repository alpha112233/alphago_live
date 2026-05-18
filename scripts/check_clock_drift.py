#!/usr/bin/env python3
"""Startup clock-drift check for TOTP-based broker auto-logins.

Dhan, Zerodha, Fyers, Kotak, Groww, Motilal Oswal and others accept TOTP
codes within a 30-second window. If the host clock drifts more than 30s
from real time, every TOTP-based auto-login fails with an opaque
"Invalid TOTP" error — even though the seed is correct.

This script queries a public NTP server at startup, prints the drift,
and warns clearly when it crosses the broker-tolerance threshold. Pure
stdlib — no dependencies. Designed to fail silently if NTP is
unreachable (offline dev box, restricted egress); call site uses
`|| true`.

Background: Linux containers share the host kernel clock, so this check
verifies the host clock indirectly. The fix when drift is high is
always on the host (`sudo timedatectl set-ntp true` on systemd hosts).
"""
import socket
import struct
import sys
import time

NTP_SERVERS = ("pool.ntp.org", "time.google.com", "time.cloudflare.com")
TIMEOUT_S = 3.0
NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 and 1970-01-01

WARN_THRESHOLD_S = 5
CRITICAL_THRESHOLD_S = 30


def query_ntp(host: str) -> float | None:
    """Return the server's transmit timestamp as Unix epoch, or None on failure."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(TIMEOUT_S)
        # NTPv3 client request (LI=0, VN=3, Mode=3) + 47 zero bytes.
        sock.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        msg, _ = sock.recvfrom(1024)
        # Transmit Timestamp is 32-bit words 10-11; we use the integer seconds.
        secs = struct.unpack("!12I", msg)[10] - NTP_EPOCH_OFFSET
        return float(secs)
    except Exception:
        return None
    finally:
        if sock is not None:
            sock.close()


def main() -> int:
    ntp_time: float | None = None
    used_host = ""
    for host in NTP_SERVERS:
        ntp_time = query_ntp(host)
        if ntp_time is not None:
            used_host = host
            break

    if ntp_time is None:
        print("[Clock] NTP unreachable from this host; cannot verify drift.")
        return 0  # not a failure — degraded networks shouldn't block startup

    local = time.time()
    drift = local - ntp_time
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    print(
        f"[Clock] Local: {time.strftime(fmt, time.gmtime(local))}  "
        f"NTP({used_host}): {time.strftime(fmt, time.gmtime(ntp_time))}  "
        f"Drift: {drift:+.2f}s"
    )

    abs_drift = abs(drift)
    if abs_drift >= CRITICAL_THRESHOLD_S:
        print(
            f"[Clock] CRITICAL: drift {drift:+.0f}s exceeds the {CRITICAL_THRESHOLD_S}s "
            "broker tolerance. TOTP-based auto-logins (Dhan/Zerodha/Fyers/Kotak/"
            "Groww/MotilalOswal) WILL fail with 'Invalid TOTP'."
        )
        print(
            "[Clock]    Fix on systemd hosts: `sudo timedatectl set-ntp true`. "
            "Containers inherit the host clock — fix the host, not the container."
        )
    elif abs_drift >= WARN_THRESHOLD_S:
        print(
            f"[Clock] WARNING: drift {drift:+.0f}s is within tolerance but trending "
            "wrong. Verify NTP on the host before it crosses 30s."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
