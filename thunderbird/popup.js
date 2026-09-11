let currentMessages = [];
let messageDetached = false;

function senderDomain() {
  const author = (currentMessages[0] && currentMessages[0].author) || "";
  const match = author.match(/@([^\s>]+)/);
  return match ? match[1].toLowerCase() : null;
}

const QUICK_ACTION_TEXT = {
  unsubscribe: () => `Unsubscribe me from ${senderDomain() || "this sender"}.`,
  "bounce-domain": () => {
    const domain = senderDomain();
    return domain
      ? `Hard bounce (550) all future mail from ${domain}.`
      : "Hard bounce (550) all future mail from this sender's domain.";
  },
  "bounce-pattern": () => {
    const domain = senderDomain();
    const example = domain ? ` (like ${domain})` : "";
    return (
      "Hard bounce (550) future mail whose sender domain matches this shape" +
      example +
      ": [[describe the shared domain shape here]]"
    );
  },
  custom: () => "",
};

function applyQuickAction(kind, instructionEl) {
  const text = QUICK_ACTION_TEXT[kind]();
  instructionEl.value = text;
  instructionEl.focus();
  const placeholderStart = text.indexOf("[[");
  if (placeholderStart !== -1) {
    instructionEl.setSelectionRange(placeholderStart, text.indexOf("]]") + 2);
  } else {
    instructionEl.setSelectionRange(text.length, text.length);
  }
}

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

// The plain-text alternative is preferred above because it reads cleanly, but
// for many senders it is a link-stripped rendering that keeps the anchor text
// and drops every URL. The HTML part is collected alongside it so a route
// that exists only as an href still reaches Mercury, which extracts it there.
function extractHtml(part) {
  if (!part) return "";
  if (part.parts && part.parts.length) {
    const htmlPart = part.parts.find((p) => p.contentType === "text/html");
    if (htmlPart) return extractHtml(htmlPart);
    for (const p of part.parts) {
      const found = extractHtml(p);
      if (found) return found;
    }
    return "";
  }
  return part.contentType === "text/html" ? part.body || "" : "";
}

const HTML_BUDGET = 40000;

// Inline CSS, scripts, and comments are most of a marketing message's bytes
// and can never hold a route, so they go first. If the result is still over
// budget the TAIL is kept rather than the head: an unsubscribe footer sits at
// the end of the document, which is exactly what a head-truncation loses.
function condenseHtml(html) {
  const stripped = (html || "")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/gi, " ")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, " ");
  return stripped.length > HTML_BUDGET ? stripped.slice(-HTML_BUDGET) : stripped;
}

// Header names are case-insensitive and getFull lower-cases its keys, but a
// value arrives as an array. Matching on the lower-cased name rather than the
// exact spelling keeps this working either way.
function headerValue(headers, name) {
  if (!headers) return "";
  const key = Object.keys(headers).find((k) => k.toLowerCase() === name.toLowerCase());
  if (!key) return "";
  const value = headers[key];
  return (Array.isArray(value) ? value.join(", ") : String(value || "")).trim();
}

function extractAddresses(headerText) {
  if (!headerText) return [];
  const matches = headerText.match(/[^\s<>,"]+@[^\s<>,"]+/g);
  return (matches || []).map((a) => a.toLowerCase());
}

const RECIPIENT_HEADER_PRIORITY = ["delivered-to", "x-original-to", "to", "cc"];

// The account an unsubscribe form actually wants is the specific address the
// list mail was delivered to, not just "the account this folder belongs to" -
// a catch-all account receives mail addressed to any of several identities.
// Only falls back to a single, unambiguous identity when the account has
// exactly one on file; an account with several identities and no header
// match returns "" rather than guessing one - a wrong address submitted to
// a real unsubscribe form is a wrong action taken silently, not a missing
// one the backend can honestly report and ask about.
async function resolveRecipientEmail(message, full) {
  let accountId;
  try {
    accountId = message.folder && message.folder.accountId;
  } catch (err) {
    accountId = undefined;
  }
  if (!accountId) return "";

  let account;
  try {
    account = await messenger.accounts.get(accountId);
  } catch (err) {
    return "";
  }
  const identityEmails = ((account && account.identities) || [])
    .map((identity) => (identity.email || "").toLowerCase())
    .filter(Boolean);
  if (!identityEmails.length) return "";

  for (const name of RECIPIENT_HEADER_PRIORITY) {
    const addresses = extractAddresses(headerValue(full.headers, name));
    const match = addresses.find((a) => identityEmails.includes(a));
    if (match) return match;
  }
  return identityEmails.length === 1 ? identityEmails[0] : "";
}

async function init() {
  const submitButton = document.getElementById("submit");
  const statusEl = document.getElementById("status");

  try {
    // A right-click "Flag for Mercury" on a message_list selection stashes
    // its messages here (background.js) since openPopup() has no way to
    // pass them directly - consume it once, so a later toolbar-button open
    // never picks up a stale selection from an earlier context-menu click.
    const { pendingFlagMessages } = await messenger.storage.local.get("pendingFlagMessages");
    if (pendingFlagMessages && pendingFlagMessages.length) {
      currentMessages = pendingFlagMessages;
      await messenger.storage.local.remove("pendingFlagMessages");
    } else {
      // Per Thunderbird's own messageDisplay example: the tab must be looked
      // up explicitly (currentWindow correctly resolves to the mail window
      // even from inside this popup - omitting tabId does not reliably find
      // the displayed message), and getDisplayedMessages resolves to a
      // MessageList object ({messages: [...], ...}), not a bare array.
      const [tab] = await messenger.tabs.query({ active: true, currentWindow: true });
      const result = await messenger.messageDisplay.getDisplayedMessages(tab.id);
      currentMessages = (result && result.messages) || [];
    }

    if (!currentMessages.length) {
      statusEl.textContent = "No message is currently displayed.";
      submitButton.disabled = true;
      return;
    }

    const subjectEl = document.getElementById("subject");
    const subjectTextEl = document.getElementById("subjectText");
    if (currentMessages.length === 1) {
      subjectTextEl.textContent = currentMessages[0].subject || "(no subject)";
    } else {
      subjectTextEl.textContent = `${currentMessages.length} messages selected: ${currentMessages
        .map((m) => m.subject || "(no subject)")
        .join("; ")}`;
    }

    document.getElementById("removeMessage").addEventListener("click", () => {
      messageDetached = true;
      subjectEl.classList.add("detached");
      subjectTextEl.textContent =
        "No message attached - this will be sent as a general instruction, not about a specific email.";
      document.getElementById("removeMessage").style.display = "none";
      ["unsubscribe", "bounce-domain"].forEach((kind) => {
        const button = document.querySelector(`[data-quick-action="${kind}"]`);
        button.disabled = true;
        button.title = "Needs an attached message to know the sender";
      });
    });

    submitButton.addEventListener("click", () => onSubmit(submitButton, statusEl));

    const instructionEl = document.getElementById("instruction");
    document.querySelectorAll("[data-quick-action]").forEach((button) => {
      button.addEventListener("click", () => {
        document
          .querySelectorAll("[data-quick-action]")
          .forEach((b) => b.classList.toggle("selected", b === button));
        applyQuickAction(button.dataset.quickAction, instructionEl);
      });
    });
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

    const messages = messageDetached ? [] : await Promise.all(
      currentMessages.map(async (m) => {
        const full = await messenger.messages.getFull(m.id);
        return {
          subject: m.subject || "",
          from: (m.author || "").toString(),
          text: extractPlainText(full).slice(0, 8000),
          html: condenseHtml(extractHtml(full)),
          list_unsubscribe: headerValue(full.headers, "list-unsubscribe"),
          list_unsubscribe_post: headerValue(full.headers, "list-unsubscribe-post"),
          recipient_email: await resolveRecipientEmail(m, full),
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
      const parts = [];
      if (data.rule) parts.push(`Proposed: ${data.rule}.`);
      if (data.action) parts.push(`${data.rule ? "Also proposed" : "Proposed"}: ${data.action}.`);
      if (!parts.length) parts.push("Sent to Mercury.");
      statusEl.textContent = `${parts.join(" ")} Check Telegram to approve.`;
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
