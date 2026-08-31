from datetime import UTC, datetime, timedelta

from cryptography import x509

from checkpoint.proxy.ca import mint_ca


def test_mint_ca_writes_cert_and_key(tmp_path):
    out = mint_ca(tmp_path)
    assert out == tmp_path / "ca.crt"
    assert (tmp_path / "ca.crt").is_file()
    assert (tmp_path / "ca.key").is_file()


def test_mint_ca_cert_is_parsable(tmp_path):
    out = mint_ca(tmp_path)
    cert = x509.load_pem_x509_certificate(out.read_bytes())
    cn = cert.subject.rfc4514_string()
    assert "checkpoint-sidecar-ca" in cn


def test_mint_ca_validity_window(tmp_path):
    out = mint_ca(tmp_path)
    cert = x509.load_pem_x509_certificate(out.read_bytes())
    now = datetime.now(UTC)
    nb = cert.not_valid_before_utc
    na = cert.not_valid_after_utc
    # notBefore is in the past (clock-skew buffer); within last 2h.
    assert nb <= now
    assert (now - nb) >= timedelta(minutes=30)
    # cert valid for at least 23h.
    assert (na - nb) >= timedelta(hours=23)


def test_mint_ca_produces_unique_serials(tmp_path):
    out1 = mint_ca(tmp_path / "a")
    out2 = mint_ca(tmp_path / "b")
    c1 = x509.load_pem_x509_certificate(out1.read_bytes())
    c2 = x509.load_pem_x509_certificate(out2.read_bytes())
    assert c1.serial_number != c2.serial_number
