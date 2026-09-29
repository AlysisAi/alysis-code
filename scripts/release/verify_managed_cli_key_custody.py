#!/usr/bin/env python3
"""Compare a custodian-supplied key with the release pin without retaining private material.

Default mode inspects only the checked-in public key. Optional private material is
read from a bounded stdin pipe inside the approved secret-management boundary;
it is never accepted as a command-line value, written to disk, or printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_KEY = ROOT / "extensions/vscode-alysis/resources/managed-cli-release-public.pem"
MAX_PRIVATE_KEY_BYTES = 16_384


def verify_key_custody(
    public_pem: bytes,
    *,
    private_pem: bytes | None = None,
    approved_fingerprint: str | None = None,
) -> dict[str, object]:
    """Return public facts only; all input failures use fixed, non-secret messages."""
    try:
        public_key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError("Pinned public key is not a valid public PEM.") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise ValueError("Pinned public key must use ECDSA P-256.")
    public_der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = hashlib.sha256(public_der).hexdigest()
    if approved_fingerprint is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", approved_fingerprint):
            raise ValueError("Approved fingerprint must be 64 lowercase hexadecimal characters.")
        if approved_fingerprint != fingerprint:
            raise ValueError("Approved fingerprint differs from the pinned public key.")
    if private_pem is not None:
        if len(private_pem) > MAX_PRIVATE_KEY_BYTES or not re.fullmatch(
            rb"-----BEGIN EC PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+"
            rb"-----END EC PRIVATE KEY-----\s*",
            private_pem,
        ):
            raise ValueError("Supply one unencrypted EC PRIVATE KEY PEM through stdin.")
        try:
            private_key = serialization.load_pem_private_key(private_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise ValueError("Private key could not be decoded as an unencrypted EC PEM.") from exc
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError("Private key must use ECDSA P-256.")
        derived_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        # The signing workflow uses diff against the OpenSSL-derived public PEM.
        if derived_pem != public_pem:
            raise ValueError("Private key does not byte-match the pinned public PEM.")
    return {
        "schema_name": "managed-cli-key-custody-check",
        "schema_version": 1,
        "public_key_sha256": fingerprint,
        "private_key_match": "passed" if private_pem is not None else "not_checked",
        "protected_fingerprint_match": (
            "passed" if approved_fingerprint is not None else "not_checked"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    parser.add_argument("--private-key-stdin", action="store_true")
    parser.add_argument("--approved-fingerprint")
    args = parser.parse_args()
    try:
        if args.private_key_stdin and sys.stdin.isatty():
            raise ValueError("Private key input must be a pipe, not an interactive terminal.")
        private_pem = (
            sys.stdin.buffer.read(MAX_PRIVATE_KEY_BYTES + 1) if args.private_key_stdin else None
        )
        result = verify_key_custody(
            args.public_key.read_bytes(),
            private_pem=private_pem,
            approved_fingerprint=args.approved_fingerprint,
        )
    except OSError:
        print("Key custody check failed: public key file could not be read.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Key custody check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
