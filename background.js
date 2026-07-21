let nativePort = null;
const HEARTBEAT_ALARM = "chromeBridgeHeartbeat";
const HEARTBEAT_MINUTES = 0.5;
const RECONNECT_ALARM = "chromeBridgeReconnect";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_FACTOR = 2;
const RECONNECT_CAP_MS = 30000;
const TASK_SESSIONS_KEY = "chromeBridgeTaskSessions";
const TASK_DEBUGGER_IDLE_MS = 30000;
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
const taskDebuggers = new Map();
const taskDebuggerAttachInFlight = new Map();
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

chrome.debugger.onDetach.addListener((source) => {
  const tabId = source.tabId;
  if (tabId === undefined || tabId === null) return;
  const taskDebugger = taskDebuggers.get(tabId);
  if (taskDebugger?.timer) clearTimeout(taskDebugger.timer);
  taskDebuggers.delete(tabId);
  taskDebuggerAttachInFlight.delete(tabId);
  monitors.delete(tabId);
  interceptors.delete(tabId);
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

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.action !== "wakeNativeHost") return false;
  connectToHost();
  sendResponse({ success: true });
  const tabId = sender && sender.tab && sender.tab.id;
  if (tabId !== undefined) {
    setTimeout(() => chrome.tabs.remove(tabId), 50);
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  scheduleHeartbeat();
  connectToHost();
});
chrome.runtime.onStartup.addListener(() => {
  if (chrome.storage && chrome.storage.local) chrome.storage.local.remove(TASK_SESSIONS_KEY);
  scheduleHeartbeat();
  connectToHost();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) sendHeartbeat();
  else if (alarm.name === RECONNECT_ALARM) connectToHost();
});
scheduleHeartbeat();
connectToHost();

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
        result = await navigateToUrl(payload.url, payload.active);
        break;
      case "getTabs":
        result = await getTabs();
        break;
      case "createTaskSession":
        result = await createTaskSession(payload.name);
        break;
      case "navigateTaskSession":
        result = await navigateTaskSession(payload.sessionId, payload.url, payload.active, payload.reuse);
        break;
      case "getTaskSessions":
        result = await getTaskSessions(payload.sessionId);
        break;
      case "closeTaskSession":
        result = await closeTaskSession(payload.sessionId);
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
        result = await observeTab(payload.tabId, payload);
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
        result = await captureScreenshot(payload.tabId, payload.format, payload.quiet);
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
      case "githubAttachUploadedFiles":
        result = await githubAttachUploadedFiles(payload.tabId, payload.inputSelector, payload.formSelector, payload.timeoutMs);
        break;
      case "githubSubmitComment":
        result = await githubSubmitComment(payload.tabId, payload.formSelector, payload.timeoutMs);
        break;
      case "githubAttachPrBody":
        result = await githubAttachPrBody(payload.tabId, payload.files, payload.timeoutMs);
        if (result?.success === false) throw new Error(result.err || 'GitHub PR-body attachment failed');
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

async function navigateToUrl(url, active = false) {
  const tab = await chrome.tabs.create({ url, active: active === true });
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

async function loadTaskSessions() {
  const store = chrome.storage && chrome.storage.local;
  if (!store) return {};
  const data = await store.get(TASK_SESSIONS_KEY);
  return data[TASK_SESSIONS_KEY] || {};
}

async function saveTaskSessions(sessions) {
  const store = chrome.storage && chrome.storage.local;
  if (store) await store.set({ [TASK_SESSIONS_KEY]: sessions });
}

function newTaskSessionId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function validOwnedTabs(tabIds) {
  const valid = [];
  for (const tabId of tabIds || []) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab) valid.push(tabId);
    } catch (error) {
      // Closed tabs are removed from the durable ownership record.
    }
  }
  return valid;
}

async function findTaskSessionForTab(tabId) {
  const sessions = await loadTaskSessions();
  for (const session of Object.values(sessions)) {
    if ((session.tabIds || []).includes(tabId)) {
      return { sessionId: session.sessionId, session };
    }
  }

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (error) {
    return null;
  }
  for (const session of Object.values(sessions)) {
    if (Number.isInteger(session.groupId) && session.groupId >= 0 && tab.groupId === session.groupId) {
      session.tabIds = [...new Set([...(session.tabIds || []), tabId])];
      session.updatedAt = Date.now();
      await saveTaskSessions(sessions);
      return { sessionId: session.sessionId, session };
    }
  }
  return null;
}

async function detachTaskDebugger(tabId, force = false) {
  const state = taskDebuggers.get(tabId);
  if (!state) return false;
  if (!force && (state.busyCount > 0 || monitors.has(tabId) || interceptors.has(tabId))) return false;
  if (state.timer) clearTimeout(state.timer);
  taskDebuggers.delete(tabId);
  if (force) {
    monitors.delete(tabId);
    interceptors.delete(tabId);
  }
  await debuggerDetach({ tabId });
  return true;
}

function scheduleTaskDebuggerDetach(tabId) {
  const state = taskDebuggers.get(tabId);
  if (!state || state.busyCount > 0 || monitors.has(tabId) || interceptors.has(tabId)) return;
  if (state.timer) clearTimeout(state.timer);
  state.lastUsedAt = Date.now();
  state.timer = setTimeout(() => {
    detachTaskDebugger(tabId).catch((error) => {
      console.warn(`Could not detach idle task debugger for tab ${tabId}:`, error.message);
    });
  }, TASK_DEBUGGER_IDLE_MS);
}

async function ensureTaskDebugger(tabId, sessionId) {
  const target = { tabId };
  let state = taskDebuggers.get(tabId);
  if (state && state.sessionId !== sessionId) {
    await detachTaskDebugger(tabId, true);
    state = null;
  }
  if (!state) {
    let attach = taskDebuggerAttachInFlight.get(tabId);
    if (!attach) {
      attach = (async () => {
        await debuggerAttach(target);
        const attached = { sessionId, lastUsedAt: Date.now(), timer: null, busyCount: 0 };
        taskDebuggers.set(tabId, attached);
        return attached;
      })().finally(() => taskDebuggerAttachInFlight.delete(tabId));
      taskDebuggerAttachInFlight.set(tabId, attach);
    }
    state = await attach;
  }
  return state;
}

async function withTaskDebugger(tabId, sessionId, fn) {
  const target = { tabId };
  const state = await ensureTaskDebugger(tabId, sessionId);
  if (state.timer) {
    clearTimeout(state.timer);
    state.timer = null;
  }
  state.busyCount = (state.busyCount || 0) + 1;
  try {
    return await fn(target);
  } finally {
    state.busyCount = Math.max(0, (state.busyCount || 1) - 1);
    scheduleTaskDebuggerDetach(tabId);
  }
}

async function detachTaskSessionDebuggers(sessionId) {
  const tabIds = [...taskDebuggers.entries()]
    .filter(([, state]) => state.sessionId === sessionId)
    .map(([tabId]) => tabId);
  await Promise.all(tabIds.map((tabId) => detachTaskDebugger(tabId, true)));
}

async function createTaskSession(name) {
  const sessions = await loadTaskSessions();
  const sessionId = newTaskSessionId();
  sessions[sessionId] = {
    sessionId,
    name: String(name || "Browser task"),
    tabIds: [],
    groupId: null,
    createdAt: Date.now(),
    updatedAt: Date.now()
  };
  await saveTaskSessions(sessions);
  return sessions[sessionId];
}

async function getTaskSessions(sessionId) {
  const sessions = await loadTaskSessions();
  let changed = false;
  for (const session of Object.values(sessions)) {
    const valid = await validOwnedTabs(session.tabIds);
    if (valid.length !== (session.tabIds || []).length) {
      session.tabIds = valid;
      session.updatedAt = Date.now();
      changed = true;
    }
  }
  if (changed) await saveTaskSessions(sessions);
  if (sessionId) {
    if (!sessions[sessionId]) throw new Error("unknown task session");
    return sessions[sessionId];
  }
  return Object.values(sessions);
}

async function groupTaskTab(session, tabId) {
  if (!chrome.tabs.group || !chrome.tabGroups) return;
  try {
    const options = { tabIds: [tabId] };
    if (Number.isInteger(session.groupId)) options.groupId = session.groupId;
    session.groupId = await chrome.tabs.group(options);
    await chrome.tabGroups.update(session.groupId, {
      title: session.name.slice(0, 40),
      color: "blue",
      collapsed: false
    });
  } catch (error) {
    console.warn("Could not group task tab:", error);
    session.groupId = null;
    try {
      session.groupId = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(session.groupId, {
        title: session.name.slice(0, 40), color: "blue", collapsed: false
      });
    } catch (retryError) {
      console.warn("Could not create replacement task group:", retryError);
    }
  }
}

async function navigateTaskSession(sessionId, url, active = false, reuse = true) {
  const sessions = await loadTaskSessions();
  const session = sessions[sessionId];
  if (!session) throw new Error("unknown task session");
  session.tabIds = await validOwnedTabs(session.tabIds);
  let tab = null;
  if (reuse !== false && session.tabIds.length) {
    const reusedTabId = session.tabIds[0];
    try {
      tab = await chrome.tabs.update(reusedTabId, { url, active: active === true });
      if (active === true && tab.windowId !== undefined) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }
    } catch (error) {
      // The owned tab can close after validOwnedTabs() checks it. Replace only
      // that raced tab and preserve any other tabs owned by the session.
      tab = await chrome.tabs.create({ url, active: active === true });
      session.tabIds = session.tabIds.filter((tabId) => tabId !== reusedTabId);
      session.tabIds.push(tab.id);
      await groupTaskTab(session, tab.id);
    }
  } else {
    tab = await chrome.tabs.create({ url, active: active === true });
    session.tabIds.push(tab.id);
    await groupTaskTab(session, tab.id);
  }
  session.updatedAt = Date.now();
  await saveTaskSessions(sessions);
  return { sessionId, tabId: tab.id, windowId: tab.windowId, active: tab.active };
}

async function closeTaskSession(sessionId) {
  const sessions = await loadTaskSessions();
  const session = sessions[sessionId];
  if (!session) throw new Error("unknown task session");
  const tabIds = await validOwnedTabs(session.tabIds);
  delete sessions[sessionId];
  await saveTaskSessions(sessions);
  await detachTaskSessionDebuggers(sessionId);
  // Delete ownership first. chrome.tabs.remove emits onRemoved events; if the
  // record still exists, an event listener can race and re-save an empty copy.
  if (tabIds.length) {
    try {
      await chrome.tabs.remove(tabIds);
    } catch (error) {
      // A tab can close between validOwnedTabs() and remove(). Ownership is
      // already durably deleted, so this race should not fail the close call.
      console.warn("Could not remove every task-session tab:", error);
    }
  }
  return { success: true, sessionId, closedTabIds: tabIds };
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const taskDebugger = taskDebuggers.get(tabId);
  if (taskDebugger?.timer) clearTimeout(taskDebugger.timer);
  taskDebuggers.delete(tabId);
  taskDebuggerAttachInFlight.delete(tabId);
  monitors.delete(tabId);
  interceptors.delete(tabId);
  const sessions = await loadTaskSessions();
  let changed = false;
  for (const session of Object.values(sessions)) {
    if ((session.tabIds || []).includes(tabId)) {
      session.tabIds = session.tabIds.filter((ownedId) => ownedId !== tabId);
      session.updatedAt = Date.now();
      changed = true;
    }
  }
  if (changed) await saveTaskSessions(sessions);
});

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
  const taskSession = await findTaskSessionForTab(tabId);
  if (taskSession) return withTaskDebugger(tabId, taskSession.sessionId, fn);
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

function parseLocatorToken(rawToken, rawLocator) {
  const token = String(rawToken ?? "").trim();
  if (!token) throw new Error(`Missing final selector in ${rawLocator}`);
  if (token.startsWith("css=")) {
    const selector = token.slice("css=".length).trim();
    if (!selector) throw new Error(`Missing CSS selector in ${rawLocator}`);
    return { kind: "css", selector };
  }
  if (token.startsWith("text=")) {
    const text = token.slice("text=".length).trim();
    if (!text) throw new Error(`Missing text in ${rawLocator}`);
    return { kind: "text", text };
  }
  if (token.startsWith("label=")) {
    const text = token.slice("label=".length).trim();
    if (!text) throw new Error(`Missing label text in ${rawLocator}`);
    return { kind: "label", text };
  }
  if (token.startsWith("aria=")) {
    const name = token.slice("aria=".length).trim();
    if (!name) throw new Error(`Missing accessible name in ${rawLocator}`);
    return { kind: "aria", name };
  }
  if (token.startsWith("role=")) {
    const roleSpec = token.slice("role=".length).trim();
    const match = roleSpec.match(/^([A-Za-z][A-Za-z0-9_-]*)(?:\[name=([^\]]+)\])?$/);
    if (!match) throw new Error(`Invalid role locator in ${rawLocator}`);
    return { kind: "role", role: match[1].toLowerCase(), name: match[2] };
  }
  return { kind: "css", selector: token };
}

function scanLocatorSeparators(raw, separator) {
  const parts = [];
  let start = 0;
  let quote = null;
  let escaped = false;
  let bracketDepth = 0;
  let parenDepth = 0;
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      continue;
    }
    if (ch === "[") {
      bracketDepth += 1;
      continue;
    }
    if (ch === "]" && bracketDepth > 0) {
      bracketDepth -= 1;
      continue;
    }
    if (ch === "(") {
      parenDepth += 1;
      continue;
    }
    if (ch === ")" && parenDepth > 0) {
      parenDepth -= 1;
      continue;
    }
    if (bracketDepth === 0 && parenDepth === 0 && raw.startsWith(separator, i)) {
      parts.push(raw.slice(start, i).trim());
      i += separator.length - 1;
      start = i + 1;
    }
  }
  parts.push(raw.slice(start).trim());
  return parts;
}

function hasUnsupportedLocatorToken(raw) {
  return scanLocatorSeparators(raw, ">>>>").length > 1 || scanLocatorSeparators(raw, "<<<").length > 1;
}

function parseActionLocator(selector) {
  const raw = String(selector ?? "");
  if (hasUnsupportedLocatorToken(raw)) {
    throw new Error(`Unsupported selector token in ${raw}`);
  }
  const shadowParts = scanLocatorSeparators(raw, ">>>");
  if (!shadowParts[0]?.trim()) {
    throw new Error(`Missing final selector in ${raw}`);
  }
  if (shadowParts.some((part, index) => index > 0 && !part.trim())) {
    throw new Error(`Missing final selector in ${raw}`);
  }
  const frameParts = scanLocatorSeparators(shadowParts[0], ">>");
  const frames = [];
  let target = null;
  for (let i = 0; i < frameParts.length; i++) {
    const part = frameParts[i].trim();
    if (!part) {
      throw new Error(`Missing final selector in ${raw}`);
    }
    if (part.startsWith("frame=") && target === null) {
      const frameSelector = part.slice("frame=".length).trim();
      if (!frameSelector) {
        throw new Error(`Missing frame selector in ${raw}`);
      }
      frames.push(frameSelector);
      continue;
    }
    if (i < frameParts.length - 1) {
      throw new Error(`Unsupported selector token in ${raw}`);
    }
    target = parseLocatorToken(part, raw);
  }
  if (!target) {
    throw new Error(`Missing final selector in ${raw}`);
  }
  const shadowSegments = shadowParts.slice(1).map((part) => parseLocatorToken(part, raw));
  return {
    frames,
    target,
    selector: target.kind === "css" ? target.selector : null,
    shadowSegments
  };
}

function locatorResolverSource() {
  return `
    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const visibleText = (el) => normalize(el.innerText || el.textContent || '');
    const isVisible = (el) => {
      if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0 || el.getClientRects().length > 0;
    };
    const byIdText = (id) => normalize((id || '').split(/\\s+/).map((part) => document.getElementById(part)?.innerText || document.getElementById(part)?.textContent || '').join(' '));
    const labelText = (el) => {
      const labels = el.labels ? Array.from(el.labels).map((label) => visibleText(label)).filter(Boolean) : [];
      if (labels.length) return normalize(labels.join(' '));
      const id = el.getAttribute('id');
      if (id) {
        const explicit = document.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (explicit) return visibleText(explicit);
      }
      const wrapped = el.closest('label');
      if (wrapped) return visibleText(wrapped);
      return '';
    };
    const accessibleName = (el) => normalize(
      el.getAttribute('aria-label') ||
      byIdText(el.getAttribute('aria-labelledby')) ||
      labelText(el) ||
      el.getAttribute('alt') ||
      el.getAttribute('title') ||
      el.getAttribute('placeholder') ||
      visibleText(el)
    );
    const implicitRole = (el) => {
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'button' || (tag === 'input' && ['button', 'submit', 'reset'].includes(type))) return 'button';
      if (tag === 'textarea' || (tag === 'input' && !['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'hidden'].includes(type))) return 'textbox';
      if (tag === 'select') return 'combobox';
      if (tag === 'input' && type === 'checkbox') return 'checkbox';
      if (tag === 'input' && type === 'radio') return 'radio';
      if (tag === 'a' && el.hasAttribute('href')) return 'link';
      if (tag === 'img') return 'img';
      if (/^h[1-6]$/.test(tag)) return 'heading';
      return '';
    };
    const candidateElements = (root) => {
      const all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
      return all.filter(isVisible);
    };
    const deepestTextMatch = (root, expected) => {
      const wanted = normalize(expected);
      const candidates = candidateElements(root);
      const pick = (contains) => candidates
        .filter((el) => {
          const text = visibleText(el);
          if (contains ? !text.includes(wanted) : text !== wanted) return false;
          return !Array.from(el.children || []).some((child) => isVisible(child) && (contains ? visibleText(child).includes(wanted) : visibleText(child) === wanted));
        })
        .sort((a, b) => (a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height))[0] || null;
      return pick(false) || pick(true);
    };
    const resolveToken = (root, token) => {
      if (!token || token.kind === 'css') {
        const selector = token?.selector || '';
        const el = root.querySelector(selector);
        return el ? { success: true, el } : { success: false, err: 'No element found for selector ' + selector };
      }
      if (token.kind === 'text') {
        const el = deepestTextMatch(root, token.text);
        return el ? { success: true, el } : { success: false, err: 'No element found for text ' + token.text };
      }
      if (token.kind === 'label') {
        const wanted = normalize(token.text);
        const controls = candidateElements(root).filter((el) => /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(el.tagName));
        const el = controls.find((item) => labelText(item) === wanted || item.getAttribute('aria-label') === wanted || byIdText(item.getAttribute('aria-labelledby')) === wanted || item.getAttribute('placeholder') === wanted) ||
          controls.find((item) => labelText(item).includes(wanted) || normalize(item.getAttribute('aria-label')).includes(wanted) || byIdText(item.getAttribute('aria-labelledby')).includes(wanted) || normalize(item.getAttribute('placeholder')).includes(wanted));
        return el ? { success: true, el } : { success: false, err: 'No form control found for label ' + token.text };
      }
      if (token.kind === 'aria') {
        const wanted = normalize(token.name);
        const matches = candidateElements(root);
        const el = matches.find((item) => accessibleName(item) === wanted) ||
          matches.find((item) => accessibleName(item).includes(wanted));
        return el ? { success: true, el } : { success: false, err: 'No element found for accessible name ' + token.name };
      }
      if (token.kind === 'role') {
        const name = normalize(token.name);
        const matches = candidateElements(root).filter((el) => (el.getAttribute('role') || implicitRole(el)) === token.role);
        const el = matches.find((item) => !name || accessibleName(item) === name) || matches.find((item) => name && accessibleName(item).includes(name));
        return el ? { success: true, el } : { success: false, err: 'No element found for role ' + token.role + (name ? ' name ' + name : '') };
      }
      return { success: false, err: 'Unsupported locator kind ' + token.kind };
    };
    const resolveLocator = (locator) => {
      let resolved = resolveToken(document, locator.target || { kind: 'css', selector: locator.selector });
      if (!resolved.success) return resolved;
      let el = resolved.el;
      for (const segment of locator.shadowSegments || []) {
        if (!el.shadowRoot) return { success: false, err: 'No open shadow root for selector segment ' + (segment.selector || segment.text || segment.role || segment.kind) };
        resolved = resolveToken(el.shadowRoot, segment);
        if (!resolved.success) return resolved;
        el = resolved.el;
      }
      return { success: true, el };
    };
  `;
}

function elementResolverExpression(locator, mode) {
  return `(() => {
    ${locatorResolverSource()}
    const resolved = resolveLocator(${JSON.stringify(locator)});
    if (!resolved.success) return resolved;
    const el = resolved.el;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const rect = el.getBoundingClientRect();
    if (${JSON.stringify(mode)} === 'focus' || ${JSON.stringify(mode)} === 'clear') {
      el.focus();
    }
    if (${JSON.stringify(mode)} === 'clear') {
      if ('value' in el) {
        el.value = '';
      } else {
        el.textContent = '';
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
    }
    return {
      success: true,
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      tagName: el.tagName,
      text: el.innerText || el.value || el.getAttribute('aria-label') || '',
      value: 'value' in el ? el.value : el.textContent
    };
  })()`;
}

function actionTargetExpression(locator, mode) {
  return elementResolverExpression(locator, mode);
}

function domClickExpression(locator) {
  return `(() => {
    ${locatorResolverSource()}
    const resolved = resolveLocator(${JSON.stringify(locator)});
    if (!resolved.success) return resolved;
    const matched = resolved.el;
    const el = typeof matched.click === 'function'
      ? matched
      : matched.closest?.('button, a, input, select, textarea, [role]') || matched;
    if (typeof el.click !== 'function') {
      return { success: false, err: 'Matched element is not clickable' };
    }
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const rect = el.getBoundingClientRect();
    const eventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      button: 0,
      buttons: 1,
      clickCount: 1
    };
    el.dispatchEvent(new MouseEvent('mousedown', eventInit));
    el.dispatchEvent(new MouseEvent('mouseup', { ...eventInit, buttons: 0 }));
    el.click();
    return {
      success: true,
      tagName: el.tagName,
      text: el.innerText || el.value || el.getAttribute('aria-label') || '',
      value: 'value' in el ? el.value : el.textContent
    };
  })()`;
}

function domScrollExpression(locator, deltaX, deltaY) {
  const locatorJson = locator ? JSON.stringify(locator) : "null";
  return `(() => {
    ${locator ? locatorResolverSource() : ""}
    const dx = ${JSON.stringify(deltaX)};
    const dy = ${JSON.stringify(deltaY)};
    if (${locatorJson} === null) {
      window.scrollBy(dx, dy);
      return { success: true, deltaX: dx, deltaY: dy };
    }
    const resolved = resolveLocator(${locatorJson});
    if (!resolved.success) return resolved;
    const el = resolved.el;
    if (typeof el.scrollBy === 'function') {
      el.scrollBy(dx, dy);
    } else {
      el.scrollLeft += dx;
      el.scrollTop += dy;
    }
    return { success: true, deltaX: dx, deltaY: dy, tagName: el.tagName };
  })()`;
}


function domSelectExpression(locator, value) {
  return `(() => {
    ${locatorResolverSource()}
    const resolved = resolveLocator(${JSON.stringify(locator)});
    if (!resolved.success) return resolved;
    const el = resolved.el;
    const value = ${JSON.stringify(value)};
    if (el.tagName !== 'SELECT') return { success: false, err: 'Element is not a SELECT' };
    const option = Array.from(el.options).find((item) => item.value === value || item.text === value);
    if (!option) return { success: false, err: 'No option matched value/text: ' + value };
    el.value = option.value;
    option.selected = true;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { success: true, value: el.value, selectedText: option.text };
  })()`;
}

function elementObjectExpression(locator) {
  return `(() => {
    ${locatorResolverSource()}
    const resolved = resolveLocator(${JSON.stringify(locator)});
    if (!resolved.success) throw new Error(resolved.err || 'Element not found');
    return resolved.el;
  })()`;
}

function domDragExpression(fromLocator, toLocator) {
  return `(() => {
    ${locatorResolverSource()}
    const from = resolveLocator(${JSON.stringify(fromLocator)});
    if (!from.success) return from;
    const to = resolveLocator(${JSON.stringify(toLocator)});
    if (!to.success) return to;
    from.el.scrollIntoView({ block: 'center', inline: 'center' });
    to.el.scrollIntoView({ block: 'center', inline: 'center' });
    const fromRect = from.el.getBoundingClientRect();
    const toRect = to.el.getBoundingClientRect();
    const dataTransfer = new DataTransfer();
    const eventInit = { bubbles: true, cancelable: true, composed: true, dataTransfer };
    from.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, composed: true, clientX: fromRect.left + fromRect.width / 2, clientY: fromRect.top + fromRect.height / 2, button: 0, buttons: 1 }));
    from.el.dispatchEvent(new DragEvent('dragstart', eventInit));
    to.el.dispatchEvent(new DragEvent('dragenter', eventInit));
    to.el.dispatchEvent(new DragEvent('dragover', eventInit));
    const dropped = to.el.dispatchEvent(new DragEvent('drop', eventInit));
    from.el.dispatchEvent(new DragEvent('dragend', eventInit));
    to.el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, composed: true, clientX: toRect.left + toRect.width / 2, clientY: toRect.top + toRect.height / 2, button: 0, buttons: 0 }));
    return { success: true, from: from.el.tagName, to: to.el.tagName, dropped };
  })()`;
}

async function evaluateInContext(target, expression, contextId) {
  const params = {
    expression,
    awaitPromise: true,
    returnByValue: true,
    allowUnsafeEvalBlockedByCSP: true
  };
  if (contextId !== null && contextId !== undefined) {
    params.contextId = contextId;
  }
  const result = await debuggerCommand(target, 'Runtime.evaluate', params);
  if (result.exceptionDetails) {
    return { success: false, err: result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'Runtime.evaluate exception', details: result.exceptionDetails };
  }
  return { success: true, val: result.result?.value };
}

function frameSelectorProbeExpression(selector) {
  return `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return { success: false, err: 'No frame found for selector ' + ${JSON.stringify(selector)} };
    const frames = Array.from(document.querySelectorAll('iframe,frame'));
    const rect = el.getBoundingClientRect();
    return {
      success: true,
      frameIndex: frames.indexOf(el),
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
      clientLeft: el.clientLeft || 0,
      clientTop: el.clientTop || 0
    };
  })()`;
}

function directChildFrames(frameTree, frameId) {
  if (!frameTree) return [];
  if (frameTree.frame?.id === frameId) return frameTree.childFrames || [];
  for (const child of frameTree.childFrames || []) {
    const found = directChildFrames(child, frameId);
    if (found.length || child.frame?.id === frameId) return found;
  }
  return [];
}

async function frameExecutionContext(target, frameId) {
  const world = await debuggerCommand(target, 'Page.createIsolatedWorld', {
    frameId,
    worldName: 'chrome-native-bridge',
    grantUniveralAccess: true
  });
  return world.executionContextId;
}

async function describeTopFrameElement(target, rootNodeId, selector) {
  const queried = await debuggerCommand(target, 'DOM.querySelector', { nodeId: rootNodeId, selector });
  if (!queried.nodeId) return null;
  const described = await debuggerCommand(target, 'DOM.describeNode', { nodeId: queried.nodeId, depth: 1, pierce: false });
  return described.node || null;
}



async function resolveActionTarget(tabId, locator, attachedTarget) {
  const run = async (target) => {
    const pageTree = await debuggerCommand(target, 'Page.getFrameTree', {});
    const topFrameId = pageTree.frameTree.frame.id;
    const doc = await debuggerCommand(target, 'DOM.getDocument', { depth: 1, pierce: false });
    let currentFrameId = topFrameId;
    let currentContextId = null;
    let currentRootNodeId = doc.root.nodeId;
    let offsetX = 0;
    let offsetY = 0;

    for (const frameSelector of locator.frames) {
      let describedNode = null;
      if (currentRootNodeId !== null) {
        describedNode = await describeTopFrameElement(target, currentRootNodeId, frameSelector);
      }
      const frameProbe = await evaluateInContext(target, frameSelectorProbeExpression(frameSelector), currentContextId);
      const frameInfo = frameProbe.val || {};
      if (!frameProbe.success || frameInfo.success === false) {
        return { success: false, err: `No frame found for selector ${frameSelector}` };
      }
      let childFrameId = describedNode?.frameId || describedNode?.contentDocument?.frameId || null;
      const children = directChildFrames(pageTree.frameTree, currentFrameId);
      if (!childFrameId && frameInfo.frameIndex >= 0 && children[frameInfo.frameIndex]) {
        childFrameId = children[frameInfo.frameIndex].frame.id;
      }
      if (!childFrameId || !children.some((child) => child.frame.id === childFrameId)) {
        return { success: false, err: `No frame found for selector ${frameSelector}` };
      }
      offsetX += (frameInfo.x || 0) + (frameInfo.clientLeft || 0);
      offsetY += (frameInfo.y || 0) + (frameInfo.clientTop || 0);
      currentFrameId = childFrameId;
      currentContextId = await frameExecutionContext(target, currentFrameId);
      currentRootNodeId = null;
    }

    const lookup = await evaluateInContext(target, actionTargetExpression(locator, 'center'), currentContextId);
    const value = lookup.val || lookup;
    if (!lookup.success || value.success === false) return value;
    return {
      ...value,
      x: (value.x || 0) + offsetX,
      y: (value.y || 0) + offsetY,
      frameId: currentFrameId,
      contextId: currentContextId,
      locator
    };
  };
  if (attachedTarget) return run(attachedTarget);
  return withDebugger(tabId, run);
}

async function focusActionTarget(target, resolved, clear) {
  const lookup = await evaluateInContext(target, actionTargetExpression(resolved.locator, clear ? 'clear' : 'focus'), resolved.contextId);
  const value = lookup.val || lookup;
  if (!lookup.success || value.success === false) return value;
  return value;
}

async function waitForSelector(tabId, selector, timeoutMs) {
  let locator;
  try {
    locator = parseActionLocator(selector);
  } catch (error) {
    return { success: false, err: error.message };
  }
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    const found = await resolveActionTarget(tabId, locator);
    if (found.success !== false) return { success: true, selector };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for selector", selector, timeoutMs };
}

async function pageContainsText(tabId, text) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (expected) => (document.body?.innerText || document.documentElement?.innerText || '').includes(expected),
      args: [String(text || '')]
    });
    return results[0]?.result === true;
  } catch (error) {
    return false;
  }
}

async function waitForText(tabId, text, timeoutMs) {
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    if (await pageContainsText(tabId, text)) return { success: true, text };
    await sleep(250);
  }
  return { success: false, err: "Timed out waiting for text", text, timeoutMs };
}

async function getCurrentState(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const observe = await observeTab(tabId, { compact: true, limit: 50 });
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

async function captureScreenshot(tabId, format, quiet = true) {
  const screenshotFormat = format || "png";
  if (quiet) {
    return withDebugger(tabId, async (target) => {
      const result = await debuggerCommand(target, "Page.captureScreenshot", { format: screenshotFormat });
      const mimeType = screenshotFormat === "jpeg" ? "image/jpeg" : "image/png";
      return { success: true, mimeType, dataUrl: `data:${mimeType};base64,${result.data}` };
    });
  }
  const activated = await activateTab(tabId);
  const dataUrl = await chrome.tabs.captureVisibleTab(activated.windowId, { format: screenshotFormat });
  const mimeType = screenshotFormat === "jpeg" ? "image/jpeg" : "image/png";
  return { success: true, mimeType, dataUrl };
}

async function extractText(tabId, maxChars) {
  const limit = maxChars || 20000;
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (maxLength) => {
      const raw = document.body ? document.body.innerText : document.documentElement?.innerText;
      const text = raw || '';
      return { text: text.slice(0, maxLength), originalLength: text.length };
    },
    args: [limit]
  });
  const result = results[0]?.result || { text: '', originalLength: 0 };
  return {
    success: true,
    text: result.text,
    truncated: result.originalLength > limit,
    chars: result.text.length
  };
}

async function getHTML(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => document.documentElement?.outerHTML || ''
  });
  return { success: true, html: results[0]?.result || '' };
}

async function getElementCenter(target, selector) {
  let locator;
  try {
    locator = parseActionLocator(selector);
  } catch (error) {
    return { success: false, err: error.message };
  }
  return resolveActionTarget(target.tabId, locator, target);
}

async function isTabActive(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    return tab.active === true;
  } catch (error) {
    return true;
  }
}

async function clickSelector(tabId, selector) {
  return withDebugger(tabId, async (target) => {
    const lookup = await getElementCenter(target, selector);
    if (lookup.success === false) return lookup;
    if (lookup.contextId !== null && lookup.contextId !== undefined || !(await isTabActive(tabId))) {
      const click = await evaluateInContext(target, domClickExpression(lookup.locator), lookup.contextId);
      const value = click.val || click;
      if (!click.success || value.success === false) return value;
      return { success: true, tagName: value.tagName, text: value.text };
    }
    const { x, y } = lookup;
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
    await debuggerCommand(target, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
    return { success: true, tagName: lookup.tagName, text: lookup.text };
  });
}

async function typeSelector(tabId, selector, text) {
  return withDebugger(tabId, async (target) => {
    const lookup = await getElementCenter(target, selector);
    if (lookup.success === false) return lookup;
    const focus = await focusActionTarget(target, lookup, false);
    if (focus.success === false) return focus;
    await debuggerCommand(target, 'Input.insertText', { text });
    const value = await focusActionTarget(target, lookup, false);
    if (value.success === false) return value;
    return { success: true, tagName: focus.tagName, value: value.value };
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
    let locator = null;
    if (selector) {
      try {
        locator = parseActionLocator(selector);
      } catch (error) {
        return { success: false, err: error.message };
      }
      point = await resolveActionTarget(tabId, locator, target);
      if (point.success === false) return point;
    } else {
      const center = await evaluateWithDebugger(target, '({ x: innerWidth / 2, y: innerHeight / 2 })');
      if (!center.success) return center;
      point = center.val;
    }
    if (!(await isTabActive(tabId))) {
      const scrolled = await evaluateInContext(target, domScrollExpression(locator, deltaX, deltaY), point.contextId);
      const value = scrolled.val || scrolled;
      if (!scrolled.success || value.success === false) return value;
      return value;
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
    const fromLocator = parseActionLocator(fromSelector);
    const toLocator = parseActionLocator(toSelector);
    const from = await resolveActionTarget(tabId, fromLocator, target);
    if (from.success === false) return from;
    const to = await resolveActionTarget(tabId, toLocator, target);
    if (to.success === false) return to;
    if (from.contextId !== to.contextId) {
      return { success: false, err: 'Drag source and target must be in the same frame context' };
    }
    const drag = await evaluateInContext(target, domDragExpression(fromLocator, toLocator), from.contextId);
    const value = drag.val || drag;
    if (!drag.success || value.success === false) return value;
    return { success: true, from: fromSelector, to: toSelector, dom: value };
  });
}

async function fillSelector(tabId, selector, text) {
  return withDebugger(tabId, async (target) => {
    const lookup = await getElementCenter(target, selector);
    if (lookup.success === false) return lookup;
    const focus = await focusActionTarget(target, lookup, true);
    if (focus.success === false) return focus;
    await debuggerCommand(target, 'Input.insertText', { text });
    const value = await focusActionTarget(target, lookup, false);
    if (value.success === false) return value;
    return { success: true, tagName: focus.tagName, value: value.value };
  });
}

async function selectOption(tabId, selector, value) {
  return withDebugger(tabId, async (target) => {
    let locator;
    try {
      locator = parseActionLocator(selector);
    } catch (error) {
      return { success: false, err: error.message };
    }
    const resolved = await resolveActionTarget(tabId, locator, target);
    if (resolved.success === false) return resolved;
    const result = await evaluateInContext(target, domSelectExpression(locator, value), resolved.contextId);
    return result.val || result;
  });
}

async function assertGitHubTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  let origin = null;
  try {
    origin = tab.url ? new URL(tab.url).origin : null;
  } catch (error) {
    origin = null;
  }
  if (origin !== "https://github.com") {
    return { success: false, err: "GitHub action requires a https://github.com tab", origin, url: tab.url || null };
  }
  return { success: true, origin, url: tab.url || null };
}

function githubAttachExpression(locator, formSelector, timeoutMs) {
  return `(() => new Promise((resolve) => {
    ${locatorResolverSource()}
    const inputResult = resolveLocator(${JSON.stringify(locator)});
    if (!inputResult.success) {
      resolve(inputResult);
      return;
    }
    const input = inputResult.el;
    if (!(input instanceof HTMLInputElement) || input.type !== 'file') {
      resolve({ success: false, err: 'GitHub attachment target must be a file input' });
      return;
    }
    if (!input.files || input.files.length === 0) {
      resolve({ success: false, err: 'No files are set on the GitHub attachment input' });
      return;
    }
    const attachment = input.closest('file-attachment');
    if (!attachment || typeof attachment.attach !== 'function') {
      resolve({ success: false, err: 'No GitHub file-attachment.attach(input.files) component found' });
      return;
    }
    const formSelector = ${JSON.stringify(formSelector || null)};
    const explicitRoot = formSelector ? document.querySelector(formSelector) : null;
    if (formSelector && !explicitRoot) {
      resolve({ success: false, err: 'No GitHub comment form matched formSelector' });
      return;
    }
    const root = explicitRoot || input.closest('form') || attachment.closest('form') || document.querySelector('.js-comment-form');
    if (!root) {
      resolve({ success: false, err: 'No GitHub comment form found for attachment' });
      return;
    }
    const textarea = root.querySelector('textarea');
    if (!textarea) {
      resolve({ success: false, err: 'No GitHub comment textarea found for attachment' });
      return;
    }
    const timeoutMs = ${JSON.stringify(timeoutMs || 30000)};
    const deadline = Date.now() + timeoutMs;
    const assetPattern = /user-attachments\\/assets\\/[A-Za-z0-9._-]+/g;
    Promise.resolve(attachment.attach(input.files)).catch((error) => {
      resolve({ success: false, err: String(error && error.message || error) });
    });
    const poll = () => {
      const value = textarea.value || '';
      const assets = Array.from(new Set(value.match(assetPattern) || []));
      if (!value.includes('Uploading') && assets.length >= input.files.length) {
        resolve({ success: true, files: input.files.length, assets, valueLength: value.length });
        return;
      }
      if (Date.now() > deadline) {
        resolve({ success: false, err: 'Timed out waiting for GitHub attachment markdown', files: input.files.length, assets, uploading: value.includes('Uploading') });
        return;
      }
      setTimeout(poll, 250);
    };
    poll();
  }))()`;
}

function githubSubmitExpression(formSelector, timeoutMs) {
  return `(() => new Promise((resolve) => {
    const formSelector = ${JSON.stringify(formSelector || null)};
    const explicitRoot = formSelector ? document.querySelector(formSelector) : null;
    if (formSelector && !explicitRoot) {
      resolve({ success: false, err: 'No GitHub comment form matched formSelector' });
      return;
    }
    const activeForm = document.activeElement && document.activeElement.closest ? document.activeElement.closest('.js-comment-form') : null;
    const commentForms = Array.from(document.querySelectorAll('.js-comment-form'));
    const root = explicitRoot || activeForm || (commentForms.length === 1 ? commentForms[0] : null);
    if (!root) {
      resolve({ success: false, err: 'No GitHub comment form found' });
      return;
    }
    const allowedLabels = new Set(['Comment', 'Add comment']);
    const forbiddenLabels = new Set(['Close with comment']);
    const buttons = Array.from(root.querySelectorAll('button[type="submit"], input[type="submit"]'))
      .filter((button) => !button.disabled && button.offsetParent !== null)
      .map((button) => ({ button, text: ((button.innerText || button.value || button.textContent || '').trim().replace(/\\s+/g, ' ')) }));
    const forbidden = buttons.find(({ text }) => forbiddenLabels.has(text));
    if (forbidden) {
      resolve({ success: false, err: 'Refusing to click GitHub Close with comment button' });
      return;
    }
    const matches = buttons.filter(({ text }) => allowedLabels.has(text));
    if (matches.length !== 1) {
      resolve({ success: false, err: 'Expected exactly one GitHub Comment or Add comment submit button', labels: buttons.map(({ text }) => text) });
      return;
    }
    matches[0].button.click();
    const timeoutMs = ${JSON.stringify(timeoutMs || 10000)};
    setTimeout(() => resolve({ success: true, label: matches[0].text }), Math.min(timeoutMs, 1000));
  }))()`;
}

async function githubAttachUploadedFiles(tabId, inputSelector, formSelector, timeoutMs) {
  const gate = await assertGitHubTab(tabId);
  if (gate.success === false) return gate;
  return withDebugger(tabId, async (target) => {
    let locator;
    try {
      locator = parseActionLocator(inputSelector);
    } catch (error) {
      return { success: false, err: error.message };
    }
    const resolved = await resolveActionTarget(tabId, locator, target);
    if (resolved.success === false) return resolved;
    const result = await evaluateInContext(target, githubAttachExpression(locator, formSelector, timeoutMs), resolved.contextId);
    return result.val || result;
  });
}

async function githubSubmitComment(tabId, formSelector, timeoutMs) {
  const gate = await assertGitHubTab(tabId);
  if (gate.success === false) return gate;
  return withDebugger(tabId, async (target) => {
    const result = await evaluateInContext(target, githubSubmitExpression(formSelector, timeoutMs), null);
    return result.val || result;
  });
}

function githubPrBodyEditorExpression(timeoutMs) {
  return `(() => new Promise((resolve) => {
    const timeoutMs = ${JSON.stringify(timeoutMs || 30000)};
    const deadline = Date.now() + timeoutMs;
    const root = document.querySelector('.js-command-palette-pull-body');
    if (!root) {
      resolve({ success: false, err: 'No GitHub pull-request body container found' });
      return;
    }
    const findEditor = () => {
      const form = root.querySelector('form.js-comment-update');
      const textarea = form && form.querySelector('textarea.js-comment-field');
      const input = form && form.querySelector('file-attachment input[type="file"]');
      const attachment = input && input.closest('file-attachment');
      return form && textarea && input && attachment && form.offsetParent !== null
        ? { form, textarea, input, attachment }
        : null;
    };
    const waitForEditor = () => {
      const editor = findEditor();
      if (editor) {
        resolve({ success: true });
        return;
      }
      if (Date.now() > deadline) {
        resolve({ success: false, err: 'Timed out waiting for GitHub pull-request body editor' });
        return;
      }
      setTimeout(waitForEditor, 200);
    };
    if (findEditor()) {
      resolve({ success: true, alreadyOpen: true });
      return;
    }
    const menu = root.querySelector('summary[aria-haspopup="menu"]');
    if (!menu || !menu.querySelector('svg[aria-label="Show options"]')) {
      resolve({ success: false, err: 'No GitHub pull-request body options menu found' });
      return;
    }
    menu.click();
    const waitForEdit = () => {
      const editButtons = Array.from(root.querySelectorAll('button.js-comment-edit-button[aria-label="Edit comment"]'))
        .filter((button) => button.offsetParent !== null);
      if (editButtons.length === 1) {
        editButtons[0].click();
        waitForEditor();
        return;
      }
      if (Date.now() > deadline) {
        resolve({ success: false, err: 'Timed out waiting for GitHub pull-request body Edit action', matches: editButtons.length });
        return;
      }
      setTimeout(waitForEdit, 200);
    };
    waitForEdit();
  }))()`;
}

function githubPrBodyInputExpression() {
  return `(() => {
    const root = document.querySelector('.js-command-palette-pull-body');
    const form = root && root.querySelector('form.js-comment-update');
    const inputs = form && form.offsetParent !== null
      ? Array.from(form.querySelectorAll('file-attachment input[type="file"]'))
      : [];
    if (inputs.length !== 1) throw new Error('Expected exactly one GitHub pull-request body file input');
    return inputs[0];
  })()`;
}

function githubPrBodyAttachAndSaveExpression(fileCount, timeoutMs) {
  return `(() => new Promise((resolve) => {
    const root = document.querySelector('.js-command-palette-pull-body');
    const form = root && root.querySelector('form.js-comment-update');
    const textarea = form && form.querySelector('textarea.js-comment-field');
    const input = form && form.querySelector('file-attachment input[type="file"]');
    const attachment = input && input.closest('file-attachment');
    if (!form || !textarea || !input || !attachment || typeof attachment.attach !== 'function') {
      resolve({ success: false, err: 'GitHub pull-request body attachment editor is incomplete' });
      return;
    }
    if (!input.files || input.files.length !== ${JSON.stringify(fileCount)}) {
      resolve({ success: false, err: 'GitHub pull-request body file input did not receive every requested file', files: input.files ? input.files.length : 0 });
      return;
    }
    const assetPattern = /https:\/\/github\.com\/user-attachments\/assets\/[A-Za-z0-9._-]+/g;
    const before = new Set((textarea.value || '').match(assetPattern) || []);
    const timeoutMs = ${JSON.stringify(timeoutMs || 30000)};
    const deadline = Date.now() + timeoutMs;
    let settled = false;
    Promise.resolve(attachment.attach(input.files)).catch((error) => {
      if (!settled) {
        settled = true;
        resolve({ success: false, err: String(error && error.message || error) });
      }
    });
    const poll = () => {
      if (settled) return;
      const value = textarea.value || '';
      const assets = Array.from(new Set(value.match(assetPattern) || []));
      const addedAssets = assets.filter((asset) => !before.has(asset));
      const uploading = /Uploading/i.test(value);
      if (!uploading && addedAssets.length >= ${JSON.stringify(fileCount)}) {
        const submitButtons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"]'))
          .filter((button) => !button.disabled && button.offsetParent !== null)
          .map((button) => ({ button, text: ((button.innerText || button.value || button.textContent || '').trim().replace(/\\s+/g, ' ')) }))
          .filter(({ text }) => text === 'Update comment' || text === 'Save');
        if (submitButtons.length !== 1) {
          settled = true;
          resolve({ success: false, err: 'Expected exactly one GitHub pull-request body save button', labels: submitButtons.map(({ text }) => text), addedAssets });
          return;
        }
        submitButtons[0].button.click();
        const label = submitButtons[0].text;
        const waitForSave = () => {
          const editorVisible = form.querySelector('textarea.js-comment-field') && form.offsetParent !== null;
          if (!editorVisible) {
            settled = true;
            resolve({ success: true, files: ${JSON.stringify(fileCount)}, assets: addedAssets, saved: true, label });
            return;
          }
          if (Date.now() > deadline) {
            settled = true;
            resolve({ success: false, err: 'Timed out waiting for GitHub pull-request body save', files: ${JSON.stringify(fileCount)}, assets: addedAssets });
            return;
          }
          setTimeout(waitForSave, 200);
        };
        waitForSave();
        return;
      }
      if (Date.now() > deadline) {
        settled = true;
        resolve({ success: false, err: 'Timed out waiting for GitHub pull-request body attachment markdown', files: ${JSON.stringify(fileCount)}, addedAssets, uploading });
        return;
      }
      setTimeout(poll, 250);
    };
    poll();
  }))()`;
}

async function githubAttachPrBody(tabId, files, timeoutMs) {
  const gate = await assertGitHubTab(tabId);
  if (gate.success === false) return gate;
  let path = null;
  try {
    path = new URL(gate.url).pathname;
  } catch (error) {
    path = null;
  }
  if (!path || !/^\/[^/]+\/[^/]+\/pull\/\d+(?:\/|$)/.test(path)) {
    return { success: false, err: 'GitHub PR-body attachment requires a /owner/repo/pull/number page', path };
  }
  if (!Array.isArray(files) || files.length === 0) {
    return { success: false, err: 'At least one attachment file is required' };
  }
  return withDebugger(tabId, async (target) => {
    const opened = await evaluateInContext(target, githubPrBodyEditorExpression(timeoutMs), null);
    const openedValue = opened.val || opened;
    if (!opened.success || openedValue.success === false) return openedValue;

    const evaluated = await debuggerCommand(target, 'Runtime.evaluate', {
      expression: githubPrBodyInputExpression(),
      awaitPromise: true,
      returnByValue: false,
      allowUnsafeEvalBlockedByCSP: true
    });
    if (evaluated.exceptionDetails || !evaluated.result?.objectId) {
      return { success: false, err: evaluated.exceptionDetails?.exception?.description || evaluated.exceptionDetails?.text || 'No GitHub pull-request body file input object found' };
    }
    await debuggerCommand(target, 'DOM.setFileInputFiles', { objectId: evaluated.result.objectId, files });
    const attached = await evaluateInContext(target, githubPrBodyAttachAndSaveExpression(files.length, timeoutMs), null);
    return attached.val || attached;
  });
}

async function uploadFile(tabId, selector, files) {
  return withDebugger(tabId, async (target) => {
    let locator;
    try {
      locator = parseActionLocator(selector);
    } catch (error) {
      return { success: false, err: error.message };
    }
    const resolved = await resolveActionTarget(tabId, locator, target);
    if (resolved.success === false) return resolved;
    const params = {
      expression: elementObjectExpression(locator),
      awaitPromise: true,
      returnByValue: false,
      allowUnsafeEvalBlockedByCSP: true
    };
    if (resolved.contextId !== null && resolved.contextId !== undefined) {
      params.contextId = resolved.contextId;
    }
    const evaluated = await debuggerCommand(target, "Runtime.evaluate", params);
    if (evaluated.exceptionDetails) {
      return { success: false, err: evaluated.exceptionDetails.exception?.description || evaluated.exceptionDetails.text || 'Runtime.evaluate exception', details: evaluated.exceptionDetails };
    }
    if (!evaluated.result?.objectId) {
      return { success: false, err: 'No element object resolved for selector: ' + selector };
    }
    await debuggerCommand(target, 'DOM.setFileInputFiles', { objectId: evaluated.result.objectId, files });
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

async function observeTabWithoutDebugger(tabId, options = {}) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    args: [options],
    func: (rawOptions) => {
      const options = rawOptions || {};
      const requestedRoles = Array.isArray(options.roles)
        ? new Set(options.roles.map((role) => String(role || '').toLowerCase()).filter(Boolean))
        : null;
      const requestedName = String(options.name || '').trim().toLowerCase();
      const rawLimit = Number(options.limit);
      const limit = Number.isFinite(rawLimit) ? Math.max(1, Math.min(500, Math.floor(rawLimit))) : 50;
      const usefulRoles = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'menuitem', 'tab', 'heading', 'img']);
      const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const inferredRole = (element) => {
        const explicit = normalize(element.getAttribute('role')).split(' ')[0].toLowerCase();
        if (explicit) return explicit;
        const tag = element.tagName.toLowerCase();
        if (tag === 'a' && element.hasAttribute('href')) return 'link';
        if (tag === 'button' || tag === 'summary') return 'button';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return 'combobox';
        if (tag === 'img') return 'img';
        if (/^h[1-6]$/.test(tag)) return 'heading';
        if (tag === 'input') {
          const type = String(element.type || 'text').toLowerCase();
          if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
          if (type === 'checkbox') return 'checkbox';
          if (type === 'radio') return 'radio';
          if (type === 'search') return 'searchbox';
          if (!['hidden', 'file', 'color', 'range'].includes(type)) return 'textbox';
        }
        return '';
      };
      const accessibleName = (element) => {
        const ariaLabel = normalize(element.getAttribute('aria-label'));
        if (ariaLabel) return ariaLabel;
        const labelledBy = normalize(element.getAttribute('aria-labelledby'));
        if (labelledBy) {
          const text = labelledBy.split(/\s+/).map((id) => normalize(document.getElementById(id)?.textContent)).filter(Boolean).join(' ');
          if (text) return text;
        }
        if (element.labels?.length) {
          const text = [...element.labels].map((label) => normalize(label.textContent)).filter(Boolean).join(' ');
          if (text) return text;
        }
        return normalize(element.getAttribute('alt') || element.getAttribute('title') || element.getAttribute('placeholder') || element.textContent).slice(0, 500);
      };
      const pending = document.documentElement ? [document.documentElement] : [];
      const nodes = [];
      while (pending.length && nodes.length < limit) {
        const element = pending.pop();
        const style = getComputedStyle(element);
        if (style.display === 'none' || style.contentVisibility === 'hidden' || Number(style.opacity) === 0) continue;
        const children = [...element.children, ...(element.shadowRoot ? element.shadowRoot.children : [])];
        for (let index = children.length - 1; index >= 0; index -= 1) pending.push(children[index]);
        if (style.visibility === 'hidden') continue;
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const role = inferredRole(element);
        if (!role) continue;
        const name = accessibleName(element);
        if (!usefulRoles.has(role) && !name) continue;
        if (requestedRoles?.size && !requestedRoles.has(role)) continue;
        if (requestedName && !name.toLowerCase().includes(requestedName)) continue;
        const basic = { role, name };
        const value = element.getAttribute('aria-valuetext') || element.getAttribute('aria-valuenow') ||
          (['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName) ? element.value : null);
        if (value != null && String(value) !== '') basic.value = value;
        nodes.push(basic);
      }
      return nodes;
    }
  });
  return results[0]?.result || [];
}

async function observeTab(tabId, options = {}) {
  if (options.compact === true) return observeTabWithoutDebugger(tabId, options);
  return withDebugger(tabId, async (target) => {
    const ax = await debuggerCommand(target, 'Accessibility.getFullAXTree', {});
    const compact = options.compact === true;
    const requestedRoles = Array.isArray(options.roles)
      ? new Set(options.roles.map((role) => String(role || '').toLowerCase()).filter(Boolean))
      : null;
    const requestedName = String(options.name || '').trim().toLowerCase();
    const rawLimit = Number(options.limit);
    const limit = Number.isFinite(rawLimit) ? Math.max(1, Math.min(500, Math.floor(rawLimit))) : (compact ? 50 : 250);
    let nodes = (ax.nodes || []).filter((node) => !node.ignored);
    if (requestedRoles && requestedRoles.size) {
      nodes = nodes.filter((node) => requestedRoles.has(String(node.role?.value || '').toLowerCase()));
    }
    if (requestedName) {
      nodes = nodes.filter((node) => String(node.name?.value || '').toLowerCase().includes(requestedName));
    }
    if (compact) {
      const usefulRoles = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'menuitem', 'tab', 'heading', 'img']);
      nodes = nodes.filter((node) => usefulRoles.has(String(node.role?.value || '').toLowerCase()) || node.name?.value || node.value?.value);
    }
    return nodes.slice(0, limit).map((node) => {
      const basic = {
        role: node.role?.value || null,
        name: node.name?.value || '',
      };
      if (node.value?.value != null) basic.value = node.value.value;
      if (compact) return basic;
      return {
        nodeId: node.nodeId,
        backendDOMNodeId: node.backendDOMNodeId || null,
        ...basic,
        description: node.description?.value || null,
        properties: Object.fromEntries((node.properties || []).map((prop) => [prop.name, prop.value?.value ?? prop.value?.description ?? null]))
      };
    });
  });
}

async function startMonitoring(tabId) {
  if (monitors.has(tabId)) return { success: true, tabId, already: true };
  const target = { tabId };
  let taskDebugger = taskDebuggers.get(tabId);
  if (!taskDebugger && !interceptors.has(tabId)) {
    const taskSession = await findTaskSessionForTab(tabId);
    if (taskSession) taskDebugger = await ensureTaskDebugger(tabId, taskSession.sessionId);
  }
  if (taskDebugger?.timer) clearTimeout(taskDebugger.timer);
  if (taskDebugger) taskDebugger.timer = null;
  const attachedHere = !interceptors.has(tabId) && !taskDebugger;
  if (attachedHere) {
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
    if (attachedHere && !interceptors.has(tabId)) {
      await debuggerDetach(target);
    } else if (taskDebugger) {
      scheduleTaskDebuggerDetach(tabId);
    }
    throw error;
  }
  return { success: true, tabId, already: false };
}

async function stopMonitoring(tabId) {
  if (!monitors.has(tabId)) return { success: true, tabId, alreadyStopped: true };
  monitors.delete(tabId);
  if (!interceptors.has(tabId)) {
    if (taskDebuggers.has(tabId)) scheduleTaskDebuggerDetach(tabId);
    else await debuggerDetach({ tabId });
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
  let taskDebugger = taskDebuggers.get(tabId);
  if (!taskDebugger && !monitors.has(tabId) && !interceptors.has(tabId)) {
    const taskSession = await findTaskSessionForTab(tabId);
    if (taskSession) taskDebugger = await ensureTaskDebugger(tabId, taskSession.sessionId);
  }
  if (taskDebugger?.timer) clearTimeout(taskDebugger.timer);
  if (taskDebugger) taskDebugger.timer = null;
  const attachedHere = !monitors.has(tabId) && !interceptors.has(tabId) && !taskDebugger;
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
    } else if (taskDebugger) {
      scheduleTaskDebuggerDetach(tabId);
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
    if (taskDebuggers.has(tabId)) scheduleTaskDebuggerDetach(tabId);
    else await debuggerDetach(target);
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

async function readBodyLengthInTab(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => (document.body?.innerText || '').length
    });
    return Number.isFinite(results[0]?.result) ? results[0].result : -1;
  } catch (error) {
    return -1;
  }
}

async function handoffBodyLength(tabId) {
  return readBodyLengthInTab(tabId);
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
  let startLen = await handoffBodyLength(tabId);
  const deadline = deadlineFrom(timeoutMs);
  while (Date.now() <= deadline) {
    await sleep(250);
    const currentUrl = (await chrome.tabs.get(tabId)).url || "";
    const currentLen = await handoffBodyLength(tabId);
    if (currentUrl !== startUrl) {
      return await settle(true);
    }
    if (startLen < 0) {
      if (currentLen >= 0) startLen = currentLen;
      continue;
    }
    if (currentLen >= 0 && currentLen !== startLen) return await settle(true);
  }
  return await settle(false);
}
