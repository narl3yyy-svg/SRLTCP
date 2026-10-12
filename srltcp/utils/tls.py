"""Localhost TLS certificate management."""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from srltcp.utils.files import ensure_dir
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir, is_android

log = get_logger(__name__)

def _cert_paths() -> tuple[Path, Path, Path]:
    # Resolved lazily: on Android the data dir is only known after startup.
    cert_dir = data_dir() / "certs"
    return cert_dir, cert_dir / "localhost.crt", cert_dir / "localhost.key"


def localhost_cert_sha256() -> str:
    """Hex SHA-256 fingerprint of the localhost certificate in DER form."""
    cert_path, _key = ensure_localhost_cert()
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


def ensure_localhost_cert() -> tuple[Path, Path]:
    """Create or load a self-signed cert for 127.0.0.1 / localhost."""
    CERT_DIR, CERT_FILE, KEY_FILE = _cert_paths()  # noqa: N806
    ensure_dir(CERT_DIR)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    log.info("Generating localhost TLS certificate in %s", CERT_DIR)
    key_bits = 2048 if is_android() else 4096
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_bits)
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

    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
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