"""Make the Ed25519 release key pair (docs/07-updates.md).

    python scripts/new_signing_key.py [--dir DIR]

Writes the private key to ``DIR\\release.key`` (default ``%USERPROFILE%\\.basecamp-signing``)
and the public key to ``basecamp/release_key.pub`` in the repo, and prints the public key
only. Refuses to replace a key that's already there. Back the private key up (password
manager or a USB stick): without it no new release can be installed by the PCs that trust
this public key.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parent.parent
PUBLIC_KEY_FILE = REPO / "basecamp" / "release_key.pub"
DEFAULT_DIR = Path(os.path.expandvars(r"%USERPROFILE%")) / ".basecamp-signing"


def make_key(key_dir: Path, pub_file: Path) -> str:
    key_path = key_dir / "release.key"
    if key_path.exists():
        raise SystemExit(f"{key_path} already exists; not replacing it")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    key_dir.mkdir(parents=True, exist_ok=True)
    # O_EXCL: never overwrite, even in a race.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    public = base64.b64encode(raw).decode()
    pub_file.write_text("# Basecamp release public key (Ed25519, base64). The private key\n"
                        "# is on the dev PC only; see docs/07-updates.md.\n" + public + "\n",
                        encoding="utf-8", newline="\n")
    return public


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--pub", type=Path, default=PUBLIC_KEY_FILE,
                        help="where to write the public key (default: the repo's)")
    args = parser.parse_args()
    public = make_key(args.dir, args.pub)
    print(f"Public key (written to {args.pub}):")
    print(public)
    print(f"Private key written to {args.dir / 'release.key'} (not shown).", file=sys.stderr)
    print("Back up that folder now (password manager or USB stick).", file=sys.stderr)


if __name__ == "__main__":
    main()
