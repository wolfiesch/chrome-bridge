#!/usr/bin/env python3
"""Offline contract for GitHub-specific attachment/comment actions."""
from pathlib import Path
import json

SCRIPT_DIR = Path(__file__).resolve().parent
failed = False


def fail(message):
    global failed
    failed = True
    print(f"FAIL: {message}")


background = (SCRIPT_DIR / "background.js").read_text(encoding="utf-8")
policy = json.loads((SCRIPT_DIR / "bridge_policy.example.json").read_text(encoding="utf-8"))
client_actions = policy["clients"]["default"]["allowedActions"]
confirm_actions = policy["clients"]["default"].get("requireConfirmation", [])

for action in ["githubAttachUploadedFiles", "githubSubmitComment", "githubAttachPrBody"]:
    if action not in background:
        fail(f"background.js missing {action} dispatch")
    if action not in client_actions:
        fail(f"bridge_policy.example.json must allow {action} for the default client")
    if action in confirm_actions:
        fail(f"{action} must not be confirmation-gated like executeScript*")

for needle in [
    "closest('file-attachment')",
    ".attach(input.files)",
    "Uploading",
    "Close with comment",
    "Comment",
    "Add comment",
    ".js-command-palette-pull-body",
    "DOM.setFileInputFiles",
    "githubPrBodyAttachAndSaveExpression",
    "Update comment",
    "matched.closest?.('button, a, input, select, textarea, [role]')",
    "Matched element is not clickable",
]:
    if needle not in background:
        fail(f"background.js missing GitHub attachment/comment needle {needle}")

if "const assetPattern = /user-attachments\\\\/assets\\\\/" not in background:
    fail("background.js must poll for GitHub user-attachments/assets markdown")
if r"https:\/\/github\.com\/user-attachments\/assets\/" not in background:
    fail("PR-body helper must wait for full GitHub CDN attachment URLs")
if "githubAttachPrBody(payload.tabId, payload.files, payload.timeoutMs)" not in background:
    fail("background.js must dispatch the first-class GitHub PR-body helper")
if "if (result?.success === false) throw new Error(result.err || 'GitHub PR-body attachment failed')" not in background:
    fail("GitHub PR-body helper failures must reach CLI and MCP callers as failures")
if "Expected exactly one GitHub pull-request body save button" not in background:
    fail("GitHub PR-body helper must fail closed rather than guessing a save button")

gate_start = background.find("async function assertGitHubTab")
gate_end = background.find("function githubAttachExpression", gate_start)
gate_source = background[gate_start:gate_end]
if 'origin !== "https://github.com"' not in gate_source:
    fail("assertGitHubTab must reject non-GitHub tab origins internally")

submit_start = background.find("function githubSubmitExpression")
submit_end = background.find("async function githubAttachUploadedFiles", submit_start)
submit_source = background[submit_start:submit_end]
if "querySelector('form')" in submit_source:
    fail("githubSubmitExpression must not fall back to a generic first form")
if "No GitHub comment form matched formSelector" not in background:
    fail("GitHub actions must fail closed when explicit formSelector matches nothing")
if "commentForms.length === 1" not in submit_source:
    fail("githubSubmitExpression must only use implicit .js-comment-form when it is unique")

if failed:
    raise SystemExit(1)
print("GitHub attachment contract OK")
