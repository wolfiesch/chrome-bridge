#!/usr/bin/env python3
"""Offline contracts for the Chrome Bridge visual brand shell."""

import json
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FAILURES = []
ICON_SIZES = (16, 32, 48, 128)
EXTENSION_FILES = (
    "background.js",
    "manifest.json",
    "popup.html",
    "popup.css",
    "popup.js",
    "wake.html",
    "wake.js",
)


def expect(condition, message):
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def png_dimensions(path):
    data = path.read_bytes()
    expect(data.startswith(b"\x89PNG\r\n\x1a\n"), f"{path} must be a PNG")
    if len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def png_rgba_colors(path):
    """Read the small 8-bit RGBA icon without adding an image dependency."""
    data = path.read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    expect(data[24:26] == b"\x08\x06", f"{path} must use 8-bit RGBA pixels")
    compressed = bytearray()
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += 12 + length
    raw = zlib.decompress(bytes(compressed))
    stride = width * 4
    previous = bytearray(stride)
    colors = set()
    cursor = 0

    def paeth(left, above, upper_left):
        estimate = left + above - upper_left
        distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
        return (left, above, upper_left)[distances.index(min(distances))]

    for _row in range(height):
        filter_type = raw[cursor]
        cursor += 1
        scanline = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        for index in range(stride):
            left = scanline[index - 4] if index >= 4 else 0
            above = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 0xFF
            elif filter_type == 2:
                scanline[index] = (scanline[index] + above) & 0xFF
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                scanline[index] = (scanline[index] + paeth(left, above, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter {filter_type}")
        colors.update(tuple(scanline[index:index + 4]) for index in range(0, stride, 4))
        previous = scanline
    return colors


for base in (ROOT, ROOT / "extension"):
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    label = str(manifest_path.relative_to(ROOT))
    expect(manifest.get("name") == "Chrome Bridge", f"{label} must use the polished visible name")
    expect(manifest.get("short_name") == "Chrome Bridge", f"{label} must declare the short visible name")
    action = manifest.get("action", {})
    expect(action.get("default_title") == "Chrome Bridge", f"{label} must use the polished toolbar label")
    expect(action.get("default_popup") == "popup.html", f"{label} must expose the toolbar popup")
    expected_icons = {str(size): f"icons/icon-{size}.png" for size in ICON_SIZES}
    expect(manifest.get("icons") == expected_icons, f"{label} must declare the complete icon family")
    expect(action.get("default_icon") == expected_icons, f"{label} toolbar action must use the icon family")

    for size in ICON_SIZES:
        icon = base / "icons" / f"icon-{size}.png"
        expect(icon.is_file(), f"missing {icon.relative_to(ROOT)}")
        if icon.is_file():
            expect(png_dimensions(icon) == (size, size), f"{icon.relative_to(ROOT)} must be {size}x{size}")

    large_icon = base / "icons" / "icon-128.png"
    if large_icon.is_file():
        colors = png_rgba_colors(large_icon)
        expect((155, 108, 255, 255) in colors, f"{large_icon.relative_to(ROOT)} must retain the violet bridge pane")
        expect((71, 215, 200, 255) in colors, f"{large_icon.relative_to(ROOT)} must retain the teal bridge pane")

    for filename in ("popup.html", "popup.css", "popup.js"):
        expect((base / filename).is_file(), f"missing {(base / filename).relative_to(ROOT)}")

popup_html = (ROOT / "popup.html").read_text(encoding="utf-8") if (ROOT / "popup.html").exists() else ""
popup_css = (ROOT / "popup.css").read_text(encoding="utf-8") if (ROOT / "popup.css").exists() else ""
popup_js = (ROOT / "popup.js").read_text(encoding="utf-8") if (ROOT / "popup.js").exists() else ""
for needle in ("Chrome Bridge", "connection-status", "active-task", "pointer-toggle", "Quiet reads"):
    expect(needle in popup_html, f"popup.html missing {needle}")
expect("getBridgeStatus" in popup_js and "setBridgePreference" in popup_js,
       "popup.js must read live status and persist the pointer preference")
expect("prefers-reduced-motion" in popup_css, "popup must respect reduced-motion preferences")

for path in (ROOT / "background.js", ROOT / "extension" / "background.js"):
    text = path.read_text(encoding="utf-8")
    label = str(path.relative_to(ROOT))
    for needle in (
        "TASK_GROUP_STATES",
        "taskGroupTitle",
        "taskGroupColor",
        "updateTaskSessionState",
        "getBridgeStatus",
        "setBridgePreference",
        "showAgentPointer",
        "__chrome_bridge_pointer__",
        "__chrome_bridge_handoff__",
        "bottom:24px",
        "pointer-events:none",
    ):
        expect(needle in text, f"{label} missing brand-shell behavior: {needle}")
    expect('case "updateTaskSessionState"' in text,
           f"{label} must dispatch task state updates")

deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
for filename in EXTENSION_FILES:
    expect(filename in deploy, f"deploy.sh must copy {filename}")
expect('"$SCRIPT_DIR/icons"' in deploy, "deploy.sh must copy the icon directory")

packager = (ROOT / "scripts" / "package_release.py").read_text(encoding="utf-8")
for filename in EXTENSION_FILES:
    expect(filename in packager, f"release package must include {filename}")
expect("icons/icon-128.png" in packager, "release package must include the icon family")

cli = (ROOT / "test_client.py").read_text(encoding="utf-8")
expect('taskSession state' in cli and '"updateTaskSessionState"' in cli,
       "CLI must expose taskSession state")

mcp = (ROOT / "mcp" / "chrome_bridge_mcp" / "server.py").read_text(encoding="utf-8")
expect("browser_task_session_state" in mcp and 'call("updateTaskSessionState"' in mcp,
       "MCP must expose task-session state updates")

ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
expect("verify_brand_shell_contract.py" in ci, "CI must run the brand-shell contract")
expect("verify_brand_shell_behavior.mjs" in ci, "CI must run the brand-shell behavioral checks")

mockup = ROOT / "docs" / "design" / "chrome-bridge-brand-shell-mockup.png"
expect(mockup.is_file(), "selected visual target must be saved under docs/design")

if FAILURES:
    raise SystemExit(1)
print("Brand shell contract OK")
