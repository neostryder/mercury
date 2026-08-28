let currentMessage = null;

function extractPlainText(part) {
  if (!part) return "";
  if (part.parts && part.parts.length) {
    const textPart = part.parts.find((p) => p.contentType === "text/plain");
    if (textPart) return extractPlainText(textPart);
    for (const p of part.parts) {
      const found = extractPlainText(p);
      if (found) return found;
    }
    return "";
  }
  return part.body || "";
}

async function init() {
  const submitButton = document.getElementById("submit");
  const statusEl = document.getElementById("status");

  try {
    // No tabId here on purpose: a message-display-action popup isn't a tab
    // in the mail-tab-strip sense, so querying for "the active tab" from
    // inside the popup itself does not reliably resolve to the 3-pane
    // window's displayed message. Omitting it uses Thunderbird's own
    // "currently active tab" default instead.
    currentMessage = await messenger.messageDisplay.getDisplayedMessage();

    if (!currentMessage) {
      statusEl.textContent = "No message is currently displayed.";
      submitButton.disabled = true;
      return;
    }
    document.getElementById("subject").textContent = currentMessage.subject || "(no subject)";

    submitButton.addEventListener("click", () => onSubmit(submitButton, statusEl));
  } catch (err) {
    statusEl.textContent = `Failed to read the open message: ${err.message}`;
    submitButton.disabled = true;
  }
}

async function onSubmit(submitButton, statusEl) {
  const instruction = document.getElementById("instruction").value.trim();
  if (!instruction) return;

  submitButton.disabled = true;
  statusEl.textContent = "Sending...";

  try {
    const { mercuryUrl, mercurySecret } = await messenger.storage.local.get([
      "mercuryUrl",
      "mercurySecret",
    ]);
    if (!mercuryUrl || !mercurySecret) {
      statusEl.textContent = "Set the Mercury URL and secret in this extension's options first.";
      submitButton.disabled = false;
      return;
    }

    const full = await messenger.messages.getFull(currentMessage.id);
    const bodyText = extractPlainText(full);
    const fromDisplay = (currentMessage.author || "").toString();

    const resp = await fetch(`${mercuryUrl.replace(/\/$/, "")}/rules/propose`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Mercury-Secret": mercurySecret,
      },
      body: JSON.stringify({
        instruction,
        message: {
          subject: currentMessage.subject || "",
          from: fromDisplay,
          text: bodyText.slice(0, 8000),
        },
      }),
    });

    const data = await resp.json();
    statusEl.classList.remove("ok", "err");
    if (data.ok) {
      statusEl.textContent = `Rule added: ${data.rule}`;
      statusEl.classList.add("ok");
    } else {
      statusEl.textContent = `Mercury reported an error: ${data.error || resp.status}`;
      statusEl.classList.add("err");
      submitButton.disabled = false;
    }
  } catch (err) {
    statusEl.classList.remove("ok");
    statusEl.classList.add("err");
    statusEl.textContent = `Failed to reach Mercury: ${err.message}`;
    submitButton.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", init);
