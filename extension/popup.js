const shell = document.querySelector(".shell");
const connectionStatus = document.getElementById("connection-status");
const connectionDetail = document.getElementById("connection-detail");
const activeTask = document.getElementById("active-task");
const taskState = document.getElementById("task-state");
const taskSymbol = document.getElementById("task-symbol");
const pointerToggle = document.getElementById("pointer-toggle");

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(response || {});
    });
  });
}

function renderStatus(status) {
  const connected = status.connected === true;
  shell.dataset.connected = String(connected);
  connectionStatus.textContent = connected ? "Connected" : "Disconnected";
  connectionDetail.textContent = connected ? "Native host ready" : "Waiting for local host";

  const task = status.activeTask;
  activeTask.textContent = task?.name || "No active task";
  taskState.textContent = task?.stateLabel || "Ready when you are";
  taskSymbol.textContent = task?.symbol || "✦";

  pointerToggle.setAttribute("aria-checked", String(status.showAgentPointer !== false));
}

async function refresh() {
  try {
    renderStatus(await sendMessage({ action: "getBridgeStatus" }));
  } catch (error) {
    renderStatus({ connected: false, showAgentPointer: true });
  }
}

pointerToggle.addEventListener("click", async () => {
  const enabled = pointerToggle.getAttribute("aria-checked") !== "true";
  pointerToggle.setAttribute("aria-checked", String(enabled));
  try {
    const result = await sendMessage({ action: "setBridgePreference", key: "showAgentPointer", value: enabled });
    renderStatus(result);
  } catch (error) {
    pointerToggle.setAttribute("aria-checked", String(!enabled));
  }
});

refresh();
