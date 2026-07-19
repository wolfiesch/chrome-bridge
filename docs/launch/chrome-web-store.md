# DRAFT - Chrome Web Store submission prep only

No Chrome Web Store package may be submitted, uploaded, or published from this document without explicit approval.

## Listing copy

### Name

Chrome Bridge

### Subtitle

Chrome Native Messaging Automation Bridge

### Summary

Let trusted local agents control your real Chrome profile through a policy-governed native messaging bridge.

### Detailed description

Chrome Bridge is a trusted-local automation bridge for people who want agents and local tools to operate the Chrome profile they already use, without launching Chrome with `--remote-debugging-port`, using an empty automation profile, or handing browsing activity to a cloud browser.

The extension is the Chrome-side half of a local native-messaging system. It connects only to the native host installed by the user on the same machine. Local clients then talk to that host over `127.0.0.1:9223` with a shared local token. The native host applies the bridge policy before forwarding actions to Chrome: it is fail-closed by default, supports explicit action and origin allow-lists, writes local audit records, redacts sensitive response fields, and can require confirmation tokens for higher-risk actions.

Chrome Bridge is built for controlled real-profile handoff rather than generic scraping. Agents can inspect tabs, navigate, wait for selectors or text, click and type, upload files, take screenshots, collect redacted diagnostics, and use the existing signed-in browser session when policy permits. For sensitive moments such as login, 2FA, captcha, passkeys, or payment confirmation, `waitForHandoff` focuses the tab, shows an in-page banner, pauses automation, and waits for the user to finish the step. `sessionStatus` can check whether the real profile appears signed in to a domain by reporting cookie names and counts, not cookie values.

The extension does not include a remote service. It is not useful by itself: users must install and register the local native host from the Chrome Bridge repository, then keep the local token, policy file, native-host manifest, audit logs, and generated extension identity private. Because this extension can control the user's real browser profile when paired with the host and an approved policy, it should be installed only on machines the user controls and only for trusted local automation workflows.

## Manifest permissions and host permissions

Source checked: `manifest.json` version `1.0.1`.

| Manifest entry | Type | Web Store review justification |
|---|---:|---|
| `nativeMessaging` | Permission | Required for the extension's core function: connecting the MV3 service worker to the locally installed native host named `com.automation.bridge`. The extension does not operate as a standalone browser feature; browser actions are requested by local clients, authorized by the token-gated native host, checked against the local policy, and then delivered to the extension through Chrome native messaging. Without this permission, Chrome Bridge cannot receive policy-approved commands from the local host or return action results to the local client. |
| `tabs` | Permission | Required to implement tab lifecycle and real-profile handoff features: listing tabs, opening a URL in a tab, activating/focusing a tab, closing/reloading a tab, reading current tab metadata needed for origin checks, waiting for tab load or URL changes, and capturing the visible tab for screenshots. The host policy evaluates tab-scoped actions against the live tab origin before forwarding them, and tab information is treated as sensitive because it can reveal browsing context. |
| `scripting` | Permission | Required for page-level automation that cannot be performed through tab metadata alone, including controlled script execution in the page's main world, semantic selectors for click/type/fill flows, DOM text/HTML extraction, file-upload interaction, and the temporary `waitForHandoff` banner. Script execution is gated by the native host policy and site-origin rules, and response redaction is applied by policy where configured. |
| `activeTab` | Permission | Required as a narrow user-context permission for operations on the currently active tab, especially visible-tab capture and active-page interactions initiated through the bridge. Chrome Bridge primarily uses explicit `tabId` operations, but the real-profile workflow also supports resolving the currently active tab when a local client asks to operate on the browser state the user is viewing. This permission supports that handoff-oriented behavior without requiring the extension to inject page code on every page at install time. |
| `cookies` | Permission | Required for redacted real-profile session checks and storage-state export. `sessionStatus` reports cookie names, counts, and a derived signed-in signal without returning cookie values; `getCookies` and `storageState` are available only when the native host policy allows them, and policy redaction replaces cookie values before responses reach clients by default. This permission is sensitive because cookie metadata can reveal account and site usage, so the listing and privacy policy should explain that all access remains local and policy-governed. |
| `debugger` | Permission | Required for Chrome DevTools Protocol-backed automation capabilities that Chrome extension APIs do not otherwise expose: console and network monitoring, request interception, JavaScript dialog handling, screenshots/viewport and emulation controls, geolocation overrides, performance metrics, and CDP script execution. The bridge attaches the debugger only for policy-approved actions and detaches when the action or monitoring session ends; long-lived monitoring and interception explicitly remain attached until stopped. This is one of the highest-scrutiny permissions and should be justified with a demo showing the local policy gate. |
| `alarms` | Permission | Required to keep the MV3 service worker connected to the native host across service-worker suspension, browser idle periods, and machine sleep/wake. The background worker uses heartbeat and reconnect alarms to retry `connectNative()` with bounded backoff so the local bridge remains reliable without a persistent always-on extension page. This permission is not used for tracking, scheduling remote work, or background data collection. |
| `storage` | Permission | Required to persist transient service-worker reconnect backoff state across MV3 suspension. The background worker prefers `chrome.storage.session` so state resets with the browser session and falls back to `storage.local` only when needed. Chrome Bridge does not use extension storage to collect browsing history, page content, cookies, or analytics. |
| `downloads` | Permission | Required for the `downloadUrl` action, which asks Chrome to download a policy-approved URL into Chrome's configured downloads location. The bridge does not silently choose arbitrary absolute output paths; Chrome's own download handling and user settings still apply. The local host policy can deny or confirm download actions before they reach the extension. |
| `contentSettings` | Permission | Required for controlled geolocation workflows. `setGeolocation` grants location permission for the current tab origin through Chrome content settings before applying a CDP geolocation override, and `clearGeolocation` resets that origin back to `ask`. This permission is used only for policy-approved geolocation actions and should be explained as part of test automation and user-approved real-profile handoff, not general site preference management. |
| `<all_urls>` | Host permission | Required because Chrome Bridge is a user-directed automation bridge for the user's real Chrome profile, and the local policy determines which origins may be automated at runtime. The extension must be technically capable of operating on arbitrary sites the user chooses: tab origin checks, page interaction, screenshots, content extraction, cookies for requested domains, monitoring, interception, downloads, and handoff flows all need access across potential web origins. The native host's fail-closed policy, action/origin allow-lists, confirmation tokens, local audit log, and redaction controls are the intended governance layer for this broad host permission. |

## Review risk and expected reviewer questions

This is a maximum-scrutiny permission set. `debugger`, `<all_urls>`, `cookies`, and `nativeMessaging` together allow deep control of a user's real Chrome profile when the local host and policy allow it. The submission should assume manual review and should not rely on generic automation language. The review narrative must consistently explain the trusted-local model: local host, loopback-only TCP API, shared local token, fail-closed policy, origin/action allow-lists, redaction, confirmation tokens, and local audit logs.

The extension is useless without the locally installed native host. A reviewer may install only the CRX/Web Store item and see no user-facing product unless the native host registration has also been run. The listing, support material, and demo should make this dependency explicit and show the supported install flow.

Review may require a demo video. The demo should show installation of the native host, the extension connecting through native messaging, a denied action under default policy, a policy-approved simple action, redacted `sessionStatus`, and `waitForHandoff` pausing for a human login/2FA-style step. The video should avoid exposing personal accounts, cookies, raw tab URLs, audit-log secrets, or local absolute paths.

Review may require a privacy policy URL even if no remote data is collected. Prepare a public privacy policy before submission; do not submit with only this local draft.

## Privacy policy skeleton

### Data collection

Chrome Bridge does not collect, sell, share, or transmit user data to the developer or to any remote server operated by the developer.

### Local-only operation

The extension communicates with a native host installed by the user on the same machine through Chrome's native messaging API. Local clients communicate with that host over the loopback interface (`127.0.0.1`). There is no cloud relay, hosted browser session, analytics endpoint, telemetry endpoint, or remote command service in the extension.

### Browser data access

When the user installs the native host and allows actions in the local policy, Chrome Bridge can access browser state needed for automation, including tab metadata, page content, screenshots, downloads, content settings for geolocation tests, debugger data, cookie metadata, and cookie values for policy-approved cookie/storage actions. The default security posture is fail-closed, and sensitive outputs such as cookie values can be redacted by policy before they reach local clients.

### Local secrets and audit logs

The shared token, optional token registry, policy file, native-host manifest, generated extension identity, debug logs, and audit logs remain on the user's machine. Audit logs are local JSONL records of request metadata and decisions; they intentionally omit payload and response bodies. Users are responsible for keeping these local files private.

### Human handoff

`waitForHandoff` is designed for sensitive steps that an agent should not perform. It focuses the tab, shows a local in-page banner, waits for the user to complete the step, and then resumes automation when the requested condition is met. It does not read, import, or overwrite authentication secrets.

## Required asset checklist

- [ ] 128 px extension icon. Blocker: the repository currently ships no icon assets and `manifest.json` does not declare an `icons` block.
- [ ] 440 x 280 small promotional tile.
- [ ] 1280 x 800 screenshots showing the native-host install, extension status, a policy-denied action, a policy-approved action, redacted `sessionStatus`, and `waitForHandoff`.
- [ ] Demo video for manual review, with a clean browser profile or scrubbed test account and no exposed local secrets, cookies, raw logs, or identity-revealing local paths.
- [ ] Public privacy policy URL based on the skeleton above.
- [ ] Support/contact URL or email for Web Store listing fields.
- [ ] Reviewer notes explaining that the extension requires the separately installed native host and local policy file.

## Store-ID migration note

A Web Store-published extension receives a store-managed extension ID that differs from the local developer-mode unpacked ID. Users who install the Web Store build must register that store ID with the local native host before Chrome native messaging will connect.

The README already documents this flow in two places: the advanced setup section says to run `./setup.sh --extension-id <id>` for an already packaged or future Web Store extension ID, and the release packaging section repeats the store-specific command:

```bash
./setup.sh --extension-id <store-id>
```

Launch materials should instruct Web Store users to run that command after installing Chrome Bridge from the store, replacing `<store-id>` with the ID shown by Chrome for the published extension. Users should then reload/enable only one bridge extension at a time so duplicate bridge instances do not race to bind port `9223`.
