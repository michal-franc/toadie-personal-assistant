# Public Viewer Tunnel Setup

Expose the read-only viewer at `toadie.mfranc.com` via Cloudflare Tunnel + Caddy.

## Architecture

```
Internet → Cloudflare Access (auth) → cloudflared → Caddy (:8089) → server
```

Caddy only exposes two routes:
- `GET /` → viewer page (proxied to `localhost:5566/viewer`)
- `GET /ws` → WebSocket (proxied to `localhost:5567`)

Everything else returns 403.

## Prerequisites

- `caddy` — [install](https://caddyserver.com/docs/install)
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
    service: http://localhost:8089
  - service: http_status:404
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

## 4. Start Services

Start Caddy (from the project root):

```bash
caddy run --config tunnel/Caddyfile
```

Start the tunnel:

```bash
cloudflared tunnel run toadie
```

## 5. Verify

```bash
# Local: should show the viewer page
curl http://localhost:8089/

# Local: should return 403
curl http://localhost:8089/api/history
curl http://localhost:8089/dashboard

# Remote: should redirect to Cloudflare Access login
curl -I https://toadie.mfranc.com/
```

## Optional: systemd Services

### Caddy

```ini
# /etc/systemd/system/caddy-viewer.service
[Unit]
Description=Caddy viewer proxy
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/caddy run --config /path/to/claude-watch/tunnel/Caddyfile
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### cloudflared

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

Enable both:

```bash
sudo systemctl enable --now caddy-viewer cloudflared-toadie
```

## Security Layers

1. **Cloudflare Access** — email/OTP authentication at the edge
2. **Caddy path restriction** — only `/` and `/ws` are exposed
3. **Tailscale auth** — `cloudflared` connects via localhost, which `verify_peer()` always allows; the rest of the server remains protected by Tailscale
