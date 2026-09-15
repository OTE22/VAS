# Intranet HTTPS access

| Application | Direct URL | TCP port |
| --- | --- | --- |
| VAS | https://192.168.1.111/ | 443 |
| VMS | https://192.168.1.111:8443/ | 8443 |

These URLs do not require DNS, mDNS, or per-PC hosts entries. Client PCs may be
on other routed intranet subnets. Routers and firewalls between them and the
server must allow these TCP ports. Reachability from a separate subnet must be
checked from a PC on that subnet; a successful server-side test cannot prove it.

## HTTPS trust

Both server certificates contain the IP SAN `192.168.1.111` and are signed by
the existing VAS internal CA. Install `certs/internal-ca.crt` in client trusted
roots, or distribute it centrally with your workstation management system.
Clients already trusting this CA do not need another root certificate.
Never distribute private keys.

The active Nginx container reads `VAS/certs`. Ubuntu `/etc/nginx/certs` contains
matching copies. Keep both copies updated on renewal. `server.crt` is the VAS
certificate, and `vms.crt` is the VMS certificate. The existing hostname SANs
remain valid. The renewed VAS certificate expires September 15, 2027.

## VAS sign-in configuration

Production Compose approves both `PUBLIC_ORIGIN` and `VAS_INTRANET_ORIGIN`
for browser CORS, login-CSRF, and WebSocket origin checks. The current intranet
origin is explicitly set in `docker/.env`; if unset, Compose uses `PUBLIC_ORIGIN`
instead of assuming an old workstation IP.
`AUTH_SAME_HOST_ORIGIN_TRUSTED` stays false and wildcard origins remain disabled.
Changing the server IP requires a matching certificate and origin-setting update.

VAS still redirects plain HTTP on port 80 to HTTPS. VMS uses its dedicated
8443 listener so bare-IP VAS traffic on 443 retains its existing destination.

See [offline server migration](OFFLINE_SERVER_MIGRATION.md) before moving to a
new address or server. The final deployment address is not assigned yet.
