const FIELDS = [
  { key: "type", label: "Type", guessed: true },
  { key: "colour", label: "Colour", guessed: true },
  { key: "fabric", label: "Fabric", guessed: true },
  { key: "brand", label: "Brand", guessed: true },
  { key: "size", label: "Size", guessed: true },
  { key: "amount_paid", label: "Amount paid (£)", number: true },
  { key: "notes", label: "Notes", long: true },
];

// The ways to narrow the list. Each gives an item's option names for it; ticking
// two options in one shows items with either, and an item must pass every one.
const DIMENSIONS = [
  { key: "type", label: "Type", names: (i) => [i.type && sentence(i.type.trim().toLowerCase())] },
  { key: "colour", label: "Colour", names: (i) => colours(i.colour) },
  { key: "fabric", label: "Fabric", names: (i) => [mainFabric(i.fabric)] },
  { key: "brand", label: "Brand", names: (i) => [i.brand?.trim() || "No brand"] },
  { key: "size", label: "Size", names: (i) => [i.size?.trim() || "No size"] },
  { key: "status", label: "Status", names: (i) => [
    (i.unchecked.length || i.status === "failed") && "To check", i.draft && "Draft", i.plan && sentence(i.plan)] },
];

// Empty values go last in every sort except the two by date.
const SORTS = [
  { key: "newest", label: "Newest" },
  { key: "oldest", label: "Oldest" },
  { key: "type", label: "Type", value: (i) => i.type?.trim().toLowerCase() },
  { key: "colour", label: "Colour", value: (i) => colours(i.colour)[0]?.toLowerCase() },
  { key: "brand", label: "Brand", value: (i) => i.brand?.trim().toLowerCase() },
  { key: "paid_desc", label: "Paid, highest", value: (i) => i.amount_paid, desc: true },
  { key: "paid_asc", label: "Paid, lowest", value: (i) => i.amount_paid },
];

let items = [];
// picked: dimension key -> ticked option keys (lower case). Kept per device.
let view = { picked: {}, sort: "newest" };
let menuDim = null; // the dimension whose menu is open
let openId = null;
let pollTimer = null;

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) showSignIn(); // the public copy, signed out (or the sign-in ran out)
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.status === 204 ? null : res.json();
}

async function load() {
  items = await api("/api/items");
  render();
  if (openId) refreshDetail();
  schedulePoll();
}

// A photo's cutout or tidy is waiting its turn ("queued") or running ("working").
const BUSY = ["queued", "working"];
const photoBusy = (p) => BUSY.includes(p.cutout_status) || BUSY.includes(p.tidy_status);
const busy = (item) => item.status === "processing" || item.photos.some(photoBusy);

// What the item's photos are waiting for, for its card.
function photosNote(item) {
  const steps = item.photos.flatMap((p) => [p.cutout_status, p.tidy_status]);
  if (steps.includes("working")) return "working on photos…";
  if (steps.includes("queued")) return "waiting its turn…";
  return "";
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (items.some(busy)) pollTimer = setTimeout(load, 2000);
}

// After a poll: redraw the fields too, unless someone is in the middle of editing them.
function refreshDetail() {
  const editing = Object.keys(changedFields()).length || $("fields").contains(document.activeElement);
  if (editing) fillImage();
  else fillDetail();
}

function replaceItem(item) {
  items = items.map((i) => (i.id === item.id ? item : i));
}

const sentence = (text) => text.charAt(0).toUpperCase() + text.slice(1);

// The best picture of a photo: the Codex redraw, then the cutout, then the photo itself.
function bestImage(photo) {
  return "/images/" + (photo.tidy || photo.cutout || photo.original);
}

// The pictures a photo has, best first.
function viewsFor(photo) {
  return [["tidy", "Tidied"], ["cutout", "Cutout"], ["original", "Original"]].filter(([key]) => photo[key]);
}

function selectedPhoto(item) {
  return item.photos.find((p) => p.id === $("detail").dataset.photo) || item.photos[0];
}

function matches(item, query) {
  if (!query) return true;
  const text = [item.type, item.colour, item.fabric, item.brand, item.size, item.notes].join(" ").toLowerCase();
  return query.split(/\s+/).every((word) => text.includes(word));
}

// "navy and cream" counts as navy and as cream.
function colours(text) {
  return (text || "").split(/\s+and\s+|[,/&]/).map((c) => c.trim().toLowerCase()).filter(Boolean).map(sentence);
}

// A label's mix like "80% wool, 20% nylon" counts as its biggest share, wool.
function mainFabric(text) {
  let best = null;
  for (const part of (text || "").split(/[,;/]/)) {
    const share = parseFloat(part.match(/(\d+(?:\.\d+)?)\s*%/)?.[1] ?? "0");
    const name = part.replace(/\d+(?:\.\d+)?\s*%/g, "").trim().toLowerCase();
    if (name && (!best || share > best.share)) best = { name, share };
  }
  return best && sentence(best.name);
}

function optionsOf(dim, item) {
  return dim.names(item).filter(Boolean);
}

function passes(item, dim) {
  const picked = view.picked[dim.key] || [];
  return !picked.length || optionsOf(dim, item).some((name) => picked.includes(name.toLowerCase()));
}

// What a dimension's menu offers: every option among the items that pass the search and
// the other dimensions, with how many there are. A ticked option stays even at 0.
function menuOptions(dim, query) {
  const counts = new Map();
  for (const item of items) {
    if (!matches(item, query) || !DIMENSIONS.every((d) => d === dim || passes(item, d))) continue;
    for (const name of optionsOf(dim, item)) {
      const key = name.toLowerCase();
      const option = counts.get(key) || { key, name, count: 0 };
      option.count += 1;
      counts.set(key, option);
    }
  }
  for (const key of view.picked[dim.key] || []) {
    if (!counts.has(key)) counts.set(key, { key, name: sentence(key), count: 0 });
  }
  // "No brand" and "No size" go last.
  return [...counts.values()].sort((a, b) =>
    a.name.startsWith("No ") - b.name.startsWith("No ") || a.name.localeCompare(b.name));
}

function sortItems(list) {
  const sort = SORTS.find((s) => s.key === view.sort) || SORTS[0];
  if (sort.key === "oldest") return [...list].reverse();
  if (!sort.value) return list;
  const rows = list.map((item, n) => ({ item, n, v: sort.value(item) ?? null }));
  rows.sort((a, b) => {
    if ((a.v === null) !== (b.v === null)) return a.v === null ? 1 : -1;
    const order = a.v === null ? 0 : typeof a.v === "number" ? a.v - b.v : a.v.localeCompare(b.v);
    return (sort.desc ? -order : order) || a.n - b.n;
  });
  return rows.map((r) => r.item);
}

function saveView() {
  try { localStorage.setItem("tidyrack-view", JSON.stringify(view)); } catch {}
}

function loadView() {
  try {
    const saved = JSON.parse(localStorage.getItem("tidyrack-view") || localStorage.getItem("wardrobe-view"));
    if (saved && typeof saved.picked === "object") view = { picked: saved.picked, sort: saved.sort || "newest" };
  } catch {}
}

function filtering() {
  return Object.values(view.picked).some((keys) => keys.length) || view.sort !== "newest"
    || $("search").value.trim() !== "";
}

function render() {
  const query = $("search").value.trim().toLowerCase();

  $("dims").replaceChildren(...DIMENSIONS.map((dim) => {
    const picked = view.picked[dim.key] || [];
    const options = menuOptions(dim, query);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "dd" + (picked.length ? " on" : "") + (dim.key === menuDim ? " open" : "");
    b.hidden = !options.length && !items.some((i) => optionsOf(dim, i).length);
    b.dataset.dim = dim.key;
    const only = options.find((o) => o.key === picked[0]);
    b.textContent = picked.length === 1 && only ? `${dim.label}: ${only.name}`
      : picked.length ? `${dim.label}: ${picked.length}` : dim.label;
    b.onclick = (e) => { e.stopPropagation(); menuDim === dim.key ? closeMenu() : openMenu(dim.key); };
    return b;
  }));
  $("sort").value = view.sort;
  $("sort-box").classList.toggle("on", view.sort !== "newest");
  if (menuDim) fillMenu(query);

  const shown = sortItems(items.filter((i) => matches(i, query) && DIMENSIONS.every((d) => passes(i, d))));

  $("empty").hidden = items.length > 0;
  $("summary").textContent = !items.length || shown.length === items.length ? ""
    : shown.length ? `${shown.length} of ${items.length}` : `None of ${items.length} match`;
  $("summary-row").hidden = !filtering();
  $("grid").replaceChildren(...shown.map(card));
  fillCheckNotice();
}

function openMenu(key) {
  menuDim = key;
  render();
  const box = $("dims").querySelector(`[data-dim="${key}"]`).getBoundingClientRect();
  const menu = $("menu");
  menu.hidden = false;
  menu.style.top = `${box.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(box.left, innerWidth - menu.offsetWidth - 8))}px`;
}

function closeMenu() {
  if (!menuDim) return;
  menuDim = null;
  $("menu").hidden = true;
  render();
}

function fillMenu(query) {
  const dim = DIMENSIONS.find((d) => d.key === menuDim);
  const picked = view.picked[dim.key] || [];
  const rows = menuOptions(dim, query).map((option) => {
    const row = document.createElement("label");
    row.className = "menu-row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = picked.includes(option.key);
    box.onchange = () => {
      const now = (view.picked[dim.key] || []).filter((k) => k !== option.key);
      view.picked[dim.key] = box.checked ? [...now, option.key] : now;
      saveView();
      render();
    };
    const name = document.createElement("span");
    name.textContent = option.name;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = option.count;
    row.append(box, name, count);
    return row;
  });
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "ghost small";
  clear.textContent = `Clear ${dim.label.toLowerCase()}`;
  clear.hidden = !picked.length;
  clear.onclick = () => { view.picked[dim.key] = []; saveView(); render(); };
  if (!rows.length) {
    const none = document.createElement("p");
    none.className = "menu-none";
    none.textContent = "Nothing here matches the other filters.";
    rows.push(none);
  }
  $("menu").replaceChildren(...rows, clear);
}

// The item's colour as worked out from its photo: one round swatch, two if it's two-tone.
function swatches(item) {
  return [item.colour_hex, item.colour_hex_2].filter(Boolean).map((hex) => {
    const dot = document.createElement("i");
    dot.className = "swatch";
    dot.style.background = hex;
    dot.title = hex;
    return dot;
  });
}

function card(item) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "card " + item.status;
  el.onclick = () => openDetail(item.id);

  const cover = item.photos[0];
  const img = document.createElement("img");
  img.src = bestImage(cover);
  img.alt = item.type || "Clothing item";
  img.loading = "lazy";
  if (cover.tidy) img.className = "tidied";
  el.append(img);

  const label = document.createElement("div");
  label.className = "card-label";
  if (item.status === "processing") {
    label.innerHTML = '<span class="spinner"></span> Working it out…';
  } else if (item.status === "failed") {
    label.textContent = "Couldn't read this one";
  } else {
    const name = document.createElement("strong");
    name.textContent = sentence(item.type || "unknown");
    const sub = document.createElement("span");
    sub.textContent = [item.colour, item.brand, item.photos.length > 1 && `${item.photos.length} photos`, photosNote(item)]
      .filter(Boolean).join(" · ");
    sub.prepend(...swatches(item));
    label.append(name, sub);
  }
  el.append(label);

  if (item.draft) {
    const draft = document.createElement("span");
    draft.className = "badge draft";
    draft.textContent = "Draft";
    draft.title = "Still adding photos: open it and close it again to finish it";
    el.append(draft);
  }
  if (item.plan === "upcycle") {
    const badge = document.createElement("span");
    badge.className = "badge plan";
    badge.textContent = "Upcycle";
    badge.title = item.goal?.label || "";
    el.append(badge);
  }
  return el;
}

function openDetail(id, photoId = "") {
  if (!items.some((i) => i.id === id)) return;
  if (openId !== id) finishDraft(openId);
  openId = id;
  $("detail").dataset.photo = photoId;
  history.replaceState(null, "", "#" + id);
  fillDetail();
  if (!$("detail").open) $("detail").showModal();
}

// Leaving a draft finishes it: its details are read again from all its photos.
function finishDraft(id) {
  if (!items.find((i) => i.id === id)?.draft) return;
  api(`/api/items/${id}/finish`, { method: "POST" })
    .then((item) => { replaceItem(item); render(); schedulePoll(); })
    .catch((err) => toast("Couldn't finish it: " + err.message, 4000));
}

// Only the picture side, so polling never wipes fields being edited.
function fillImage() {
  const item = items.find((i) => i.id === openId);
  if (!item) return;
  const photo = selectedPhoto(item);
  const views = viewsFor(photo);
  let view = $("detail").dataset.view;
  if (!views.some(([key]) => key === view)) view = views[0][0];
  $("detail-img").src = "/images/" + photo[view];
  $("detail-img").className = view === "tidy" ? "tidied" : "";

  $("views").replaceChildren(...(views.length > 1 ? views : []).map(([key, label]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip" + (key === view ? " on" : "");
    b.textContent = label;
    b.onclick = () => { $("detail").dataset.view = key; fillImage(); };
    return b;
  }));

  const isLabel = photo.kind === "label";
  const isShop = photo.kind === "shop";
  const isWorn = photo.kind === "worn";
  const queued = photo.tidy_status === "queued";
  const working = BUSY.includes(photo.tidy_status);
  $("tidy-btn").hidden = isLabel || isShop;
  $("tidy-btn").disabled = working;
  $("tidy-btn").textContent = queued ? "Waiting to tidy…" : working ? "Tidying… about 30 s" : photo.tidy ? "Tidy again" : "Tidy up";
  $("image-note").textContent =
    isLabel ? "Label close-up: read for the details, not tidied." :
    isShop && photo.cutout_status !== "failed" ? "Shop photo from the product page: not tidied, and left out of Save photos." :
    queued ? "Waiting for other photos to be tidied first." :
    working ? "Tidyrack is redrawing this as a catalogue photo." :
    photo.tidy_status === "failed" ? `Tidy up didn't work: ${photo.error}` :
    photo.cutout_status === "failed" ? `Cutout didn't work: ${photo.error}` :
    view === "tidy" && isWorn ? "Redrawn by AI from you wearing it: parts it couldn't see, like a covered waistband, are guesses. A mirror selfie's print stays mirrored." :
    view === "tidy" ? "Redrawn by AI. Labels and small text may differ from the real item." :
    isWorn && view === "original" ? "Cropped from your outfit photo." : "";
  $("image-note").className = "image-note"
    + (!working && (photo.tidy_status === "failed" || photo.cutout_status === "failed") ? " failed" : "");

  $("thumbs").replaceChildren(...item.photos.map((p, n) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "thumb" + (p.id === photo.id ? " on" : "");
    b.title = n === 0 ? "Cover photo" : p.kind === "label" ? "Label close-up" : p.kind === "shop" ? "Shop photo" : `Photo ${n + 1}`;
    const img = document.createElement("img");
    img.src = "/images/" + (p.cutout || p.original);
    img.alt = b.title;
    b.append(img);
    if (n === 0 || p.kind === "label" || p.kind === "shop") {
      const tag = document.createElement("span");
      tag.className = "thumb-tag";
      tag.textContent = p.kind === "shop" ? "Shop" : n === 0 ? "Cover" : "Label";
      b.append(tag);
    }
    if (photoBusy(p)) {
      const spin = document.createElement("span");
      spin.className = "spinner thumb-spin";
      b.append(spin);
    }
    b.onclick = () => { $("detail").dataset.photo = p.id; $("detail").dataset.view = ""; fillImage(); };
    return b;
  }));

  $("label-toggle").checked = isLabel;
  $("label-toggle").parentElement.hidden = isShop || isWorn;
  $("cover-btn").hidden = photo.position === 0;
  $("delete-photo-btn").hidden = item.photos.length === 1;
}

function fillDetail() {
  const item = items.find((i) => i.id === openId);
  if (!item) return;
  fillImage();

  const place = checkQueue.indexOf(item.id);
  $("detail").classList.toggle("checking", place >= 0);
  $("detail-status").textContent = (place >= 0 ? `New item ${place + 1} of ${checkQueue.length}. ` : "") + (
    item.draft ? "Add the back or a label close-up if you like. When you close this, the details are read from all its photos." :
    item.status === "processing" ? "Still working out the details…" :
    item.status === "failed" ? `Something went wrong: ${item.error}` :
    item.unchecked.length ? "Highlighted fields are the model's guesses. Fix them or tap All correct." : "");
  $("detail-status").className = "status-note " + item.status;
  // Reads the details again from the photos; fields already fixed or ticked are kept.
  $("retry-btn").hidden = item.draft || item.status === "processing";
  $("retry-btn").textContent = item.status === "failed" ? "Try again" : "Read again";
  $("retry-btn").title = "Read the details again from the photos. Fields you've fixed or ticked stay as they are.";
  $("sell-btn").hidden = item.status !== "ready";
  $("confirm-all-btn").hidden = !item.unchecked.length;

  $("fields").replaceChildren(...FIELDS.map((f) => {
    const row = document.createElement("label");
    row.className = "field" + (item.unchecked.includes(f.key) ? " guess" : "");
    const name = document.createElement("span");
    name.textContent = f.label;
    const input = document.createElement(f.long ? "textarea" : "input");
    input.name = f.key;
    if (f.number) { input.type = "number"; input.step = "0.01"; input.min = "0"; input.inputMode = "decimal"; }
    input.value = item[f.key] ?? "";
    input.dataset.initial = input.value;
    if (f.long) input.rows = 2;
    if (f.key === "colour" && item.colour_hex) {
      const line = document.createElement("div");
      line.className = "colour-line";
      line.append(input, ...colourPickers(item));
      row.append(name, line, ...colourShares(item));
    } else {
      row.append(name, input);
    }
    return row;
  }));

  fillPlan(item);
  const added = new Date(item.added_at);
  $("detail-added").textContent = "Added " + added.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
}

// The colour swatches in the item view. Tapping one opens the phone's colour
// picker; a colour picked by hand is saved with the rest and kept from then on.
function colourPickers(item) {
  return ["colour_hex", "colour_hex_2"].filter((key) => item[key]).map((key) => {
    const picker = document.createElement("input");
    picker.type = "color";
    picker.className = "swatch";
    picker.name = key;
    picker.value = item[key];
    picker.dataset.initial = picker.value;
    picker.title = item.colour_hex_set_by_hand ? "Picked by hand. Tap to change" : "From the photo. Tap to correct";
    return picker;
  });
}

// The garment's main colours as measured from its photo, as a thin bar split by
// how much of the garment each covers. Empty until they're measured.
function colourShares(item) {
  const colours = item.colours?.colours ?? [];
  if (!colours.length) return [];
  const bar = document.createElement("div");
  bar.className = "colour-shares";
  for (const c of colours) {
    const part = document.createElement("span");
    part.style.background = c.hex;
    part.style.flexGrow = c.share;
    bar.append(part);
  }
  const kelvin = item.colours.kelvin;
  const light = kelvin && kelvin < 5000 ? ", warm room light taken out"
    : kelvin && kelvin > 8000 ? ", shade's blue taken out" : "";
  bar.title = "Measured from the photo: " + colours.map((c) => `${c.hex} ${Math.round(c.share * 100)}%`).join(", ") + light;
  return [bar];
}

function changedFields() {
  const changes = {};
  for (const input of $("fields").querySelectorAll("input, textarea")) {
    if (input.value === input.dataset.initial) continue;
    changes[input.name] = input.type === "number"
      ? (input.value === "" ? null : Number(input.value))
      : input.value;
  }
  return changes;
}

async function save(extra = {}) {
  const item = await api(`/api/items/${openId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...changedFields(), ...extra }),
  });
  items = items.map((i) => (i.id === item.id ? item : i));
  render();
  return item;
}

// Send files one at a time, showing how far it's got. A file that fails doesn't
// stop the rest; the failed ones can be sent again with Try again.
// `send(file)` uploads one; `after()` runs once all have been tried.
async function sendAll(files, send, after) {
  const failed = [];
  for (const [n, file] of files.entries()) {
    showUploads(`Uploading ${files.length > 1 ? `${n + 1} of ${files.length}` : "photo"}…`, n / files.length);
    try {
      await send(file);
    } catch (err) {
      failed.push({ file, error: err.message });
    }
  }
  if (failed.length < files.length) await after().catch((err) => failed.push({ error: err.message }));
  if (!failed.length) {
    showUploads(files.length > 1 ? `${files.length} photos uploaded` : "Photo uploaded", 1);
    setTimeout(hideUploads, 2500);
    return;
  }
  const retry = failed.map((f) => f.file).filter(Boolean);
  showUploads(`${retry.length || "Some"} of ${files.length} didn't upload: ${failed[0].error}`, null, true,
    retry.length ? () => sendAll(retry, send, after) : null);
}

let uploadsTimer;
function showUploads(text, done, failed = false, retry = null) {
  clearTimeout(uploadsTimer);
  const panel = $("uploads");
  $("uploads-text").textContent = text;
  $("uploads-bar").hidden = done === null;
  if (done !== null) $("uploads-bar").value = done;
  panel.classList.toggle("failed", failed);
  $("uploads-retry").hidden = !retry;
  $("uploads-retry").onclick = retry;
  $("uploads-close").hidden = !failed;
  // Show it again so it stays above a dialog opened since.
  if (panel.matches(":popover-open")) panel.hidePopover();
  panel.showPopover();
}

function hideUploads() {
  if ($("uploads").matches(":popover-open")) $("uploads").hidePopover();
}

// Several photos picked from Add item: one item, or one item each?
function askBatch(count) {
  $("batch-title").textContent = `${count} photos`;
  $("batch-one").textContent = `One item, ${count} photos`;
  $("batch-separate").textContent = `${count} separate items`;
  $("batch").returnValue = "";
  $("batch").showModal();
  return new Promise((resolve) => {
    $("batch").onclose = () => resolve($("batch").returnValue || "cancel");
  });
}

function showNew(item) {
  items = [item, ...items.filter((i) => i.id !== item.id)];
  render();
  schedulePoll();
}

// A photo becomes a finished item in the list, without opening it, so the next
// garment is two taps away: Add item, then its photo. Several photos at once make
// one item from all of them, or one item each. Either way the new items join
// "N new items to check"; a back or label can still go on later with + Photo.
async function upload(files) {
  const mode = files.length > 1 ? await askBatch(files.length) : "separate";
  if (mode === "cancel") return;

  if (mode === "separate") {
    const added = [];
    await sendAll(files, async (file) => {
      const item = await api("/api/items", { method: "POST", body: photoForm(file, "photo") });
      added.push(item.id);
      // If only the finish fails, the photo is in: keep the draft rather than upload it twice.
      showNew(await api(`/api/items/${item.id}/finish`, { method: "POST" }).catch(() => item));
    }, async () => offerCheck(added));
    return;
  }

  let item = null;
  await sendAll(files, async (file) => {
    item = await api(item ? `/api/items/${item.id}/photos` : "/api/items", { method: "POST", body: photoForm(file, "photo") });
    showNew(item);
  }, async () => {
    if (item.draft) showNew(item = await api(`/api/items/${item.id}/finish`, { method: "POST" }));
    offerCheck([item.id]);
  });
}

function photoForm(file, kind) {
  const body = new FormData();
  body.append("photo", file);
  body.append("kind", kind);
  return body;
}

async function addPhotos(files, kind) {
  const id = openId;
  let item;
  await sendAll(files, async (file) => {
    item = await api(`/api/items/${id}/photos`, { method: "POST", body: photoForm(file, kind) });
    replaceItem(item);
  }, async () => {
    render();
    schedulePoll();
    if (openId === id) {
      $("detail").dataset.photo = item.photos[item.photos.length - 1].id;
      $("detail").dataset.view = "";
      refreshDetail();
    }
  });
}

$("uploads-close").onclick = hideUploads;

// Add item on a phone opens Tidyrack's own camera, which stays open: each tap of
// the shutter is a new item, + This one adds the next shot to the last item. Photos
// upload one after another behind it, and each item is finished when the next one
// starts or the camera closes. Elsewhere, and on plain http, Add item opens the picker.
const cam = {
  stream: null,
  queue: Promise.resolve(), // uploads, one after another, in the order taken
  last: null, // the id of the latest item started here
  added: [], // ids of the items started here
  sending: 0,
  failed: [], // shots that didn't upload: { blob, same }
};

const useCamera = () => window.isSecureContext && !!navigator.mediaDevices?.getUserMedia
  && matchMedia("(pointer: coarse)").matches;

async function openCamera() {
  Object.assign(cam, { last: null, added: [], sending: 0, failed: [] });
  $("camera-last").hidden = true;
  $("camera-more").disabled = true;
  fillCamera();
  $("camera").showModal();
  await startStream();
}

async function startStream() {
  try {
    cam.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 2560 }, height: { ideal: 1920 } },
      audio: false,
    });
    $("camera-video").srcObject = cam.stream;
    $("camera-shot").disabled = false;
  } catch (err) {
    $("camera-shot").disabled = true;
    cameraNote("Tidyrack can't use the camera (" + err.message + "). Allow it in Settings, or tap Library.");
  }
}

function stopStream() {
  cam.stream?.getTracks().forEach((t) => t.stop());
  cam.stream = null;
  $("camera-video").srcObject = null;
}

function cameraNote(text) {
  $("camera-note").textContent = text;
  $("camera-note").hidden = !text;
}

function fillCamera() {
  const n = cam.added.length;
  $("camera-count").textContent = [n ? `${n} added` : "Point at the next item",
    cam.sending && `${cam.sending} uploading`].filter(Boolean).join(" · ");
  if (cam.failed.length) cameraNote(`${cam.failed.length} didn't upload. Tap here to try again.`);
  else if ($("camera-note").textContent.includes("didn't upload")) cameraNote("");
}

// The frame on screen as a JPEG, and a small copy for the + This one button.
async function grab() {
  const video = $("camera-video");
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
  const small = document.createElement("canvas");
  small.height = 96;
  small.width = Math.round(96 * canvas.width / canvas.height);
  small.getContext("2d").drawImage(canvas, 0, 0, small.width, small.height);
  return { blob, thumb: small.toDataURL("image/jpeg", 0.7) };
}

async function shoot(same) {
  if (!cam.stream || !$("camera-video").videoWidth) return;
  const { blob, thumb } = await grab();
  if (!blob) return;
  $("camera-video").animate([{ opacity: 0.3 }, { opacity: 1 }], 180);
  if (!same) {
    $("camera-last").src = thumb;
    $("camera-last").hidden = false;
    $("camera-more").disabled = false;
  }
  queueShot({ blob, same });
}

function queueShot(shot) {
  cam.sending++;
  fillCamera();
  cam.queue = cam.queue.then(() => sendShot(shot));
}

async function sendShot(shot) {
  const file = new File([shot.blob], "camera.jpg", { type: "image/jpeg" });
  try {
    if (shot.same && cam.last) {
      replaceItem(await api(`/api/items/${cam.last}/photos`, { method: "POST", body: photoForm(file, "photo") }));
      render();
      schedulePoll();
    } else {
      const before = cam.last;
      const item = await api("/api/items", { method: "POST", body: photoForm(file, "photo") });
      cam.last = item.id;
      cam.added.push(item.id);
      showNew(item);
      if (before) await finishShot(before);
    }
  } catch (err) {
    cam.failed.push(shot);
  } finally {
    cam.sending--;
    fillCamera();
  }
}

async function finishShot(id) {
  try {
    replaceItem(await api(`/api/items/${id}/finish`, { method: "POST" }));
    render();
    schedulePoll();
  } catch {
    // It stays a draft with its photos in; opening and closing it finishes it.
  }
}

async function closeCamera() {
  if (cam.failed.length && !cam.leaving) {
    cam.leaving = true;
    cameraNote(`${cam.failed.length} didn't upload. Tap here to try again, or Done to leave without them.`);
    return;
  }
  cam.leaving = false;
  stopStream();
  $("camera").close();
  await cam.queue;
  if (cam.last) await finishShot(cam.last);
  offerCheck(cam.added);
}

$("camera-shot").onclick = () => shoot(false);
$("camera-more").onclick = () => shoot(true);
$("camera-done").onclick = closeCamera;
$("camera").oncancel = (e) => { e.preventDefault(); closeCamera(); };
$("camera-note").onclick = () => {
  if (!cam.failed.length) return;
  const again = cam.failed;
  cam.failed = [];
  cam.leaving = false;
  again.forEach(queueShot);
};
$("camera-library").onchange = async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  await closeCamera();
  if (!$("camera").open) await upload(files);
};
// The phone stops the camera while Tidyrack is in the background; start it again on return.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && $("camera").open
      && !cam.stream?.getVideoTracks().some((t) => t.readyState === "live")) startStream();
});

$("add-btn").onclick = (e) => {
  if (e.target.id === "photo-input" || !useCamera()) return;
  e.preventDefault();
  openCamera();
};

// After adding: the new items' guesses, checked one at a time.
let checkIds = []; // items added since the page loaded, from the camera or Add item
let checkQueue = []; // the ones being checked now, in order
const needsCheck = (item) => item && !item.draft
  && (item.unchecked.length > 0 || item.status === "failed" || item.status === "processing");

// New items join the ones not checked yet, so adding one at a time builds one list.
function offerCheck(ids) {
  checkIds = [...checkIds.filter((id) => !ids.includes(id)), ...ids];
  render();
}

function fillCheckNotice() {
  const left = checkIds.filter((id) => needsCheck(items.find((i) => i.id === id)));
  $("check-new").hidden = !left.length || $("camera").open;
  $("check-new-text").textContent = left.length === 1 ? "1 new item to check."
    : `${left.length} new items to check, one at a time.`;
}

$("check-new-go").onclick = () => {
  checkQueue = checkIds.filter((id) => needsCheck(items.find((i) => i.id === id)));
  if (checkQueue.length) openDetail(checkQueue[0]);
};
$("check-new-close").onclick = () => { checkIds = []; render(); };

// After All correct or Save: the next new item to check, or back to the list.
function nextToCheck() {
  const at = checkQueue.indexOf(openId);
  const next = at < 0 ? null : checkQueue.slice(at + 1).find((id) => needsCheck(items.find((i) => i.id === id)));
  if (!next) {
    if (at >= 0) toast("All the new items are checked", 2500);
    checkQueue = [];
    $("detail").close();
    return;
  }
  $("detail").dataset.view = "";
  openDetail(next);
  $("detail").querySelector(".detail-body")?.scrollTo(0, 0);
  $("detail").scrollTo(0, 0);
}

// Photo actions all answer with the updated item.
async function photoAction(path, options, keepSelection = true) {
  try {
    replaceItem(await api(path, options));
    if (!keepSelection) $("detail").dataset.photo = "";
    render();
    refreshDetail();
    schedulePoll();
  } catch (err) {
    toast(err.message, 4000);
  }
}

let toastTimer;
function toast(text, ms) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => ($("toast").hidden = true), ms);
}

$("photo-input").onchange = async (e) => {
  if (e.target.files.length) await upload([...e.target.files]);
  e.target.value = "";
};

for (const input of document.querySelectorAll(".add-photo")) {
  input.onchange = async () => {
    if (input.files.length) await addPhotos([...input.files], input.dataset.kind);
    input.value = "";
  };
}

$("label-toggle").onchange = () => {
  const photo = selectedPhoto(items.find((i) => i.id === openId));
  photoAction(`/api/photos/${photo.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: $("label-toggle").checked ? "label" : "photo" }),
  });
};

$("cover-btn").onclick = () => {
  const photo = selectedPhoto(items.find((i) => i.id === openId));
  photoAction(`/api/photos/${photo.id}/cover`, { method: "POST" });
};

$("delete-photo-btn").onclick = () => {
  const photo = selectedPhoto(items.find((i) => i.id === openId));
  if (!confirm("Delete this photo?")) return;
  photoAction(`/api/photos/${photo.id}`, { method: "DELETE" }, false);
};

$("search").oninput = render;

$("sort").replaceChildren(...SORTS.map((s) => new Option(s.label, s.key)));
$("sort").onchange = () => { view.sort = $("sort").value; saveView(); render(); };

$("clear-btn").onclick = () => {
  view = { picked: {}, sort: "newest" };
  $("search").value = "";
  saveView();
  closeMenu();
  render();
};

// The menu closes on a tap outside it, Escape, or when what it hangs from moves.
$("menu").onclick = (e) => e.stopPropagation();
document.addEventListener("click", closeMenu);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("scroll", closeMenu);
window.addEventListener("resize", closeMenu);
$("chips").addEventListener("scroll", closeMenu);

$("detail-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await save();
    nextToCheck();
  } catch (err) {
    toast("Couldn't save: " + err.message, 4000);
  }
};

$("confirm-all-btn").onclick = async () => {
  const item = items.find((i) => i.id === openId);
  await save({ checked: item.unchecked });
  nextToCheck();
};

$("delete-btn").onclick = async () => {
  if (!confirm("Delete this item and its photos?")) return;
  await api(`/api/items/${openId}`, { method: "DELETE" });
  items = items.filter((i) => i.id !== openId);
  $("detail").close();
  render();
};

$("retry-btn").onclick = () => photoAction(`/api/items/${openId}/retry`, { method: "POST" });

$("tidy-btn").onclick = () => {
  const photo = selectedPhoto(items.find((i) => i.id === openId));
  $("detail").dataset.view = "";
  photoAction(`/api/photos/${photo.id}/tidy`, { method: "POST" });
};

$("close-btn").onclick = () => $("detail").close();
$("detail").onclose = () => {
  finishDraft(openId);
  openId = null;
  checkQueue = [];
  $("detail").dataset.view = "";
  $("detail").dataset.photo = "";
  history.replaceState(null, "", location.pathname);
};
$("detail").onclick = (e) => { if (e.target === $("detail")) $("detail").close(); };

// Sell on Vinted: the AI drafts the listing; the owner copies it into Vinted by hand
// (Vinted has no listing API for personal accounts and bans bots).
let sellId = null;
let sellFiles = [];
let sellLoad = 0;

// Vinted can suspend accounts for misleading photos and dislikes edited ones, so only the
// real photos go by default: the cover first, then the other photos and labels. No cutouts,
// and no other kind, like shop photos. A worn photo's crop of the outfit photo is real pixels,
// so it goes too. The AI-tidied picture goes last, if asked for.
const SELL_KINDS = ["photo", "worn", "label"];

function sellPhotos(item) {
  const real = item.photos.map((p, n) => [p, n]).filter(([p]) => SELL_KINDS.includes(p.kind));
  const cover = real.find(([p]) => p.kind !== "label")?.[0];
  return [...real.sort(([a], [b]) => (b === cover) - (a === cover))
            .map(([p, n]) => [p.kind === "label" ? `label-${n + 1}` : `photo-${n + 1}`, p.original]),
          $("sell-tidy").checked && cover?.tidy && ["AI-tidied", cover.tidy]]
    .filter(Boolean);
}

// Fetch the photos ahead, so Save photos can open the share sheet straight from the tap.
function loadSellFiles(item) {
  const load = ++sellLoad;
  sellFiles = [];
  Promise.all(sellPhotos(item).map(async ([label, name]) => {
    const blob = await (await fetch("/images/" + name)).blob();
    return new File([blob], `${(item.type || "item").replace(/\W+/g, "-")}-${label}.${name.split(".").pop()}`, { type: blob.type });
  })).then((files) => { if (load === sellLoad) sellFiles = files; });
}

async function openSell(id) {
  sellId = id;
  const item = items.find((i) => i.id === id);
  $("sell-body").hidden = true;
  $("sell-tidy").checked = false;
  $("sell").showModal();
  loadSellFiles(item);

  if (item.listing) fillSell(item);
  else await writeListing();
}

function sellOptionsChanged() {
  const item = items.find((i) => i.id === sellId);
  loadSellFiles(item);
  if (item.listing) showSellPhotos(item);
}

$("sell-tidy").onchange = sellOptionsChanged;

async function writeListing() {
  $("sell-status").textContent = "Tidyrack is writing the listing… about 10 seconds.";
  $("sell-status").className = "status-note";
  $("sell-again").disabled = true;
  try {
    const item = await api(`/api/items/${sellId}/listing`, { method: "POST" });
    items = items.map((i) => (i.id === item.id ? item : i));
    fillSell(item);
  } catch (err) {
    $("sell-status").textContent = err.message;
    $("sell-status").className = "status-note failed";
  } finally {
    $("sell-again").disabled = false;
  }
}

function showSellPhotos(item) {
  $("sell-photos").replaceChildren(...sellPhotos(item).map(([label, name]) => {
    const fig = document.createElement("figure");
    const img = document.createElement("img");
    img.src = "/images/" + name;
    img.alt = label;
    const cap = document.createElement("figcaption");
    cap.textContent = sentence(label.replaceAll("-", " "));
    fig.append(img, cap);
    return fig;
  }));
}

function fillSell(item) {
  const l = item.listing;
  $("sell-status").textContent = item.unchecked.length
    ? "Some details are still unchecked guesses, so check them in the listing."
    : "";
  $("sell-status").className = "status-note";
  $("sell-title").value = l.title;
  $("sell-description").value = l.description;
  showSellPhotos(item);
  const facts = [
    ["Category", l.category],
    ["Brand", item.brand || "No brand"],
    ["Size", item.size || "Add a size on the item first"],
    ["Condition", l.condition],
    ["Colour", item.colour],
    ["Material", item.fabric],
    ["Parcel size", l.parcel_size],
    ["Price", `£${l.price_gbp}. ${l.price_reason}`],
  ];
  $("sell-facts").replaceChildren(...facts.flatMap(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v || "–";
    if (k === "Size" && !item.size) dd.className = "missing";
    return [dt, dd];
  }));
  $("sell-body").hidden = false;
}

for (const button of document.querySelectorAll("[data-copy]")) {
  button.onclick = async (e) => {
    e.preventDefault();
    try {
      await navigator.clipboard.writeText($(button.dataset.copy).value);
      toast("Copied", 1500);
    } catch {
      $(button.dataset.copy).select();
      toast("Couldn't copy; it's selected, so copy it yourself", 3000);
    }
  };
}

$("sell-save-photos").onclick = async () => {
  if (!sellFiles.length) return toast("Photos still loading, try again in a moment", 2500);
  if (navigator.canShare && navigator.canShare({ files: sellFiles })) {
    try { await navigator.share({ files: sellFiles }); } catch { /* closed the share sheet */ }
    return;
  }
  for (const file of sellFiles) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(file);
    a.download = file.name;
    a.click();
    URL.revokeObjectURL(a.href);
  }
};

$("sell-btn").onclick = () => openSell(openId);
$("sell-again").onclick = writeListing;
$("sell-close").onclick = () => $("sell").close();
$("sell").onclose = () => { sellId = null; };

// On me: the item drawn onto one of your Outfit photos (the newest, unless you pick
// another). Each picture is kept on the item until deleted. It's a model's picture,
// so it lives apart from the item's photos and never reaches Save photos.
const onme = { id: null, selfies: [], selfie: null, shown: null, timer: null };
const NO_SELFIE = "On me needs a photo of you wearing clothes. Tap Outfit (at the top) and add a mirror selfie "
  + "with your whole outfit in view; it's kept for trying things on.";

async function openOnMe(id) {
  Object.assign(onme, { id, selfie: null, shown: null });
  $("onme-status").textContent = "";
  $("onme-status").className = "status-note";
  $("onme").showModal();
  try {
    onme.selfies = await api("/api/selfies");
  } catch (err) {
    $("onme-status").textContent = err.message;
    $("onme-status").className = "status-note failed";
    return;
  }
  onme.selfie = onme.selfies[0]?.id || null;
  const item = items.find((i) => i.id === id);
  onme.shown = item.tryons.find((t) => t.selfie_id === onme.selfie)?.id || null;
  fillOnMe();
}

function fillOnMe() {
  const item = items.find((i) => i.id === onme.id);
  if (!item) return;
  const none = !onme.selfies.length;
  for (const id of ["onme-pair", "onme-picker-box", "onme-delete-selfie", "onme-go"]) $(id).hidden = none;
  if (none) {
    $("onme-status").textContent = NO_SELFIE;
    return;
  }
  const selfie = onme.selfies.find((s) => s.id === onme.selfie);
  $("onme-selfie").src = "/images/" + selfie.original;
  $("onme-picker").replaceChildren(...onme.selfies.map((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "thumb" + (s.id === onme.selfie ? " on" : "");
    b.title = "Outfit photo of " + new Date(s.added_at).toLocaleDateString();
    const img = document.createElement("img");
    img.src = "/images/" + s.original;
    img.alt = b.title;
    b.append(img);
    b.onclick = () => {
      onme.selfie = s.id;
      onme.shown = item.tryons.find((t) => t.selfie_id === s.id)?.id || null;
      fillOnMe();
    };
    return b;
  }));

  const tryon = item.tryons.find((t) => t.id === onme.shown);
  const what = [item.colour, item.type].filter(Boolean).join(" ") || "this item";
  const box = $("onme-result");
  box.replaceChildren();
  box.className = "onme-result";
  if (!tryon) {
    box.textContent = `Tap Try it on to see the ${what} on you.`;
    $("onme-caption").textContent = "";
  } else if (tryon.status === "working") {
    const spin = document.createElement("span");
    spin.className = "spinner";
    box.append(spin, " Drawing… up to a minute");
    $("onme-caption").textContent = "";
  } else if (tryon.status === "failed") {
    box.textContent = tryon.error || "It didn't work. Try it on again.";
    box.className = "onme-result failed";
    $("onme-caption").textContent = "";
  } else {
    const img = document.createElement("img");
    img.src = "/images/" + tryon.image;
    img.alt = `You in the ${what}, drawn by AI`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "ghost small danger onme-delete";
    del.textContent = "Delete picture";
    del.onclick = () => deleteTryOn(tryon.id);
    box.append(img, del);
    $("onme-caption").textContent = `With the ${what}`;
  }
  $("onme-go").disabled = tryon?.status === "working";
  $("onme-go").textContent = tryon ? "Try again" : "Try it on";
  clearTimeout(onme.timer);
  if (item.tryons.some((t) => t.status === "working")) onme.timer = setTimeout(pollOnMe, 3000);
}

async function pollOnMe() {
  if (!onme.id) return;
  try {
    replaceItem(await api(`/api/items/${onme.id}`));
  } catch { /* try again on the next tick */ }
  fillOnMe();
}

async function deleteTryOn(tryonId) {
  try {
    replaceItem(await api(`/api/tryons/${tryonId}`, { method: "DELETE" }));
    onme.shown = null;
    fillOnMe();
  } catch (err) {
    toast(err.message, 4000);
  }
}

$("onme-go").onclick = async () => {
  try {
    const item = await api(`/api/items/${onme.id}/tryons`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selfie_id: onme.selfie }),
    });
    replaceItem(item);
    onme.shown = item.tryons[0].id;
    fillOnMe();
  } catch (err) {
    $("onme-status").textContent = err.message;
    $("onme-status").className = "status-note failed";
  }
};

$("onme-delete-selfie").onclick = async () => {
  if (!confirm("Delete this Outfit photo? Its items and any pictures made from it stay.")) return;
  try {
    await api(`/api/selfies/${onme.selfie}`, { method: "DELETE" });
    onme.selfies = onme.selfies.filter((s) => s.id !== onme.selfie);
    onme.selfie = onme.selfies[0]?.id || null;
    const item = items.find((i) => i.id === onme.id);
    onme.shown = item.tryons.find((t) => t.selfie_id === onme.selfie)?.id || null;
    fillOnMe();
  } catch (err) {
    toast(err.message, 4000);
  }
};

$("onme-btn").onclick = () => openOnMe(openId);
$("onme-close").onclick = () => $("onme").close();
$("onme").onclose = () => { clearTimeout(onme.timer); onme.id = null; };

// Keep, Upcycle or Sell, and What if I…: the garment drawn dyed, faded, shortened or with new
// buttons before choosing. The pictures are AI-drawn, so they're never Vinted photos.
const PLANS = [["keep", "Keep"], ["upcycle", "Upcycle"], ["sell", "Sell"]];
const LONG_UNWORN_DAYS = 180;
let whatIfId = null;
let whatIfTimer = null;
let whatIfView = null;
let whatIfPick = null;  // the edit picked for Upcycle

// A quiet suggestion, never a pop-up: for an item marked for sale (or with a listing drafted),
// or one not seen worn in a long time. Outfit photos are the only record of wearing, so an item
// with none counts from the day it was added.
function whatIfNudge(item) {
  if (item.status !== "ready" || item.plan === "keep" || item.plan === "upcycle") return "";
  if (item.plan === "sell" || item.listing) return "Before you sell it: what if you dyed or altered it?";
  const worn = item.photos.filter((p) => p.kind === "worn").map((p) => p.added_at).sort();
  const since = (Date.now() - new Date(worn.at(-1) || item.added_at)) / 864e5;
  return since > LONG_UNWORN_DAYS ? "Not worn in a while? See what it could become." : "";
}

function fillPlan(item) {
  $("plan-choices").replaceChildren(...PLANS.map(([key, label]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip" + (item.plan === key ? " on" : "");
    b.textContent = label;
    b.onclick = () => {
      if (item.plan === key) return choosePlan(null);
      if (key === "upcycle") return openWhatIf(item.id, "Pick the picture you want it to become.");
      choosePlan(key);
    };
    return b;
  }));
  $("plan").hidden = item.status !== "ready";
  $("what-if-btn").hidden = item.status !== "ready";
  const nudge = whatIfNudge(item);
  $("what-if-nudge").hidden = !nudge;
  $("what-if-link").textContent = nudge;

  const goal = item.plan === "upcycle" && item.goal;
  $("goal").hidden = !goal;
  if (!goal) return;
  const h = goal.howto;
  $("goal-img").src = "/images/" + goal.image;
  $("goal-img").alt = goal.label;
  $("goal-label").textContent = "Goal: " + goal.label;
  $("goal-cost").textContent = `${sentence(h.cost)} · ${h.time}. The picture is drawn by AI.`;
  const list = (rows) => rows.map((text) => Object.assign(document.createElement("li"), { textContent: text }));
  $("goal-materials").replaceChildren(...list(h.materials));
  $("goal-steps").replaceChildren(...list(h.steps));
}

async function choosePlan(plan, editId = null) {
  const item = await api(`/api/items/${openId || whatIfId}/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan, edit_id: editId }),
  });
  replaceItem(item);
  render();
  if (openId === item.id) fillDetail();
  return item;
}

// The picture edits start from, as the server picks it: the first tidied view, else the first view.
function whatIfSource(item) {
  const views = item.photos.filter((p) => p.kind !== "label");
  return views.find((p) => p.tidy)?.tidy || views[0]?.original;
}

async function openWhatIf(id, note = "") {
  whatIfId = id;
  whatIfPick = null;
  const item = items.find((i) => i.id === id);
  $("what-if-name").textContent = [sentence([item.colour, item.fabric, item.type].filter(Boolean).join(" ")), item.brand]
    .filter(Boolean).join(" · ");
  $("what-if-status").textContent = note;
  $("what-if-status").className = "status-note";
  $("what-if-text").value = "";
  $("what-if-grid").replaceChildren();
  $("what-if").showModal();
  fillWhatIf(await api(`/api/items/${id}/what-if`));
}

async function askWhatIf(option, text = "") {
  try {
    fillWhatIf(await api(`/api/items/${whatIfId}/what-if`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ option, text }),
    }));
  } catch (err) {
    $("what-if-status").textContent = err.message;
    $("what-if-status").className = "status-note failed";
  }
}

// One picture, looking like a card in the list. A drawn edit is picked with a tap; one not drawn
// yet shows the garment as it is, faded, with what a tap will do over it.
function whatIfTile(title, lines, image, { over = "", onclick = null, on = false, goal = false } = {}) {
  const tile = document.createElement(onclick ? "button" : "div");
  tile.className = "what-if-tile" + (on ? " on" : "") + (over ? " waiting" : "");
  if (onclick) { tile.type = "button"; tile.onclick = onclick; }
  const pic = document.createElement("span");
  pic.className = "what-if-pic";
  const img = document.createElement("img");
  img.src = "/images/" + image;
  img.alt = title;
  pic.append(img);
  if (over) {
    const label = document.createElement("span");
    label.className = "what-if-over";
    label.innerHTML = over;
    pic.append(label);
  }
  if (goal) pic.append(Object.assign(document.createElement("span"), { className: "badge plan", textContent: "Your goal" }));
  const label = document.createElement("span");
  label.className = "card-label";
  label.append(Object.assign(document.createElement("strong"), { textContent: title }),
    ...lines.filter(Boolean).map((text) => Object.assign(document.createElement("span"), { textContent: text })));
  tile.append(pic, label);
  return tile;
}

function fillWhatIf(view) {
  clearTimeout(whatIfTimer);
  if (!whatIfId) return;
  whatIfView = view;
  const item = items.find((i) => i.id === whatIfId);
  const isGoal = (edit) => item.plan === "upcycle" && item.goal?.edit_id === edit.id;
  // The latest try of each offered edit, then the person's own ideas.
  const latest = (key) => view.edits.filter((e) => e.option === key).at(-1);
  const rows = [...view.options.map((o) => [o, latest(o.key)]),
                ...view.edits.filter((e) => e.option === "own").map((e) => [e, e])];
  if (!view.edits.some((e) => e.id === whatIfPick && e.status === "ready")) whatIfPick = null;
  $("what-if-all").hidden = !view.options.some((o) => !latest(o.key));
  $("what-if-grid").replaceChildren(
    whatIfTile("Now", ["As it is"], whatIfSource(item)),
    ...rows.map(([o, edit]) => {
      const lines = [o.cost, o.effort];
      if (!edit) return whatIfTile(sentence(o.label), lines, whatIfSource(item), { over: "Show me", onclick: () => askWhatIf(o.key) });
      if (BUSY.includes(edit.status)) {
        return whatIfTile(sentence(o.label), lines, whatIfSource(item), {
          over: '<span class="spinner"></span> ' + (edit.status === "queued" ? "Waiting its turn…" : "Drawing… about 30 s") });
      }
      if (edit.status === "failed") {
        return whatIfTile(sentence(o.label), ["Didn't work: " + edit.error], whatIfSource(item), {
          over: "Try again", onclick: () => askWhatIf(o.key === "own" ? "own" : o.key, o.key === "own" ? o.label : "") });
      }
      return whatIfTile(sentence(o.label), lines, edit.image, {
        on: whatIfPick === edit.id, goal: isGoal(edit),
        onclick: () => { whatIfPick = whatIfPick === edit.id ? null : edit.id; fillWhatIf(whatIfView); } });
    }));
  const picked = view.edits.find((e) => e.id === whatIfPick);
  const upcycle = $("what-if-upcycle");
  upcycle.disabled = !picked || isGoal(picked);
  upcycle.textContent = !picked ? "Upcycle" : isGoal(picked) ? "Your goal" : "Upcycle to this";
  upcycle.title = picked ? "" : "Tap a picture first";
  if (view.edits.some((e) => BUSY.includes(e.status))) {
    whatIfTimer = setTimeout(async () => fillWhatIf(await api(`/api/items/${whatIfId}/what-if`)), 2500);
  }
}

async function upcycleTo(edit) {
  $("what-if-status").textContent = "Writing how to do it… about 10 seconds.";
  $("what-if-status").className = "status-note";
  try {
    await choosePlan("upcycle", edit.id);
    $("what-if").close();
  } catch (err) {
    $("what-if-status").textContent = err.message;
    $("what-if-status").className = "status-note failed";
  }
}

$("what-if-btn").onclick = () => openWhatIf(openId);
$("what-if-link").onclick = () => openWhatIf(openId);
$("what-if-close").onclick = () => $("what-if").close();
$("what-if").onclose = () => { clearTimeout(whatIfTimer); whatIfId = null; };
$("what-if-draw").onclick = () => {
  const text = $("what-if-text").value.trim();
  if (!text) return $("what-if-text").focus();
  $("what-if-text").value = "";
  askWhatIf("own", text);
};
// Enter in the box would submit the dialog's form and close it.
$("what-if-text").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); $("what-if-draw").click(); } };
$("what-if-all").onclick = async () => {
  const view = await api(`/api/items/${whatIfId}/what-if`);
  for (const o of view.options) {
    if (!view.edits.some((e) => e.option === o.key)) await askWhatIf(o.key);
  }
};
$("what-if-upcycle").onclick = () => upcycleTo(whatIfView.edits.find((e) => e.id === whatIfPick));
$("what-if-keep").onclick = async () => { await choosePlan("keep"); $("what-if").close(); };
$("what-if-sell").onclick = async () => {
  const id = whatIfId;
  await choosePlan("sell");
  $("what-if").close();
  openSell(id);
};

// Add from link: the server reads the shop's page; a shop that blocks it gets a plain message here.
$("link-btn").onclick = () => {
  $("link-status").textContent = "";
  $("link-url").value = "";
  $("link").showModal();
};
$("link-close").onclick = () => $("link").close();
$("link-form").onsubmit = async (e) => {
  e.preventDefault();
  $("link-add").disabled = true;
  $("link-status").className = "status-note";
  $("link-status").textContent = "Reading the shop's page…";
  try {
    const item = await api("/api/items/from-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("link-url").value }),
    });
    $("link").close();
    items.unshift(item);
    render();
    schedulePoll();
    openDetail(item.id);
    toast("Added from the shop. Working out the type…", 2500);
  } catch (err) {
    $("link-status").className = "status-note failed";
    $("link-status").textContent = err.message;
  } finally {
    $("link-add").disabled = false;
  }
};

// Outfit: one photo of someone wearing clothes becomes one item per garment.
$("outfit-input").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  toast("Looking for the clothes in your photo…");
  const body = new FormData();
  body.append("photo", file);
  try {
    const added = await api("/api/items/from-outfit", { method: "POST", body });
    items = [...added, ...items];
    render();
    schedulePoll();
    toast(`Found ${added.length === 1 ? "1 garment" : `${added.length} garments`}: ${added.map((i) => i.photos[0].part).join(", ")}. Tidying and reading the details…`, 4000);
  } catch (err) {
    toast(err.message, 5000);
  }
};

$("phone-btn").onclick = async () => {
  let link;
  try { link = await api("/api/phone"); } catch (err) { return toast(err.message, 4000); }
  const { url, qr_svg } = link;
  $("qr").innerHTML = qr_svg;
  $("phone-url").textContent = url;
  $("phone").showModal();
};

// The public copy: sign in with OpenRouter first. The personal copy has no sign-in.
function showSignIn() {
  clearTimeout(pollTimer);
  document.body.classList.add("signed-out");
  $("sign-in").hidden = false;
}

$("old-key-done").onclick = async () => {
  $("old-key").hidden = true;
  await api("/api/old-key/done", { method: "POST" }).catch(() => {});
};

$("sign-out-btn").onclick = async () => {
  await api("/api/sign-out", { method: "POST" }).catch(() => {});
  location.replace("/");
};

// The public copy's footer: privacy, the feedback button, and Delete my account.
function showFooter() {
  $("foot").hidden = false;
  // launch.js reads config.js, so they load in order (async = false keeps it).
  for (const src of ["/launch/config.js", "/launch/launch.js"]) {
    const script = document.createElement("script");
    script.src = src;
    script.async = false;
    document.body.append(script);
  }
}

$("delete-account-btn").onclick = () => {
  $("account-ask").hidden = false;
  $("account-done").hidden = true;
  $("account-status").textContent = "";
  $("account").showModal();
};
$("account-cancel").onclick = () => $("account").close();
$("account-delete").onclick = async () => {
  $("account-delete").disabled = true;
  $("account-status").textContent = "Deleting…";
  let done;
  try {
    done = await api("/api/account", { method: "DELETE" });
  } catch (err) {
    $("account-status").textContent = "Couldn't delete it: " + err.message;
    return;
  } finally {
    $("account-delete").disabled = false;
  }
  clearTimeout(pollTimer);
  items = [];  // they're gone: don't leave them showing behind the dialog
  render();
  const list = $("account-keys");
  list.replaceChildren();
  // OpenRouter's page for each key: the current one, and an older one not yet deleted.
  done.key_urls.forEach((url, i) => {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = i === 0 ? "Delete the Tidyrack key at OpenRouter" : "…and the older Tidyrack key";
    const li = document.createElement("li");
    li.append(a);
    list.append(li);
  });
  if (!done.key_urls.length) {  // the key couldn't be read: send them to the list of all their keys
    list.innerHTML = '<li><a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">Delete the Tidyrack key at OpenRouter</a></li>';
  }
  $("account-ask").hidden = true;
  $("account-done").hidden = false;
};
// Escape or Done: back to the sign-in screen.
$("account").onclose = () => { if (!$("account-done").hidden) location.replace("/"); };
$("account-finish").onclick = () => $("account").close();

// The server sends a code back from a failed sign-in; only these words are ever shown.
const SIGN_IN_ERRORS = {
  cancelled: "Sign-in was cancelled.",
  expired: "That sign-in had expired. Please try again.",
  refused: "OpenRouter didn't accept the sign-in. Please try again.",
  no_user: "OpenRouter didn't say who you are, so Tidyrack can't keep your items. Please try again.",
};

async function start() {
  const signInError = new URLSearchParams(location.search).get("sign_in_error");
  if (signInError) {
    history.replaceState(null, "", "/" + location.hash);
    toast(SIGN_IN_ERRORS[signInError] || SIGN_IN_ERRORS.refused, 6000);
  }
  const me = await api("/api/me");
  if (me.public) {
    $("phone-btn").hidden = true;
    $("sign-out-btn").hidden = false;
    showFooter();
  }
  if (!me.signed_in) {
    showSignIn();
    return false;
  }
  if (me.old_key_url) {
    $("old-key-link").href = me.old_key_url;
    $("old-key").hidden = false;
  }
  await load();
  return true;
}

loadView();
start()
  .then((loaded) => {
    if (!loaded) return;
    // #<id> opens an item; #<id>/sell also opens its Vinted listing.
    const [id, action] = location.hash.slice(1).split("/");
    if (id) openDetail(id);
    if (id && action === "sell" && openId === id) openSell(id);
  })
  .catch((err) => toast("Couldn't load Tidyrack:" + err.message));
