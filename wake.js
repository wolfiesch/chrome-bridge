(async () => {
  try {
    await chrome.runtime.sendMessage({ action: "wakeNativeHost" });
  } finally {
    try {
      const tab = await chrome.tabs.getCurrent();
      if (tab && tab.id !== undefined) await chrome.tabs.remove(tab.id);
    } catch (error) {
      window.close();
    }
  }
})();
