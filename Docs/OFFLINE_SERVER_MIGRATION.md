# Move this server to the offline intranet

**The applications use stable hostnames. Changing the server IP is a network/DNS task.**
Keep the same installed applications, databases, Docker volumes, models, secrets,
and certificates. There is no reinstall or data transfer.

## 1. Before moving

- [ ] Take a current backup and shut down Ubuntu normally before transport.
- [ ] Obtain the intranet IP, network prefix, gateway, and internal DNS details.

## 2. Connect to the intranet

- [ ] Configure the server's network address in Ubuntu.
- [ ] Ask the network administrator to point these internal DNS names to that address:

Use the [DNS commands and script](INTRANET_DNS.md) to preview the records and
check `10.0.16.1`. Applying them requires the DNS administrator's access.

| Application | Stable link |
| --- | --- |
| VAS | https://face-detector.internal/ |
| VMS | https://armyeye-vms.internal/ |
| Chatbot | https://armyeye-chatbot/ |

- [ ] Allow client access to TCP **443**; port **80** redirects to HTTPS.
- [ ] Ensure clients trust the existing VAS root certificate (`certs/internal-ca.crt`).
- [ ] Ensure the server can reach cameras and required internal services.

**If the IP changes later, update DNS. Do not reissue certificates or change
application settings just because the IP changed.** Certificates still need
normal renewal before expiry. Internal DNS works without internet.

## 3. Reboot offline and check

From the VAS folder:

```bash
sudo bash scripts/prepare_offline_bundle.sh --same-server
```

This read-only check verifies running services, VAS offline/origin policy,
certificate hostnames, HTTPS pages, and health endpoints. It changes no settings.
To test a particular address before DNS is ready, use:

```bash
sudo bash scripts/prepare_offline_bundle.sh --same-server --server-address 10.90.0.20
```

Replace the example with the actual server address. It is used only for the
connection test; hostnames and certificate verification remain in use.

From another intranet PC, open the stable links and test sign-in, camera
playback, detection/webhook results, maps, and a chatbot question about stored
data. Check the server's clock. A server-local check cannot prove remote DNS,
routing, camera access, or an end-to-end workflow.

Local models and data are already installed. Cloud APIs, internet searches,
and new downloads remain unavailable offline.

Technical reference: [hostname-based intranet setup](INTRANET_ACCESS.md).
