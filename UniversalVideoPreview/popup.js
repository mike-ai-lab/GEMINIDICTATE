const toggle = document.getElementById("toggle");
const status = document.getElementById("status");

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) {
    toggle.disabled = true;
    toggle.textContent = "Unavailable on this page";
    status.textContent = "Open a regular website to use previews.";
    return;
  }

  const send = async (message) => {
    try { return await chrome.tabs.sendMessage(tab.id, message); }
    catch { return null; }
  };

  let state = await send({ type: "UVP_GET_STATE" });

  if (!state) {
    toggle.textContent = "Reload page to activate";
    status.textContent = "Reload the page, then open this popup again.";
    toggle.onclick = () => chrome.tabs.reload(tab.id);
    return;
  }

  const paint = () => {
    toggle.textContent = state.enabled ? "Disable hover previews" : "Enable hover previews";
    toggle.classList.toggle("off", !state.enabled);
    status.textContent = state.enabled
      ? "Hover over any video thumbnail to preview it."
      : "Previews are off. Click to enable.";
  };

  paint();

  toggle.onclick = async () => {
    toggle.disabled = true;
    state = await send({ type: "UVP_SET_ENABLED", enabled: !state.enabled }) || state;
    paint();
    toggle.disabled = false;
  };
})();
