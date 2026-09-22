# Nginx Proxy Manager

Settings for a proxy host that forwards to the gaming PC running
Plachataa Web.

## Details tab

| Field | Value |
|---|---|
| Domain Names | `voice.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | LAN IP of the gaming PC |
| Forward Port | `7870` |
| Cache Assets | off |
| Block Common Exploits | on (fine) |
| **Websockets Support** | **on** (required for the real-time voice changer) |

Without *Websockets Support* the page loads and file conversion works,
but the real-time mode fails with close code 1006 as soon as you press
Start.

## SSL tab

Request a Let's Encrypt certificate (or pick an existing one) and turn
on *Force SSL*. Browsers only allow microphone access on HTTPS, so the
real-time mode needs this.

## Advanced tab → Custom Nginx Configuration

```nginx
client_max_body_size 200m;
proxy_read_timeout 600s;
proxy_send_timeout 600s;
proxy_buffering off;
```

The timeouts keep long uploads and the first real-time start (model
download) alive; the server also sends a heartbeat every 5 s while
loading, so these are belt and braces.

## Access List (optional)

If you do not set `PLACHATAA_BASIC_AUTH` on the gaming PC, protect the
host with an NPM Access List (Details tab → Access List) so it is not
open to the internet.

## Sub-path instead of a hostname

Nginx Proxy Manager cannot strip a path prefix from the UI. Use a
dedicated hostname as above; sub-path hosting needs a hand-written
nginx config (see `nginx.conf` in this folder).
