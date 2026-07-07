# Handoff demo

`handoff_demo.py` is a narrated, screen-recordable launch demo for Chrome Bridge's real-profile human handoff. It shows an agent checking a redacted session summary, opening a login page in the user's real Chrome profile, pausing while the human completes login or 2FA, then resuming automatically to capture a redacted confirmation and screenshot.

The script is intentionally conservative: it prints only `loggedIn`, cookie count, tab id, extracted text length, and screenshot metadata. It does not print cookie values, raw page text, or page HTML.

## Prerequisites

1. Load the Chrome Bridge extension in Chrome.
2. Register and start the native host so the bridge is listening on `127.0.0.1:9223`.
3. Make sure `bridge_token.txt` exists in the repo root, or pass `--token-file`.
4. Allow the demo actions and target origin in policy. For the default GitHub demo:

```bash
python3 test_client.py policy allow-action sessionStatus
python3 test_client.py policy allow-action navigate
python3 test_client.py policy allow-action waitForHandoff
python3 test_client.py policy allow-action extractText
python3 test_client.py policy allow-action screenshot
python3 test_client.py policy allow-origin https://github.com
```

For another target, replace the origin with the scheme and host you pass to `--url` (for example, `https://example.com`; paths are not used in policy origin matches). If policy blocks a step, run:

```bash
python3 test_client.py policy doctor
```

## Run the demo

```bash
python3 examples/handoff_demo.py
```

For an actual recording, use a login URL that redirects to a post-login page and wait for that post-login-only URL substring:

```bash
python3 examples/handoff_demo.py \
  --url 'https://github.com/login?return_to=%2Fsettings%2Fprofile' \
  --wait-for github.com/settings/profile
```

Useful overrides and documented defaults:

```bash
python3 examples/handoff_demo.py \
  --url https://github.com/login \
  --wait-for github.com \
  --port 9223 \
  --token-file ./bridge_token.txt \
  --timeout-ms 120000
```

After a successful run, the screenshot is written to:

```text
/tmp/handoff_demo.png
```

## Recording recipe

1. Put the terminal on the left and Chrome on the right.
2. Start the macOS screen recorder with `Shift-Command-5`, select both windows, and record a roughly 60 second take.
3. Run the demo in the terminal.
4. When Chrome is focused, complete the login or 2FA step by hand. The agent resumes after the URL contains the `--wait-for` substring.
5. Stop the recording and convert the `.mov` to a GIF. Example:

```bash
ffmpeg -i handoff-demo.mov \
  -vf "fps=12,scale=1280:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 handoff-demo.gif
```

Keep the final GIF focused on the bridge behavior: redacted session probe, human-controlled login or 2FA, automatic resume, and the saved screenshot path.
