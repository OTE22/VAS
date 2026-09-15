# DNS records on the existing intranet DNS server

The server currently uses `10.0.16.1` and `8.8.8.8` as DNS clients. It does not
host an intranet DNS service. Use the existing internal DNS infrastructure;
no additional DNS server has been installed by this change.

## Check the records now

From the VAS folder:

```bash
bash scripts/intranet_dns.sh --check
```

This queries `10.0.16.1` directly. A single-record equivalent is:

```bash
dig @10.0.16.1 armyeye-vms.internal. A +short
```

The September 15 check for `face-detector.internal` returned `SERVFAIL`; that
means the DNS server did not resolve it successfully. It does not identify
the DNS server's software or prove the cause of the failure.

## Prepare the records for the DNS administrator

```bash
bash scripts/intranet_dns.sh --server-ip 192.168.1.111
```

This is a preview only, using the current server IP. When the final intranet
address is assigned, substitute that address. The output lists these A records:

| Name | Value |
| --- | --- |
| `face-detector.internal` | Server IP |
| `armyeye-vms.internal` | Server IP |
| `armyeye-chatbot` | Server IP |

Your administrator must create these records in the internal DNS system.
They may use Windows DNS Manager, a router/firewall interface, or Linux DNS
configuration. We do not yet know which platform manages `10.0.16.1`.

## Optional authenticated update command

The script can apply the records **only if the DNS server supports and permits
dynamic updates**. It never uses your Ubuntu administrator password for DNS.
With a TSIG key supplied by the DNS administrator:

```bash
bash scripts/intranet_dns.sh --apply --server-ip 192.168.1.111 \
  --tsig-key /path/to/authorized-dns-update.key
```

Alternatively `--gss-tsig` uses an existing authorized Kerberos login on a
compatible server. These options use the standard
[ISC nsupdate utility](https://bind9.readthedocs.io/en/latest/manpages.html#nsupdate-dynamic-dns-update-utility).
Authentication and zone permissions must already be configured by the DNS owner.

This helper expects the `internal.` zone and, for the existing single-label
chatbot URL, an `armyeye-chatbot.` zone with its apex A record. It checks both
zones before sending updates; it does not create zones. The administrator may
instead manage the same exact names through their DNS platform's interface.
The two zones update separately, so a failure can leave a partial change.
Previous answers are saved to the printed file for review/recovery.

After adding records, run `--check --server-ip <assigned-ip>` to verify them.

## Client computers

Clients must send private-name lookups to internal DNS. Public DNS `8.8.8.8`
does not contain these private records and is unavailable offline. Ask the
network administrator to distribute the appropriate internal DNS settings via
DHCP/network policy. Another internal DNS server can provide redundancy.
Changing only this Ubuntu server's DNS settings does not configure other PCs.

No application configuration or certificate changes are needed when these DNS
records are pointed to a different server IP.
