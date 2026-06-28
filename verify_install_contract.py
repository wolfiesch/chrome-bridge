#!/usr/bin/env python3
"""Offline contract test for extension identity and install/deploy scripts."""
import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import extension_identity

SCRIPT_DIR = Path(__file__).resolve().parent
failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=SCRIPT_DIR, text=True, capture_output=True, **kwargs)


def last_json(stdout):
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise ValueError(f"no JSON object in output: {stdout!r}")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def visible_names(path):
    return sorted(p.name for p in Path(path).iterdir() if not p.name.startswith("."))


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        key = tmp / "extension_key.pem"
        out_manifest = tmp / "manifest.json"

        r = run([sys.executable, "extension_identity.py", "ensure", "--key", str(key)])
        expect(r.returncode == 0, f"ensure failed: {r.stderr}")
        expect(key.exists(), "ensure should create key")
        if os.name == "posix":
            expect(mode(key) == 0o600, f"key mode should be 0600, got {oct(mode(key))}")

        r = run([sys.executable, "extension_identity.py", "write-manifest",
                 "--source", "manifest.json", "--output", str(out_manifest), "--key", str(key)])
        expect(r.returncode == 0, f"write-manifest failed: {r.stderr}")
        cli_id = r.stdout.strip()
        manifest = json.loads(out_manifest.read_text())
        root_manifest = json.loads((SCRIPT_DIR / "manifest.json").read_text())
        expect("key" in manifest, "keyed output manifest should include key")
        expect("storage" in manifest.get("permissions", []), "keyed manifest should keep storage permission")
        expect("key" not in root_manifest, "root manifest must remain unkeyed")
        der = base64.b64decode(manifest["key"])
        expect(extension_identity.extension_id_from_der(der) == cli_id,
               "CLI extension ID should match independently derived ID")

        ext_dir = tmp / "extension"
        r = run(["./deploy.sh", "--ext", str(ext_dir), "--with-local-key", "--key-file", str(key)])
        expect(r.returncode == 0, f"keyed deploy failed: {r.stderr}")
        expect(visible_names(ext_dir) == ["background.js", "manifest.json", "wake.html", "wake.js"],
               f"extension deploy should contain background.js, manifest.json, wake.html, and wake.js, got {visible_names(ext_dir)}")
        deployed = json.loads((ext_dir / "manifest.json").read_text())
        expect("key" in deployed, "keyed deploy manifest should include key")

        missing_mode_dir = tmp / "missing-mode-extension"
        r = run(["./deploy.sh", "--ext", str(missing_mode_dir)])
        expect(r.returncode != 0, "deploy without manifest mode should fail")
        expect("ERROR: choose exactly one extension manifest mode" in r.stderr,
               f"missing mode error mismatch: {r.stderr}")

        host_dir = tmp / "host"
        r = run(["./deploy.sh", "--host", str(host_dir), "--copy-policy", "--copy-token"])
        expect(r.returncode == 0, f"host deploy failed: {r.stderr}")
        expect((host_dir / "bridge_policy.json").exists(), "host policy should be copied")
        if os.name == "posix":
            expect(mode(host_dir / "bridge_policy.json") == 0o600,
                   f"host policy mode should be 0600, got {oct(mode(host_dir / 'bridge_policy.json'))}")
            if (SCRIPT_DIR / "bridge_token.txt").exists():
                expect(mode(host_dir / "bridge_token.txt") == 0o600,
                       f"host token mode should be 0600, got {oct(mode(host_dir / 'bridge_token.txt'))}")

        custom = {"default": {"allowedActions": ["ping"]}}
        policy_path = host_dir / "bridge_policy.json"
        policy_path.write_text(json.dumps(custom))
        try:
            os.chmod(policy_path, 0o644)
        except OSError:
            pass
        r = run(["./deploy.sh", "--host", str(host_dir), "--copy-policy"])
        expect(r.returncode == 0, f"host redeploy failed: {r.stderr}")
        expect(json.loads(policy_path.read_text()) == custom,
               "deploy --copy-policy must not overwrite an existing custom policy")
        if os.name == "posix":
            expect(mode(policy_path) == 0o600,
                   f"host redeploy should restrict existing broad policy to 0600, got {oct(mode(policy_path))}")

        install_env = os.environ.copy()
        install_env["HOME"] = str(tmp / "home")
        install_env["XDG_CONFIG_HOME"] = str(tmp / "xdg-config")
        state_dir = tmp / "state"
        r = run([
            "./setup.sh",
            "--state-dir", str(state_dir),
            "--ext", str(tmp / "extension"),
            "--host-port", "19223",
            "--print-json",
        ], env=install_env)
        expect(r.returncode == 0, f"setup state-dir failed: {r.stderr}")
        if r.returncode == 0:
            setup_info = last_json(r.stdout)
            launcher = Path(setup_info["launcher"])
            expect(launcher.exists(), "setup state-dir launcher should exist")
            expect('BRIDGE_PORT="${BRIDGE_PORT:-19223}"' in launcher.read_text(),
                   "setup state-dir launcher should use host port 19223")
            expect((state_dir / "extension_id.txt").exists(),
                   "setup state-dir should write extension_id.txt")
            expect(setup_info.get("extensionIdFile") == str(state_dir / "extension_id.txt"),
                   "setup JSON should include extensionIdFile")
            expect(setup_info.get("hostPort") == "19223",
                   "setup JSON should include hostPort 19223")

        rust_state_dir = tmp / "state-rs"
        r = run([
            "./setup-rs.sh",
            "--state-dir", str(rust_state_dir),
            "--ext", str(tmp / "extension-rs"),
            "--host-port", "19223",
            "--print-json",
        ], env=install_env)
        if r.returncode == 0:
            setup_info = last_json(r.stdout)
            launcher = Path(setup_info["launcher"])
            expect(launcher.exists(), "setup-rs state-dir launcher should exist")
            expect('BRIDGE_PORT="${BRIDGE_PORT:-19223}"' in launcher.read_text(),
                   "setup-rs state-dir launcher should use host port 19223")
            expect((rust_state_dir / "extension_id.txt").exists(),
                   "setup-rs state-dir should write extension_id.txt")
            expect(setup_info.get("extensionIdFile") == str(rust_state_dir / "extension_id.txt"),
                   "setup-rs JSON should include extensionIdFile")
            expect(setup_info.get("hostPort") == "19223",
                   "setup-rs JSON should include hostPort 19223")
        else:
            expect("Build the Rust host first" in (r.stdout + r.stderr),
                   f"setup-rs missing build-first message: stdout={r.stdout} stderr={r.stderr}")

    if failures:
        print(f"\n{len(failures)} install contract failure(s).")
        return 1
    print("Install contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
