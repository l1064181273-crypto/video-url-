export const LIFECYCLE_PING = "lvt.lifecycle.ping";
export const LIFECYCLE_READY = "lvt.lifecycle.ready";

export async function initializeExtension(): Promise<void> {
  await Promise.all([
    chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }),
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }),
  ]);
}

chrome.runtime.onInstalled.addListener(() => {
  void initializeExtension();
});

chrome.runtime.onStartup.addListener(() => {
  void initializeExtension();
});

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (
    typeof message === "object" &&
    message !== null &&
    "type" in message &&
    message.type === LIFECYCLE_PING
  ) {
    sendResponse({ type: LIFECYCLE_READY });
  }
});

void initializeExtension();
