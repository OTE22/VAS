"""Real OpenSSL checks; runnable with standard-library unittest, without pytest."""
import hashlib
import importlib.util
import ipaddress
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/tls/prepare-static-ip.py"
spec = importlib.util.spec_from_file_location("static_ip", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CertificateStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.certs = cls.root / "certs"
        cls.certs.mkdir()
        cls.run_ssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
                    "-subj", "/CN=Test CA", "-keyout", cls.certs / "internal-ca.key",
                    "-out", cls.certs / "internal-ca.crt",
                    "-addext", "basicConstraints=critical,CA:TRUE")
        for name, dns in (("server", "face-detector.internal"),
                          ("vms", "armyeye-vms.internal"),
                          ("laf-ai-chatbot", "armyeye-chatbot")):
            cls.run_ssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj",
                        "/CN=" + dns, "-keyout", cls.certs / (name + ".key"),
                        "-out", cls.root / "request.csr")
            ext = cls.root / "ext.cnf"
            ext.write_text("basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
                           "subjectAltName=DNS:" + dns + ",IP:127.0.0.1,IP:192.168.1.111\n")
            cls.run_ssl("x509", "-req", "-in", cls.root / "request.csr",
                        "-CA", cls.certs / "internal-ca.crt", "-CAkey", cls.certs / "internal-ca.key",
                        "-set_serial", "0x" + module.openssl("rand", "-hex", "16").decode().strip(),
                        "-days", "2", "-extfile", ext, "-out", cls.certs / (name + ".crt"))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def run_ssl(*args):
        return module.openssl(*args)

    def test_stage_keeps_trust_dns_keys_and_live_files(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.certs.iterdir()}
        output = self.root / "staged"
        result = module.stage(self.certs, ipaddress.IPv4Address("10.90.0.20"), output)
        self.assertEqual(len(result["certificates"]), 3)
        self.assertLess(result["validity_days"], 3)  # Cannot outlive the test CA.
        serials = set()
        for name in result["certificates"]:
            cert = output / (name + ".crt")
            self.run_ssl("verify", "-verify_ip", "10.90.0.20", "-CAfile", self.certs / "internal-ca.crt", cert)
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_ssl("verify", "-verify_ip", "192.168.1.111", "-CAfile", self.certs / "internal-ca.crt", cert)
            self.assertEqual(self.run_ssl("x509", "-in", cert, "-pubkey", "-noout"),
                             self.run_ssl("pkey", "-in", self.certs / (name + ".key"), "-pubout"))
            serials.add(self.run_ssl("x509", "-in", cert, "-noout", "-serial"))
        self.assertEqual(len(serials), 3)
        self.assertFalse(list(output.glob("*.key")))
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.certs.iterdir()})

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "marker").write_text("keep")
        with self.assertRaises(ValueError):
            module.stage(self.certs, ipaddress.IPv4Address("10.90.0.20"), output)
        self.assertEqual((output / "marker").read_text(), "keep")

    def test_subject_alternative_names_replace_only_nonloopback_ips(self):
        self.assertEqual(module.names_for_ip(
            "DNS:vas.internal, DNS:vas.local, IP Address:127.0.0.1, IP Address:192.168.1.111",
            ipaddress.IPv4Address("10.90.0.20")),
            ["DNS:vas.internal", "DNS:vas.local", "IP:127.0.0.1", "IP:10.90.0.20"])

    def test_invalid_ip_cli_creates_no_output(self):
        for address in ("not-an-ip", "127.0.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255"):
            output = self.root / "invalid"
            result = subprocess.run(["python3", str(SCRIPT), "--ip", address,
                                     "--output", str(output)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
