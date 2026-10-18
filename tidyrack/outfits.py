"""How well a top, a bottom, an optional layer and shoes go together, as a score with its reasons.

Each garment is described from what Tidyrack already knows: its type and fabric
(words the model read) and its measured colours (tidyrack/colour.py). Four
things are scored, each in plain rules a stylist would recognise:

- colour: all neutrals, or one strong colour with neutrals, go; two strong
  colours go if their hues are close (analogous) or opposite (complementary);
  light against dark helps.
- formality: a hoodie with tailored trousers is a stretch.
- weight: a heavy coat over a linen vest is odd.
- season: shorts with a wool jumper pull two ways.

The weights were fitted on Polyvore Outfits' training outfits (real outfits
against ones with a piece swapped at random); the check is tests/outfit_check.py.
"""

import re

import numpy as np

from tidyrack import colour

SLOTS = ("top", "bottom", "layer", "full", "shoes")
# Words that say what a garment is: (slot, formality 1 casual to 5 formal, warmth 1 summer to 5 winter).
TYPES = [
    # Shoes first, so "high-top trainers" and "dress shoes" are shoes; "oxfords" needs its s, so an oxford shirt is a top.
    (r"sandals?|flip ?flops?|sliders|slides|espadrilles", "shoes", 1.5, 1.0),
    (r"trainers?|sneakers?|plimsolls?|running shoes|canvas shoes", "shoes", 1.5, 2.5),
    (r"chelsea|desert boots?|ankle boots?", "shoes", 3.0, 3.5),
    (r"\bboots?\b", "shoes", 2.5, 4.0),
    (r"loafers?|brogues?|oxfords|derbys?|monk ?straps?|dress shoes|heels|court shoes|pumps", "shoes", 4.0, 2.5),
    (r"mules?|ballet flats|\bflats\b|\bshoes?\b", "shoes", 2.5, 2.5),
    (r"hoodie|hoody", "top", 1.0, 3.5),
    (r"sweatshirt|crew ?neck sweat", "top", 1.5, 3.5),
    (r"tank|vest top|camisole|cami\b|crop top|bralette|bandeau|tube top", "top", 1.5, 1.0),
    (r"t-?shirt|\btee\b|graphic", "top", 1.5, 2.0),
    (r"polo", "top", 2.5, 2.0),
    (r"jumper|sweater|pullover|knit|turtleneck|roll ?neck", "top", 2.5, 4.0),
    (r"blouse", "top", 3.5, 2.0),
    (r"shirt", "top", 3.0, 2.5),
    (r"bodysuit|top\b|tunic", "top", 2.5, 2.0),
    (r"joggers?|sweatpants|track ?pants|leggings", "bottom", 1.0, 3.0),
    (r"shorts", "bottom", 1.5, 1.0),
    (r"jeans|denim", "bottom", 2.0, 3.0),
    (r"chinos?|cargo", "bottom", 2.5, 3.0),
    (r"skirt", "bottom", 3.0, 2.0),
    (r"suit trousers|tailored|pleated trousers|dress pants", "bottom", 4.5, 3.0),
    (r"trousers|pants|slacks", "bottom", 3.5, 3.0),
    (r"puffer|parka|down jacket|padded", "layer", 1.5, 5.0),
    (r"coat|trench|overcoat|peacoat", "layer", 3.5, 4.5),
    (r"blazer|suit jacket|sport ?coat", "layer", 4.5, 3.0),
    (r"cardigan", "layer", 2.5, 3.5),
    (r"gilet|body ?warmer", "layer", 2.0, 3.5),
    (r"overshirt|shacket|shirt jacket", "layer", 2.5, 3.0),
    (r"jacket|bomber|anorak|windbreaker", "layer", 2.5, 3.5),
    (r"jumpsuit|playsuit|romper|dungarees|overalls", "full", 2.0, 2.0),
    (r"dress|gown", "full", 3.5, 2.0),
]
# Fabric words: (formality shift, weight 1 light to 3 heavy, warmth shift).
FABRICS = [
    (r"linen", 0.0, 1.0, -1.5),
    (r"silk|satin|chiffon", 1.0, 1.0, -0.5),
    (r"jersey", -0.5, 1.5, 0.0),
    (r"cotton|poplin|oxford", 0.0, 1.5, 0.0),
    (r"viscose|rayon|polyester", 0.0, 1.5, 0.0),
    (r"denim", -0.5, 2.5, 0.5),
    (r"corduroy|cord\b", 0.0, 2.5, 1.0),
    (r"fleece|sherpa|teddy", -1.0, 2.5, 1.5),
    (r"leather|suede", 0.5, 2.5, 1.0),
    (r"cashmere|merino|wool|tweed|flannel", 0.5, 2.5, 0.5),
    (r"down|quilted|padded", -0.5, 3.0, 2.0),
]
NEUTRAL_CHROMA = 0.045  # below this a colour is a neutral: black, white, grey, and greyish browns
EARTH_CHROMA = 0.08  # beige, camel, khaki and brown count as neutrals up to this
DENIM_CHROMA = 0.10  # so does a dull blue (denim, navy)
ANALOGOUS = 40  # degrees: two strong hues this close sit together
COMPLEMENTARY = (150, 210)  # degrees: two strong hues this far apart balance each other
ECHO = 10  # Delta E: a colour this close to one on another piece is the same colour, repeated

# Score = sum of weight x feature. Started from the weights fitted on Polyvore's training
# outfits (tests/outfit_check.py), which like tonal, equally muted outfits; rounded, with
# a clash and a season mix still counting against, as a stylist would have it.
WEIGHTS = {
    "all_neutral": 0.0,
    "one_strong": 0.1,
    "strong_pair_go": 0.4,
    "strong_clash": -0.4,
    "light_dark": -0.3,
    "same_shade": -0.2,
    "echo": 0.4,
    "muted_match": 0.45,
    "formality_gap": -0.35,
    "weight_order": -0.1,
    "season_gap": -0.2,
}


def describe(type_text: str | None, fabric_text: str | None, colours: list[dict] | None,
             slot: str | None = None) -> dict | None:
    """What the scorer needs to know about one garment. None if it isn't a top,
    bottom, layer, full-length piece or shoes (bags, hats...)."""
    words = (type_text or "").lower()
    fabric = (fabric_text or "").lower()
    found = next((t for t in TYPES if re.search(t[0], words)), None)
    if found is None and slot is None:
        return None
    _, kind, formality, warmth = found or (None, slot, 2.5, 2.5)
    weight = 1.5 if kind in ("top", "full") else 2.0
    for pattern, shift, heavy, warm in FABRICS:
        if re.search(pattern, fabric) or re.search(pattern, words):
            formality, weight, warmth = formality + shift, heavy, warmth + warm
            break
    if kind == "layer":
        weight = max(weight, 2.0)
    lab = [(colour.hex_to_oklab(c["hex"]), c["share"]) for c in (colours or [])]
    return {"slot": slot or kind, "formality": formality, "weight": weight, "warmth": warmth, "colours": lab}


def from_item(item: dict) -> dict | None:
    """describe() for a Tidyrack item: its measured colours, or its list colour if not measured yet."""
    measured = (item.get("colours") or {}).get("colours")
    if not measured and item.get("colour_hex"):
        measured = [{"hex": item["colour_hex"], "share": 1.0}]
    return describe(item.get("type"), item.get("fabric"), measured)


def is_neutral(lab: np.ndarray) -> bool:
    chroma = float(np.hypot(lab[1], lab[2]))
    hue = float(np.degrees(np.arctan2(lab[2], lab[1]))) % 360
    if chroma < NEUTRAL_CHROMA:
        return True
    if 40 <= hue <= 110 and chroma < EARTH_CHROMA and lab[0] < 0.85:  # beige, camel, khaki, brown
        return True
    return 230 <= hue <= 280 and chroma < DENIM_CHROMA  # denim, navy


def hue_gap(a: np.ndarray, b: np.ndarray) -> float:
    apart = abs(np.degrees(np.arctan2(a[2], a[1]) - np.arctan2(b[2], b[1]))) % 360
    return min(apart, 360 - apart)


def features(pieces: list[dict]) -> dict:
    """The outfit's features, each 0 to 1 (a gap is in its own units), from its pieces' descriptions."""
    mains = [p["colours"][0][0] for p in pieces if p["colours"]]
    strong = [c for c in mains if not is_neutral(c)]
    f = dict.fromkeys(WEIGHTS, 0.0)
    if mains:
        f["all_neutral"] = float(not strong)
        f["one_strong"] = float(len(strong) == 1)
        if len(strong) >= 2:
            gaps = [hue_gap(a, b) for i, a in enumerate(strong) for b in strong[i + 1:]]
            go = all(g <= ANALOGOUS or COMPLEMENTARY[0] <= g <= COMPLEMENTARY[1] for g in gaps)
            f["strong_pair_go" if go else "strong_clash"] = 1.0
        lightness = [c[0] for c in mains]
        f["light_dark"] = float(min(1.0, (max(lightness) - min(lightness)) / 0.4))
        # Two pieces nearly the same shade but not quite: it looks like a near-miss.
        near = [colour.delta_e(a, b) for i, a in enumerate(mains) for b in mains[i + 1:]]
        f["same_shade"] = float(any(4 < d < 12 for d in near))
        # A colour repeated from one piece to another, a print's blue picked up by the skirt, ties them together.
        f["echo"] = max((min(sa, sb) / 0.3 for i, p in enumerate(pieces) for q in pieces[i + 1:]
                         for a, sa in p["colours"] for b, sb in q["colours"]
                         if not is_neutral(a) and colour.delta_e(a, b) < ECHO), default=0.0)
        f["echo"] = min(1.0, f["echo"])
        # Muted with muted, bright with bright.
        chroma = [float(np.hypot(c[1], c[2])) for c in mains]
        f["muted_match"] = 1.0 - min(1.0, (max(chroma) - min(chroma)) / 0.15)
    formality = [p["formality"] for p in pieces]
    f["formality_gap"] = max(0.0, max(formality) - min(formality) - 1.0)
    warmth = [p["warmth"] for p in pieces]
    f["season_gap"] = max(0.0, max(warmth) - min(warmth) - 1.5)
    layer = [p for p in pieces if p["slot"] == "layer"]
    under = [p for p in pieces if p["slot"] in ("top", "full")]
    if layer and under:  # a layer lighter than what's under it
        f["weight_order"] = max(0.0, max(p["weight"] for p in under) - layer[0]["weight"])
    return f


REASONS = {
    "all_neutral": "all neutrals",
    "one_strong": "one strong colour with neutrals",
    "strong_pair_go": "the colours sit well together",
    "strong_clash": "the colours clash",
    "light_dark": "light against dark",
    "same_shade": "two nearly-matching shades",
    "echo": "a colour picked up from one piece to another",
    "muted_match": "the colours are equally muted or bright",
    "formality_gap": "dressy and casual mixed",
    "weight_order": "the layer is lighter than what's under it",
    "season_gap": "summer and winter pieces together",
}


# How a feature reads when it counts against an outfit, where REASONS wouldn't.
AGAINST = {"light_dark": "a strong light-dark contrast"}


def score(pieces: list[dict]) -> tuple[float, list[str]]:
    """The outfit's score (higher goes better) and its reasons: what works, strongest
    first, then what counts against it ("but ...")."""
    f = features(pieces)
    parts = {k: WEIGHTS[k] * v for k, v in f.items()}
    order = sorted(parts.items(), key=lambda kv: -kv[1])
    reasons = [REASONS[k] for k, v in order if v >= 0.1]
    reasons += ["but " + AGAINST.get(k, REASONS[k]) for k, v in reversed(order) if v <= -0.1]
    return float(sum(parts.values())), reasons
