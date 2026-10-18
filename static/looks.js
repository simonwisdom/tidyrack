// Looks: Dress me, Copy a look, The missing piece and the Morning card (tidyrack/looks.py).
// Uses app.js's $, api and toast.

const looksState = { outfits: [], at: 0 };

function looksTab(name) {
  for (const tab of document.querySelectorAll("#looks .looks-tabs button")) {
    tab.classList.toggle("on", tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll("#looks .looks-panel")) {
    panel.hidden = panel.dataset.tab !== name;
  }
  if (name === "dress" && !looksState.outfits.length) dressMe();
  if (name === "missing" && !$("missing-body").dataset.loaded) missingPiece();
}

// A row of the outfit's pieces, each its own picture, with + between them.
function piecesRow(pieces) {
  const row = document.createElement("div");
  row.className = "look-pieces";
  pieces.forEach((p, n) => {
    if (n) row.append(Object.assign(document.createElement("span"), { className: "plus", textContent: "+" }));
    const tile = document.createElement("figure");
    const img = document.createElement("img");
    img.src = p.image || "";
    img.alt = [p.colour, p.type].filter(Boolean).join(" ");
    const cap = document.createElement("figcaption");
    cap.textContent = img.alt;
    tile.append(img, cap);
    row.append(tile);
  });
  return row;
}

function dots(pieces) {
  const row = document.createElement("div");
  row.className = "look-dots";
  for (const p of pieces) {
    if (!p.hex) continue;
    const dot = document.createElement("span");
    dot.className = "swatch big";
    dot.style.background = p.hex;
    row.append(dot);
  }
  return row;
}

// Wait for a drawing to finish, then show it. A drawing takes 30-60 seconds.
async function waitForDrawing(look, img, note) {
  img.hidden = true;
  note.classList.remove("failed");
  note.textContent = "Drawing… this takes about a minute.";
  while (look.status === "queued" || look.status === "working") {
    await new Promise((done) => setTimeout(done, 3000));
    look = await api(`/api/looks/${look.id}`);
  }
  if (look.status === "ready") {
    img.src = look.image_url;
    img.hidden = false;
    note.textContent = "Drawn by AI from your own clothes.";
  } else {
    note.textContent = "Couldn't draw it: " + (look.error || "something went wrong") + ". Try again.";
    note.classList.add("failed");
  }
}

async function dressMe() {
  $("dress-note").textContent = "Picking outfits…";
  try {
    const res = await api("/api/looks/dress-me");
    looksState.outfits = res.outfits;
    looksState.at = 0;
    $("dress-count").textContent = res.total > res.outfits.length
      ? `${res.total} outfits from your clothes; these are the best ${res.outfits.length}.`
      : `${res.total} outfits from your clothes, best first.`;
    showOutfit();
  } catch (err) {
    $("dress-note").textContent = err.message;
  }
}

// "a colour picked up…, light against dark, but dressy and casual mixed and two nearly-matching shades"
function reasonsText(reasons) {
  const good = reasons.filter((r) => !r.startsWith("but "));
  const bad = reasons.filter((r) => r.startsWith("but ")).map((r) => r.slice(4));
  const against = bad.length ? "but " + bad.join(" and ") : "";
  return [good.join(", "), against].filter(Boolean).join(good.length ? ", " : "");
}

function showOutfit() {
  const outfit = looksState.outfits[looksState.at % looksState.outfits.length];
  $("dress-drawn").hidden = true;
  $("dress-stage").replaceChildren(dots(outfit.pieces), piecesRow(outfit.pieces));
  $("dress-why").textContent = reasonsText(outfit.reasons);
  $("dress-note").textContent = "";
  $("dress-save").textContent = "Save outfit";
  $("dress-save").disabled = false;
}

async function feedback(action) {
  const outfit = looksState.outfits[looksState.at % looksState.outfits.length];
  await api("/api/looks/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: outfit.items, action, score: outfit.score }),
  });
}

$("dress-shuffle").onclick = async () => {
  if (!looksState.outfits.length) return;
  feedback("shuffled").catch(() => {});
  looksState.at += 1;
  showOutfit();
};

$("dress-save").onclick = async () => {
  await feedback("saved");
  $("dress-save").textContent = "Saved";
  $("dress-save").disabled = true;
};

$("dress-draw").onclick = async () => {
  const outfit = looksState.outfits[looksState.at % looksState.outfits.length];
  try {
    const look = await api("/api/looks/draw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: outfit.items }),
    });
    await waitForDrawing(look, $("dress-drawn"), $("dress-note"));
  } catch (err) {
    $("dress-note").textContent = err.message;
  }
};

$("copy-input").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  $("copy-note").classList.remove("failed");
  $("copy-note").textContent = "Reading the outfit…";
  $("copy-body").hidden = true;
  const form = new FormData();
  form.append("photo", file);
  try {
    const res = await api("/api/looks/copy", { method: "POST", body: form });
    $("copy-note").textContent = "";
    $("copy-matched").replaceChildren(res.matched.length ? piecesRow(res.matched.map((m) => m.piece)) : "");
    $("copy-matched-title").textContent = res.matched.length
      ? `From your wardrobe (${res.matched.map((m) => m.want.type).join(", ")})`
      : "Nothing in your wardrobe matches this one.";
    $("copy-missing").replaceChildren(...res.missing.map((m) => {
      const li = document.createElement("li");
      const dot = Object.assign(document.createElement("span"), { className: "swatch" });
      dot.style.background = m.hex || "transparent";
      const link = Object.assign(document.createElement("a"), {
        href: m.vinted, target: "_blank", rel: "noopener", textContent: "Find on Vinted",
      });
      li.append(dot, `${m.type} · `, link);
      return li;
    }));
    $("copy-missing-title").hidden = !res.missing.length;
    $("copy-draw").hidden = !res.matched.length;
    $("copy-draw").dataset.look = res.look.id;
    $("copy-drawn").hidden = true;
    $("copy-body").hidden = false;
  } catch (err) {
    $("copy-note").textContent = err.message;
    $("copy-note").classList.add("failed");
  }
};

$("copy-draw").onclick = async () => {
  try {
    const look = await api(`/api/looks/${$("copy-draw").dataset.look}/draw-copy`, { method: "POST" });
    await waitForDrawing(look, $("copy-drawn"), $("copy-note"));
  } catch (err) {
    $("copy-note").textContent = err.message;
  }
};

async function missingPiece() {
  $("missing-note").textContent = `Trying everyday pieces against your wardrobe…`;
  try {
    const res = await api("/api/looks/missing");
    const [best, ...next] = res.best;
    $("missing-body").dataset.loaded = "1";
    $("missing-headline").textContent = best.adds
      ? `${best.type[0].toUpperCase() + best.type.slice(1)} would take you from ${res.outfits_now} to ${best.outfits} good outfits.`
      : `You have ${res.outfits_now} good outfits; none of ${res.tried} everyday pieces would add more.`;
    $("missing-dot").style.background = best.hex;
    $("missing-vinted").href = best.vinted;
    $("missing-next").textContent = next.length
      ? "Next best: " + next.map((n) => `${n.type} (+${n.adds})`).join(", ") + "."
      : "";
    $("missing-note").textContent = `Tried ${res.tried} everyday pieces.`;
  } catch (err) {
    $("missing-note").textContent = err.message;
  }
}

$("missing-draw").onclick = async () => {
  try {
    const look = await api("/api/looks/missing/draw", { method: "POST" });
    await waitForDrawing(look, $("missing-drawn"), $("missing-note"));
  } catch (err) {
    $("missing-note").textContent = err.message;
  }
};

let morningPick = null;
$("morning-go").onclick = async () => {
  $("morning-note").textContent = "Checking the weather…";
  try {
    morningPick = await api("/api/looks/morning?" + new URLSearchParams({ events: $("morning-events").value }));
    $("morning-line").textContent = morningPick.line;
    $("morning-why").textContent = morningPick.why;
    $("morning-stage").replaceChildren(piecesRow(morningPick.pieces));
    $("morning-sketch").hidden = false;
    $("morning-drawn").hidden = true;
    $("morning-note").textContent = "";
  } catch (err) {
    $("morning-note").textContent = err.message;
  }
};

$("morning-sketch").onclick = async () => {
  try {
    const look = await api("/api/looks/morning/sketch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: morningPick.items }),
    });
    await waitForDrawing(look, $("morning-drawn"), $("morning-note"));
  } catch (err) {
    $("morning-note").textContent = err.message;
  }
};

for (const tab of document.querySelectorAll("#looks .looks-tabs button")) {
  tab.onclick = () => looksTab(tab.dataset.tab);
}
$("looks-btn").onclick = () => {
  $("looks").showModal();
  looksTab("dress");
};
$("looks-close").onclick = () => $("looks").close();
