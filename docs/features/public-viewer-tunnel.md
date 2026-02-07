# Public Viewer Tunnel

Read-only public access to the Claude Watch viewer via Cloudflare Tunnel.

## Overview

The viewer page (`/viewer`) shows a live animated creature and chat stream. To share it publicly without exposing the full server, a Cloudflare Tunnel with path-based ingress rules routes only `/viewer` and `/ws` to the server. Everything else returns 403.

## Architecture

```
Internet
  │
  ▼
Cloudflare Edge (Access: email OTP auth)
  │
  ▼
cloudflared (runs on server, connects to localhost)
  ├── /viewer → localhost:5566 (HTTP server)
  ├── /ws     → localhost:5567 (WebSocket server)
  └── *       → 403 Forbidden
```

## What's Exposed

| Route | Target | Content |
|-------|--------|---------|
| `/viewer` | `:5566` | Viewer HTML page |
| `/ws` | `:5567` | WebSocket (live state, chat, tool events) |

## What's Blocked

Everything else: `/api/*`, `/dashboard`, `POST` endpoints, audio files, config, permission system. cloudflared returns 403 for any path not matching the ingress rules.

## Security Layers

1. **Cloudflare Access** — Visitors must authenticate via email one-time PIN before reaching the tunnel. Configured in the Cloudflare Zero Trust dashboard.

2. **cloudflared path restriction** — Only two paths are routed to the server. All API endpoints, the dashboard, and write operations are blocked by the catch-all 403 ingress rule.

3. **Tailscale peer verification** — The `cloudflared` daemon connects to the server via `localhost`. The server's `verify_peer()` function (in `tailscale_auth.py`) always allows localhost connections, so tunnel traffic passes through. Direct access to the server from the internet remains blocked by Tailscale.

No changes to `server.py` were needed — the existing localhost allowance in Tailscale auth handles tunnel traffic naturally.

## Auto-Connect

When the viewer is accessed via a non-localhost origin (e.g., `https://toadie.mfranc.com/viewer`), it auto-derives the WebSocket URL from the page origin (`wss://toadie.mfranc.com/ws`) and connects automatically, skipping the manual connect screen.

On localhost, the connect screen still appears as before.

## Files

- `tunnel/README.md` — Setup instructions for cloudflared + Cloudflare Access
- `viewer.html` — Auto-detection of WebSocket URL from page origin

## Setup

See [`tunnel/README.md`](../../tunnel/README.md) for step-by-step instructions.

## Related

- [Tailscale Auth](tailscale-auth.md) — Peer verification that protects the server
- `tailscale_auth.py:89` — `verify_peer()` localhost allowance
