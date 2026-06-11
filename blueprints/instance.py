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

Also exposes the per-container audit log (#88) at /api/instance/audit
+ a CSV export at /api/instance/audit/export.
"""
from __future__ import annotations

import csv
import io
import logging
import os

from database.instance_config_db import get_config
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request, session

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
    ipv4_pool_csv = get_config("EGRESS_V4_POOL_IPS")
    ipv4_pool = [ip.strip() for ip in ipv4_pool_csv.split(",") if ip.strip()] if ipv4_pool_csv else []
    return {
        "subdomain": subdomain,
        "url": host_server or (f"https://{subdomain}.hostingsol.alphaquark.in" if subdomain else ""),
        "ipv6": os.getenv("CLIENT_IPV6") or "",
        "ipv4_primary": get_config("EGRESS_V4_PRIMARY_IP"),
        "ipv4_secondary": get_config("EGRESS_V4_SECONDARY_IP"),
        # If only one entry in pool == primary, it's truly dedicated.
        # If >1, it's pool-routing (V1 mode); we don't ship that anymore
        # but the field stays consistent.
        "ipv4_pool": ipv4_pool,
        "is_ipv4_dedicated": len(ipv4_pool) <= 1 and bool(get_config("EGRESS_V4_PRIMARY_IP")),
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
    """Container uptime, image SHA, version of running code.

    Uptime is measured from gunicorn (PID 1 inside the container)'s start
    time — NOT the kernel boot time. With network_mode: host the container
    shares the host's net namespace, so psutil.boot_time() reports host
    boot which is misleading.
    """
    info = {
        "uptime_seconds": 0,
        "boot_time_utc": None,
        "image_sha": _read_image_sha(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "",
    }
    # Container/process uptime: PID 1 starttime from /proc/1/stat (field 22,
    # in clock ticks since system boot). Add to system boot time.
    try:
        import psutil
        boot = psutil.boot_time()
        clk_tck = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/1/stat") as f:
            stat_line = f.read()
        # PID 1's comm may contain spaces — split safely from the right of ')'
        rparen = stat_line.rfind(")")
        rest = stat_line[rparen + 1:].split()
        start_ticks = int(rest[19])  # field 22 in proc(5), index 19 after stripping pid+comm
        container_start_ts = boot + (start_ticks / clk_tck)
        info["boot_time_utc"] = datetime.fromtimestamp(container_start_ts, tz=timezone.utc).isoformat()
        info["uptime_seconds"] = max(0, int(datetime.now(timezone.utc).timestamp() - container_start_ts))
    except Exception as e:
        logger.warning(f"container uptime calc failed: {e}")
        # Fallback to psutil.boot_time (will be host-boot under network_mode: host)
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
                if "docker" in line:
                    parts = line.strip().split("/")
                    cid = parts[-1].replace("docker-", "").replace(".scope", "")
                    if len(cid) >= 12:
                        info["container_id"] = cid[:12]
                        break
    except Exception:
        pass
    return info


def _read_image_sha() -> str:
    """Image SHA / version string. Reads multiple sources in order:
      1) OPENALGO_VERSION env (set if compose passes it via environment:)
      2) /app/.version file (baked at image build time — preferred)
      3) /etc/openalgo-version (alternative bake location)
    Returns empty string if none present."""
    v = (os.getenv("OPENALGO_VERSION") or "").strip()
    if v:
        return v
    for path in ("/app/.version", "/etc/openalgo-version"):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return ""


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


# ---------------------------------------------------------------------------
# Audit log endpoints (#88)
# ---------------------------------------------------------------------------

@instance_bp.route("/api/instance/audit/verify", methods=["GET"])
def instance_audit_verify_endpoint():
    """Walk the audit chain from genesis forward, recomputing each row's
    hash. A break (row tampered with, or one deleted) is reported with
    the row id where the chain first diverges + a short reason.

    Customer-session-authed. Idempotent + read-only."""
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    try:
        from utils.audit import verify_chain, head_hash
        result = verify_chain()
        result.update(head_hash())
        return jsonify({"status": "success", "data": result})
    except Exception as e:
        logger.exception("audit verify failed")
        return jsonify({"status": "error", "message": str(e)}), 500


@instance_bp.route("/api/instance/audit/head", methods=["GET"])
def instance_audit_head_endpoint():
    """Most recent row's hash + id + total row count. Customer saves
    this externally; if a future verify shows the same head_hash, no row
    in between has been tampered with."""
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    try:
        from utils.audit import head_hash
        return jsonify({"status": "success", "data": head_hash()})
    except Exception as e:
        logger.exception("audit head failed")
        return jsonify({"status": "error", "message": str(e)}), 500


@instance_bp.route("/api/instance/audit", methods=["GET"])
def instance_audit_endpoint():
    """List recent audit rows for this container. Customer-session-authed.

    Query params:
      limit=200 (max 5000)
      actor=customer|admin|system|broker
      action_prefix=order. | broker. | session.
      since=ISO-8601
    """
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    try:
        from utils.audit import query_recent
        rows = query_recent(
            limit=int(request.args.get("limit", "200")),
            actor=request.args.get("actor") or None,
            action_prefix=request.args.get("action_prefix") or None,
            since_iso=request.args.get("since") or None,
        )
        return jsonify({"status": "success", "data": rows, "count": len(rows)})
    except Exception as e:
        logger.exception("audit query failed")
        return jsonify({"status": "error", "message": str(e)}), 500


@instance_bp.route("/api/instance/audit/export", methods=["GET"])
def instance_audit_export_endpoint():
    """CSV export of the audit log for offline compliance / archival.
    Same filter query params as /api/instance/audit."""
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    try:
        from utils.audit import query_recent
        rows = query_recent(
            limit=int(request.args.get("limit", "5000")),
            actor=request.args.get("actor") or None,
            action_prefix=request.args.get("action_prefix") or None,
            since_iso=request.args.get("since") or None,
        )
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "ts", "actor", "action", "resource",
                    "before", "after", "src_ip", "status", "note"])
        for r in rows:
            w.writerow([
                r.get("id"), r.get("ts"), r.get("actor"), r.get("action"),
                r.get("resource") or "",
                _stringify(r.get("before")),
                _stringify(r.get("after")),
                r.get("src_ip") or "", r.get("status") or "", r.get("note") or "",
            ])
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"audit-{(os.getenv('HSOL_SUBDOMAIN') or 'instance')}-{ts}.csv"
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.exception("audit export failed")
        return jsonify({"status": "error", "message": str(e)}), 500


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            import json
            return json.dumps(v, default=str)[:2000]
        except Exception:
            return str(v)[:2000]
    return str(v)[:2000]
