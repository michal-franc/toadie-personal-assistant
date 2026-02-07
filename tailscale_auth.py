"""
Tailscale peer verification for server access control.

Verifies connecting peers via Tailscale's local API (tailscaled Unix socket).
Only explicitly allowed nodes can access the server when enabled.

Configuration:
    TAILSCALE_ALLOWED_NODES env var - comma-separated hostnames (e.g. "michal-phone,mfranc-MS-7E06")
    If not set or empty, verification is disabled (all connections allowed).
"""

import http.client
import json
import os
import socket
import time

from logger import logger

# Cache: ip -> (hostname, allowed, timestamp)
_peer_cache = {}
_CACHE_TTL = 300  # 5 minutes

TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"

_LOCALHOST_ADDRS = {"127.0.0.1", "::1"}


def _get_allowed_nodes():
    """Parse TAILSCALE_ALLOWED_NODES env var into a set of lowercase hostnames."""
    raw = os.environ.get("TAILSCALE_ALLOWED_NODES", "").strip()
    if not raw:
        return None  # Feature disabled
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection over a Unix domain socket."""

    def __init__(self, socket_path):
        # Host must be "local-tailscaled.sock" — tailscaled rejects other Host headers
        super().__init__("local-tailscaled.sock")
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)
        self.sock.settimeout(5)


def _query_tailscale_whois(ip):
    """Query Tailscale local API for peer identity. Returns hostname or None."""
    try:
        conn = _UnixHTTPConnection(TAILSCALE_SOCKET)
        conn.request("GET", f"/localapi/v0/whois?addr={ip}:1")
        resp = conn.getresponse()
        if resp.status != 200:
            logger.warning(f"[TAILSCALE] whois returned {resp.status} for {ip}")
            return None
        data = json.loads(resp.read())
        node = data.get("Node", {})
        hostname = node.get("ComputedName", "") or node.get("Name", "")
        # Strip trailing dot and domain suffix (e.g. "myhost.tailnet-name.ts.net." -> "myhost")
        hostname = hostname.split(".")[0].lower()
        return hostname
    except FileNotFoundError:
        logger.warning("[TAILSCALE] Socket not found - is Tailscale running?")
        return None
    except Exception as e:
        logger.warning(f"[TAILSCALE] whois error for {ip}: {e}")
        return None


def verify_peer(ip):
    """Check if a peer IP is allowed to connect.

    Returns True if allowed, False if denied.

    Behavior:
    - If TAILSCALE_ALLOWED_NODES is not set: always returns True (feature disabled)
    - Localhost (127.0.0.1, ::1): always allowed
    - Otherwise: queries Tailscale local API and checks against allowlist
    - Results cached for 5 minutes
    """
    allowed_nodes = _get_allowed_nodes()
    if allowed_nodes is None:
        return True  # Feature disabled

    if ip in _LOCALHOST_ADDRS:
        return True

    # Check cache
    cached = _peer_cache.get(ip)
    if cached:
        hostname, allowed, ts = cached
        if time.time() - ts < _CACHE_TTL:
            return allowed

    # Query Tailscale
    hostname = _query_tailscale_whois(ip)
    if hostname is None:
        # Can't verify - fail closed when feature is enabled
        _peer_cache[ip] = (None, False, time.time())
        logger.warning(f"[TAILSCALE] DENIED {ip} (could not resolve identity)")
        return False

    allowed = hostname in allowed_nodes
    _peer_cache[ip] = (hostname, allowed, time.time())

    if allowed:
        logger.info(f"[TAILSCALE] ALLOWED {ip} (node: {hostname})")
    else:
        logger.warning(f"[TAILSCALE] DENIED {ip} (node: {hostname}, not in allowlist)")

    return allowed
