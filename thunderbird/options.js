async function load() {
  const { mercuryUrl, mercurySecret } = await messenger.storage.local.get([
    "mercuryUrl",
    "mercurySecret",
  ]);
  document.getElementById("url").value = mercuryUrl || "";
  document.getElementById("secret").value = mercurySecret || "";
}

async function save() {
  const mercuryUrl = document.getElementById("url").value.trim();
  const mercurySecret = document.getElementById("secret").value.trim();
  await messenger.storage.local.set({ mercuryUrl, mercurySecret });
  document.getElementById("status").textContent = "Saved.";
}

document.addEventListener("DOMContentLoaded", load);
document.getElementById("save").addEventListener("click", save);
