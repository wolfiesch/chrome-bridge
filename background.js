let nativePort = null;
const HEARTBEAT_ALARM = "chromeBridgeHeartbeat";
const HEARTBEAT_MINUTES = 0.5;
const RECONNECT_ALARM = "chromeBridgeReconnect";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_FACTOR = 2;
const RECONNECT_CAP_MS = 30000;
// Persist retry state across SW suspension. A bare module variable is lost when
// the MV3 service worker suspends, so we keep backoff state in chrome.storage.
// The manifest grants "storage"; prefer storage.session (resets on browser
// restart, no disk churn) and fall back to storage.local, then an in-memory copy
// so the worker never throws even if storage is unavailable.
const reconnectStore =
  (chrome.storage && (chrome.storage.session || chrome.storage.local)) || null;
let reconnectStateFallback = { attempt: 0, delay: RECONNECT_BASE_MS };

async function getReconnectState() {
  if (!reconnectStore) return { ...reconnectStateFallback };
  const data = await reconnectStore.get("reconnectState");
  return data.reconnectState || { attempt: 0, delay: RECONNECT_BASE_MS };
}

async function setReconnectState(state) {
  reconnectStateFallback = state;
  if (!reconnectStore) return;
  await reconnectStore.set({ reconnectState: state });
}

async function resetBackoff() {
  await setReconnectState({ attempt: 0, delay: RECONNECT_BASE_MS });
  await chrome.alarms.clear(RECONNECT_ALARM);
}

async function scheduleReconnect() {
  const state = await getReconnectState();
  const currentDelay = state.delay || RECONNECT_BASE_MS;
  // Durable mechanism: an alarm survives SW suspension. Alarms only fire on a
  // ~30s granularity in practice, so also fire an OPPORTUNISTIC immediate
  // setTimeout fast-path; the alarm remains the authoritative retry trigger.
  const jitter = Math.random() * 0.3 * currentDelay;
  const delayMs = Math.min(currentDelay + jitter, RECONNECT_CAP_MS);
  chrome.alarms.create(RECONNECT_ALARM, { delayInMinutes: delayMs / 60000 });
  setTimeout(connectToHost, delayMs);
  const nextDelay = Math.min(currentDelay * RECONNECT_FACTOR, RECONNECT_CAP_MS);
  await setReconnectState({ attempt: (state.attempt || 0) + 1, delay: nextDelay });
}
const monitors = new Map();
const interceptors = new Map();
const MONITOR_LIMIT = 200;

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source.tabId) return;

  if (method === "Fetch.requestPaused" && interceptors.has(source.tabId)) {
    const interceptor = interceptors.get(source.tabId);
    const request = params.request || {};
    const redacted = redactUrl(request.url || "");
    const record = {
      requestId: params.requestId,
      ts: Date.now(),
      url: redacted.url,
      hasQuery: redacted.hasQuery,
      method: request.method || "GET",
      resourceType: params.resourceType || "Document"
    };
    pushLimited(interceptor.requests, record);

    const mode = interceptor.mode;
    const target = { tabId: source.tabId };

    if (mode === "continue") {
      chrome.debugger.sendCommand(target, "Fetch.continueRequest", {
        requestId: params.requestId
      }, (result) => {
        if (chrome.runtime.lastError) {
          console.warn("Fetch.continueRequest failed:", chrome.runtime.lastError.message);
        }
      });
    } else if (mode === "abort") {
      chrome.debugger.sendCommand(target, "Fetch.failRequest", {
        requestId: params.requestId,
        errorReason: "Aborted"
      }, (result) => {
        if (chrome.runtime.lastError) {
          console.warn("Fetch.failRequest failed:", chrome.runtime.lastError.message);
        }
      });
    } else if (mode === "fulfill") {
      const responseCode = interceptor.status ?? 200;
      const responseHeaders = [
        { name: "Content-Type", value: "text/plain" }
      ];
      const encodedBody = toBase64(interceptor.body || "");
      chrome.debugger.sendCommand(target, "Fetch.fulfillRequest", {
        requestId: params.requestId,
        responseCode,
        responseHeaders,
        body: encodedBody
      }, (result) => {
        if (chrome.runtime.lastError) {
          console.warn("Fetch.fulfillRequest failed:", chrome.runtime.lastError.message);
        }
      });
    }
    return;
  }

  if (!monitors.has(source.tabId)) return;
  const monitor = monitors.get(source.tabId);
  const ts = Date.now();

  if (method === "Runtime.consoleAPICalled") {
    pushLimited(monitor.console, {
      ts,
      type: params.type || "console",
      level: params.type || "log",
      text: (params.args || []).map(stringifyRemoteValue).join(" "),
      args: (params.args || []).map(stringifyRemoteValue)
    });
    return;
  }

  if (method === "Log.entryAdded") {
    const entry = params.entry || {};
    pushLimited(monitor.console, {
      ts,
      type: "log",
      level: entry.level || "info",
      text: entry.text || "",
      args: []
    });
    return;
  }

  if (method === "Network.requestWillBeSent") {
    const request = params.request || {};
    const redacted = redactUrl(request.url || "");
    monitor.network.set(params.requestId, {
      requestId: params.requestId,
      ts,
      method: request.method || "GET",
      url: redacted.url,
      hasQuery: redacted.hasQuery,
      type: params.type || null,
      status: null,
      mimeType: null
    });
    trimNetwork(monitor.network);
    return;
  }

  if (method === "Network.responseReceived") {
    const response = params.response || {};
    const existing = monitor.network.get(params.requestId);
    if (existing) {
      existing.status = response.status ?? null;
      existing.mimeType = response.mimeType || null;
    }
    return;
  }

  if (method === "Page.javascriptDialogOpening") {
    pushLimited(monitor.dialogs, {
      ts,
      type: params.type || null,
      message: params.message || "",
      defaultPrompt: params.defaultPrompt || ""
    });
  }
});

function scheduleHeartbeat() {
  chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: HEARTBEAT_MINUTES });
}

function sendHeartbeat() {
  if (!nativePort) {
    connectToHost();
    return;
  }
  try {
    nativePort.postMessage({ action: "heartbeat", ts: Date.now() });
  } catch (error) {
    console.warn("Heartbeat failed:", error);
    nativePort = null;
    // Don't wait for the next heartbeat alarm; schedule a backed-off reconnect now.
    scheduleReconnect();
  }
}
function connectToHost() {
  if (nativePort) return;
  const hostName = "com.automation.bridge";
  console.log("Connecting to native host:", hostName);
  try {
    nativePort = chrome.runtime.connectNative(hostName);
  } catch (error) {
    console.error("Failed to connect native host:", error);
    nativePort = null;
    scheduleReconnect();
    return;
  }

  nativePort.onMessage.addListener((message) => {
    console.log("Received message from native host:", message);
    handleMessageFromHost(message);
  });

  nativePort.onDisconnect.addListener(() => {
    console.warn("Disconnected from native host:", chrome.runtime.lastError);
    nativePort = null;
    scheduleReconnect();
  });

  // Connection established: reset backoff and clear any pending reconnect alarm.
  resetBackoff();
}

chrome.runtime.onInstalled.addListener(() => {
  scheduleHeartbeat();
  connectToHost();
});
chrome.runtime.onStartup.addListener(() => {
  scheduleHeartbeat();
  connectToHost();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) sendHeartbeat();
  else if (alarm.name === RECONNECT_ALARM) connectToHost();
});
scheduleHeartbeat();

async function handleMessageFromHost(message) {
  const { id, action, payload } = message;
  try {
    const result = await dispatchAction(action, payload);
    sendResponseToHost({ id, success: true, result });
  } catch (error) {
    sendResponseToHost({ id, success: false, error: error.message });
  }
}

async function runBatch(steps, defaultTabId) {
  if (!Array.isArray(steps)) {
    throw new Error("batch requires a steps array");
  }
  const results = [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i] || {};
    if (step.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, step.delayMs));
    }
    if (!step.action) {
      results.push(null);
      continue;
    }
    const stepPayload = step.payload ? { ...step.payload } : {};
    if (stepPayload.tabId === undefined && defaultTabId !== undefined) {
      stepPayload.tabId = defaultTabId;
    }
    try {
      const stepResult = await dispatchAction(step.action, stepPayload);
      if (stepResult && typeof stepResult === "object" && stepResult.success === false) {
        throw new Error(stepResult.error || "step reported success=false");
      }
      results.push(stepResult);
    } catch (error) {
      throw new Error(`batch step ${i} (${step.action}) failed: ${error.message}`);
    }
  }
  return results;
}

async function dispatchAction(action, payload) {
    let result;
    switch (action) {
      case "batch":
        result = await runBatch(payload.steps, payload.tabId);
        break;
      case "ping":
        result = "pong";
        break;
      case "navigate":
        result = await navigateToUrl(payload.url);
        break;
      case "getTabs":
        result = await getTabs();
        break;
      case "executeScript":
        result = await runScriptInTab(payload.tabId, payload.code);
        break;
      case "executeScriptCDP":
        result = await runScriptWithDebugger(payload.tabId, payload.code);
        break;
      case "click":
        result = await clickSelector(payload.tabId, payload.selector);
        break;
      case "type":
        result = await typeSelector(payload.tabId, payload.selector, payload.text);
        break;
      case "observe":
        result = await observeTab(payload.tabId);
        break;
      case "getCookies":
        result = await chrome.cookies.getAll({ domain: payload.domain });
        break;
      case "activateTab":
        result = await activateTab(payload.tabId);
        break;
      case "closeTab":
        result = await closeTab(payload.tabId);
        break;
      case "reload":
        result = await reloadTab(payload.tabId);
        break;
      case "goBack":
        result = await goHistory(payload.tabId, -1);
        break;
      case "goForward":
        result = await goHistory(payload.tabId, 1);
        break;
      case "waitForLoad":
        result = await waitForLoad(payload.tabId, payload.timeoutMs);
        break;
      case "waitForSelector":
        result = await waitForSelector(payload.tabId, payload.selector, payload.timeoutMs);
        break;
      case "waitForText":
        result = await waitForText(payload.tabId, payload.text, payload.timeoutMs);
        break;
      case "waitForUrl":
        result = await waitForUrl(payload.tabId, payload.substring, payload.timeoutMs);
        break;
      case "getCurrentState":
        result = await getCurrentState(payload.tabId);
        break;
      case "screenshot":
        result = await captureScreenshot(payload.tabId, payload.format);
        break;
      case "extractText":
        result = await extractText(payload.tabId, payload.maxChars);
        break;
      case "getHTML":
        result = await getHTML(payload.tabId);
        break;
      case "hover":
        result = await hoverSelector(payload.tabId, payload.selector);
        break;
      case "scroll":
        result = await scrollTarget(payload.tabId, payload.deltaX, payload.deltaY, payload.selector);
        break;
      case "press":
        result = await pressKey(payload.tabId, payload.key);
        break;
      case "drag":
        result = await dragSelector(payload.tabId, payload.fromSelector, payload.toSelector);
        break;
      case "fill":
        result = await fillSelector(payload.tabId, payload.selector, payload.text);
        break;
      case "select":
        result = await selectOption(payload.tabId, payload.selector, payload.value);
        break;
      case "uploadFile":
        result = await uploadFile(payload.tabId, payload.selector, payload.files);
        break;
      case "setViewport":
        result = await setViewport(payload.tabId, payload.width, payload.height, payload.deviceScaleFactor);
        break;
      case "setCpuThrottling":
        result = await setCpuThrottling(payload.tabId, payload.rate);
        break;
      case "setNetworkConditions":
        result = await setNetworkConditions(payload.tabId, payload.offline, payload.latency, payload.downloadThroughput, payload.uploadThroughput);
        break;
      case "clearNetworkConditions":
        result = await clearNetworkConditions(payload.tabId);
        break;
      case "setColorScheme":
        result = await setColorScheme(payload.tabId, payload.scheme);
        break;
      case "setUserAgent":
        result = await setUserAgent(payload.tabId, payload.userAgent);
        break;
      case "startMonitoring":
        result = await startMonitoring(payload.tabId);
        break;
      case "stopMonitoring":
        result = await stopMonitoring(payload.tabId);
        break;
      case "consoleMessages":
        result = consoleMessages(payload.tabId);
        break;
      case "networkRequests":
        result = networkRequests(payload.tabId);
        break;
      case "handleDialog":
        result = await handleDialog(payload.tabId, payload.accept, payload.promptText);
        break;
      case "downloadUrl":
        result = await downloadUrl(payload.url, payload.filename);
        break;
      case "storageState":
        result = await getStorageState(payload.tabId);
        break;
      case "setGeolocation":
        result = await setGeolocation(payload.tabId, payload.latitude, payload.longitude, payload.accuracy);
        break;
      case "clearGeolocation":
        result = await clearGeolocation(payload.tabId);
        break;
      case "startInterception":
        result = await startInterception(payload.tabId, payload.urlPattern, payload.mode, payload.status, payload.body);
        break;
      case "stopInterception":
        result = await stopInterception(payload.tabId);
        break;
      case "interceptedRequests":
        result = interceptedRequests(payload.tabId);
        break;
      case "performanceMetrics":
        result = await performanceMetrics(payload.tabId);
        break;
      case "sessionStatus":
        result = await sessionStatus(payload.domains);
        break;
      case "waitForHandoff":
        result = await waitForHandoff(payload);
        break;
      case "__tabOrigin":
        result = await tabOrigin(payload.tabId);
        break;
      default:
        throw new Error(`Unsupported action: ${action}`);
    }
    return result;
}

function sendResponseToHost(response) {
  if (nativePort) {
    nativePort.postMessage(response);
  } else {
    console.error("Cannot send response, nativePort is disconnected.");
  }
}

async function navigateToUrl(url) {
  const tab = await chrome.tabs.create({ url });
  return { tabId: tab.id };
}

async function getTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs.map((tab) => ({
    id: tab.id,
    windowId: tab.windowId,
    active: tab.active,
    highlighted: tab.highlighted,
    title: tab.title,
    url: tab.url,
    status: tab.status
  }));
}

// Reserved internal action used by the native host's tab-origin policy check.
// Returns only the target tab's URL/origin (no page content) so the host can
// evaluate site policy for tab-scoped actions before forwarding them. When no
// tabId is given, resolves the active tab, then the first tab.
async function tabOrigin(tabId) {
  let tab;
  if (tabId === undefined || tabId === null) {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    tab = tabs && tabs[0];
    if (!tab) {
      const all = await chrome.tabs.query({});
      tab = all && all[0];
    }
  } else {
    tab = await chrome.tabs.get(tabId);
  }
  if (!tab) throw new Error("no such tab");
  let origin = null;
  try {
    origin = tab.url ? new URL(tab.url).origin : null;
  } catch (e) {
    origin = null;
  }
  return { tabId: tab.id ?? null, url: tab.url || null, origin };
}

async function activateTab(tabId) {
  const tab = await chrome.tabs.update(tabId, { active: true });
  if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
  return { success: true, tabId, windowId: tab.windowId ?? null };
}

async function closeTab(tabId) {
  await chrome.tabs.remove(tabId);
  return { success: true, tabId };
}

async function reloadTab(tabId) {
  await chrome.tabs.reload(tabId);
  return { success: true, tabId };
}

async function goHistory(tabId, delta) {
  return withDebugger(tabId, async (target) => {
    const history = await debuggerCommand(target, 'Page.getNavigationHistory', {});
    const targetIndex = history.currentIndex + delta;
    if (targetIndex < 0 || targetIndex >= history.entries.length) {
      return { success: false, err: "No history entry in requested direction" };
    }
    const entryId = history.entries[targetIndex].id;
    await debuggerCommand(target, 'Page.navigateToHistoryEntry', { entryId });
    return { success: true, tabId, entryId };
  });
}

async function runScriptInTab(tabId, code) {
  const response = await chrome.scripting.executeScript({
    target: { tabId: tabId },
    world: 'MAIN',
    func: (codeString) => {
      try {
        return { success: true, val: (0, eval)(codeString) };
      } catch (err) {
        return { success: false, err: err.message };
      }
    },
    args: [code]
  });
  return response[0].result;
}

function debuggerAttach(target) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach(target, '1.3', () => {
      const err = chrome.runtime.lastError;
      err ? reject(new Error(err.message)) : resolve();
    });
  });
}

function debuggerCommand(target, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params, (result) => {
      const err = chrome.runtime.lastError;
      err ? reject(new Error(err.message)) : resolve(result);
    });
  });
}

function debuggerDetach(target) {
  return new Promise((resolve) => {
    chrome.debugger.detach(target, () => resolve());
  });
}

async function withDebugger(tabId, fn) {
  const target = { tabId };
  if (monitors.has(tabId) || interceptors.has(tabId)) return fn(target);
  await debuggerAttach(target);
  try {
    return await fn(target);
  } finally {
    await debuggerDetach(target);
  }
}

async function evaluateWithDebugger(target, expression) {
  const result = await debuggerCommand(target, 'Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    allowUnsafeEvalBlockedByCSP: true
  });
  if (result.exceptionDetails) {
    return { success: false, err: result.exceptionDetails.text || 'Runtime.evaluate exception', details: result.exceptionDetails };
  }
  return { success: true, val: result.result?.value ?? result.result?.description ?? null };
}

async function runScriptWithDebugger(tabId, code) {
  return withDebugger(tabId, (target) => evaluateWithDebugger(target, code));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function deadlineFrom(timeoutMs) {
  return Date.now() + Math.max(0, timeoutMs || 10000);
}

async function waitForLoad(tabId, timeoutMs) {
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return { success: true, tabId, status: "complete" };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for tab load", timeoutMs };
}

async function waitForUrl(tabId, substring, timeoutMs) {
  if (!substring) return { success: false, err: "Missing URL substring" };
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    const tab = await chrome.tabs.get(tabId);
    const url = tab.url || "";
    if (url.includes(substring)) return { success: true, tabId, url };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for URL", substring, timeoutMs };
}

async function waitForSelector(tabId, selector, timeoutMs) {
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    const found = await withDebugger(tabId, (target) => evaluateWithDebugger(target, `Boolean(document.querySelector(${JSON.stringify(selector)}))`));
    if (found.success && found.val === true) return { success: true, selector };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for selector", selector, timeoutMs };
}

async function waitForText(tabId, text, timeoutMs) {
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    const found = await withDebugger(tabId, (target) => evaluateWithDebugger(target, `(document.body?.innerText || '').includes(${JSON.stringify(text)})`));
    if (found.success && found.val === true) return { success: true, text };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for text", text, timeoutMs };
}

async function getCurrentState(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const observe = await observeTab(tabId);
  return {
    success: true,
    tab: {
      id: tab.id,
      windowId: tab.windowId,
      active: tab.active,
      status: tab.status,
      title: tab.title,
      url: tab.url
    },
    observe
  };
}

async function captureScreenshot(tabId, format) {
  const activated = await activateTab(tabId);
  const dataUrl = await chrome.tabs.captureVisibleTab(activated.windowId, { format: format || "png" });
  return { success: true, mimeType: "image/png", dataUrl };
}

async function extractText(tabId, maxChars) {
  const limit = maxChars || 20000;
  return withDebugger(tabId, async (target) => {
    const result = await evaluateWithDebugger(target, `(() => {
      const raw = document.body ? document.body.innerText : document.documentElement.innerText;
      const text = raw || '';
      const limit = ${JSON.stringify(limit)};
      return { text: text.slice(0, limit), originalLength: text.length };
    })()`);
    if (!result.success) return result;
    return {
      success: true,
      text: result.val.text,
      truncated: result.val.originalLength > limit,
      chars: result.val.text.length
    };
  });
}

async function getHTML(tabId) {
  return withDebugger(tabId, async (target) => {
    const result = await evaluateWithDebugger(target, 'document.documentElement.outerHTML');
    if (!result.success) return result;
    return { success: true, html: result.val || "" };
  });
}

async function getElementCenter(target, selector) {
  const lookup = await evaluateWithDebugger(target, `(() => {
    const selector = ${JSON.stringify(selector)};
    const el = document.querySelector(selector);
    if (!el) return { success: false, err: 'No element matched selector: ' + selector };
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const rect = el.getBoundingClientRect();
    return {
      success: true,
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      tagName: el.tagName,
      text: el.innerText || el.value || el.getAttribute('aria-label') || '',
      value: 'value' in el ? el.value : null
    };
  })()`);
  if (!lookup.success || lookup.val?.success === false) return lookup.val || lookup;
  return lookup.val;
}

async function clickSelector(tabId, selector) {
  return withDebugger(tabId, async (target) => {
    const lookup = await getElementCenter(target, selector);
    if (lookup.success === false) return lookup;
    const { x, y } = lookup;
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
    return { success: true, tagName: lookup.tagName, text: lookup.text };
  });
}

async function typeSelector(tabId, selector, text) {
  return withDebugger(tabId, async (target) => {
    const focus = await evaluateWithDebugger(target, `(() => {
      const selector = ${JSON.stringify(selector)};
      const el = document.querySelector(selector);
      if (!el) return { success: false, err: 'No element matched selector: ' + selector };
      el.scrollIntoView({ block: 'center', inline: 'center' });
      el.focus();
      return { success: true, tagName: el.tagName };
    })()`);
    if (!focus.success || focus.val?.success === false) return focus.val || focus;
    await debuggerCommand(target, 'Input.insertText', { text });
    const value = await evaluateWithDebugger(target, `(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      return el ? ('value' in el ? el.value : el.textContent) : null;
    })()`);
    return { success: true, tagName: focus.val.tagName, value: value.val };
  });
}

async function hoverSelector(tabId, selector) {
  return withDebugger(tabId, async (target) => {
    const lookup = await getElementCenter(target, selector);
    if (lookup.success === false) return lookup;
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x: lookup.x, y: lookup.y, button: 'none' });
    return { success: true, tagName: lookup.tagName, text: lookup.text };
  });
}

async function scrollTarget(tabId, deltaX, deltaY, selector) {
  return withDebugger(tabId, async (target) => {
    let point;
    if (selector) {
      point = await getElementCenter(target, selector);
      if (point.success === false) return point;
    } else {
      const center = await evaluateWithDebugger(target, '({ x: innerWidth / 2, y: innerHeight / 2 })');
      if (!center.success) return center;
      point = center.val;
    }
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseWheel', x: point.x, y: point.y, deltaX, deltaY });
    return { success: true, deltaX, deltaY };
  });
}

function keyDefinition(key) {
  const map = {
    Enter: { key: 'Enter', code: 'Enter', vk: 13 },
    Escape: { key: 'Escape', code: 'Escape', vk: 27 },
    Tab: { key: 'Tab', code: 'Tab', vk: 9 },
    Backspace: { key: 'Backspace', code: 'Backspace', vk: 8 },
    Delete: { key: 'Delete', code: 'Delete', vk: 46 },
    ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', vk: 38 },
    ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
    ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
    ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
    Home: { key: 'Home', code: 'Home', vk: 36 },
    End: { key: 'End', code: 'End', vk: 35 },
    PageUp: { key: 'PageUp', code: 'PageUp', vk: 33 },
    PageDown: { key: 'PageDown', code: 'PageDown', vk: 34 },
    Space: { key: ' ', code: 'Space', vk: 32 }
  };
  if (map[key]) return map[key];
  if (key.length === 1) {
    const upper = key.toUpperCase();
    return { key, code: `Key${upper}`, vk: upper.charCodeAt(0) };
  }
  return null;
}

async function pressKey(tabId, keySpec) {
  return withDebugger(tabId, async (target) => {
    const parts = String(keySpec || '').split('+').filter(Boolean);
    const key = parts.pop();
    const modifierMap = { Alt: 1, Ctrl: 2, Control: 2, Meta: 4, Command: 4, Cmd: 4, Shift: 8 };
    let modifiers = 0;
    for (const part of parts) modifiers |= modifierMap[part] || 0;
    const def = keyDefinition(key);
    if (!def) return { success: false, err: `Unsupported key: ${key}` };
    if (key.length === 1 && modifiers === 0) {
      await debuggerCommand(target, 'Input.insertText', { text: key });
      return { success: true, key: keySpec };
    }
    const event = {
      key: def.key,
      code: def.code,
      windowsVirtualKeyCode: def.vk,
      nativeVirtualKeyCode: def.vk,
      modifiers
    };
    await debuggerCommand(target, 'Input.dispatchKeyEvent', { ...event, type: 'keyDown' });
    await debuggerCommand(target, 'Input.dispatchKeyEvent', { ...event, type: 'keyUp' });
    return { success: true, key: keySpec };
  });
}

async function dragSelector(tabId, fromSelector, toSelector) {
  return withDebugger(tabId, async (target) => {
    const from = await getElementCenter(target, fromSelector);
    if (from.success === false) return from;
    const to = await getElementCenter(target, toSelector);
    if (to.success === false) return to;
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x: from.x, y: from.y, button: 'none', buttons: 0 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mousePressed', x: from.x, y: from.y, button: 'left', buttons: 1, clickCount: 1 });
    for (let step = 1; step <= 5; step++) {
      const x = from.x + ((to.x - from.x) * step) / 5;
      const y = from.y + ((to.y - from.y) * step) / 5;
      await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'left', buttons: 1 });
    }
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', buttons: 0, clickCount: 1 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x: to.x, y: to.y, button: 'none', buttons: 0 });
    return { success: true, from: fromSelector, to: toSelector };
  });
}

async function fillSelector(tabId, selector, text) {
  return withDebugger(tabId, async (target) => {
    const focus = await evaluateWithDebugger(target, `(() => {
      const selector = ${JSON.stringify(selector)};
      const el = document.querySelector(selector);
      if (!el) return { success: false, err: 'No element matched selector: ' + selector };
      el.scrollIntoView({ block: 'center', inline: 'center' });
      el.focus();
      if ('value' in el) {
        el.value = '';
      } else {
        el.textContent = '';
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
      return { success: true, tagName: el.tagName };
    })()`);
    if (!focus.success || focus.val?.success === false) return focus.val || focus;
    await debuggerCommand(target, 'Input.insertText', { text });
    const value = await evaluateWithDebugger(target, `(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      return el ? ('value' in el ? el.value : el.textContent) : null;
    })()`);
    return { success: true, tagName: focus.val.tagName, value: value.val };
  });
}

async function selectOption(tabId, selector, value) {
  return withDebugger(tabId, async (target) => {
    const result = await evaluateWithDebugger(target, `(() => {
      const selector = ${JSON.stringify(selector)};
      const value = ${JSON.stringify(value)};
      const el = document.querySelector(selector);
      if (!el) return { success: false, err: 'No element matched selector: ' + selector };
      if (el.tagName !== 'SELECT') return { success: false, err: 'Element is not a SELECT: ' + selector };
      const option = Array.from(el.options).find((item) => item.value === value || item.text === value);
      if (!option) return { success: false, err: 'No option matched value/text: ' + value };
      el.value = option.value;
      option.selected = true;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { success: true, value: el.value, selectedText: option.text };
    })()`);
    return result.val || result;
  });
}

async function uploadFile(tabId, selector, files) {
  return withDebugger(tabId, async (target) => {
    const doc = await debuggerCommand(target, 'DOM.getDocument', { depth: 1, pierce: true });
    const found = await debuggerCommand(target, 'DOM.querySelector', { nodeId: doc.root.nodeId, selector });
    if (!found.nodeId) return { success: false, err: 'No element matched selector: ' + selector };
    await debuggerCommand(target, 'DOM.setFileInputFiles', { nodeId: found.nodeId, files });
    return { success: true, selector, files: files.length };
  });
}

async function setViewport(tabId, width, height, deviceScaleFactor) {
  if (width <= 0 || height <= 0) return { success: false, err: "Viewport width and height must be positive" };
  return withDebugger(tabId, async (target) => {
    const scale = deviceScaleFactor || 1;
    await debuggerCommand(target, 'Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: scale, mobile: false });
    return { success: true, width, height, deviceScaleFactor: scale };
  });
}

async function setCpuThrottling(tabId, rate) {
  const throttlingRate = Number(rate);
  if (!Number.isFinite(throttlingRate) || throttlingRate < 1) return { success: false, err: 'CPU throttling rate must be >= 1' };
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Emulation.setCPUThrottlingRate', { rate: throttlingRate });
    return { success: true, tabId, rate: throttlingRate };
  });
}

async function setNetworkConditions(tabId, offline, latency, downloadThroughput, uploadThroughput) {
  const conditions = {
    offline: !!offline,
    latency: latency !== undefined && latency !== null ? Number(latency) : 0,
    downloadThroughput: downloadThroughput !== undefined && downloadThroughput !== null ? Number(downloadThroughput) : -1,
    uploadThroughput: uploadThroughput !== undefined && uploadThroughput !== null ? Number(uploadThroughput) : -1
  };
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Network.enable', {});
    await debuggerCommand(target, 'Network.emulateNetworkConditions', conditions);
    return { success: true, tabId, offline: conditions.offline };
  });
}

async function clearNetworkConditions(tabId) {
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Network.enable', {});
    await debuggerCommand(target, 'Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1
    });
    return { success: true, tabId };
  });
}

async function setColorScheme(tabId, scheme) {
  if (!['light', 'dark', 'no-preference'].includes(scheme)) return { success: false, err: 'scheme must be light|dark|no-preference' };
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: scheme }] });
    return { success: true, tabId, scheme };
  });
}

async function setUserAgent(tabId, userAgent) {
  if (typeof userAgent !== 'string' || !userAgent.trim()) return { success: false, err: 'userAgent must be a non-empty string' };
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Network.enable', {});
    await debuggerCommand(target, 'Network.setUserAgentOverride', { userAgent });
    return { success: true, tabId };
  });
}

async function observeTab(tabId) {
  return withDebugger(tabId, async (target) => {
    const ax = await debuggerCommand(target, 'Accessibility.getFullAXTree', {});
    const nodes = (ax.nodes || []).filter((node) => !node.ignored).slice(0, 250);
    return nodes.map((node) => ({
      nodeId: node.nodeId,
      backendDOMNodeId: node.backendDOMNodeId || null,
      role: node.role?.value || null,
      name: node.name?.value || '',
      value: node.value?.value || null,
      description: node.description?.value || null,
      properties: Object.fromEntries((node.properties || []).map((prop) => [prop.name, prop.value?.value ?? prop.value?.description ?? null]))
    }));
  });
}

async function startMonitoring(tabId) {
  if (monitors.has(tabId)) return { success: true, tabId, already: true };
  const target = { tabId };
  if (!interceptors.has(tabId)) {
    await debuggerAttach(target);
  }
  monitors.set(tabId, { console: [], network: new Map(), dialogs: [] });
  try {
    await debuggerCommand(target, 'Runtime.enable', {});
    await debuggerCommand(target, 'Log.enable', {});
    await debuggerCommand(target, 'Network.enable', {});
    await debuggerCommand(target, 'Page.enable', {});
  } catch (error) {
    monitors.delete(tabId);
    if (!interceptors.has(tabId)) {
      await debuggerDetach(target);
    }
    throw error;
  }
  return { success: true, tabId, already: false };
}

async function stopMonitoring(tabId) {
  if (!monitors.has(tabId)) return { success: true, tabId, alreadyStopped: true };
  monitors.delete(tabId);
  if (!interceptors.has(tabId)) {
    await debuggerDetach({ tabId });
  }
  return { success: true, tabId };
}

function consoleMessages(tabId) {
  const monitor = monitors.get(tabId);
  if (!monitor) return { success: false, err: `Monitoring is not active for tab ${tabId}; run startMonitoring first` };
  return { success: true, tabId, messages: [...monitor.console] };
}

function networkRequests(tabId) {
  const monitor = monitors.get(tabId);
  if (!monitor) return { success: false, err: `Monitoring is not active for tab ${tabId}; run startMonitoring first` };
  return { success: true, tabId, requests: [...monitor.network.values()] };
}

async function handleDialog(tabId, accept, promptText) {
  try {
    return await withDebugger(tabId, async (target) => {
      const params = { accept };
      if (promptText != null) params.promptText = promptText;
      await debuggerCommand(target, 'Page.handleJavaScriptDialog', params);
      return { success: true, tabId, accept };
    });
  } catch (error) {
    return { success: false, err: error.message };
  }
}

function pushLimited(items, item) {
  items.push(item);
  if (items.length > MONITOR_LIMIT) items.splice(0, items.length - MONITOR_LIMIT);
}

function trimNetwork(items) {
  while (items.size > MONITOR_LIMIT) {
    const first = items.keys().next().value;
    items.delete(first);
  }
}

function stringifyRemoteValue(arg) {
  return String(arg.value ?? arg.description ?? arg.type ?? '');
}

function redactUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    return { url: parsed.origin + parsed.pathname, hasQuery: Boolean(parsed.search) };
  } catch (_error) {
    return { url: rawUrl.split('?')[0], hasQuery: rawUrl.includes('?') };
  }
}

async function downloadUrl(url, filename) {
  const options = { url: url, saveAs: false };
  if (filename) {
    options.filename = filename;
  }
  const downloadId = await chrome.downloads.download(options);
  return { downloadId };
}

async function getStorageState(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const url = tab.url || "";
  let origin = "";
  try {
    if (url) {
      origin = new URL(url).origin;
    }
  } catch (e) {
    // Ignore invalid URL
  }

  let localStorageVal = {};
  let sessionStorageVal = {};

  if (origin && origin !== "null" && origin.startsWith("http")) {
    try {
      const storageRes = await withDebugger(tabId, async (target) => {
        return await evaluateWithDebugger(target, `(() => {
          const ls = {};
          const ss = {};
          try {
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i);
              ls[k] = localStorage.getItem(k);
            }
          } catch(e) {}
          try {
            for (let i = 0; i < sessionStorage.length; i++) {
              const k = sessionStorage.key(i);
              ss[k] = sessionStorage.getItem(k);
            }
          } catch(e) {}
          return { localStorage: ls, sessionStorage: ss };
        })()`);
      });

      if (storageRes.success && storageRes.val) {
        localStorageVal = storageRes.val.localStorage || {};
        sessionStorageVal = storageRes.val.sessionStorage || {};
      }
    } catch (e) {
      // Ignore debugger or evaluation errors
    }
  }

  let cookies = [];
  if (origin && origin.startsWith("http")) {
    try {
      cookies = await chrome.cookies.getAll({ url: origin });
    } catch (e) {
      // Ignore cookie errors
    }
  }

  return {
    origin,
    cookies,
    localStorage: localStorageVal,
    sessionStorage: sessionStorageVal
  };
}

async function setGeolocation(tabId, latitude, longitude, accuracy) {
  const tab = await chrome.tabs.get(tabId);
  let origin = "";
  try {
    origin = new URL(tab.url).origin;
  } catch (e) {}

  return withDebugger(tabId, async (target) => {
    let grantError = null;
    if (origin && origin.startsWith("http")) {
      try {
        await chrome.contentSettings.location.set({
          primaryPattern: `${origin}/*`,
          setting: 'allow'
        });
      } catch (contentSettingsError) {
        try {
          await debuggerCommand(target, 'Browser.setPermission', {
            permission: { name: 'geolocation' },
            setting: 'granted',
            origin: origin
          });
        } catch (setPermissionError) {
          try {
            await debuggerCommand(target, 'Browser.grantPermissions', {
              permissions: ['geolocation'],
              origin: origin
            });
          } catch (grantPermissionsError) {
            grantError = `${contentSettingsError.message}; ${setPermissionError.message}; ${grantPermissionsError.message}`;
          }
        }
      }
    }
    const params = {
      latitude: Number(latitude),
      longitude: Number(longitude),
      accuracy: accuracy !== undefined && accuracy !== null ? Number(accuracy) : 100
    };
    await debuggerCommand(target, 'Emulation.setGeolocationOverride', params);
    return { success: true, tabId, latitude, longitude, accuracy: params.accuracy, grantError };
  });
}

async function clearGeolocation(tabId) {
  const tab = await chrome.tabs.get(tabId);
  let origin = "";
  try {
    origin = new URL(tab.url).origin;
  } catch (e) {}
  if (origin && origin.startsWith("http")) {
    try {
      await chrome.contentSettings.location.set({
        primaryPattern: `${origin}/*`,
        setting: 'ask'
      });
    } catch (_error) {}
  }
  await withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Emulation.clearGeolocationOverride', {});
  });
  return { success: true, tabId };
}

function toBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let binString = "";
  for (let i = 0; i < bytes.length; i++) {
    binString += String.fromCharCode(bytes[i]);
  }
  return btoa(binString);
}

async function startInterception(tabId, urlPattern, mode, status, body) {
  const target = { tabId };
  const attachedHere = !monitors.has(tabId) && !interceptors.has(tabId);
  if (attachedHere) {
    await debuggerAttach(target);
  }

  const interceptor = {
    urlPattern,
    mode,
    status: (status !== undefined && status !== null) ? parseInt(status, 10) : 200,
    body: body || "",
    requests: []
  };
  interceptors.set(tabId, interceptor);

  try {
    await debuggerCommand(target, 'Fetch.enable', {
      patterns: [{ urlPattern: urlPattern, requestStage: "Request" }]
    });
    return { success: true, tabId, urlPattern, mode };
  } catch (error) {
    interceptors.delete(tabId);
    if (attachedHere && !monitors.has(tabId)) {
      await debuggerDetach(target);
    }
    throw error;
  }
}

async function stopInterception(tabId) {
  if (!interceptors.has(tabId)) return { success: true, tabId, alreadyStopped: true };
  const target = { tabId };
  try {
    await debuggerCommand(target, 'Fetch.disable', {});
  } catch (error) {
    console.warn("Fetch.disable failed:", error.message);
  }
  interceptors.delete(tabId);
  if (!monitors.has(tabId)) {
    await debuggerDetach(target);
  }
  return { success: true, tabId };
}

function interceptedRequests(tabId) {
  const interceptor = interceptors.get(tabId);
  if (!interceptor) {
    return { success: false, err: `Interception is not active for tab ${tabId}; run startInterception first` };
  }
  return { success: true, tabId, requests: [...interceptor.requests] };
}

async function performanceMetrics(tabId) {
  return withDebugger(tabId, async (target) => {
    await debuggerCommand(target, 'Performance.enable', {});
    try {
      const response = await debuggerCommand(target, 'Performance.getMetrics', {});
      const metrics = {};
      if (response && response.metrics) {
        for (const item of response.metrics) {
          metrics[item.name] = item.value;
        }
      }
      return { success: true, tabId, metrics };
    } finally {
      await debuggerCommand(target, 'Performance.disable', {}).catch(() => {});
    }
  });
}

const SESSION_COOKIE_HINTS = ["session", "sess", "sid", "auth", "token", "login", "logged_in", "jwt", "remember"];

async function sessionStatus(domains) {
  if (!Array.isArray(domains) || domains.length === 0) return { sessions: [] };
  const sessions = [];
  for (const domain of domains) {
    const cookies = await chrome.cookies.getAll({ domain });
    const cookieNames = cookies.map((cookie) => cookie.name);
    const hasSessionCookie = cookieNames.some((name) => {
      const lower = name.toLowerCase();
      return SESSION_COOKIE_HINTS.some((hint) => lower.includes(hint));
    });
    sessions.push({
      domain,
      cookieCount: cookies.length,
      cookieNames,
      hasSessionCookie,
      loggedIn: hasSessionCookie
    });
  }
  return { sessions };
}

async function handoffBodyLength(tabId) {
  const res = await withDebugger(tabId, (target) => evaluateWithDebugger(target, "(document.body && document.body.innerText || '').length"));
  return res.success ? res.val : -1;
}

async function handoffResult(tabId, mode, startedAt) {
  const tab = await chrome.tabs.get(tabId);
  const redacted = redactUrl(tab.url || "");
  return {
    success: true,
    handedOff: true,
    mode,
    elapsedMs: Date.now() - startedAt,
    tabId,
    finalUrl: redacted.url,
    finalUrlHasQuery: redacted.hasQuery,
  };
}

async function showHandoffOverlay(tabId, message) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (msg) => {
        const id = "__chrome_bridge_handoff__";
        document.getElementById(id)?.remove();
        const el = document.createElement("div");
        el.id = id;
        el.textContent = "\u270b Automation paused \u2014 " + String(msg || "please complete this step");
        el.style.cssText = [
          "position:fixed", "top:0", "left:0", "right:0", "z-index:2147483647",
          "background:#1a73e8", "color:#fff", "font:600 14px system-ui,sans-serif",
          "padding:10px 16px", "text-align:center", "box-shadow:0 2px 8px rgba(0,0,0,.3)",
        ].join(";");
        (document.body || document.documentElement).appendChild(el);
      },
      args: [message || ""],
    });
  } catch (_e) {
    // Overlay is best-effort (blocked by strict CSP / unsupported pages); the
    // handoff still proceeds without it.
  }
}

async function hideHandoffOverlay(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => document.getElementById("__chrome_bridge_handoff__")?.remove(),
    });
  } catch (_e) {
    // Best-effort cleanup.
  }
}

async function waitForHandoff(payload) {
  payload = payload || {};
  const until = payload.until || {};
  const mode = until.mode || "manual";
  const timeoutMs = payload.timeoutMs || 120000;
  let tabId = payload.tabId;
  if (tabId === undefined || tabId === null) {
    const active = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = active[0] && active[0].id;
  }
  if (tabId === undefined || tabId === null) {
    return { success: false, err: "No target tab for handoff" };
  }
  const tab = await chrome.tabs.update(tabId, { active: true });
  if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
  const startedAt = Date.now();
  await showHandoffOverlay(tabId, payload.message);
  const timeoutErr = { success: false, err: `handoff timeout after ${timeoutMs}ms (${mode})` };
  const settle = async (found) => {
    await hideHandoffOverlay(tabId);
    return found ? await handoffResult(tabId, mode, startedAt) : timeoutErr;
  };
  if (mode === "selector") {
    const found = await waitForSelector(tabId, until.selector, timeoutMs);
    return await settle(found.success);
  }
  if (mode === "url") {
    const found = await waitForUrl(tabId, until.urlSubstring, timeoutMs);
    return await settle(found.success);
  }
  if (mode === "text") {
    const found = await waitForText(tabId, until.text, timeoutMs);
    return await settle(found.success);
  }
  const startUrl = (await chrome.tabs.get(tabId)).url || "";
  const startLen = await handoffBodyLength(tabId);
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    await sleep(250);
    const currentUrl = (await chrome.tabs.get(tabId)).url || "";
    const currentLen = await handoffBodyLength(tabId);
    if (currentUrl !== startUrl || currentLen !== startLen) {
      return await settle(true);
    }
  }
  return await settle(false);
}

connectToHost();
