# VMS HTTPS

Open **https://armyeye-vms.internal/** from intranet PCs.

Internal DNS points `armyeye-vms.internal` to the server's current network
address. Nginx serves its existing hostname certificate and forwards requests
to `VMS:5555` over the Docker `webhook_integration` network.

When the server address changes, update DNS. No VMS configuration or certificate
change is needed solely for that address change. The previous direct-IP HTTPS
port 8443 is retired. Port 5555 remains the existing HTTP service interface;
users should use the hostname HTTPS link.

## Certificate and client trust

- Active Docker certificate files: `VAS/certs/vms.crt` and `VAS/certs/vms.key`.
- Ubuntu copies: `/etc/nginx/certs/vms.crt` and `/etc/nginx/certs/vms.key`.
- Issuer: the existing VAS CA (`internal-ca.crt`), trusted on client PCs.
- The current VMS certificate expires September 15, 2027; renew before expiry.
- `scripts/tls/make-vms-cert.sh` issues an initial hostname-only VMS certificate
  and refuses to overwrite existing certificate files.

## Networking and checks

Allow TCP 443 from the required intranet subnets. Port 80 redirects the VMS
hostname to HTTPS. Central DNS works offline and across routed subnets; the
previous fixed-IP mDNS publisher is disabled.

Nginx sets secure session-cookie attributes and streams camera responses
without response buffering. HTTP login-page checks do not prove authenticated
camera playback: include that in final intranet acceptance testing.

See [the same-server move checklist](OFFLINE_SERVER_MIGRATION.md).
