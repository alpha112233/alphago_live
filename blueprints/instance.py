# blueprints/instance.py
"""Customer-facing 'Your Infrastructure' info — runtime metadata about
THIS customer's dedicated trading instance.

Surfaces what each customer owns (network identity, compute, storage,
active broker, uptime, image version) so the dashboard can clearly
communicate that they're operating their own dedicated container, not
sharing a service.

All info is read live from the customer's own container (env + psutil +
broker_creds_db). Nothing reaches outside their own runtime, so any
operator action shows up here without trust assumptions.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, session

logger = logging.getLogger(__name__)

instance_bp = Blueprint("instance", __name__)


@instance_bp.route("/api/instance/info", methods=["GET"])
def instance_info_endpoint():
    """Return runtime metadata for the customer's dedicated container.

    Auth: customer session (must be logged in)."""
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    try:
        return jsonify({"status": "success", "data": _collect_instance_info()})
    except Exception as e:
        logger.exception("instance/info collection failed")
        return jsonify({"status": "error", "message": f"info collection failed: {e}"}), 500


def _collect_instance_info() -> dict:
    """All the runtime metadata — split into focused sub-dicts."""
    return {
        "network": _network_identity(),
        "compute": _compute_info(),
        "storage": _storage_info(),
        "broker": _active_broker_info(),
        "runtime": _runtime_info(),
        "data_sovereignty": _data_sovereignty_info(),
    }


def _network_identity() -> dict:
    """Subdomain, IPv6, IPv4 — the customer's network identity.
    All come from per-customer env vars written by the provisioner."""
    subdomain = (os.getenv("HSOL_SUBDOMAIN") or "").strip()
    host_server = (os.getenv("HOST_SERVER") or "").strip()
    ipv4_pool_csv = (os.getenv("EGRESS_V4_POOL_IPS") or "").strip()
    ipv4_pool = [ip.strip() for ip in ipv4_pool_csv.split(",") if ip.strip()] if ipv4_pool_csv else []
    return {
        "subdomain": subdomain,
        "url": host_server or (f"https://{subdomain}.hostingsol.alphaquark.in" if subdomain else ""),
        "ipv6": os.getenv("CLIENT_IPV6") or "",
        "ipv4_primary": os.getenv("EGRESS_V4_PRIMARY_IP") or "",
        "ipv4_secondary": os.getenv("EGRESS_V4_SECONDARY_IP") or "",
        # If only one entry in pool == primary, it's truly dedicated.
        # If >1, it's pool-routing (V1 mode); we don't ship that anymore
        # but the field stays consistent.
        "ipv4_pool": ipv4_pool,
        "is_ipv4_dedicated": len(ipv4_pool) <= 1 and bool(os.getenv("EGRESS_V4_PRIMARY_IP")),
    }


def _compute_info() -> dict:
    """CPU + RAM as seen INSIDE the container — i.e., what the customer
    actually has available. Reads docker/cgroup limits when set,
    otherwise the host's view."""
    info = {
        "cpu_count_host": 0,
        "cpu_limit_cores": None,  # None = no explicit limit
        "mem_total_bytes": 0,
        "mem_used_bytes": 0,
        "mem_used_pct": 0.0,
        "load_1m": 0.0,
    }
    try:
        import psutil
        info["cpu_count_host"] = psutil.cpu_count(logical=True) or 0
        vm = psutil.virtual_memory()
        info["mem_total_bytes"] = int(vm.total)
        info["mem_used_bytes"] = int(vm.used)
        info["mem_used_pct"] = round(vm.percent, 1)
        info["load_1m"] = round(psutil.getloadavg()[0], 2)
    except Exception as e:
        logger.warning(f"compute info psutil failed: {e}")

    # cgroup v2 cpu.max — "<quota> <period>". If quota=max, no limit.
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().strip().split()
        if quota != "max":
            info["cpu_limit_cores"] = round(int(quota) / int(period), 2)
    except Exception:
        pass
    # cgroup v1 fallback
    if info["cpu_limit_cores"] is None:
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
                quota = int(f.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
                period = int(f.read().strip())
            if quota > 0 and period > 0:
                info["cpu_limit_cores"] = round(quota / period, 2)
        except Exception:
            pass
    return info


def _storage_info() -> dict:
    """Disk usage on the /app/db volume — customer's data lives here."""
    info = {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "used_pct": 0.0}
    try:
        import psutil
        # Customer DB lives under /app/db; report the volume that holds it.
        for candidate in ("/app/db", "/app", "/"):
            if os.path.exists(candidate):
                du = psutil.disk_usage(candidate)
                info["total_bytes"] = int(du.total)
                info["used_bytes"] = int(du.used)
                info["free_bytes"] = int(du.free)
                info["used_pct"] = round(du.percent, 1)
                info["path"] = candidate
                break
    except Exception as e:
        logger.warning(f"storage info psutil failed: {e}")
    return info


def _active_broker_info() -> dict:
    """Which broker the customer currently has active + when last activated.
    Read from broker_creds_db (encrypted at rest with their pepper)."""
    info = {"active_broker": None, "last_activated_at": None, "last_auth_at": None,
            "saved_brokers": []}
    try:
        from database.broker_creds_db import db_session, BrokerCreds
        with db_session() as s:
            for row in s.query(BrokerCreds).all():
                if row.status == "active":
                    info["active_broker"] = row.broker
                    info["last_activated_at"] = (row.last_activated_at.isoformat()
                                                 if row.last_activated_at else None)
                    info["last_auth_at"] = (row.last_auth_at.isoformat()
                                            if row.last_auth_at else None)
                info["saved_brokers"].append({
                    "broker": row.broker,
                    "status": row.status,
                    "last_activated_at": (row.last_activated_at.isoformat()
                                          if row.last_activated_at else None),
                })
    except Exception as e:
        logger.warning(f"broker info read failed: {e}")
    return info


def _runtime_info() -> dict:
    """Container uptime, image SHA, version of running code."""
    info = {
        "uptime_seconds": 0,
        "boot_time_utc": None,
        "image_sha": os.getenv("OPENALGO_VERSION") or "",
        "hostname": os.uname().nodename if hasattr(os, "uname") else "",
    }
    try:
        import psutil
        boot = psutil.boot_time()
        info["boot_time_utc"] = datetime.fromtimestamp(boot, tz=timezone.utc).isoformat()
        info["uptime_seconds"] = int(datetime.now(timezone.utc).timestamp() - boot)
    except Exception:
        pass

    # Container ID from /proc/self/cgroup (works in both v1 and v2 layouts).
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                # cgroupv1: 'X:name:/docker/<container_id>'
                # cgroupv2: '0::/docker/<container_id>' or
                # '0::/system.slice/docker-<id>.scope'
                if "docker" in line:
                    parts = line.strip().split("/")
                    cid = parts[-1].replace("docker-", "").replace(".scope", "")
                    if len(cid) >= 12:
                        info["container_id"] = cid[:12]
                        break
    except Exception:
        pass
    return info


def _data_sovereignty_info() -> dict:
    """What's encrypted with the customer's own key, what isn't.
    Crucial for the compliance / 'your dedicated instance' story."""
    pepper = os.getenv("API_KEY_PEPPER") or ""
    return {
        # Customer's Fernet key is derived from this 64-char hex secret;
        # written into their openalgo.env at provision time and never
        # leaves their container. Without it, NO ONE can decrypt their
        # broker creds — not us, not another customer.
        "encryption_key_unique_per_instance": bool(pepper),
        "encryption_key_present_first8": pepper[:8] + "..." if pepper else "",
        "encrypted_at_rest": [
            "broker_api_key (per-broker)",
            "broker_api_secret / refresh_token",
            "broker_trading_password (when stored for hands-free renewal)",
            "totp_seed",
            "session_token",
        ],
        "stored_per_container_db": [
            "/app/db/openalgo.db (Auth + sessions)",
            "/app/db/broker_creds.db (encrypted broker credentials)",
            "/app/db/distribution.db (received signals + dispatch history)",
        ],
    }
