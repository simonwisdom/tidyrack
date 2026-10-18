"""The two model steps: cut the garment out of its background, and guess its details.

Background removal runs locally with rembg, so the garment itself is never
redrawn by a model. The details come from Codex on your own ChatGPT login (no
per-photo cost), or from a vision model through OpenRouter if
TIDYRACK_DETAILS=openrouter.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
DEFAULT_MODEL = "google/gemini-3.8-flash"
DEFAULT_TIDY_MODEL = "google/gemini-3.1-flash-image"  # the public copy's tidy-up, on the person's own key
CODEX = shutil.which("codex") or "/opt/homebrew/bin/codex"
DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_REMBG_MODEL = "birefnet-general-lite"
MAX_PIXELS = 50_000_000  # a 48-megapixel phone photo fits; tiny files that decode to gigabytes don't
UPLOAD_FORMATS = ["JPEG", "PNG", "WEBP"]  # what the public copy accepts


def env(name: str, default=None):
    """A TIDYRACK_<name> setting; the old WARDROBE_<name> still works."""
    return os.environ.get(f"TIDYRACK_{name}", os.environ.get(f"WARDROBE_{name}", default))

PROMPT = """You are cataloguing one item of clothing. The attached photos all show the same item:
views of it (front, back) and maybe close-ups of its labels. Read any brand, size or care label you can see.
Reply with JSON only, with these keys:
- "type": what the item is, in one or two plain words (e.g. "t-shirt", "jeans", "trainers", "wool coat").
- "colour": the main colour in plain words; add a second if it's clearly two-tone (e.g. "navy", "cream and black").
- "fabric": the material; copy the composition from a care label if one shows (e.g. "80% wool, 20% nylon"), otherwise always give your best guess (e.g. "cotton", "denim", "wool").
- "brand": the brand only if a logo, label or very distinctive design shows it; otherwise null.
- "size": the size exactly as a label shows it (e.g. "M", "UK 10", "32W 30L"); null if no size label is readable.
Use null for anything you can't judge from the photos. Don't guess a brand from style alone."""


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    raise RuntimeError("No OPENROUTER_API_KEY in the environment")


def is_image(data: bytes) -> bool:
    """Whether the bytes are a JPEG, PNG or WebP photo of a sensible size. Reads only
    the header for the size, so a tiny file that would decode to gigabytes is caught."""
    try:
        image = Image.open(io.BytesIO(data), formats=UPLOAD_FORMATS)
        if image.width * image.height > MAX_PIXELS:
            return False
        image.verify()
        return True
    except Exception:
        return False


def upright_jpeg(photo: bytes, max_side: int = 1600) -> bytes:
    """Rotate a phone photo the way it was held and shrink it to a sensible size."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
    image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def on_white(png: bytes, max_side: int = 1600) -> bytes:
    """A cutout on a white background, as a JPEG: how try-on models expect a garment."""
    cutout = Image.open(io.BytesIO(png)).convert("RGBA")
    cutout.thumbnail((max_side, max_side))
    white = Image.new("RGB", cutout.size, "white")
    white.paste(cutout, mask=cutout.split()[3])
    out = io.BytesIO()
    white.save(out, "JPEG", quality=90)
    return out.getvalue()


_sessions = {}
_session_lock = threading.Lock()  # one cutout at a time; the models are large


def _load_session(model: str | None = None):
    """Load a cutout model once (the usual one unless named). Call with _session_lock held."""
    import onnxruntime as ort
    from rembg import new_session  # slow import; only load when needed

    model = model or env("REMBG_MODEL", DEFAULT_REMBG_MODEL)
    if model not in _sessions:
        # onnxruntime's default memory pool keeps ~7 GB after one cutout, which
        # runs an 8 GB server out of memory on the next; without it ~1 GB stays.
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        _sessions[model] = new_session(model, sess_opts=opts)
    return _sessions[model]


def warm_up() -> None:
    """Load the cutout model ahead of the first photo."""
    with _session_lock:
        _load_session()


def remove_background(jpeg: bytes) -> bytes:
    """Return a PNG of the garment on a transparent background, trimmed to its edges."""
    from rembg import remove

    with _session_lock:
        cutout = remove(Image.open(io.BytesIO(jpeg)), session=_load_session())
    box = cutout.getbbox()
    if box:
        cutout = cutout.crop(box)
    out = io.BytesIO()
    cutout.save(out, "PNG")
    return out.getvalue()


# What the clothing model finds, in the order it gives its masks.
OUTFIT_PARTS = ("top", "bottoms", "full-length piece")
OUTFIT_MODEL = "u2net_cloth_seg"
MIN_PART_SHARE = 0.01  # a garment must cover this share of the photo; less is a stray patch
PATCH_SHARE = 0.05  # a patch of a garment's mask counts if it's this share of its biggest patch
CROP_MARGIN = 0.03  # room left around a garment's crop, as a share of the photo's sides


def main_patches(mask: Image.Image) -> Image.Image:
    """The garment's main patches of a clothing-model mask, without its specks.

    The model scatters specks of one garment over the others (a top's specks all
    down the trousers, say); kept, they stretch the crop over the whole outfit and
    a garment that's only specks counts as found. Patches at least PATCH_SHARE of
    the biggest stay, so a top split in two by hair keeps both halves.
    """
    # The mask is soft at its edges; a hard edge drops the faint outline of the next garment.
    hard = np.array(mask.convert("L")) > 128
    labels, count = ndimage.label(hard)
    if count == 0:
        return Image.fromarray(np.zeros_like(hard, dtype=np.uint8))
    sizes = ndimage.sum(hard, labels, range(1, count + 1))
    keep = np.flatnonzero(sizes >= PATCH_SHARE * sizes.max()) + 1
    return Image.fromarray((np.isin(labels, keep) * 255).astype(np.uint8))


def split_outfit(jpeg: bytes) -> list[dict]:
    """Find each garment in a photo of someone wearing clothes, with rembg's clothing
    model. For each: its part, a JPEG crop of the photo around it (real pixels, kept
    as its photo) and a PNG cutout of only the garment, without skin, phone or the
    other garments. Takes a few seconds."""
    photo = Image.open(io.BytesIO(jpeg)).convert("RGB")
    with _session_lock:
        masks = _load_session(OUTFIT_MODEL).predict(photo)
    parts = []
    for part, mask in zip(OUTFIT_PARTS, masks):
        mask = main_patches(mask)
        box = mask.getbbox()
        if box is None or np.count_nonzero(np.array(mask)) < MIN_PART_SHARE * photo.width * photo.height:
            continue
        mx, my = int(CROP_MARGIN * photo.width), int(CROP_MARGIN * photo.height)
        crop = photo.crop((max(0, box[0] - mx), max(0, box[1] - my),
                           min(photo.width, box[2] + mx), min(photo.height, box[3] + my)))
        cutout = photo.convert("RGBA")
        cutout.putalpha(mask)
        crop_out, cutout_out = io.BytesIO(), io.BytesIO()
        crop.save(crop_out, "JPEG", quality=90)
        cutout.crop(box).save(cutout_out, "PNG")
        parts.append({"part": part, "photo": crop_out.getvalue(), "cutout": cutout_out.getvalue()})
    return parts


def guess_details(jpegs: list[bytes], key: str | None = None, part: str | None = None) -> dict:
    """`key`: the signed-in person's OpenRouter key, in the public copy. `part`: for a
    crop of someone wearing an outfit, the garment to describe ("top", "bottoms"...)."""
    prompt = PROMPT + (WORN_PROMPT.format(part=part) if part else "")
    if key or env("DETAILS", "codex") == "openrouter":
        return guess_details_openrouter(jpegs, key, prompt)
    return guess_details_codex(jpegs, prompt)


# Added to PROMPT for a crop of an outfit photo, which shows bits of the other garments too.
WORN_PROMPT = ("\nThe photos show someone wearing an outfit. Describe only the {part} they are wearing, "
               "not the other clothes, jewellery or accessories in view.")


def guess_details_codex(jpegs: list[bytes], prompt: str = PROMPT) -> dict:
    """Ask Codex, signed in with ChatGPT, to read the photos. Takes about 5 seconds."""
    return parse_guesses(ask_codex(prompt, jpegs))


def ask_codex(prompt: str, jpegs: list[bytes]) -> str:
    """One text answer from Codex about the photos."""
    model = env("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    with tempfile.TemporaryDirectory() as tmp:
        reply = Path(tmp) / "reply.txt"
        images = []
        for n, jpeg in enumerate(jpegs, 1):
            photo = Path(tmp) / f"photo-{n}.jpg"
            photo.write_bytes(jpeg)
            images += ["-i", str(photo)]
        result = subprocess.run(
            [CODEX, "exec", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
             "-s", "read-only", "-m", model, "-c", "model_reasoning_effort=low",
             "-C", tmp, *images, "-o", str(reply), prompt],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not reply.is_file():
            raise RuntimeError(f"codex exec failed: {(result.stderr or result.stdout).strip()[-300:]}")
        return reply.read_text()


LISTING_PROMPT = """Write a Vinted UK listing for the item of clothing in the photos (all of the same item).
What the owner has recorded about it (fields marked "guess" haven't been checked yet):
{details}

Reply with JSON only, with these keys:
- "title": at most 60 characters: brand (if known), item and one key detail. No emojis, hashtags or capitals for emphasis.
- "description": 3 to 6 short lines in plain, friendly British English: what it is, colour, material, size and fit if known, and any wear you can see. Only state what the details or photo support, and never say where a detail came from. No hashtags.
- "category": the most specific Vinted category path, e.g. "Men > Clothing > Tops & t-shirts > T-shirts".
- "condition": exactly one of "New with tags", "New without tags", "Very good", "Good", "Satisfactory", judged from the photo.
- "parcel_size": exactly one of "Small", "Medium", "Large".
- "price_gbp": a realistic Vinted UK asking price, as a number.
- "price_reason": one short sentence on why that price."""

LISTING_KEYS = ["title", "description", "category", "condition", "parcel_size", "price_gbp", "price_reason"]


def write_listing(jpegs: list[bytes], details: dict, key: str | None = None) -> dict:
    """Draft a Vinted listing from the photos and the item's details. Takes about 10 seconds.
    With `key`, through OpenRouter on that key; otherwise Codex."""
    lines = "\n".join(
        f"- {field}: {value}{' (guess)' if field in details.get('unchecked', []) else ''}"
        for field, value in details.items()
        if field != "unchecked" and value not in (None, "")
    )
    prompt = LISTING_PROMPT.format(details=lines or "- nothing yet")
    data = json_object(ask_openrouter(prompt, jpegs, key) if key else ask_codex(prompt, jpegs))
    missing = [k for k in LISTING_KEYS if data.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Listing came back without {', '.join(missing)}")
    return {k: data[k] for k in LISTING_KEYS}


HOWTO_PROMPT = """The owner of this garment wants to upcycle it themselves at home, in the UK.
The first picture is the garment now; the second is a picture of the goal, drawn by AI.
The garment: {details}
The change: {change}

Reply with JSON only, with these keys:
- "materials": a short list of what to buy or have, each with a rough UK price if it costs anything (e.g. "Dylon machine dye, navy, about £8").
- "steps": 3 to 6 steps in plain British English, in order, each one sentence of at most 20 words. Include the one thing most likely to go wrong for this fabric and how to avoid it.
- "cost": the rough total in plain words, e.g. "about £10".
- "time": how long it takes in plain words, e.g. "an hour, plus a wash cycle".
If it's work for a tailor rather than at home, say so in the steps and give a tailor's rough price."""

HOWTO_KEYS = ["materials", "steps", "cost", "time"]


def write_howto(jpegs: list[bytes], details: str, change: str, key: str | None = None) -> dict:
    """Materials and steps for an upcycle, from the garment now and the goal picture.
    Takes about 10 seconds. With `key`, through OpenRouter on that key; otherwise Codex."""
    prompt = HOWTO_PROMPT.format(details=details or "not known", change=change)
    data = json_object(ask_openrouter(prompt, jpegs, key) if key else ask_codex(prompt, jpegs))
    missing = [k for k in HOWTO_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"The how-to came back without {', '.join(missing)}")
    return {k: data[k] for k in HOWTO_KEYS}


TIDY_PROMPT = (
    "Change only this, keep everything else identical: turn this into a clean catalogue photo of the garment: "
    "plain light grey background, no hanger or room, garment laid flat and centred, gently smoothed. "
    "Keep its exact colours, print, text, logos and labels. Don't add labels or details that aren't there."
)
# A crop from a photo of someone wearing an outfit shows bits of the other garments.
TIDY_WORN_PROMPT = (
    "This photo shows part of a person wearing clothes. Draw only the {part} they are wearing, "
    "as a clean catalogue photo: plain light grey background, no person, hands, phone or room, "
    "no other garments, laid flat and centred, gently smoothed. "
    "Keep its exact colours, print, text, logos and labels. Don't add labels or details that aren't there."
)
CODEX_IMAGES = Path.home() / ".codex" / "generated_images"


def tidy_up(jpeg: bytes, part: str | None = None, key: str | None = None) -> bytes:
    """Have Codex's image tool redraw the photo as a catalogue shot. Takes 30-40 seconds.
    With `part` ("top", "bottoms"...), the photo is a crop of someone wearing an
    outfit, and only that garment is drawn. With `key`, a Gemini image model through
    OpenRouter on that key does it instead.

    The result is a new picture, not the garment's real pixels: small text such as
    labels and sizes can come out wrong.
    """
    prompt = TIDY_WORN_PROMPT.format(part=part) if part else TIDY_PROMPT
    return redraw(jpeg, prompt, key)


def redraw(jpeg: bytes, prompt: str, key: str | None = None) -> bytes:
    """A new picture made from the photo by following the prompt, as a PNG: Codex's
    image tool (30-40 seconds), or with `key` a Gemini image model through OpenRouter."""
    if key:
        return tidy_up_openrouter(jpeg, prompt, key)
    return codex_image(prompt, [jpeg])


def codex_image(prompt: str, jpegs: list[bytes]) -> bytes:
    """One picture from Codex's image tool, drawn from the prompt and the photos. Returns a PNG."""
    model = env("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for n, jpeg in enumerate(jpegs, 1):
            photo = Path(tmp) / f"image-{n}.jpg"
            photo.write_bytes(jpeg)
            paths.append(str(photo))
        brief = (
            "Call your image generation tool exactly once, immediately, with these arguments and nothing else. "
            "Pass the prompt verbatim; do not rewrite it. Run no shell commands. Then reply 'done'.\n"
            f"prompt={json.dumps(prompt)}\n"
            "transparent_background=false\n"
            f"referenced_image_paths={json.dumps(paths)}"
        )
        result = subprocess.run(
            [CODEX, "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
             "-s", "read-only", "-m", model, "-c", "model_reasoning_effort=low", brief],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, cwd=tmp,
        )
    thread, said = None, []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        thread = thread or event.get("thread_id")
        item = event.get("item") or {}
        if item.get("type") in ("agent_message", "error"):
            said.append(item.get("text") or item.get("message") or "")
    pngs = sorted((CODEX_IMAGES / thread).glob("*.png")) if thread else []
    if not pngs:
        detail = " | ".join(said[-2:]) or result.stderr.strip()[-300:]
        raise RuntimeError(f"Codex made no image (exit {result.returncode}): {detail}")
    return pngs[-1].read_bytes()


def tidy_up_openrouter(jpeg: bytes, prompt: str, key: str) -> bytes:
    """The tidy-up through a Gemini image model on OpenRouter. Returns a PNG."""
    return openrouter_image(prompt, [jpeg], key, "The image model sent back no picture. Try Tidy again.")


def openrouter_image(prompt: str, jpegs: list[bytes], key: str, none_back: str) -> bytes:
    """One picture from a Gemini image model on OpenRouter, drawn from the prompt and the
    photos. `none_back`: what to tell the person if no picture comes back. Returns a PNG."""
    data = call_openrouter(OPENROUTER_IMAGES_URL, key, {
        "model": env("TIDY_MODEL", DEFAULT_TIDY_MODEL),
        "prompt": prompt,
        "input_references": [{"type": "image_url", "image_url": {"url": _data_url(j)}} for j in jpegs],
    }, timeout=180)
    images = [d["b64_json"] for d in data.get("data") or [] if d.get("b64_json")]
    if not images:
        raise ModelError(none_back)
    out = io.BytesIO()
    Image.open(io.BytesIO(base64.b64decode(images[0]))).save(out, "PNG")
    return out.getvalue()


TRY_ON_PROMPT = (
    "The first image is a photo of a person. The second image shows one garment: {what}. "
    "Edit the first photo so the person is wearing this exact garment, in place of what they wear there now. "
    "Change only that. Keep everything else identical: face, hair, hands, phone, pose, body, "
    "the other clothes, the room, the light and the framing. "
    "Keep the garment's exact colours, pattern, print, text, logos, labels, buttons and pockets, "
    "and let it fit and drape naturally on this body. Don't add details that aren't on it."
)


def try_on(selfie: bytes, garment: bytes, what: str, key: str | None = None) -> bytes:
    """Draw the garment onto the person in the selfie. `what`: the garment in plain words
    ("a brown corduroy jacket"). With `key`, a Gemini image model through OpenRouter on
    that key; otherwise Codex's image tool. Returns a PNG: a new picture, not real pixels."""
    prompt = TRY_ON_PROMPT.format(what=what)
    if key:
        return openrouter_image(prompt, [selfie, garment], key, "The image model sent back no picture. Try again.")
    return codex_image(prompt, [selfie, garment])


def guess_details_openrouter(jpegs: list[bytes], key: str | None = None, prompt: str = PROMPT) -> dict:
    return parse_guesses(ask_openrouter(prompt, jpegs, key or _api_key()))


def ask_openrouter(prompt: str, jpegs: list[bytes], key: str) -> str:
    """One JSON answer about the photos from a vision model on OpenRouter."""
    images = [{"type": "image_url", "image_url": {"url": _data_url(j)}} for j in jpegs]
    data = call_openrouter(OPENROUTER_URL, key, {
        "model": env("MODEL", DEFAULT_MODEL),
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *images]}],
    }, timeout=90)
    return data["choices"][0]["message"]["content"]


class ModelError(RuntimeError):
    """A model call that failed in a way the person can act on; the message says how."""


def call_openrouter(url: str, key: str, payload: dict, timeout: int) -> dict:
    response = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {}
    # Errors can also come back with status 200, in the body.
    status = response.status_code if response.status_code != 200 else (data.get("error") or {}).get("code", 200)
    if status == 401:
        raise ModelError("Your OpenRouter key no longer works (it may have been deleted). Sign out and sign in again.")
    if status == 402 or (status == 403 and "limit" in json.dumps(data).lower()):
        # OpenRouter's docs give 402 or 403 for running out of credit or hitting the key's limit.
        raise ModelError("Your OpenRouter account is out of credit, or this key hit its spending limit. "
                         "Top up at openrouter.ai/settings/credits, then try again.")
    if status != 200:
        message = (data.get("error") or {}).get("message") or response.text[:200]
        raise RuntimeError(f"OpenRouter said {status}: {message}")
    return data


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def json_object(text: str) -> dict:
    """Pull the JSON object out of the model's reply, tolerating a code fence."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"Model reply had no JSON: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def parse_guesses(text: str) -> dict:
    data = json_object(text)
    guesses = {}
    for field in ("type", "colour", "fabric", "brand", "size"):
        value = data.get(field)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in ("null", "unknown"):
            guesses[field] = value.strip()
    return guesses


# Outfits (tidyrack/looks.py): a picture drawn from several garments, and a JSON answer about a photo.

def draw(prompt: str, jpegs: list[bytes], key: str | None = None) -> bytes:
    """A new picture from the prompt, using the photos as references. Codex's image
    tool in the personal copy (30-60 seconds), a Gemini image model on the person's key
    in the public one. Returns a PNG."""
    if key:
        refs = [{"type": "image_url", "image_url": {"url": _data_url(j)}} for j in jpegs]
        data = call_openrouter(OPENROUTER_IMAGES_URL, key, {
            "model": env("TIDY_MODEL", DEFAULT_TIDY_MODEL), "prompt": prompt, "input_references": refs,
        }, timeout=180)
        images = [d["b64_json"] for d in data.get("data") or [] if d.get("b64_json")]
        if not images:
            raise ModelError("The image model sent back no picture. Try again.")
        out = io.BytesIO()
        Image.open(io.BytesIO(base64.b64decode(images[0]))).save(out, "PNG")
        return out.getvalue()
    model = env("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for n, jpeg in enumerate(jpegs, 1):
            path = Path(tmp) / f"ref-{n}.jpg"
            path.write_bytes(jpeg)
            paths.append(str(path))
        brief = (
            "Call your image generation tool exactly once, immediately, with these arguments and nothing else. "
            "Pass the prompt verbatim; do not rewrite it. Run no shell commands. Then reply 'done'.\n"
            f"prompt={json.dumps(prompt)}\n"
            "transparent_background=false\n"
            f"referenced_image_paths={json.dumps(paths)}"
        )
        result = subprocess.run(
            [CODEX, "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
             "-s", "read-only", "-m", model, "-c", "model_reasoning_effort=low", brief],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, cwd=tmp,
        )
    thread = next((e.get("thread_id") for e in map(_json_line, result.stdout.splitlines()) if e.get("thread_id")), None)
    pngs = sorted((CODEX_IMAGES / thread).glob("*.png")) if thread else []
    if not pngs:
        raise RuntimeError(f"Codex made no image (exit {result.returncode}): {result.stderr.strip()[-300:]}")
    return pngs[-1].read_bytes()


def _json_line(line: str) -> dict:
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def ask_json(prompt: str, jpegs: list[bytes], key: str | None = None) -> dict:
    """A JSON answer about the photos: Codex in the personal copy, OpenRouter on the person's key in the public one."""
    return json_object(ask_openrouter(prompt, jpegs, key) if key else ask_codex(prompt, jpegs))
