#!/usr/bin/env python3
"""Manage deterministic local Chrome extension identity keys."""
import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys


def _run(cmd):
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        print(f"Error: missing executable: {cmd[0]}", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            sys.stderr.write(exc.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(exc.returncode or 1)


def ensure_private_key(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        _run(["openssl", "genrsa", "-out", path, "2048"])
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def public_key_der(path: str) -> bytes:
    return _run(["openssl", "rsa", "-in", path, "-pubout", "-outform", "DER"]).stdout


def manifest_key(path: str) -> str:
    return base64.b64encode(public_key_der(path)).decode("ascii")


def extension_id_from_der(der: bytes) -> str:
    alphabet = "abcdefghijklmnop"
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(alphabet[int(ch, 16)] for ch in digest)


def write_keyed_manifest(source_manifest: str, output_manifest: str, key_path: str) -> str:
    ensure_private_key(key_path)
    der = public_key_der(key_path)
    with open(source_manifest, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["key"] = base64.b64encode(der).decode("ascii")
    os.makedirs(os.path.dirname(os.path.abspath(output_manifest)) or ".", exist_ok=True)
    with open(output_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return extension_id_from_der(der)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ensure", help="create a local private key if needed")
    p.add_argument("--key", required=True)

    p = sub.add_parser("id", help="print the extension id for a private key")
    p.add_argument("--key", required=True)

    p = sub.add_parser("write-manifest", help="write a keyed manifest and print its extension id")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--key", required=True)

    args = parser.parse_args()
    if args.command == "ensure":
        ensure_private_key(args.key)
        return 0
    if args.command == "id":
        ensure_private_key(args.key)
        print(extension_id_from_der(public_key_der(args.key)))
        return 0
    if args.command == "write-manifest":
        print(write_keyed_manifest(args.source, args.output, args.key))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
