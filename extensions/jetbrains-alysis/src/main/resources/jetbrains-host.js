(function () {
  "use strict";
  let counter = 0;
  let state;
  const pending = new Map();
  function call(message) {
    if (pending.size >= 64) return Promise.reject(new Error("Too many pending IDE actions. Wait for the current action to finish."));
    const id = `ui-${++counter}`;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error("The IDE did not respond. Reconnect and try again.")); }, 95000);
      pending.set(id, { resolve, reject, timeout });
      try { window.alysisNativeSend(JSON.stringify({ ...message, id })); }
      catch (error) { pending.delete(id); clearTimeout(timeout); reject(error); }
    });
  }
  const controller = new window.AlysisPortableChat(
    (method, params) => call({ type: "rpc", method, params }),
    data => window.dispatchEvent(new MessageEvent("message", { data })),
    message => call({ type: "native", action: message })
  );
  window.alysisHost = {
    getState: () => state,
    setState: value => { state = value; },
    postMessage: message => { void controller.handle(message); }
  };
  // Add only implemented settings before the shared renderer binds its command buttons.
  // Keep the common DOM intact: its renderer still updates the hidden VS Code detail fields.
  const settings = document.querySelector("#settingsSurface .settings-groups");
  if (settings) {
    const group = document.createElement("section");
    group.className = "settings-group";
    group.setAttribute("data-native-settings", "");
    const heading = document.createElement("h2"); heading.textContent = "Local agent"; group.append(heading);
    for (const [command, label] of [["alysis.locateCli", "Choose CLI"], ["alysis.showBridgeHealth", "Connect"]]) {
      const button = document.createElement("button"); button.type = "button"; button.className = "settings-row";
      button.dataset.command = command; button.textContent = label; group.append(button);
    }
    const detail = document.createElement("p");
    detail.textContent = "This preview uses your CLI provider configuration. Change permissions below the conversation.";
    group.append(detail); settings.prepend(group);
  }
  window.addEventListener("message", event => {
    const data = event.data;
    if (data?.type === "native.result") {
      const request = pending.get(data.id);
      if (!request) return;
      pending.delete(data.id); clearTimeout(request.timeout);
      if (data.ok) request.resolve(data.result);
      else request.reject(new Error(data.error || "The IDE rejected the action."));
    } else if (data?.type === "native.event") controller.onEvent(data.event);
    else if (data?.type === "native.disconnected") {
      for (const request of pending.values()) { clearTimeout(request.timeout); request.reject(new Error(data.message)); }
      pending.clear(); controller.disconnected(data.message);
    }
  });
  window.addEventListener("pagehide", () => {
    controller.dispose();
    for (const request of pending.values()) { clearTimeout(request.timeout); request.reject(new Error("The sidebar closed.")); }
    pending.clear();
  });
})();
