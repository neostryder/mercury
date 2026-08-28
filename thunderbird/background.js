// Thunderbird's MV3 event-page background does not reliably keep a menu
// item registered when creation is gated behind runtime.onInstalled (which
// only fires once per install/update) - Thunderbird's own official
// quickfilter example creates its menu items unconditionally at top-level
// script scope instead, for this reason. Guard against the "already exists"
// rejection since this now runs on every background-script start.
try {
  await messenger.menus.create({
    id: "mercury-flag",
    contexts: ["message_list"],
    title: "Flag for Mercury",
    icons: { "16": "icons/icon-16.png", "32": "icons/icon-32.png" },
  });
} catch (err) {
  if (!err.message?.includes("already exists")) throw err;
}

messenger.menus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "mercury-flag") {
    await messenger.messageDisplayAction.openPopup({ windowId: tab.windowId });
  }
});
