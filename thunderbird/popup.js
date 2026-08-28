let currentMessages = [];

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
    // getDisplayedMessages (plural) returns every message displayed in the
    // tab - more than one when several are selected in a list view. No
    // tabId here on purpose: omitting it uses Thunderbird's own "currently
    // active tab" default, which is what a message-display-action popup
    // wants.
    currentMessages = await messenger.messageDisplay.getDisplayedMessages();

    if (!currentMessages || !currentMessages.length) {
      statusEl.textContent = "No message is currently displayed.";
      submitButton.disabled = true;
      return;
    }

    const subjectEl = document.getElementById("subject");
    if (currentMessages.length === 1) {
      subjectEl.textContent = currentMessages[0].subject || "(no subject)";
    } else {
      subjectEl.textContent = `${currentMessages.length} messages selected: ${currentMessages
        .map((m) => m.subject || "(no subject)")
        .join("; ")}`;
    }

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

    const messages = await Promise.all(
      currentMessages.map(async (m) => {
        const full = await messenger.messages.getFull(m.id);
        return {
          subject: m.subject || "",
          from: (m.author || "").toString(),
          text: extractPlainText(full).slice(0, 8000),
        };
      })
    );

    const resp = await fetch(`${mercuryUrl.replace(/\/$/, "")}/rules/propose`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Mercury-Secret": mercurySecret,
      },
      body: JSON.stringify({ instruction, messages }),
    });

    const data = await resp.json();
    statusEl.classList.remove("ok", "err");
    if (data.ok) {
      const actionNote = data.action ? ` Also proposed: ${data.action}.` : "";
      statusEl.textContent = `Proposed: ${data.rule}${actionNote} Check Telegram to approve.`;
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
