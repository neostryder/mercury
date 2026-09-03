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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = input.value.trim();
    if (!username) return;

    submitButton.disabled = true;
    statusEl.textContent = "";

    try {
      await messenger.runtime.sendMessage({
        type: "mercury-set-from-alias",
        tabId,
        username,
      });
      window.close();
    } catch (err) {
      statusEl.textContent = `Failed to set the From address: ${err.message}`;
      submitButton.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
