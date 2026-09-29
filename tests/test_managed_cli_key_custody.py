from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from scripts.release.verify_managed_cli_key_custody import (
    MAX_PRIVATE_KEY_BYTES,
    PUBLIC_KEY,
    ROOT,
    verify_key_custody,
)


def _pair():
    private = ec.generate_private_key(ec.SECP256R1())
    return private, private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _private_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def test_public_only_check_does_not_claim_private_custody():
    result = verify_key_custody(PUBLIC_KEY.read_bytes())
    assert result["private_key_match"] == "not_checked"
    assert result["protected_fingerprint_match"] == "not_checked"


def test_matching_key_and_der_fingerprint():
    key, public = _pair()
    fingerprint = hashlib.sha256(
        key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()
    result = verify_key_custody(
        public, private_pem=_private_pem(key), approved_fingerprint=fingerprint
    )
    assert result["private_key_match"] == result["protected_fingerprint_match"] == "passed"
    assert result["public_key_sha256"] == fingerprint
    assert "PRIVATE" not in json.dumps(result)


def test_rejects_a_different_private_key():
    _, public = _pair()
    wrong, _ = _pair()
    with pytest.raises(ValueError, match="does not byte-match"):
        verify_key_custody(public, private_pem=_private_pem(wrong))


def test_rejects_multiple_or_trailing_private_records():
    key, public = _pair()
    private = _private_pem(key)
    for suffix in (private, b"secret-sentinel"):
        with pytest.raises(ValueError, match="one unencrypted"):
            verify_key_custody(public, private_pem=private + suffix)


@pytest.mark.parametrize("fingerprint", ["0" * 64, "A" * 64, "secret-value"])
def test_rejects_bad_approved_fingerprints_without_echoing(fingerprint):
    _, public = _pair()
    with pytest.raises(ValueError) as error:
        verify_key_custody(public, approved_fingerprint=fingerprint)
    assert fingerprint not in str(error.value)


@pytest.mark.parametrize("kind", ["rsa", "p384", "encrypted", "pkcs8", "malformed", "oversized"])
def test_rejects_unsupported_or_malformed_private_keys(kind):
    key, public = _pair()
    if kind == "rsa":
        private = _private_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif kind == "p384":
        private = _private_pem(ec.generate_private_key(ec.SECP384R1()))
    elif kind in {"encrypted", "pkcs8"}:
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8
            if kind == "pkcs8"
            else serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(b"fixture-password")
            if kind == "encrypted"
            else serialization.NoEncryption(),
        )
    else:
        private = b"-----BEGIN EC PRIVATE KEY-----\nsecret-sentinel\n"
        if kind == "oversized":
            private += b"x" * MAX_PRIVATE_KEY_BYTES
    with pytest.raises(ValueError) as error:
        verify_key_custody(public, private_pem=private)
    assert "secret-sentinel" not in str(error.value)
    assert "fixture-password" not in str(error.value)


def test_cli_failure_never_prints_supplied_private_material():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/verify_managed_cli_key_custody.py"),
            "--private-key-stdin",
        ],
        input=b"-----BEGIN EC PRIVATE KEY-----\nprivate-sentinel\n",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert not result.stdout
    assert b"private-sentinel" not in result.stderr
    assert b"Traceback" not in result.stderr
