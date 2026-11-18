"""Run with: python3 -m unittest discover -s tests

Checks the certificate pinning of bin/localsend-helper.py against a real
local https server with a throwaway self-signed certificate."""

import hashlib
import http.server
import importlib.util
import io
import json
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("helper", os.path.join(HERE, "..", "bin", "localsend-helper.py"))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def make_certificate(directory, name):
    cert = os.path.join(directory, name + ".pem")
    key = os.path.join(directory, name + ".key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=test", "-keyout", key, "-out", cert], capture_output=True, check=True)
    with open(cert, encoding="ascii") as handle:
        der = ssl.PEM_cert_to_DER_cert(handle.read())
    return cert, key, hashlib.sha256(der).hexdigest().upper()


class FakeLocalSend(http.server.BaseHTTPRequestHandler):
    claimed_fingerprint = ""
    requests = []

    def log_message(self, *_args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeLocalSend.requests.append(self.path)
        self.reply(200, {"alias": "Fake", "fingerprint": FakeLocalSend.claimed_fingerprint, "deviceType": "desktop"})

    def do_POST(self):
        FakeLocalSend.requests.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.startswith("/api/localsend/v2/prepare-upload"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.reply(200, {})


class PinningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cert, key, cls.fingerprint = make_certificate(cls.tmp.name, "server")
        _cert, _key, cls.other_fingerprint = make_certificate(cls.tmp.name, "other")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLocalSend)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        helper.STATE_DIR = os.path.join(cls.tmp.name, "state")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeLocalSend.requests = []
        FakeLocalSend.claimed_fingerprint = self.fingerprint

    def send(self, fingerprint):
        out = io.StringIO()
        args = ["send", "--ip", "127.0.0.1", "--port", str(self.port), "--protocol", "https"]
        if fingerprint is not None:
            args.append("--fingerprint=" + fingerprint)
        with tempfile.NamedTemporaryFile(prefix="-looks-like-an-option", dir=self.tmp.name) as handle:
            with redirect_stdout(out):
                code = helper.main(args + ["--", handle.name])
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        return code, events

    def test_probe_marks_a_matching_certificate_verified(self):
        device = helper.probe_info("127.0.0.1", self.port, "https", "own")
        self.assertTrue(device["verified"])

    def test_probe_drops_a_device_whose_certificate_does_not_match_its_claim(self):
        FakeLocalSend.claimed_fingerprint = self.other_fingerprint
        self.assertIsNone(helper.probe_info("127.0.0.1", self.port, "https", "own"))

    def test_send_goes_through_when_the_certificate_matches(self):
        code, events = self.send(self.fingerprint.lower())
        self.assertNotEqual(events[-1].get("code"), "fingerprintMismatch")
        self.assertTrue(any(p.startswith("/api/localsend/v2/prepare-upload") for p in FakeLocalSend.requests))

    def test_send_refuses_a_different_certificate_before_any_request(self):
        code, events = self.send(self.other_fingerprint)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "fingerprintMismatch")
        self.assertEqual(FakeLocalSend.requests, [])

    def test_send_refuses_a_pin_that_is_not_a_certificate_hash(self):
        code, events = self.send("not-a-hash")
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["code"], "badTarget")
        self.assertEqual(FakeLocalSend.requests, [])

    def discovery(self):
        discovery = helper.Discovery([])
        discovery.info = {"fingerprint": "own"}
        return discovery

    def test_a_verified_record_replaces_an_unverified_one(self):
        discovery = self.discovery()
        base = {"fingerprint": "AB", "ip": "10.0.0.1", "port": 1, "verified": False}
        with redirect_stdout(io.StringIO()) as out:
            helper.report_device(discovery, base)
            helper.report_device(discovery, dict(base))
            helper.report_device(discovery, dict(base, fingerprint="ab", verified=True))
            helper.report_device(discovery, dict(base))
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        self.assertTrue(discovery.found["ab"]["verified"])

    def announced(self, fingerprint):
        return {"alias": "Fake", "ip": "127.0.0.1", "port": self.port, "protocol": "https",
                "fingerprint": fingerprint, "verified": False}

    def run_announce(self, *devices):
        discovery = self.discovery()
        with redirect_stdout(io.StringIO()) as out:
            for device in devices:
                helper.accept_announced(discovery, device)
            for checker in discovery.checkers:
                checker.join(5)
        return discovery, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_an_announced_https_device_appears_only_after_its_certificate_matched(self):
        discovery, events = self.run_announce(self.announced(self.fingerprint), self.announced(self.fingerprint))
        self.assertEqual(len(discovery.checkers), 1, "each key is checked once")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["verified"])

    def test_an_impostor_claiming_first_does_not_block_the_real_device(self):
        impostor = dict(self.announced(self.fingerprint), port=1)
        discovery, events = self.run_announce(impostor, self.announced(self.fingerprint))
        self.assertEqual(len(discovery.checkers), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["port"], self.port)

    def test_one_host_cannot_use_up_the_checks(self):
        flood = [dict(self.announced("%064x" % n), port=1) for n in range(20)]
        discovery, _events = self.run_announce(*flood)
        self.assertEqual(len(discovery.checkers), helper.MAX_CHECKS_PER_IP)

    def test_an_announced_https_impostor_never_appears(self):
        FakeLocalSend.claimed_fingerprint = self.other_fingerprint
        discovery, events = self.run_announce(self.announced(self.other_fingerprint))
        self.assertEqual(events, [])


class ClipboardTest(unittest.TestCase):
    def test_temporary_clipboard_image_is_removed_after_the_send(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"png")
        handle.close()
        entry = dict(helper.file_entry(handle.name, "clipboard.png", "image/png"), _temporary=True)
        original = helper.clipboard_payload
        helper.clipboard_payload = lambda: [entry]
        try:
            with redirect_stdout(io.StringIO()):
                helper.main(["send", "--ip", "127.0.0.1", "--port", "9", "--protocol", "http", "--clipboard"])
        finally:
            helper.clipboard_payload = original
        self.assertFalse(os.path.exists(handle.name))


if __name__ == "__main__":
    unittest.main()
