const ALIAS_DOMAIN = "rpgm.tools";
const BARE_ADDRESS = `aaron@${ALIAS_DOMAIN}`;

async function getDefaultIdentity() {
  const accounts = await messenger.accounts.list();
  for (const account of accounts) {
    for (const identity of account.identities || []) {
      if ((identity.email || "").toLowerCase() === BARE_ADDRESS) return identity;
    }
  }
  return null;
}

function parseTabId() {
  const params = new URLSearchParams(window.location.search);
  return Number(params.get("tabId"));
}

async function init() {
  const tabId = parseTabId();
  const form = document.getElementById("form");
  const input = document.getElementById("username");
  const statusEl = document.getElementById("status");
  const submitButton = form.querySelector("button");

  input.value = "aaron";
  input.focus();
  input.select();

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") window.close();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = input.value.trim();
    if (!username) return;

    submitButton.disabled = true;
    statusEl.textContent = "";

    try {
      // Set directly rather than round-tripping through background.js -
      // this page already has the compose permission itself, and messaging
      // an MV3 event page that may have been suspended is a real source of
      // "Could not establish connection" failures for no functional gain.
      const address = `${username}@${ALIAS_DOMAIN}`;
      const identity = await getDefaultIdentity();
      const from = identity && identity.name ? `"${identity.name}" <${address}>` : address;
      await messenger.compose.setComposeDetails(tabId, { from });
      window.close();
    } catch (err) {
      statusEl.textContent = `Failed to set the From address: ${err.message}`;
      submitButton.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
