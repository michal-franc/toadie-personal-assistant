# Public Viewer Tunnel Setup

Expose the read-only viewer at `toadie.mfranc.com` via Cloudflare Tunnel.

## Architecture

```
Internet → Cloudflare Access (auth) → cloudflared → server
```

cloudflared ingress rules restrict access to two paths:
- `/viewer` → `localhost:5566` (viewer HTML page)
- `/ws` → `localhost:5567` (WebSocket)

Everything else returns 403.

## Prerequisites

- `cloudflared` — [install](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- A Cloudflare account with the domain configured

## 1. Create the Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create toadie
cloudflared tunnel route dns toadie toadie.mfranc.com
```

This creates a tunnel named `toadie` and points the DNS record to it.

## 2. Configure cloudflared

Create or edit `~/.cloudflared/config.yml`:

```yaml
tunnel: toadie
credentials-file: ~/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: toadie.mfranc.com
    path: ^/viewer$
    service: http://localhost:5566
  - hostname: toadie.mfranc.com
    path: ^/ws$
    service: http://localhost:5567
  - service: http_status:403
```

Replace `<tunnel-id>` with the UUID from step 1.

## 3. Set Up Cloudflare Access (Authentication)

In the Cloudflare Zero Trust dashboard:

1. Go to **Access → Applications → Add an application**
2. Type: Self-hosted
3. Application domain: `toadie.mfranc.com`
4. Add a policy (e.g., "Allow email OTP"):
   - Action: Allow
   - Include: Emails ending in your domain, or specific email addresses
   - Authentication: One-time PIN
5. Save

This ensures visitors must authenticate via email before reaching the viewer.

## 4. Start the Tunnel

```bash
cloudflared tunnel run toadie
```

## 5. Verify

```bash
# Remote: should redirect to Cloudflare Access login
curl -I https://toadie.mfranc.com/viewer

# After authenticating, /viewer should return the page
# /api/history, /dashboard, etc. should return 403
```

## Optional: systemd Service

```ini
# /etc/systemd/system/cloudflared-toadie.service
[Unit]
Description=Cloudflare Tunnel for toadie
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared tunnel run toadie
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cloudflared-toadie
```

## Security Layers

1. **Cloudflare Access** — email/OTP authentication at the edge
2. **cloudflared path restriction** — only `/viewer` and `/ws` are routed; everything else returns 403
3. **Tailscale auth** — `cloudflared` connects via localhost, which `verify_peer()` always allows; the rest of the server remains protected by Tailscale
