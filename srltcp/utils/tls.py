"""Localhost TLS certificate management."""

from __future__ import annotations

import datetime
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from srltcp.utils.files import ensure_dir
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

log = get_logger(__name__)

CERT_DIR = data_dir() / "certs"
CERT_FILE = CERT_DIR / "localhost.crt"
KEY_FILE = CERT_DIR / "localhost.key"


def ensure_localhost_cert() -> tuple[Path, Path]:
    """Create or load a self-signed cert for 127.0.0.1 / localhost."""
    ensure_dir(CERT_DIR)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    log.info("Generating localhost TLS certificate in %s", CERT_DIR)
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "SRLTCP Localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SRLTCP"),
        ]
    )
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CERT_FILE, KEY_FILE


def create_ssl_context() -> ssl.SSLContext:
    """TLS 1.2+ context for local HTTPS (localhost only)."""
    cert_path, key_path = ensure_localhost_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS")
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    ctx.options |= ssl.OP_NO_RENEGOTIATION
    return ctx