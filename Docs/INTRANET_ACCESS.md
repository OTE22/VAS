# Hostname-based intranet access

| Application | HTTPS URL | Nginx certificate |
| --- | --- | --- |
| VAS | https://face-detector.internal/ | `server.crt` |
| VMS | https://armyeye-vms.internal/ | `vms.crt` |
| Chatbot | https://armyeye-chatbot/ | `laf-ai-chatbot.crt` |

All three use TCP 443. Nginx selects the application by hostname and forwards
requests to Docker service names, independently of the physical server IP.
The former VMS direct-IP port 8443 has been retired.

## What changes when the server IP changes?

See [DNS setup commands](INTRANET_DNS.md) for the preview/check script and
the authenticated update option for compatible internal DNS servers.

Configure the new address in Ubuntu and update the three internal DNS records.
The application origins, URLs, and hostname certificates stay unchanged.
VAS allows `PUBLIC_ORIGIN=https://face-detector.internal` for credentialed
browser requests; `VAS_INTRANET_ORIGIN` is no longer used. Wildcard origins and
automatic trust of arbitrary Host headers remain disabled.

VMS webhooks use the internal Docker alias `face-detector.internal`. The chatbot
uses Docker service names for local Ollama and VAS access. Those internal names
also stay unchanged when the server's physical address changes.

## Offline clients and certificates

Internal DNS and network routing must work within the intranet. PCs must trust
`certs/internal-ca.crt`. No public DNS or internet certificate service is needed.
Retain the CA and private keys; certificate renewal remains necessary before
expiry. Existing certificates may still contain older IP SAN entries, but the
supported application URLs use their DNS identities and do not rely on those IPs.

Docker Nginx mounts `VAS/certs` at `/etc/nginx/certs`. Ubuntu's
`/etc/nginx/certs` holds matching copies. Keep both copies updated at renewal.
Private keys remain protected. Never distribute them to client PCs.

The former fixed-IP mDNS publishers are disabled. Central intranet DNS is the
supported name-resolution mechanism, including across routed subnets. Local
`/etc/hosts` entries on this server do not configure other PCs.

## Check the deployment

```bash
sudo bash scripts/prepare_offline_bundle.sh --same-server
```

Optional `--server-address <IPv4>` bypasses DNS for connection tests only; it
never writes that address to configuration. The old `--apply`/`--ip` interface
is rejected to prevent accidental IP-dependent application changes.

The obsolete direct-IP certificate-staging utility and fixed-IP VMS mDNS
unit were removed. Hostname certificate issuance and normal renewal remain
separate from DNS address changes.
