"""What if I…: the edits offered for a garment before choosing Keep, Upcycle or Sell.

Each edit is drawn by an image model from the garment's tidied picture. Which
edits are offered follows a few plain rules, not a model, so they're quick and
always the same: dye only goes darker (home dye can't lighten), bleach ruins wool
and silk and barely touches polyester, and only garments that have buttons get
new ones. The dye colours are ones that go with the rest of the wardrobe.

Colours are compared in OKLab, where equal distances look about equally
different (Björn Ottosson, 2020).
"""

import numpy as np

# Home dye colours, roughly as the dye packets show them.
DYES = {
    "navy": "#1f2a48", "charcoal": "#3a3b40", "chocolate brown": "#4a2f22", "black": "#1b1b1d",
    "olive": "#5a6332", "forest green": "#2f4f3b", "teal": "#1f5f63", "denim blue": "#3a5a8c",
    "burgundy": "#6a1f2f", "rust": "#97462a", "plum": "#563050", "mustard": "#b88a2a",
}
NEUTRAL_DYES = ("navy", "charcoal", "chocolate brown", "black")
NEUTRAL = 0.04  # OKLab chroma below this is a neutral: grey, black, white, beige-ish
DARKER = 0.06  # a dye must be at least this much darker (OKLab lightness) to show
SAME_COLOUR = 0.08  # closer than this (OKLab distance) and the dye changes little
ANALOGOUS, COMPLEMENT = 40, 30  # degrees of hue that count as going together

PLANT = ("cotton", "linen", "denim", "viscose", "rayon", "lyocell", "tencel", "hemp", "modal",
         "corduroy", "chambray", "canvas", "jersey", "flannel", "twill")
ANIMAL = ("wool", "silk", "cashmere", "merino", "mohair", "alpaca", "angora", "lambswool")
SYNTHETIC = ("polyester", "nylon", "acrylic", "polyamide", "elastane", "spandex", "acetate", "fleece")
NO_DYE = ("leather", "suede", "pvc", "faux", "sequin", "down")

BOTTOMS = ("trousers", "jeans", "chinos", "joggers", "leggings", "cargo", "pants")
SKIRTS = ("skirt", "dress")
JACKETS = ("blazer", "jacket", "coat", "parka", "overcoat")
KNITS = ("jumper", "sweater", "cardigan", "knit")
TOPS = ("t-shirt", "tee", "tank", "vest", "top", "shirt", "blouse", "polo", "sweatshirt", "hoodie", "jumper",
        "sweater", "cardigan")
JERSEY = ("t-shirt", "tee", "tank", "vest", "sweatshirt", "hoodie")
BUTTONED = ("shirt", "blouse", "blazer", "jacket", "coat", "cardigan", "waistcoat", "overshirt")
NOT_BUTTONED = ("t-shirt", "tee", "sweatshirt", "polo shirt")

BASE = ("Edit this photo of a garment. Change only this, keep everything else identical: the same garment, "
        "its shape, fold, creases, seams, fabric texture, pockets, label and the plain background. ")
FREE_TEXT_MAX = 200


def oklab(hex_code: str) -> np.ndarray:
    """OKLab (lightness 0-1, a, b) of a #rrggbb colour."""
    c = np.array([int(hex_code[i:i + 2], 16) for i in (1, 3, 5)]) / 255.0
    rgb = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    lms = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                    [0.2119034982, 0.6806995451, 0.1073969566],
                    [0.0883024619, 0.2817188376, 0.6299787005]]) @ rgb
    return np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                     [1.9779984951, -2.4285922050, 0.4505937099],
                     [0.0259040371, 0.7827717662, -0.8086757660]]) @ np.cbrt(lms)


def oklab_to_hex(lab: np.ndarray) -> str:
    lms = np.array([[1.0, 0.3963377774, 0.2158037573],
                    [1.0, -0.1055613458, -0.0638541728],
                    [1.0, -0.0894841775, -1.2914855480]]) @ lab
    rgb = np.array([[4.0767416621, -3.3077115913, 0.2309699292],
                    [-1.2684380046, 2.6097574011, -0.3413193965],
                    [-0.0041960863, -0.7034186147, 1.7076147010]]) @ (lms ** 3)
    rgb = np.clip(rgb, 0, 1)
    c = np.where(rgb > 0.0031308, 1.055 * rgb ** (1 / 2.4) - 0.055, 12.92 * rgb)
    return "#" + "".join(f"{round(v * 255):02x}" for v in c)


def delta_e(hex_a: str, hex_b: str) -> float:
    """Distance in OKLab, times 100 so it reads like the usual ΔE: about 2 is just noticeable."""
    return float(np.linalg.norm(oklab(hex_a) - oklab(hex_b)) * 100)


def _chroma(lab) -> float:
    return float(np.hypot(lab[1], lab[2]))


def _hue(lab) -> float:
    return float(np.degrees(np.arctan2(lab[2], lab[1])) % 360)


def goes_with(hex_a: str, hex_b: str) -> bool:
    """Plain colour-wheel pairing: a neutral goes with anything; two colours go together
    when their hues are close (analogous) or opposite (complementary)."""
    a, b = oklab(hex_a), oklab(hex_b)
    if _chroma(a) < NEUTRAL or _chroma(b) < NEUTRAL:
        return True
    gap = abs(_hue(a) - _hue(b)) % 360
    gap = min(gap, 360 - gap)
    return gap <= ANALOGOUS or abs(gap - 180) <= COMPLEMENT


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def fibre(fabric: str | None) -> str:
    """plant | animal | synthetic | none (leather and the like) | unknown, by the biggest share."""
    best, share = (fabric or "").lower(), -1.0
    for part in (fabric or "").lower().replace(";", ",").split(","):
        digits = "".join(ch for ch in part if ch.isdigit() or ch == ".")
        n = float(digits) if digits.replace(".", "", 1).isdigit() else 0.0
        if n > share:
            best, share = part, n
    for name, words in (("none", NO_DYE), ("animal", ANIMAL), ("synthetic", SYNTHETIC), ("plant", PLANT)):
        if _has(best, words):
            return name
    return "unknown"


def dye_colours(garment_hex: str, wardrobe: list[str], count: int = 3) -> list[str]:
    """Dye colours for the garment: darker than it now, clearly different, and going
    with as many of the other clothes as possible. One dark neutral, then colours."""
    now = oklab(garment_hex)
    usable = [name for name, hex_code in DYES.items()
              if oklab(hex_code)[0] <= now[0] - DARKER and delta_e(hex_code, garment_hex) >= SAME_COLOUR * 100]
    neutrals = [n for n in NEUTRAL_DYES if n in usable][:1]
    # Neutrals go with everything, so they don't tell colours apart: score against the coloured clothes.
    coloured = [h for h in wardrobe if _chroma(oklab(h)) >= NEUTRAL]
    others = sorted((n for n in usable if n not in NEUTRAL_DYES),
                    key=lambda n: -sum(goes_with(DYES[n], h) for h in coloured))
    return (neutrals + others)[:count]


def options(item: dict, wardrobe: list[str]) -> list[dict]:
    """The edits offered for an item. `wardrobe`: the colours of the owner's other clothes.
    Each has a key, a label, a cost and effort in plain words, the prompt for the image
    model, and for dye and fade the colour aimed at."""
    kind = (item.get("type") or "").lower()
    fib = fibre(item.get("fabric"))
    now = item.get("colour_hex")
    offered = []

    if now and fib != "none":
        how = {"animal": ("Acid dye on the hob, about £10", "An afternoon; keep it below a simmer or it shrinks"),
               "synthetic": ("Synthetic-fibre dye, about £10", "An afternoon on the hob; the colour may come out paler")
               }.get(fib, ("Machine dye, about £8", "Easy: an hour and a wash cycle"))
        for name in dye_colours(now, wardrobe):
            offered.append({
                "key": "dye-" + name.replace(" ", "-"), "label": f"Dyed {name}", "cost": how[0], "effort": how[1],
                "target_hex": DYES[name],
                "prompt": BASE + f"Dye the whole garment {name} ({DYES[name]}), as home fabric dye would: the fabric "
                          "takes the new colour evenly and its texture still shows. Buttons, label and stitching "
                          "keep their colour."})

    lab = oklab(now) if now else None
    if now and fib in ("plant", "unknown") and lab[0] < 0.8 and _chroma(lab) >= NEUTRAL / 2:
        faded = oklab_to_hex(np.array([min(lab[0] + 0.15, 0.92), lab[1] * 0.55, lab[2] * 0.55]))
        offered.append({
            "key": "fade", "label": "Faded", "cost": "Washing soda or diluted bleach, about £3",
            "effort": "A soak and a few washes; test a hidden spot first", "target_hex": faded,
            "prompt": BASE + f"Make it look faded and washed out, as after a bleach soak and many washes: lighter "
                      f"and duller, close to {faded}, a little uneven and paler along seams and edges."})

    crop = None
    if _has(kind, ("shorts", "shoe", "trainer", "boot", "bag", "hat", "scarf", "sock", "belt")):
        crop = None
    elif _has(kind, BOTTOMS):
        crop = ("Cut into shorts", "Tailor about £15, or scissors and a hem at home",
                "An hour at home" if "jean" in kind or "denim" in (item.get("fabric") or "").lower()
                else "Some sewing", "Cut the legs off into shorts ending mid-thigh, with a neat hem.")
    elif _has(kind, SKIRTS):
        crop = ("Shortened", "Tailor about £15", "A tailor's job, or careful sewing",
                "Shorten the hem to just above the knee, with a neat hem.")
    elif _has(kind, JACKETS):
        crop = ("Sleeves shortened", "Tailor about £25", "A tailor's job",
                "Shorten the sleeves to just below the elbow, with neat cuffs.")
    elif _has(kind, KNITS):
        crop = ("Cropped", "Tailor about £20", "A tailor's job: cut knits unravel",
                "Crop the body to just above the waist, finished with the same ribbed hem.")
    elif _has(kind, JERSEY):  # jersey doesn't fray, so a raw cut edge is fine
        crop = ("Cropped", "Free with scissors", "Ten minutes",
                "Crop the body to just above the waist, with a clean raw edge.")
    elif _has(kind, TOPS):
        crop = ("Cropped", "Tailor about £10", "A tailor's job, or a simple hem at home",
                "Crop the body to just above the waist, with a neat hem.")
    if crop:
        offered.append({"key": "crop", "label": crop[0], "cost": crop[1], "effort": crop[2],
                        "prompt": BASE + crop[3]})

    if _has(kind, BUTTONED) and not _has(kind, NOT_BUTTONED):
        dark = lab is not None and lab[0] < 0.55
        buttons = "brass" if dark else "dark horn"
        offered.append({
            "key": "buttons", "label": f"{buttons.capitalize()} buttons", "cost": "Buttons about £6",
            "effort": "Half an hour with a needle",
            "prompt": BASE + f"Replace every button with {buttons} buttons of the same size; nothing else changes."})
    return offered


def source(item: dict) -> str | None:
    """The picture edits start from: the first tidied view, else a shop photo or the first view as taken."""
    views = [p for p in item["photos"] if p["kind"] != "label"]
    tidied = [p["tidy"] for p in views if p["tidy"]]
    return (tidied or [p["original"] for p in views] or [None])[0]


def own_option(text: str) -> dict:
    """The person's own "what if", in their words."""
    text = " ".join(text.split())[:FREE_TEXT_MAX]
    return {"key": "own", "label": text, "cost": "Shown when you pick it", "effort": "",
            "prompt": BASE + text}
