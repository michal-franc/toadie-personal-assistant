# Tailscale Peer Verification

Access control for the Claude Watch server using Tailscale node identity.

## Overview

When enabled, the server verifies each connecting peer against a list of allowed Tailscale node names. This ensures only explicitly trusted devices can access the server, even if they're on the same Tailscale network.

Tailscale already provides encrypted WireGuard tunnels between devices — this feature adds **caller-level restriction** on top of that.

## How It Works

1. An HTTP or WebSocket request arrives
2. The server extracts the peer IP address
3. Localhost connections (127.0.0.1, ::1) are always allowed
4. For other IPs, the server queries Tailscale's local API via the `tailscaled` Unix socket (`/var/run/tailscale/tailscaled.sock`)
5. The API returns the Tailscale node identity (hostname) for that IP
6. The hostname is checked against the allowlist
7. Results are cached for 5 minutes to avoid repeated socket calls

## Configuration

Set the `TAILSCALE_ALLOWED_NODES` environment variable with a comma-separated list of Tailscale node names:

```bash
TAILSCALE_ALLOWED_NODES="michal-phone,mfranc-MS-7E06" ./server.py /path/to/project
```

Or in a systemd unit file:

```ini
[Service]
Environment=TAILSCALE_ALLOWED_NODES=michal-phone,mfranc-MS-7E06
```

### Finding Node Names

Run `tailscale status` to see all nodes on your tailnet:

```
$ tailscale status
100.x.x.x    mfranc-MS-7E06    user@...  linux   -
100.x.x.x    michal-phone      user@...  android -
```

Use the names from the first column after the IP (e.g., `mfranc-MS-7E06`, `michal-phone`).

## Behavior Matrix

| Scenario | Result |
|---|---|
| `TAILSCALE_ALLOWED_NODES` not set or empty | All connections allowed (feature disabled) |
| `TAILSCALE_ALLOWED_NODES="phone,desktop"` | Only those nodes + localhost allowed |
| Localhost connection (127.0.0.1, ::1) | Always allowed (dashboard, permission hook) |
| Tailscale not running / socket missing | Connections denied (fail-closed when feature is enabled) |
| Cached peer (within 5 minutes) | Served from cache, no socket call |
| Unknown IP (not on tailnet) | Denied |

## Files

- `tailscale_auth.py` — Peer verification module (`verify_peer()` function)
- `server.py` — Integration points in `do_GET()`, `do_POST()`, and `websocket_handler()`

## Security Notes

- **Fail-open when disabled**: If the env var is not set, all connections are allowed. This keeps local development working without Tailscale.
- **Fail-closed when enabled**: If Tailscale is enabled but the socket is unavailable or the peer can't be identified, connections are denied.
- **Localhost always allowed**: The permission hook (`permission_hook.py`) and dashboard connect from localhost and must not be blocked.
- **Node names are case-insensitive**: Comparison is done in lowercase.
- **Cache TTL is 5 minutes**: A denied node won't be rechecked for 5 minutes after being blocked. To force a recheck, restart the server.

## Related

- [Issue #7](https://github.com/michal-franc/claude-watch/issues/7) — Original request for encrypted/secured communication
- Tailscale provides the encryption layer (WireGuard); this feature adds authorization
