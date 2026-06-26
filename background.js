let nativePort = null;
const HEARTBEAT_ALARM = "chromeBridgeHeartbeat";
const HEARTBEAT_MINUTES = 0.5;
const monitors = new Map();
const MONITOR_LIMIT = 200;

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source.tabId || !monitors.has(source.tabId)) return;
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
    return;
  }

  nativePort.onMessage.addListener((message) => {
    console.log("Received message from native host:", message);
    handleMessageFromHost(message);
  });

  nativePort.onDisconnect.addListener(() => {
    console.warn("Disconnected from native host:", chrome.runtime.lastError);
    nativePort = null;
    setTimeout(connectToHost, 5000);
  });
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
});
scheduleHeartbeat();

async function handleMessageFromHost(message) {
  const { id, action, payload } = message;
  try {
    let result;
    switch (action) {
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
      default:
        throw new Error(`Unsupported action: ${action}`);
    }
    sendResponseToHost({ id, success: true, result });
  } catch (error) {
    sendResponseToHost({ id, success: false, error: error.message });
  }
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
  if (monitors.has(tabId)) return fn(target);
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
  await debuggerAttach(target);
  monitors.set(tabId, { console: [], network: new Map(), dialogs: [] });
  try {
    await debuggerCommand(target, 'Runtime.enable', {});
    await debuggerCommand(target, 'Log.enable', {});
    await debuggerCommand(target, 'Network.enable', {});
    await debuggerCommand(target, 'Page.enable', {});
  } catch (error) {
    monitors.delete(tabId);
    await debuggerDetach(target);
    throw error;
  }
  return { success: true, tabId, already: false };
}

async function stopMonitoring(tabId) {
  if (!monitors.has(tabId)) return { success: true, tabId, alreadyStopped: true };
  monitors.delete(tabId);
  await debuggerDetach({ tabId });
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

connectToHost();
