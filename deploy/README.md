Quick deploy notes for sedabox.com

1) Purpose
- `nginx.sedabox.conf` is an example nginx site designed to proxy requests to a web container named `soundbox_web` on port 8000.
- `docker-compose.nginx.yml` shows how to run nginx in a container and attach it to the same Docker network as your web container.

2) How to use (basic)
- Place the files in your project `deploy/` directory and adjust paths:
  - Ensure `proxy_pass` in `nginx.sedabox.conf` points to the correct docker service name and port (`soundbox_web:8000` by default).
  - Ensure `STATIC_ROOT` is collected into `deploy/static` or update the `alias` in the nginx config.
- If your main `docker-compose.yml` creates a network `soundbox_net` (or similar), mark it `external: true` and share the network with nginx. Otherwise, create a shared network or merge this compose with your main compose file.

3) Firewall (on host)
- Allow HTTP/HTTPS through UFW:

```powershell
# On the server (Linux):
sudo ufw allow 80
sudo ufw allow 443
sudo ufw reload
```

4) Cloudflare
- Initially set Cloudflare DNS A record for `sedabox.com` to point to your server IP and set the record to "DNS only" (grey cloud) while testing.
- Once nginx serves HTTPS with a valid certificate, enable Cloudflare proxy (orange cloud) and set SSL to "Full (strict)".

5) Testing
- From the server, check Gunicorn:

```bash
curl -I http://127.0.0.1:8000/
```

- From your workstation:

```powershell
curl.exe -I http://sedabox.com/
curl.exe -I https://sedabox.com/
```

6) Notes
- If you're using Docker containers for everything, prefer running nginx in a container on the same Docker network rather than installing it on the host.
- Do not expose Gunicorn (8000) publicly; only expose 80/443 via nginx.
