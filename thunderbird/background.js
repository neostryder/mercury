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

// Mirrors backend/app.py's CATEGORIES list. The two lists have to be kept
// in sync by hand since the extension and the backend ship from the same
// repo but not as a shared module.
const CATEGORY_TAGS = {
  NEWSLETTER: { key: "mercury-newsletter", name: "Mercury: Newsletter", color: "#3B6FA0" },
  PROMOTIONAL: { key: "mercury-promotional", name: "Mercury: Promotional", color: "#D98C2B" },
  TRANSACTIONAL: { key: "mercury-transactional", name: "Mercury: Transactional", color: "#2E9E6D" },
  SHIPPING_DELIVERY: { key: "mercury-shipping-delivery", name: "Mercury: Shipping/Delivery", color: "#1CA9C9" },
  ACCOUNT_SECURITY: { key: "mercury-account-security", name: "Mercury: Account Security", color: "#8E44AD" },
  PERSONAL: { key: "mercury-personal", name: "Mercury: Personal", color: "#C96B8F" },
  SOCIAL: { key: "mercury-social", name: "Mercury: Social", color: "#5B4FCF" },
  FINANCIAL: { key: "mercury-financial", name: "Mercury: Financial", color: "#A67C00" },
  POLITICAL_FUNDRAISING: { key: "mercury-political-fundraising", name: "Mercury: Political/Fundraising", color: "#6D5A73" },
  PHISHING: { key: "mercury-phishing", name: "Mercury: Phishing", color: "#C0392B" },
  SCAM: { key: "mercury-scam", name: "Mercury: Scam", color: "#E8622C" },
  MALWARE: { key: "mercury-malware", name: "Mercury: Malware", color: "#7A1E1E" },
  OTHER: { key: "mercury-other", name: "Mercury: Other", color: "#7F8C8D" },
};

async function ensureCategoryTagsExist() {
  const existing = await messenger.messages.tags.list();
  const existingKeys = new Set(existing.map((t) => t.key));
  for (const { key, name, color } of Object.values(CATEGORY_TAGS)) {
    if (existingKeys.has(key)) continue;
    try {
      await messenger.messages.tags.create(key, name, color);
    } catch (err) {
      if (!err.message?.includes("already exists")) throw err;
    }
  }
}

function getHeaderValue(headers, name) {
  if (!headers) return undefined;
  const lower = name.toLowerCase();
  const foundKey = Object.keys(headers).find((k) => k.toLowerCase() === lower);
  if (!foundKey) return undefined;
  const value = headers[foundKey];
  return Array.isArray(value) ? value[0] : value;
}

async function tagMessageWithCategory(message) {
  const full = await messenger.messages.getFull(message.id);
  const rawCategory = getHeaderValue(full.headers, "x-mercury-category");
  if (!rawCategory) return;

  const category = rawCategory.trim().toUpperCase();
  const tagInfo = CATEGORY_TAGS[category];
  if (!tagInfo) return;

  const currentTags = message.tags || [];
  if (currentTags.includes(tagInfo.key)) return;

  await messenger.messages.update(message.id, {
    tags: [...currentTags, tagInfo.key],
  });
}

// A failure here must never take down the rest of this script - the menu
// and popup registered above are load-bearing and cannot be allowed to
// depend on the tagging feature succeeding.
try {
  await ensureCategoryTagsExist();
} catch (err) {
  console.error("Mercury: failed to set up category tags", err);
}

messenger.messages.onNewMailReceived.addListener(async (folder, messageList) => {
  for (const message of messageList.messages) {
    try {
      await tagMessageWithCategory(message);
    } catch (err) {
      console.error("Mercury: failed to tag message", message.id, err);
    }
  }
});
