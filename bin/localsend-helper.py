#!/usr/bin/python3
"""LocalSend protocol helper for the mae.localsend Omarchy shell plugin.

Speaks LocalSend protocol v2 with the Python standard library only. Every
subcommand prints JSON, one object per line, so the QML service can parse
events as they happen.

  status                       own alias, whether a receiver listens, own IPs
  discover [--timeout S]       nearby devices: multicast announce plus a
                               subnet scan, each device as it is found
  send --ip IP --port PORT --protocol http|https [--fingerprint FP]
       [--pin PIN] (--clipboard | [--] FILE...)
                               send files or the clipboard to one device

Over https a LocalSend fingerprint is the SHA-256 of the device's TLS
certificate, so it can be checked: discovery marks a device "verified" when
its certificate matches the fingerprint it claims and drops it when it does
not, and send refuses to talk to a peer whose certificate does not match
--fingerprint. Over http nothing can be checked.

Everything that comes back from the network is untrusted: strings are
bounded and stripped of control characters, numbers are range-checked,
and the response bodies are capped before they are parsed.
"""

import argparse
import signal
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

MULTICAST_GROUP = "224.0.0.167"
LOCALSEND_PORT = 53317
PROTOCOL_VERSION = "2.1"
MAX_BODY = 64 * 1024
MAX_ALIAS = 64
MAX_MODEL = 64
MAX_VERSION = 16
MAX_FINGERPRINT = 128
DEVICE_TYPES = ("mobile", "desktop", "web", "headless", "server")
TEXT_PREVIEW_LIMIT = 16 * 1024
UPLOAD_CHUNK = 256 * 1024

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "mae-localsend")
PREFS_PATH = os.path.expanduser("~/.local/share/org.localsend.localsend_app/shared_preferences.json")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    out = "".join(ch for ch in value if ch.isprintable() and ch not in "  ")
    return out.strip()[:limit]


def certificate_paths():
    return os.path.join(STATE_DIR, "cert.pem"), os.path.join(STATE_DIR, "key.pem")


def ensure_certificate():
    """A self-signed certificate identifies this helper to other devices:
    LocalSend 2.2 peers want a client certificate and use its SHA-256 as the
    sender's fingerprint. Generated once with openssl, kept in the state dir."""
    cert_path, key_path = certificate_paths()
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=LocalSend User", "-keyout", key_path, "-out", cert_path],
                       capture_output=True, timeout=60, check=True)
        os.chmod(key_path, 0o600)
        return cert_path, key_path
    except (OSError, subprocess.SubprocessError):
        return None, None


def own_fingerprint():
    cert_path, _key_path = ensure_certificate()
    if cert_path:
        try:
            with open(cert_path, encoding="ascii") as handle:
                der = ssl.PEM_cert_to_DER_cert(handle.read())
            return hashlib.sha256(der).hexdigest().upper()
        except (OSError, ValueError):
            pass
    path = os.path.join(STATE_DIR, "fingerprint")
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
    except OSError:
        pass
    return value


def own_alias():
    try:
        with open(PREFS_PATH, encoding="utf-8") as handle:
            prefs = json.load(handle)
        alias = clean_text(prefs.get("flutter.ls_alias"), MAX_ALIAS)
        if alias:
            return alias
    except (OSError, ValueError):
        pass
    return clean_text(socket.gethostname(), MAX_ALIAS) or "Omarchy"


def own_info(port, protocol):
    return {
        "alias": own_alias(),
        "version": PROTOCOL_VERSION,
        "deviceModel": "Omarchy",
        "deviceType": "desktop",
        "fingerprint": own_fingerprint(),
        "port": port,
        "protocol": protocol,
        "download": False,
    }


def own_ipv4_addresses():
    """Global IPv4 addresses with their prefix length, from `ip -j`."""
    try:
        raw = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
        data = json.loads(raw or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    result = []
    for iface in data:
        name = str(iface.get("ifname", ""))
        if name.startswith(("docker", "br-", "veth", "virbr", "tun", "tap", "lo")):
            continue
        for addr in iface.get("addr_info", []):
            try:
                ip = ipaddress.ip_address(addr.get("local", ""))
                prefix = int(addr.get("prefixlen", 24))
            except ValueError:
                continue
            if ip.is_loopback or ip.version != 4:
                continue
            result.append((str(ip), prefix, name))
    return result


def receiver_listening(port):
    try:
        raw = subprocess.run(["ss", "-ltnH", "sport = :%d" % port],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return raw.strip() != ""


def sanitize_device(payload, ip, fallback_port=LOCALSEND_PORT, fallback_protocol="https"):
    """Validate a device record from the network. Returns None if unusable."""
    if not isinstance(payload, dict):
        return None
    alias = clean_text(payload.get("alias"), MAX_ALIAS)
    if not alias:
        return None
    fingerprint = clean_text(payload.get("fingerprint"), MAX_FINGERPRINT)
    if not all(c.isalnum() or c in ":_-." for c in fingerprint):
        fingerprint = ""
    device_type = str(payload.get("deviceType", "")).lower()
    if device_type not in DEVICE_TYPES:
        device_type = "desktop"
    port = payload.get("port", fallback_port)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = fallback_port
    if not 1 <= port <= 65535:
        port = fallback_port
    protocol = str(payload.get("protocol", fallback_protocol)).lower()
    if protocol not in ("http", "https"):
        protocol = fallback_protocol
    try:
        ip_text = str(ipaddress.ip_address(ip))
    except ValueError:
        return None
    return {
        "verified": False,
        "alias": alias,
        "ip": ip_text,
        "port": port,
        "protocol": protocol,
        "deviceType": device_type,
        "deviceModel": clean_text(payload.get("deviceModel"), MAX_MODEL),
        "version": clean_text(payload.get("version"), MAX_VERSION),
        "fingerprint": fingerprint,
        "download": payload.get("download") is True,
    }


_CONTEXT = None


def unverified_context():
    """Peers use self-signed certificates, so no CA can vouch for them. The
    certificate hash is compared with the fingerprint instead, see
    open_connection. The client certificate identifies us to them."""
    global _CONTEXT
    if _CONTEXT is None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        cert_path, key_path = ensure_certificate()
        if cert_path and key_path:
            try:
                context.load_cert_chain(cert_path, key_path)
            except (OSError, ssl.SSLError):
                pass
        _CONTEXT = context
    return _CONTEXT


class FingerprintMismatch(Exception):
    pass


def normalize_fingerprint(value):
    return str(value or "").strip().lower()


def peer_fingerprint(conn):
    der = conn.sock.getpeercert(binary_form=True) if conn.sock else None
    return hashlib.sha256(der).hexdigest() if der else ""


def open_connection(ip, port, protocol, timeout, expected_fingerprint=""):
    """An open connection. Over https it is already connected, and when
    expected_fingerprint is given the peer certificate must hash to it, or
    FingerprintMismatch is raised before any request is sent."""
    if protocol != "https":
        return http.client.HTTPConnection(ip, port, timeout=timeout)
    conn = http.client.HTTPSConnection(ip, port, timeout=timeout, context=unverified_context())
    conn.connect()
    expected = normalize_fingerprint(expected_fingerprint)
    if expected and peer_fingerprint(conn) != expected:
        conn.close()
        raise FingerprintMismatch("certificate does not match the fingerprint")
    return conn


def read_json_body(response):
    body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    if not body.strip():
        return {}
    return json.loads(body.decode("utf-8", "replace"))


# --------------------------------------------------------------------- status

def cmd_status(args):
    addresses = own_ipv4_addresses()
    emit({
        "event": "status",
        "alias": own_alias(),
        "fingerprint": own_fingerprint(),
        "port": LOCALSEND_PORT,
        "receiving": receiver_listening(LOCALSEND_PORT),
        "ips": [ip for ip, _prefix, _name in addresses],
        "appInstalled": any(os.access(os.path.join(p, "localsend"), os.X_OK) for p in os.environ.get("PATH", "").split(":") if p),
    })
    return 0


# ------------------------------------------------------------------- discover

class RegisterServer(threading.Thread):
    """Minimal HTTP listener for the /register calls devices answer an
    announce with. Only POSTs with a small JSON body are accepted."""

    def __init__(self, info, found, lock, own_ips):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.info = info
        self.found = found
        self.lock = lock
        self.own_ips = own_ips
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn, addr[0]), daemon=True).start()
        self.sock.close()

    def handle(self, conn, ip):
        conn.settimeout(2.0)
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < MAX_BODY:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _sep, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            request = lines[0].split(" ") if lines else []
            length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = 0
            if length > MAX_BODY:
                conn.sendall(b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            while len(body) < length:
                chunk = conn.recv(min(4096, length - len(body)))
                if not chunk:
                    break
                body += chunk
            if len(request) >= 2 and request[0] == "POST" and request[1].startswith("/api/localsend/v2/register"):
                try:
                    device = sanitize_device(json.loads(body.decode("utf-8", "replace") or "{}"), ip)
                except ValueError:
                    device = None
                if device and ip not in [own for own, _p, _n in self.own_ips]:
                    report_device(self.found, self.lock, device, self.own_ips, self.info["fingerprint"])
                    if device["protocol"] == "https":
                        threading.Thread(target=verify_device, args=(device, self.found, self.lock, self.own_ips, self.info), daemon=True).start()
                payload = json.dumps(self.info).encode("utf-8")
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(payload) + payload)
            else:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def report_device(found, lock, device, own_ips, own_fp):
    """Emit a device once per key. A verified record replaces an unverified
    one with the same key and is emitted again."""
    if device["fingerprint"] and normalize_fingerprint(device["fingerprint"]) == normalize_fingerprint(own_fp):
        return
    key = normalize_fingerprint(device["fingerprint"]) or "%s:%d" % (device["ip"], device["port"])
    with lock:
        known = found.get(key)
        if known and (known["verified"] or not device["verified"]):
            return
        found[key] = device
    emit(dict(device, event="device"))


def multicast_round(info, found, lock, own_ips, deadline):
    """Announce on every interface and collect UDP answers until deadline."""
    message = json.dumps(dict(info, announce=True)).encode("utf-8")
    listener = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(("0.0.0.0", LOCALSEND_PORT))
        for ip, _prefix, _name in own_ips:
            try:
                listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                    socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip))
            except OSError:
                pass
        listener.settimeout(0.2)
    except OSError:
        listener = None

    senders = []
    for ip, _prefix, _name in own_ips or [("0.0.0.0", 0, "")]:
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            if ip != "0.0.0.0":
                sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sender.sendto(message, (MULTICAST_GROUP, LOCALSEND_PORT))
            senders.append(sender)
        except OSError:
            pass
    # A second announce half a second later catches devices that missed the first.
    repeat_at = time.monotonic() + 0.5
    repeated = False
    while time.monotonic() < deadline:
        if listener is None:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        else:
            try:
                data, addr = listener.recvfrom(MAX_BODY)
                try:
                    payload = json.loads(data.decode("utf-8", "replace"))
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and addr[0] not in [ip for ip, _p, _n in own_ips]:
                    device = sanitize_device(payload, addr[0])
                    if device:
                        report_device(found, lock, device, own_ips, info["fingerprint"])
                        if device["protocol"] == "https":
                            threading.Thread(target=verify_device, args=(device, found, lock, own_ips, info), daemon=True).start()
                        # Devices that announce want a register in return so they
                        # learn about us too; answering keeps us visible to them.
                        if payload.get("announce") is True:
                            threading.Thread(target=register_with, args=(device, info), daemon=True).start()
            except socket.timeout:
                pass
            except OSError:
                break
        if not repeated and time.monotonic() >= repeat_at:
            repeated = True
            for sender in senders:
                try:
                    sender.sendto(message, (MULTICAST_GROUP, LOCALSEND_PORT))
                except OSError:
                    pass
    for sender in senders:
        sender.close()
    if listener is not None:
        listener.close()


def verify_device(device, found, lock, own_ips, info):
    """Ask an https device for its info so its certificate can be checked;
    a match reports it again as verified."""
    try:
        checked = probe_info(device["ip"], device["port"], "https", info["fingerprint"], timeout=1.5)
    except (OSError, ValueError, http.client.HTTPException):
        return
    if checked and normalize_fingerprint(checked["fingerprint"]) == normalize_fingerprint(device["fingerprint"]):
        report_device(found, lock, checked, own_ips, info["fingerprint"])


def register_with(device, info):
    try:
        conn = open_connection(device["ip"], device["port"], device["protocol"], 2.0)
        conn.request("POST", "/api/localsend/v2/register", body=json.dumps(info),
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read(MAX_BODY)
        conn.close()
    except (OSError, http.client.HTTPException):
        pass


def probe_info(ip, port, protocol, fingerprint, timeout=0.7):
    """The device at ip:port, or None. Over https a device whose certificate
    does not match the fingerprint it claims is dropped, one that matches is
    marked verified."""
    conn = open_connection(ip, port, protocol, timeout)
    try:
        cert_fingerprint = peer_fingerprint(conn) if protocol == "https" else ""
        conn.request("GET", "/api/localsend/v2/info?fingerprint=%s" % quote(fingerprint))
        response = conn.getresponse()
        if response.status != 200:
            return None
        payload = read_json_body(response)
    finally:
        conn.close()
    device = sanitize_device(payload, ip, port, protocol)
    if device and protocol == "https":
        if not cert_fingerprint or normalize_fingerprint(device["fingerprint"]) != cert_fingerprint:
            return None
        device["verified"] = True
    return device


def scan_round(info, found, lock, own_ips, deadline):
    """Ask every host of the own /24 networks for its info; https then http."""
    targets = []
    seen = set()
    for ip, prefix, _name in own_ips:
        network = ipaddress.ip_network("%s/%d" % (ip, max(prefix, 24)), strict=False)
        for host in network.hosts():
            text = str(host)
            if text == ip or text in seen:
                continue
            seen.add(text)
            targets.append(text)
    if not targets:
        return

    def probe(target):
        if time.monotonic() >= deadline:
            return
        for protocol in ("https", "http"):
            try:
                device = probe_info(target, LOCALSEND_PORT, protocol, info["fingerprint"])
            except ssl.SSLError:
                continue
            except (OSError, ValueError, http.client.HTTPException):
                return
            if device:
                report_device(found, lock, device, own_ips, info["fingerprint"])
            return

    with ThreadPoolExecutor(max_workers=64) as pool:
        pool.map(probe, targets)


def cmd_discover(args):
    own_ips = own_ipv4_addresses()
    found = {}
    lock = threading.Lock()
    server = None
    try:
        server = RegisterServer(own_info(LOCALSEND_PORT, "http"), found, lock, own_ips)
        server.start()
        info = own_info(server.port, "http")
        server.info = info
    except OSError:
        info = own_info(LOCALSEND_PORT, "http")
    deadline = time.monotonic() + max(0.5, min(args.timeout, 10.0))
    threads = [threading.Thread(target=multicast_round, args=(info, found, lock, own_ips, deadline), daemon=True)]
    if not args.no_scan:
        threads.append(threading.Thread(target=scan_round, args=(info, found, lock, own_ips, deadline), daemon=True))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()) + 3.0)
    if server is not None:
        server.stop.set()
    emit({"event": "done", "count": len(found)})
    return 0


# ----------------------------------------------------------------------- send

class SendError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def clipboard_payload():
    """The clipboard as a LocalSend file entry: text stays text, an image
    becomes a PNG file, anything else is an error."""
    try:
        types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=5, check=False).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        types = []
    text_types = [t for t in types if t.startswith("text/")]
    if text_types:
        text_type = "text/plain;charset=utf-8" if "text/plain;charset=utf-8" in types else ("text/plain" if "text/plain" in types else text_types[0])
        data = subprocess.run(["wl-paste", "--no-newline", "--type", text_type], capture_output=True, timeout=10, check=False).stdout
        if not data.strip():
            raise SendError("clipboardEmpty", "clipboard is empty")
        return [text_entry(data.decode("utf-8", "replace"))]
    image_types = [t for t in types if t.startswith("image/")]
    if image_types:
        image_type = "image/png" if "image/png" in types else image_types[0]
        data = subprocess.run(["wl-paste", "--type", image_type], capture_output=True, timeout=30, check=False).stdout
        if not data:
            raise SendError("clipboardEmpty", "clipboard is empty")
        suffix = mimetypes.guess_extension(image_type) or ".png"
        handle = tempfile.NamedTemporaryFile(prefix="clipboard-", suffix=suffix, delete=False)
        handle.write(data)
        handle.close()
        return [dict(file_entry(handle.name, "clipboard" + suffix, image_type), _temporary=True)]
    raise SendError("clipboardEmpty", "clipboard is empty")


def text_entry(text):
    data = text.encode("utf-8")
    entry = {
        "id": uuid.uuid4().hex,
        "fileName": "%d.txt" % int(time.time() * 1000),
        "size": len(data),
        "fileType": "text/plain",
        "sha256": hashlib.sha256(data).hexdigest(),
        "_data": data,
    }
    if len(data) <= TEXT_PREVIEW_LIMIT:
        entry["preview"] = text
    return entry


def file_entry(path, name=None, mime=None):
    try:
        size = os.path.getsize(path)
    except OSError as error:
        raise SendError("fileMissing", str(error))
    if not os.path.isfile(path):
        raise SendError("fileMissing", "%s is not a file" % path)
    return {
        "id": uuid.uuid4().hex,
        "fileName": name or os.path.basename(path),
        "size": size,
        "fileType": mime or mimetypes.guess_type(path)[0] or "application/octet-stream",
        "_path": path,
    }


def cmd_send(args):
    try:
        ip = str(ipaddress.ip_address(args.ip))
    except ValueError:
        emit({"event": "error", "code": "badTarget", "message": "invalid ip"})
        return 2
    if not 1 <= args.port <= 65535 or args.protocol not in ("http", "https"):
        emit({"event": "error", "code": "badTarget", "message": "invalid port or protocol"})
        return 2
    fingerprint = normalize_fingerprint(args.fingerprint)
    if fingerprint and not all(c in "0123456789abcdef" for c in fingerprint):
        # Not a certificate hash, so there is nothing to check it against.
        fingerprint = ""
    entries = []
    try:
        entries = clipboard_payload() if args.clipboard else [file_entry(path) for path in args.files]
        if not entries:
            raise SendError("nothingToSend", "no files given")
        return run_transfer(ip, args.port, args.protocol, args.pin, entries, fingerprint)
    except SendError as error:
        emit({"event": "error", "code": error.code, "message": str(error)})
        return 1
    except KeyboardInterrupt:
        emit({"event": "error", "code": "cancelled", "message": "cancelled"})
        return 130
    finally:
        for entry in entries:
            if entry.get("_temporary"):
                try:
                    os.unlink(entry["_path"])
                except OSError:
                    pass


def run_transfer(ip, port, protocol, pin, entries, fingerprint=""):
    def connect(timeout):
        return open_connection(ip, port, protocol, timeout, fingerprint)

    info = own_info(LOCALSEND_PORT, "https")
    files = {}
    for entry in entries:
        files[entry["id"]] = {k: v for k, v in entry.items() if not k.startswith("_")}
    total_size = sum(entry["size"] for entry in entries)
    emit({"event": "waiting", "files": len(entries), "totalSize": total_size})

    # The receiver shows an accept dialog, so this request can legitimately
    # take as long as a person needs to notice it.
    try:
        conn = connect(180.0)
        path = "/api/localsend/v2/prepare-upload" + ("?pin=%s" % quote(pin) if pin else "")
        conn.request("POST", path, body=json.dumps({"info": info, "files": files}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        status = response.status
        payload = read_json_body(response) if status == 200 else response.read(MAX_BODY)
        conn.close()
    except FingerprintMismatch as error:
        raise SendError("fingerprintMismatch", str(error))
    except ssl.SSLError as error:
        raise SendError("unreachable", "tls: %s" % error)
    except (OSError, http.client.HTTPException) as error:
        raise SendError("unreachable", str(error))
    except ValueError as error:
        raise SendError("badResponse", str(error))

    if status == 204:
        # A text message travels inside prepare-upload itself (the preview);
        # a receiver that shows it and has nothing left to fetch answers 204.
        if all("preview" in entry for entry in entries):
            emit({"event": "done", "files": len(entries), "totalSize": total_size, "text": True})
            return 0
        raise SendError("nothingAccepted", "receiver accepted no files")
    if status == 401:
        raise SendError("pinRequired", "pin required or wrong")
    if status == 403:
        raise SendError("declined", "receiver declined")
    if status == 409:
        raise SendError("busy", "receiver is busy with another session")
    if status == 429:
        raise SendError("tooManyRequests", "too many requests")
    if status != 200:
        raise SendError("httpError", "prepare-upload returned %d" % status)

    session_id = clean_text(payload.get("sessionId") if isinstance(payload, dict) else "", 128)
    tokens = payload.get("files") if isinstance(payload, dict) else None
    if not session_id or not isinstance(tokens, dict):
        raise SendError("badResponse", "prepare-upload answer incomplete")
    accepted = [entry for entry in entries if isinstance(tokens.get(entry["id"]), str)]
    if not accepted:
        raise SendError("nothingAccepted", "receiver accepted no files")
    emit({"event": "accepted", "files": len(accepted), "totalSize": sum(e["size"] for e in accepted)})

    sent_total = 0
    accepted_total = sum(e["size"] for e in accepted)
    for index, entry in enumerate(accepted):
        token = tokens[entry["id"]]
        query = "/api/localsend/v2/upload?sessionId=%s&fileId=%s&token=%s" % (
            quote(session_id), quote(entry["id"]), quote(token))
        try:
            conn = connect(60.0)
            conn.putrequest("POST", query)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(entry["size"]))
            conn.endheaders()
            last_report = 0.0
            sent = 0

            def report(force=False):
                nonlocal last_report
                now = time.monotonic()
                if force or now - last_report >= 0.1:
                    last_report = now
                    emit({"event": "progress", "index": index, "count": len(accepted), "name": entry["fileName"],
                          "sent": sent, "size": entry["size"], "totalSent": sent_total + sent, "totalSize": accepted_total})

            report(True)
            if "_data" in entry:
                data = entry["_data"]
                for offset in range(0, len(data), UPLOAD_CHUNK):
                    conn.send(data[offset:offset + UPLOAD_CHUNK])
                    sent = min(len(data), offset + UPLOAD_CHUNK)
                    report()
            else:
                with open(entry["_path"], "rb") as handle:
                    while True:
                        chunk = handle.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)
                        report()
            response = conn.getresponse()
            response.read(MAX_BODY)
            conn.close()
        except KeyboardInterrupt:
            cancel_session(ip, port, protocol, session_id, fingerprint)
            raise
        except FingerprintMismatch as error:
            raise SendError("fingerprintMismatch", str(error))
        except (OSError, http.client.HTTPException) as error:
            cancel_session(ip, port, protocol, session_id, fingerprint)
            raise SendError("transferFailed", "%s: %s" % (entry["fileName"], error))
        if response.status != 200:
            cancel_session(ip, port, protocol, session_id, fingerprint)
            raise SendError("transferFailed", "%s: upload returned %d" % (entry["fileName"], response.status))
        report(True)
        sent_total += entry["size"]
    emit({"event": "done", "files": len(accepted), "totalSize": accepted_total})
    return 0


def quote(value):
    from urllib.parse import quote as url_quote
    return url_quote(str(value), safe="")


def cancel_session(ip, port, protocol, session_id, fingerprint=""):
    try:
        conn = open_connection(ip, port, protocol, 5.0, fingerprint)
        conn.request("POST", "/api/localsend/v2/cancel?sessionId=%s" % quote(session_id))
        conn.getresponse().read(MAX_BODY)
        conn.close()
    except (OSError, http.client.HTTPException, FingerprintMismatch):
        pass


# ----------------------------------------------------------------------- main

def _terminate(_signum, _frame):
    # The shell ends a cancelled transfer with SIGTERM; turn it into the same
    # path Ctrl-C takes so the receiver is told the session is cancelled.
    raise KeyboardInterrupt


def main(argv):
    signal.signal(signal.SIGTERM, _terminate)
    parser = argparse.ArgumentParser(prog="localsend-helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(run=cmd_status)
    discover = sub.add_parser("discover")
    discover.add_argument("--timeout", type=float, default=2.5)
    discover.add_argument("--no-scan", action="store_true")
    discover.set_defaults(run=cmd_discover)
    send = sub.add_parser("send")
    send.add_argument("--ip", required=True)
    send.add_argument("--port", type=int, default=LOCALSEND_PORT)
    send.add_argument("--protocol", default="https")
    send.add_argument("--fingerprint", default="")
    send.add_argument("--pin", default="")
    send.add_argument("--clipboard", action="store_true")
    send.add_argument("files", nargs="*")
    send.set_defaults(run=cmd_send)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
