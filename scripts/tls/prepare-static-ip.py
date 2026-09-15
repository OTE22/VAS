#!/usr/bin/env python3
"""Stage CA-signed certificates for a future IPv4 address without changing services.

sudo python3 scripts/tls/prepare-static-ip.py --ip <assigned-ip> --output /secure/stage
The existing CA and server private keys are reused; no internet is required.
Only public certificates and non-secret address settings enter the output.
"""

import argparse
import datetime as dt
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile


def openssl(*args, data=None):
    return subprocess.check_output(["openssl", *map(str, args)], input=data,
                                   stderr=subprocess.PIPE)


def names_for_ip(san_text, address):
    """Preserve DNS identities and loopback SANs; replace prior LAN addresses."""
    names = re.findall(r"DNS:([^,\s]+)", san_text)
    if any(not re.fullmatch(r"[A-Za-z0-9*._-]+", name) for name in names):
        raise ValueError("Unsupported DNS SAN; review this certificate manually")
    result = ["DNS:" + name for name in names]
    for raw in re.findall(r"IP Address:([^,\s]+)", san_text):
        ip = ipaddress.ip_address(raw)
        if ip.is_loopback:
            result.append("IP:" + str(ip))
    result.append("IP:" + str(address))
    return list(dict.fromkeys(result))


def stage(cert_dir, address, output):
    if output.exists():
        raise ValueError("Output already exists; choose a new staging directory")
    ca = cert_dir / "internal-ca.crt"
    ca_key = cert_dir / "internal-ca.key"
    # Do not issue a leaf beyond the existing CA's remaining lifetime.
    expiry = openssl("x509", "-in", ca, "-noout", "-enddate").decode().strip()
    expires = dt.datetime.strptime(expiry.split("=", 1)[1], "%b %d %H:%M:%S %Y GMT")
    remaining = (expires.replace(tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).days
    days = min(365, remaining)
    if days < 1:
        raise ValueError("Existing CA expires too soon; renew its trust deployment first")
    leaves = ("server", "vms", "laf-ai-chatbot")
    for name in leaves:
        cert, key = cert_dir / (name + ".crt"), cert_dir / (name + ".key")
        openssl("verify", "-CAfile", ca, cert)
        cert_pub = openssl("x509", "-in", cert, "-pubkey", "-noout")
        key_pub = openssl("pkey", "-in", key, "-pubout")
        if cert_pub != key_pub:
            raise ValueError("Certificate/private-key mismatch: " + name)
    # Stage fully before publishing; failures leave the live deployment untouched.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".static-ip-", dir=output.parent) as temporary:
        work = Path(temporary)
        bundle = work / "bundle"
        bundle.mkdir(mode=0o700)
        manifest = {"ip": str(address), "validity_days": days, "certificates": {}}
        for name in leaves:
            cert, key = cert_dir / (name + ".crt"), cert_dir / (name + ".key")
            sans = names_for_ip(openssl("x509", "-in", cert, "-noout", "-ext",
                                       "subjectAltName").decode(), address)
            extensions = work / "extensions.cnf"
            extensions.write_text("basicConstraints=critical,CA:FALSE\n"
                                  "keyUsage=critical,digitalSignature,keyEncipherment\n"
                                  "extendedKeyUsage=serverAuth\n"
                                  "subjectAltName=" + ",".join(sans) + "\n")
            csr = work / "request.csr"
            openssl("x509", "-x509toreq", "-in", cert, "-signkey", key, "-out", csr)
            target = bundle / (name + ".crt")
            serial = "0x" + openssl("rand", "-hex", "16").decode().strip()
            openssl("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key,
                    "-set_serial", serial, "-days", days, "-sha256",
                    "-extfile", extensions, "-out", target)
            openssl("verify", "-purpose", "sslserver", "-verify_ip", address,
                    "-CAfile", ca, target)
            for san in sans:
                if san.startswith("DNS:"):
                    openssl("verify", "-verify_hostname", san[4:], "-CAfile", ca, target)
            manifest["certificates"][name] = sans
        (bundle / "intranet.env").write_text(
            f"VAS_INTRANET_IP={address}\nVAS_INTRANET_ORIGIN=https://{address}\n")
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (bundle / "README.txt").write_text(
            "STAGED ONLY: no active configuration, network address, or service changed.\n"
            "Read Docs/OFFLINE_SERVER_MIGRATION.md before installing these certificates.\n"
            "These certificates match the EXISTING leaf private keys in the source cert directory.\n"
            "Keep those keys and the existing CA when moving the deployment.\n"
            "Do not replace docker/.env with intranet.env: update only its named settings.\n")
        bundle.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path,
                        default=Path(__file__).resolve().parents[2] / "certs")
    args = parser.parse_args()
    try:
        address = ipaddress.IPv4Address(args.ip)
        if address.is_unspecified or address.is_loopback or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("Supply the assigned unicast intranet IPv4 address")
        manifest = stage(args.cert_dir.resolve(), address, args.output.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Certificate staging failed: {error}\n")
    print(f"Staged {len(manifest['certificates'])} certificates in {args.output}")
    print("Active services and network settings are unchanged.")


if __name__ == "__main__":
    main()
