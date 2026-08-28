messenger.runtime.onInstalled.addListener(() => {
  messenger.menus.create({
    id: "mercury-flag",
    contexts: ["message_list"],
    title: "Flag for Mercury",
  });
});

messenger.menus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "mercury-flag") {
    await messenger.messageDisplayAction.openPopup({ windowId: tab.windowId });
  }
});
