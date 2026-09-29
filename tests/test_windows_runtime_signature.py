from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release/verify_windows_runtime.ps1"


@pytest.fixture
def signing_probe(tmp_path: Path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for the Windows signature verifier tests")
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alysis Test Publisher")])

    def certificate():
        key = ec.generate_private_key(ec.SECP256R1())
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=3))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.DER)
        )

    def probe(case: str, target: str = "win32-x64", *, unsigned: bool = False):
        cert = certificate()
        executable = tmp_path / f"alysis-{target}.exe"
        executable.write_bytes(b"unsigned fixture bytes")
        output = tmp_path / f"{case}-{target}.json"
        env = {
            **os.environ,
            "TEST_SIGNING_SCRIPT": str(SCRIPT),
            "TEST_EXECUTABLE": str(executable),
            "TEST_OUTPUT": str(output),
            "TEST_TARGET": target,
            "TEST_CERT": base64.b64encode(cert).decode("ascii"),
            "TEST_CASE": case,
        }
        # Mock only the operating system signature result. Exercise the real verifier,
        # certificate hashes, executable bytes, and evidence serialization.
        mock = r"""
        $cert = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
            [Convert]::FromBase64String($env:TEST_CERT))
        function Get-AuthenticodeSignature {
            param([string]$LiteralPath)
            [pscustomobject]@{
                Status = $(if ($env:TEST_CASE -eq 'invalid') { 'HashMismatch' } else { 'Valid' })
                SignatureType = $(if ($env:TEST_CASE -eq 'catalog') { 'Catalog' } else { 'Authenticode' })
                SignerCertificate = $(if ($env:TEST_CASE -eq 'no-signer') { $null } else { $cert })
                TimeStamperCertificate = $(if ($env:TEST_CASE -eq 'no-timestamp') { $null } else { $cert })
            }
        }
        """
        invocation = r"""
        $ErrorActionPreference = 'Stop'
        $subject = 'CN=Alysis Test Publisher'
        if ($env:TEST_CASE -eq 'wrong-publisher') { $subject = 'CN=Another Publisher' }
        if ($env:TEST_CASE -eq 'missing-subject') { $subject = ' ' }
        if ($env:TEST_CASE -eq 'multiline-subject') { $subject += "`nCN=Another Publisher" }
        & $env:TEST_SIGNING_SCRIPT -Executable $env:TEST_EXECUTABLE -Target $env:TEST_TARGET `
            -ExpectedSubject $subject -OutputPath $env:TEST_OUTPUT
        """
        result = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ("" if unsigned else mock) + invocation,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, output, cert, executable

    return probe


def test_verified_evidence_binds_each_rotating_certificate_and_executable(signing_probe):
    identities = []
    for target in ("win32-x64", "win32-arm64"):
        result, path, cert, executable = signing_probe("valid", target)
        assert result.returncode == 0, result.stderr
        evidence = json.loads(path.read_text(encoding="utf-8"))
        assert evidence["target"] == target
        assert evidence["executable"] == executable.name
        assert evidence["executableSha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
        assert evidence["signerIdentity"] == "sha256:" + hashlib.sha256(cert).hexdigest()
        assert evidence["signerThumbprint"] == hashlib.sha1(cert).hexdigest()
        assert evidence["timestampSignerIdentity"] == evidence["signerIdentity"]
        identities.append(evidence["signerIdentity"])
    assert identities[0] != identities[1]


@pytest.mark.parametrize(
    "case",
    [
        "invalid",
        "catalog",
        "no-signer",
        "no-timestamp",
        "wrong-publisher",
        "missing-subject",
        "multiline-subject",
    ],
)
def test_signature_verifier_rejects_untrusted_or_incomplete_evidence(signing_probe, case):
    result, path, _, _ = signing_probe(case)
    assert result.returncode != 0
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Real Authenticode inspection requires Windows")
def test_signature_verifier_rejects_an_actual_unsigned_file(signing_probe):
    result, path, _, _ = signing_probe("unsigned", unsigned=True)
    assert result.returncode != 0
    assert not path.exists()
