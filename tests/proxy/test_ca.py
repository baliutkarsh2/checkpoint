"""CertificateAuthority: the files it writes and the leaf certificates it serves."""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta

import certifi
import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from checkpoint.proxy.ca import CertificateAuthority, normalize_host


def _handshake(ca: CertificateAuthority, server_host: str, sni: str | None,
               client: ssl.SSLContext) -> ssl.SSLSocket:
    """TLS handshake against ``ca.server_context(server_host)`` over a socketpair."""
    server_sock, client_sock = socket.socketpair()
    server = ca.server_context(server_host).wrap_socket(server_sock, server_side=True,
                                                       do_handshake_on_connect=False)
    thread = threading.Thread(target=lambda: _quietly(server.do_handshake), daemon=True)
    thread.start()
    tls = client.wrap_socket(client_sock, server_hostname=sni)
    thread.join(5)
    server.close()
    return tls


def _quietly(fn) -> None:
    try:
        fn()
    except (ssl.SSLError, OSError):
        pass


def _peer_cert(tls: ssl.SSLSocket) -> x509.Certificate:
    return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))


def test_create_writes_cert_key_and_bundle(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    assert ca.cert_path == tmp_path / "ca.crt" and ca.cert_path.is_file()
    assert ca.key_path == tmp_path / "ca.key" and ca.key_path.is_file()
    assert ca.bundle_path == tmp_path / "bundle.pem" and ca.bundle_path.is_file()
    assert not list(tmp_path.glob("*.tmp")), "atomic writes must not leave temp files"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_private_key_is_owner_only(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    assert os.stat(ca.key_path).st_mode & 0o777 == 0o600


def test_bundle_is_public_roots_plus_our_ca(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    bundle = ca.bundle_path.read_text()
    assert bundle.endswith(ca.cert_path.read_text())
    marker = "-----BEGIN CERTIFICATE-----"
    with open(certifi.where()) as roots:
        assert bundle.count(marker) == roots.read().count(marker) + 1


def test_ca_certificate_properties(tmp_path):
    cert = x509.load_pem_x509_certificate(CertificateAuthority.create(tmp_path).cert_path.read_bytes())
    assert "Checkpoint intercept CA" in cert.subject.rfc4514_string()
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    now = datetime.now(UTC)
    # notBefore is backdated for container clock skew; validity is about a day.
    assert timedelta(minutes=30) <= now - cert.not_valid_before_utc <= timedelta(hours=2)
    assert timedelta(hours=23) <= cert.not_valid_after_utc - now <= timedelta(hours=25)


def test_each_ca_is_unique(tmp_path):
    a = CertificateAuthority.create(tmp_path / "a").cert_path.read_bytes()
    b = CertificateAuthority.create(tmp_path / "b").cert_path.read_bytes()
    assert x509.load_pem_x509_certificate(a).serial_number != x509.load_pem_x509_certificate(b).serial_number


def test_leaf_verifies_against_the_ca_with_serverauth_and_http11_only(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    client = ssl.create_default_context(cafile=str(ca.cert_path))
    client.set_alpn_protocols(["h2", "http/1.1"])
    tls = _handshake(ca, "api.github.com", "api.github.com", client)
    leaf = _peer_cert(tls)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["api.github.com"]
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert tls.selected_alpn_protocol() == "http/1.1"  # never h2: the proxy parses HTTP/1.1
    tls.close()


def test_ip_literal_gets_an_ip_san(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    client = ssl.create_default_context(cafile=str(ca.cert_path))
    tls = _handshake(ca, "127.0.0.1", "127.0.0.1", client)
    san = _peer_cert(tls).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]
    tls.close()


def test_certificate_follows_sni_when_it_differs_from_the_requested_host(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    client = ssl.create_default_context(cafile=str(ca.cert_path))
    tls = _handshake(ca, "connect-target.example", "uploads.github.com", client)
    san = _peer_cert(tls).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["uploads.github.com"]
    tls.close()


def test_contexts_are_cached_per_normalized_host(tmp_path):
    ca = CertificateAuthority.create(tmp_path)
    assert ca.server_context("API.GitHub.com.") is ca.server_context("api.github.com")
    assert ca.server_context("api.github.com") is not ca.server_context("slack.com")


def test_long_hostnames_are_supported(tmp_path):
    host = ("a" * 60) + ".example.test"  # longer than X.509's 64-char CN limit
    ca = CertificateAuthority.create(tmp_path)
    client = ssl.create_default_context(cafile=str(ca.cert_path))
    tls = _handshake(ca, host, host, client)
    san = _peer_cert(tls).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [host]
    tls.close()


def test_normalize_host():
    assert normalize_host(" API.Example.COM. ") == "api.example.com"
    assert normalize_host("[::1]") == "::1"
    assert normalize_host("bücher.example") == "xn--bcher-kva.example"
