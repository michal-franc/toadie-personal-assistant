# Public Viewer Tunnel

Read-only public access to the Claude Watch viewer via Cloudflare Tunnel.

## Overview

The viewer page (`/viewer`) shows a live animated creature and chat stream. To share it publicly without exposing the full server, a Cloudflare Tunnel routes `toadie.mfranc.com` through a Caddy reverse proxy that only allows the viewer page and WebSocket.

## Architecture

```
Internet
  │
  ▼
Cloudflare Edge (Access: email OTP auth)
  │
  ▼
cloudflared (runs on server, connects to localhost)
  │
  ▼
Caddy (:8089)
  ├── GET /   → reverse_proxy localhost:5566/viewer
  ├── GET /ws → reverse_proxy localhost:5567 (WebSocket)
  └── *       → 403 Forbidden
  │
  ▼
Claude Watch server (:5566 HTTP, :5567 WS)
```

## What's Exposed

| Route | Target | Content |
|-------|--------|---------|
| `GET /` | `/viewer` on `:5566` | Viewer HTML page |
| `GET /ws` | `:5567` WebSocket | Live state, chat, tool events |

## What's Blocked

Everything else: `/api/*`, `/dashboard`, `POST` endpoints, audio files, config, permission system. Caddy returns 403 for any route not in the allowlist.

## Security Layers

1. **Cloudflare Access** — Visitors must authenticate via email one-time PIN before reaching the tunnel. Configured in the Cloudflare Zero Trust dashboard.

2. **Caddy path restriction** — Only two routes are proxied. All API endpoints, the dashboard, and write operations are blocked at the proxy level.

3. **Tailscale peer verification** — The `cloudflared` daemon connects to the server via `localhost`. The server's `verify_peer()` function (in `tailscale_auth.py`) always allows localhost connections, so tunnel traffic passes through. Direct access to the server from the internet remains blocked by Tailscale.

No changes to `server.py` were needed — the existing localhost allowance in Tailscale auth handles tunnel traffic naturally.

## Auto-Connect

When the viewer is accessed via a non-localhost origin (e.g., `https://toadie.mfranc.com`), it auto-derives the WebSocket URL from the page origin (`wss://toadie.mfranc.com/ws`) and connects automatically, skipping the manual connect screen.

On localhost, the connect screen still appears as before.

## Files

- `tunnel/Caddyfile` — Caddy reverse proxy configuration
- `tunnel/README.md` — Setup instructions for cloudflared + Cloudflare Access
- `viewer.html` — Auto-detection of WebSocket URL from page origin

## Setup

See [`tunnel/README.md`](../../tunnel/README.md) for step-by-step instructions.

## Related

- [Tailscale Auth](tailscale-auth.md) — Peer verification that protects the server
- `tailscale_auth.py:89` — `verify_peer()` localhost allowance
