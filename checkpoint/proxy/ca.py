"""A short-lived certificate authority for Checkpoint's intercept proxy.

The proxy terminates TLS for routed hosts (api.github.com, *.supabase.co, ...)
with leaf certificates signed by this CA, so an agent that trusts the CA reaches
the local twins through its unmodified SDK. A fresh CA is minted per run and
expires after a day: only the agent under test ever trusts it, so it should not
outlive the run.

Files written to the output directory:

* ``ca.crt`` — the CA certificate (``NODE_EXTRA_CA_CERTS`` wants exactly this).
  Written last, so its appearance means every other file is complete.
* ``ca.key`` — its private key, mode 0600 where the OS supports it.
* ``bundle.pem`` — certifi's public roots plus ``ca.crt``. Pointing
  ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` here (not at
  ``ca.crt`` alone) keeps every NON-intercepted HTTPS call, such as the agent's
  own LLM traffic, verifying against the real roots.
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import ssl
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("checkpoint.proxy")

# Docker Desktop's VM clock can lag the host after sleep (worst on macOS);
# backdating notBefore keeps a freshly minted CA valid inside the container.
_CLOCK_SKEW = timedelta(hours=1)
_CA_COMMON_NAME = "Checkpoint intercept CA"
_ORGANIZATION = "Checkpoint"


def normalize_host(host: str) -> str:
    """Canonical form for matching and certificates: lowercase ASCII, no brackets or trailing dot.

    Python's ``sni_callback`` hands over IDNA-decoded Unicode, while CONNECT
    lines carry A-labels; normalizing both to A-labels makes them compare equal
    and keeps ``x509.DNSName`` (ASCII only) happy.
    """
    host = host.strip().rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host.isascii():
        # A host IDNA cannot encode is used as it came: the certificate will
        # not match it, which is the honest outcome, rather than a crash here.
        with contextlib.suppress(UnicodeError):
            host = host.encode("idna").decode("ascii")
    return host


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write atomically: readers (the harness container, `_wait_for_ca`) never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class CertificateAuthority:
    """A CA whose key signs per-host leaf certificates on demand.

    Build one with :meth:`create`. :meth:`server_context` is thread-safe and
    cached per host, so the proxy's event loop can call it mid-handshake.
    """

    def __init__(self, directory: Path, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey):
        self.directory = directory
        self._cert = cert
        self._key = key
        self._cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        # One key for every leaf: generating a key per host buys nothing (the CA
        # is the trust anchor) and would add a keygen to each first handshake.
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._leaf_key_pem = self._leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    @classmethod
    def create(cls, out_dir: str | os.PathLike[str], *, validity_hours: int = 24) -> CertificateAuthority:
        """Mint a new CA and write ``ca.key``, ``bundle.pem`` and ``ca.crt`` into ``out_dir``."""
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, _CA_COMMON_NAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _ORGANIZATION),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + timedelta(hours=validity_hours))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        _write_atomic(
            directory / "ca.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            mode=0o600,
        )
        roots = Path(certifi.where()).read_bytes().rstrip(b"\n")
        _write_atomic(directory / "bundle.pem", roots + b"\n" + cert_pem)
        _write_atomic(directory / "ca.crt", cert_pem)
        return cls(directory, cert, key)

    @property
    def cert_path(self) -> Path:
        return self.directory / "ca.crt"

    @property
    def key_path(self) -> Path:
        return self.directory / "ca.key"

    @property
    def bundle_path(self) -> Path:
        return self.directory / "bundle.pem"

    def server_context(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context presenting a leaf certificate for ``host``.

        The context also follows the client's SNI: if a client CONNECTs to one
        name but sends another in its ClientHello, it gets a certificate for the
        name it will actually verify.
        """
        host = normalize_host(host)
        with self._lock:
            ctx = self._contexts.get(host)
            if ctx is None:
                ctx = self._contexts[host] = self._build_context(host)
        return ctx

    def _build_context(self, host: str) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= ssl.OP_NO_RENEGOTIATION
        # Intercepted connections speak HTTP/1.1 only. Advertising exactly that
        # makes h2-capable clients (curl, Node, httpx[http2]) fall back cleanly
        # instead of starting an HTTP/2 session the proxy cannot parse.
        ctx.set_alpn_protocols(["http/1.1"])
        ctx.sni_callback = self._select_by_sni
        # SSLContext only loads key material from files; the file lives for the
        # duration of one call and is created 0600 by mkstemp.
        fd, path = tempfile.mkstemp(prefix="checkpoint-leaf-", suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self._issue_leaf(host) + self._cert_pem + self._leaf_key_pem)
            ctx.load_cert_chain(path)
        finally:
            os.unlink(path)
        return ctx

    def _issue_leaf(self, host: str) -> bytes:
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        now = datetime.now(UTC)
        # CN is capped at 64 characters by X.509; clients match on the SAN anyway.
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, host)] if len(host) <= 64 else []
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._cert.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(self._cert.not_valid_after_utc)
            .add_extension(x509.SubjectAlternativeName([san]), critical=not subject)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM)

    def _select_by_sni(self, sslobj: ssl.SSLObject | ssl.SSLSocket, server_name: str | None,
                       current: ssl.SSLContext) -> int | None:
        if not server_name:
            return None
        try:
            chosen = self.server_context(server_name)
        except Exception:
            # Raising here would surface as an "unraisable" traceback on stderr;
            # fail this one handshake instead.
            log.debug("could not issue a certificate for SNI %r", server_name, exc_info=True)
            return ssl.ALERT_DESCRIPTION_INTERNAL_ERROR
        if chosen is not current:
            sslobj.context = chosen
        return None
