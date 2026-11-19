# §12. TLS reverse proxy for the admin Mini App

Telegram WebApp requires HTTPS. Xray Reality already owns :443 on the host. Solution: Caddy installed via the official apt repo, listening on **:9443** for `<dashboard-host>`, automatically handling Let's Encrypt via HTTP-01 on :80. Caddyfile is two lines:
```
<dashboard-host>:9443 {
    reverse_proxy 127.0.0.1:8080
}
```
Cert renews automatically. The bot publishes `WEBAPP_URL=https://<dashboard-host>:9443/` to admins; the hardcoded ngrok default in `settings.py` is now empty so the bot fails loudly instead of pointing at a dead URL.
