# VMS HTTPS

## Direct access across the intranet

Open `https://192.168.1.111:8443/` from any intranet subnet that can route to the
server and reach TCP port 8443. No DNS, mDNS, or hosts-file entry is needed for
this address. Clients still need to trust the existing VAS root certificate.
Network routers and firewalls between subnets must permit the connection.

Nginx publishes port 8443 on all host interfaces and routes it to VMS. The VMS
certificate includes IP address `192.168.1.111`. If the server IP changes, update
and reissue the certificate; client PCs may have any source IP permitted by the
network. The IP HTTPS login page and secure cookie attributes were verified on
the server. A request from another subnet has not been tested.

## Hostname access

VMS is available at `https://armyeye-vms.internal/` through the VAS production
Nginx container. `armyeye-vms` and `armyeye-vms.local` are also covered by the
certificate and the Nginx route.

## Certificate locations

- Ubuntu: `/etc/nginx/certs/vms.crt` and `/etc/nginx/certs/vms.key`.
- Active Docker bind mount: `VAS/certs/vms.crt` and `VAS/certs/vms.key`, exposed
  inside Nginx as `/etc/nginx/certs/vms.crt` and `/etc/nginx/certs/vms.key`.
- Issuer: the existing `VAS/certs/internal-ca.crt`; its signing key stays in VAS.
- The initial VMS certificate expires on September 15, 2027.

The Ubuntu and VAS directories contain copies, not a synchronized mount. Update
both during renewal. `scripts/tls/make-vms-cert.sh <server-ipv4>` issues the initial certificate
and deliberately refuses to overwrite an existing VMS certificate/key.

## Client access

For PCs on the same LAN with mDNS support, open `https://armyeye-vms.local/`.
The enabled `armyeye-vms-mdns.service` publishes that name as `192.168.1.111`
through Avahi and starts at boot. Its source is
`scripts/tls/armyeye-vms-mdns.service`. The installed unit reads
`VAS_INTRANET_IP` from `/etc/nginx/intranet.env`. Update that setting and restart
the unit if the server's LAN address changes.

The server's hosts file resolves the three VMS names to loopback. For networks
that block mDNS, or clients without mDNS support, configure central DNS for
`armyeye-vms.internal`, or add this hosts entry:

```text
192.168.1.111 armyeye-vms.internal armyeye-vms armyeye-vms.local
```

Clients must trust `internal-ca.crt` as a root certificate, as they do for VAS and
the chatbot. Never distribute private keys. For IP access, use port 8443;
port 443 continues to serve the existing VAS default site for bare-IP requests.
The mDNS publisher was verified with `avahi-resolve-host-name`, and the HTTPS
login page returned HTTP 200 through `192.168.1.111` with certificate verification.
Access from a separate physical PC has not been tested.

## Routing and validation

`nginx.prod.conf` redirects HTTP on the VMS hostnames to HTTPS and forwards HTTPS
requests over `webhook_integration` to `VMS:5555`. Nginx protects the session
cookie and disables response buffering for camera streams. VMS still publishes
its existing HTTP port 5555; browser users should use the HTTPS address above.

The deployment passed `nginx -t`, verified the certificate chain and all three
VMS hostnames, returned HTTP 200 for `/login`, and confirmed secure session cookie
attributes and the HTTP 308 redirect. VAS and chatbot TLS checks also passed.
Authenticated VMS operations and camera playback were not tested.
