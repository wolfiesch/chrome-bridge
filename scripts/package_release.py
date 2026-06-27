#!/usr/bin/env python3
"""Build release artifacts for chrome-native-bridge."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from fnmatch import fnmatch
from pathlib import Path

RUST_BUILD_ERROR = "ERROR: Rust release binary not found; run cargo build --release --manifest-path host-rs/Cargo.toml"

EXCLUDED_NAMES = {
    ".git",
    "__pycache__",
    ".DS_Store",
    "bridge_token.txt",
    "bridge_tokens.txt",
    "bridge_tokens.txt.lock",
    "extension_key.pem",
    "com.automation.bridge.json",
    "com.automation.bridge.rust.json",
    "bridge-host-launch.sh",
    "bridge-host-python-launch.sh",
    "bridge_policy.json",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".log",
    ".pem",
    ".key",
}

EXCLUDED_GLOBS = {
    ".bridge_tokens.*",
    ".env",
    ".env.*",
    "bridge_policy.local.json",
    "bridge_token_*.txt",
    "com.automation.bridge*.json",
    "*.log",
    "*.pyc",
    "*.pem",
    "*.key",
    "*.secret",
    "*.secrets",
    "*.token",
    "*.tokens",
    "*.policy",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package chrome-native-bridge release artifacts")
    parser.add_argument("--version", required=True, help="Release version string used in artifact names")
    parser.add_argument("--dist", required=True, help="Directory for generated artifacts")
    return parser.parse_args()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def load_gitignore_patterns(repo_root: Path) -> dict[Path, list[str]]:
    patterns: dict[Path, list[str]] = {}
    for gitignore in repo_root.rglob(".gitignore"):
        if any(part == ".git" for part in gitignore.parts):
            continue
        base = gitignore.parent
        loaded: list[str] = []
        for raw_line in gitignore.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            loaded.append(line)
        if loaded:
            patterns[base] = loaded
    return patterns


def match_gitignore_pattern(relative: Path, pattern: str) -> bool:
    path = relative.as_posix()
    name = relative.name
    directory_pattern = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")

    if not pattern:
        return False

    if directory_pattern:
        return path == pattern or path.startswith(f"{pattern}/") or any(part == pattern for part in relative.parts)

    if "/" in pattern or anchored:
        return fnmatch(path, pattern) or path.startswith(f"{pattern}/")

    return fnmatch(name, pattern) or any(fnmatch(part, pattern) for part in relative.parts)


def ignored_by_gitignore(path: Path, repo_root: Path, patterns: dict[Path, list[str]]) -> bool:
    for base, base_patterns in patterns.items():
        if not is_relative_to(path, base):
            continue
        relative = path.relative_to(base)
        if any(match_gitignore_pattern(relative, pattern) for pattern in base_patterns):
            return True
    return False


def should_exclude(path: Path, repo_root: Path, dist_dir: Path, patterns: dict[Path, list[str]]) -> bool:
    relative = path.relative_to(repo_root)
    parts = relative.parts

    if is_relative_to(path, dist_dir):
        return True
    if ".git" in parts or "__pycache__" in parts:
        return True
    if len(parts) >= 2 and parts[0] == "host-rs" and parts[1] == "target":
        return True
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    if any(fnmatch(relative.as_posix(), pattern) or fnmatch(path.name, pattern) for pattern in EXCLUDED_GLOBS):
        return True
    if ignored_by_gitignore(path, repo_root, patterns):
        return True
    return False


def add_source_zip(repo_root: Path, dist_dir: Path, version: str) -> Path:
    source_zip = dist_dir / f"chrome-native-bridge-source-{version}.zip"
    patterns = load_gitignore_patterns(repo_root)
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(repo_root.rglob("*")):
            if should_exclude(path, repo_root, dist_dir, patterns):
                if path.is_dir():
                    continue
                continue
            if path.is_file():
                archive.write(path, path.relative_to(repo_root).as_posix())
    return source_zip


def add_extension_zip(repo_root: Path, dist_dir: Path, version: str) -> Path:
    extension_zip = dist_dir / f"chrome-native-bridge-extension-unpacked-{version}.zip"
    manifest = json.loads((repo_root / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("key", None)
    with zipfile.ZipFile(extension_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(repo_root / "background.js", "background.js")
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    return extension_zip


def cargo_target_dir(repo_root: Path) -> Path | None:
    try:
        result = subprocess.run(
            [
                "cargo",
                "metadata",
                "--format-version",
                "1",
                "--no-deps",
                "--manifest-path",
                str(repo_root / "host-rs" / "Cargo.toml"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(json.loads(result.stdout)["target_directory"])
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return None


def rust_binary_path(repo_root: Path) -> Path:
    target_dir = cargo_target_dir(repo_root)
    if target_dir is None:
        target_dir = repo_root / "host-rs" / "target"
    binary_name = "bridge-host.exe" if platform.system() == "Windows" else "bridge-host"
    return target_dir / "release" / binary_name


def runner_os_name() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    return "Linux"


def copy_rust_binary(repo_root: Path, dist_dir: Path, version: str) -> Path:
    rust_bin = rust_binary_path(repo_root)
    if not rust_bin.is_file():
        print(RUST_BUILD_ERROR, file=sys.stderr)
        raise SystemExit(2)
    output = dist_dir / f"bridge-host-{runner_os_name()}-{version}"
    shutil.copy2(rust_bin, output)
    output.chmod(output.stat().st_mode | 0o755)
    return output


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = Path(args.dist).expanduser().resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    add_source_zip(repo_root, dist_dir, args.version)
    add_extension_zip(repo_root, dist_dir, args.version)
    copy_rust_binary(repo_root, dist_dir, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
