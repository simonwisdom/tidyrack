"""An item's main colours, measured from the pixels of its photo, not guessed by a model.

Only the garment's own pixels count: the cutout's alpha channel is the mask, and
the cutout's colours are the original photo's (rembg only removes pixels). They
are grouped by colour (k-means in OKLab, where distance roughly matches how
different two colours look), light and shadow of one colour are merged, and the
two or three biggest groups are the garment's colours, each with its share.

A phone photo carries the room's light: a warm lamp turns navy charcoal, shade
turns cream blue. The light is read from the photo around the garment (a wall, a
bed, a floor): its colour cast is estimated (shades of grey, the brightest
pixels counting most) and taken out of the garment's pixels before they're
grouped. Room lights lie near the line of a glowing body's colours, from candle
orange to sky blue; the part of the cast off that line is a coloured wall, not
the light, so it's dropped. The brightness is left alone: a dark wall in good
light looks like a light wall in a dim room, and the photo can't tell them apart.

The check, with numbers, is in tests/colour_check.py.
"""

import io

import numpy as np
from PIL import Image
from scipy import ndimage, signal

SIDE = 96  # downsample the garment to this many pixels a side; plenty for its colours
SOLID = 200  # alpha above this counts as garment, not background or edge
CLEAR = 10  # alpha below this is background
K = 6
SAME = 0.12  # groups closer than this (OKLab) are one colour in light and shadow
SAME_HUE = 20  # so are two coloured groups whose hues are fewer degrees apart than this
COLOURED = 0.03  # a group is coloured, not grey, above this chroma (OKLab)
MAX_COLOURS = 3
MIN_SHARE = 0.08  # a colour needs this share of the garment to be listed
TWO_TONE_SHARE = 0.30  # colour_hex_2 (the list and filters) needs this share
TWO_TONE_DELTA = 0.15  # and must be this different from the first (OKLab)
GAP = 0.04  # keep the room's pixels this share of the photo away from the garment's edge
MIN_ROOM = 0.10  # without this share of the photo around the garment, the light isn't read
P_NORM = 6  # shades of grey: 1 is the plain average, higher leans on the brightest pixels
ON_LINE_ONLY = True  # drop the part of the cast off the glowing-body line
STRENGTH = 0.7  # how much of the cast to take out: a warm floor looks like warm light, so not all of it
WARMEST, COOLEST = 2000, 12000  # kelvin: the light is taken to be in this range

# Linear sRGB to and from CIE XYZ (D65).
_TO_XYZ = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
_FROM_XYZ = np.linalg.inv(_TO_XYZ)


def linear(rgb: np.ndarray) -> np.ndarray:
    """0-255 sRGB to linear light, 0-1."""
    c = rgb / 255.0
    return np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)


def encoded(lin: np.ndarray) -> np.ndarray:
    """Linear light, 0-1, to 0-255 sRGB."""
    lin = np.clip(lin, 0, 1)
    return 255 * np.where(lin > 0.0031308, 1.055 * lin ** (1 / 2.4) - 0.055, 12.92 * lin)


def linear_to_oklab(lin: np.ndarray) -> np.ndarray:
    """Linear sRGB rows to OKLab rows (Björn Ottosson's matrices)."""
    lms = lin @ np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                          [0.2119034982, 0.6806995451, 0.1073969566],
                          [0.0883024619, 0.2817188376, 0.6299787005]]).T
    lms = np.cbrt(lms)
    return lms @ np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                           [1.9779984951, -2.4285922050, 0.4505937099],
                           [0.0259040371, 0.7827717662, -0.8086757660]]).T


def oklab_to_linear(lab: np.ndarray) -> np.ndarray:
    lms = lab @ np.array([[1.0, 0.3963377774, 0.2158037573],
                          [1.0, -0.1055613458, -0.0638541728],
                          [1.0, -0.0894841775, -1.2914855480]]).T
    return lms ** 3 @ np.array([[4.0767416621, -3.3077115913, 0.2309699292],
                                [-1.2684380046, 2.6097574011, -0.3413193965],
                                [-0.0041960863, -0.7034186147, 1.7076147010]]).T


def rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """0-255 sRGB rows to OKLab rows."""
    return linear_to_oklab(linear(np.asarray(rgb, dtype=float)))


def oklab_to_hex(lab: np.ndarray) -> str:
    rgb = encoded(oklab_to_linear(np.asarray(lab, dtype=float)[None]))[0]
    return "#" + "".join(f"{round(v):02x}" for v in rgb)


def hex_to_oklab(hex_code: str) -> np.ndarray:
    return rgb_to_oklab(np.array([[int(hex_code[i:i + 2], 16) for i in (1, 3, 5)]]))[0]


def delta_e(a: np.ndarray, b: np.ndarray) -> float:
    """Distance in OKLab, times 100 so it reads like the usual Delta E: around 2
    is just noticeable, over 20 is a different colour."""
    return float(100 * np.linalg.norm(np.asarray(a) - np.asarray(b)))


def _blackbody_xy(kelvin: float) -> tuple[float, float]:
    """The colour of a glowing body at this temperature, as CIE xy (Kim et al.'s fit)."""
    t = kelvin
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def light_rgb(kelvin: float) -> np.ndarray:
    """A light of this temperature as linear sRGB, scaled so green is 1."""
    x, y = _blackbody_xy(kelvin)
    rgb = _FROM_XYZ @ np.array([x / y, 1.0, (1 - x - y) / y])
    return rgb / rgb[1]


_KELVINS = np.geomspace(WARMEST, COOLEST, 200)
# Each light's colour as (log red/green, log blue/green): a light's place on the glowing-body line.
_LINE = np.log(np.array([light_rgb(k) for k in _KELVINS])[:, [0, 2]])
_D65 = np.log(light_rgb(6504))[[0, 2]]


def place(rgb: np.ndarray) -> np.ndarray:
    return np.log(np.maximum(rgb[[0, 2]], 1e-6) / max(rgb[1], 1e-6))


def nearest_light(rgb: np.ndarray) -> tuple[float, np.ndarray]:
    """The room light on the glowing-body line nearest this cast: kelvin and linear sRGB."""
    i = int(np.argmin(np.sum((_LINE - place(rgb)) ** 2, axis=1)))
    return float(_KELVINS[i]), light_rgb(_KELVINS[i])


def estimate_light(room: np.ndarray) -> np.ndarray | None:
    """The room light's colour, as linear sRGB with green 1, from the linear pixels
    around the garment. None if there aren't enough of them to tell."""
    usable = room[(room.max(axis=1) < 0.98) & (room.min(axis=1) > 0.01)]  # clipped or black pixels lost the cast
    if len(usable) < 50:
        return None
    cast = np.mean(usable ** P_NORM, axis=0) ** (1 / P_NORM)
    cast = cast / cast[1]
    if ON_LINE_ONLY:
        cast = nearest_light(cast)[1]
    return cast


def correct(lin: np.ndarray, light: np.ndarray | None) -> np.ndarray:
    """Linear garment pixels as they'd look in daylight (D65), keeping their brightness."""
    if light is None:
        return lin
    light = light_rgb(6504) * (light / light_rgb(6504)) ** STRENGTH
    gains = light_rgb(6504) / light
    gains = gains / (_TO_XYZ[1] @ (gains * light)) * (_TO_XYZ[1] @ light)  # same brightness for the light itself
    return lin * gains


def locate(photo: Image.Image, cutout: Image.Image) -> tuple[int, int] | None:
    """Where the cutout's top-left corner sits in the photo it was cut from, or None
    if it doesn't fit. The cutout is the photo's own pixels, trimmed to the garment."""
    if cutout.size == photo.size:
        return 0, 0
    if cutout.width > photo.width or cutout.height > photo.height:
        return None
    step = max(1, max(photo.size) // 300)  # find it roughly on small copies, then exactly
    small_p = np.asarray(photo.convert("L").reduce(step), dtype=float)
    small_c = cutout.reduce(step)
    grey = np.asarray(small_c.convert("L"), dtype=float)
    mask = (np.asarray(small_c.getchannel("A")) > SOLID).astype(float)
    if mask.sum() == 0:
        return None
    # Sum over the mask of (photo - cutout)^2, for every offset, by correlation.
    cost = (signal.correlate(small_p**2, mask, mode="valid")
            - 2 * signal.correlate(small_p, mask * grey, mode="valid"))
    oy, ox = np.unravel_index(np.argmin(cost), cost.shape)
    best, where = None, None
    full_p = np.asarray(photo.convert("L"), dtype=float)
    full_c = np.asarray(cutout.convert("L"), dtype=float)
    full_m = np.asarray(cutout.getchannel("A")) > SOLID
    for y in range(oy * step - step, oy * step + step + 1):
        for x in range(ox * step - step, ox * step + step + 1):
            if 0 <= x <= photo.width - cutout.width and 0 <= y <= photo.height - cutout.height:
                window = full_p[y:y + cutout.height, x:x + cutout.width]
                diff = float(np.mean(np.abs(window[full_m] - full_c[full_m])))
                if best is None or diff < best:
                    best, where = diff, (x, y)
    return where if best is not None and best < 12 else None  # JPEG noise is a few levels


def room_pixels(photo: Image.Image, cutout: Image.Image) -> np.ndarray | None:
    """Linear pixels of the photo around the garment, away from its edge, or None
    if the cutout can't be placed in the photo or there's too little room around it."""
    corner = locate(photo, cutout)
    if corner is None:
        return None
    garment = np.zeros((photo.height, photo.width), bool)
    x, y = corner
    garment[y:y + cutout.height, x:x + cutout.width] = np.asarray(cutout.getchannel("A")) > CLEAR
    scale = max(1, max(photo.size) // 400)
    small = garment[::scale, ::scale]
    near = ndimage.binary_dilation(small, iterations=max(1, int(GAP * max(small.shape))))
    room = np.asarray(photo.convert("RGB"), dtype=float)[::scale, ::scale][~near]
    if len(room) < MIN_ROOM * small.size:
        return None
    return linear(room)


def _same_colour(a: np.ndarray, b: np.ndarray) -> bool:
    """One colour in light and shadow? Shadow darkens a colour but keeps its hue,
    so coloured groups are compared by hue; greys by plain distance."""
    if min(np.hypot(a[1], a[2]), np.hypot(b[1], b[2])) > COLOURED:
        apart = abs(np.degrees(np.arctan2(a[2], a[1]) - np.arctan2(b[2], b[1]))) % 360
        return min(apart, 360 - apart) < SAME_HUE
    return np.linalg.norm(a - b) < SAME


def _kmeans(points: np.ndarray, k: int, rounds: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """Centres and each point's group. Seeded, so the same photo always gives the same answer."""
    rng = np.random.default_rng(0)
    centres = [points[rng.integers(len(points))]]
    while len(centres) < k:  # k-means++: start each centre far from the others
        far = np.min([np.sum((points - c) ** 2, axis=1) for c in centres], axis=0)
        if far.sum() == 0:
            break
        centres.append(points[rng.choice(len(points), p=far / far.sum())])
    centres = np.array(centres)
    for _ in range(rounds):
        groups = np.argmin(((points[:, None] - centres[None]) ** 2).sum(axis=2), axis=1)
        moved = np.array([points[groups == i].mean(axis=0) if np.any(groups == i) else centres[i]
                          for i in range(len(centres))])
        if np.allclose(moved, centres):
            break
        centres = moved
    return centres, groups


def palette(lab: np.ndarray) -> list[tuple[np.ndarray, float]]:
    """OKLab garment pixels to their main colours and shares, biggest first."""
    centres, groups = _kmeans(lab, min(K, len(np.unique(lab.round(3), axis=0))))
    merged = []  # [centre, pixel count]; groups that are one colour in light and shadow go into the bigger
    for i in np.argsort(-np.bincount(groups, minlength=len(centres))):
        count = int(np.sum(groups == i))
        if count == 0:
            continue
        near = next((m for m in merged if _same_colour(m[0], centres[i])), None)
        if near:
            near[0] = (near[0] * near[1] + centres[i] * count) / (near[1] + count)
            near[1] += count
        else:
            merged.append([centres[i], count])
    merged.sort(key=lambda m: -m[1])
    return [(c, n / len(lab)) for c, n in merged]


def garment_pixels(cutout: Image.Image) -> np.ndarray:
    small = cutout.copy()
    small.thumbnail((SIDE, SIDE))
    pixels = np.asarray(small, dtype=float).reshape(-1, 4)
    return pixels[pixels[:, 3] > SOLID][:, :3]


def measure(cutout_png: bytes, photo: bytes | None = None) -> dict:
    """The garment's main colours: {"colours": [{"hex", "share"}...], "kelvin": the
    room light taken out, or None}. With the photo the cutout came from, the room's
    light is read from around the garment and taken out. No colours if the cutout
    has no solid pixels."""
    cutout = Image.open(io.BytesIO(cutout_png)).convert("RGBA")
    pixels = garment_pixels(cutout)
    if len(pixels) == 0:
        return {"colours": [], "kelvin": None}
    light = None
    if photo is not None:
        room = room_pixels(Image.open(io.BytesIO(photo)).convert("RGB"), cutout)
        light = None if room is None else estimate_light(room)
    lab = linear_to_oklab(np.clip(correct(linear(pixels), light), 0, None))
    groups = [(c, s) for c, s in palette(lab) if s >= MIN_SHARE][:MAX_COLOURS] or palette(lab)[:1]
    return {"colours": [{"hex": oklab_to_hex(c), "share": round(s, 2)} for c, s in groups],
            "kelvin": None if light is None else round(nearest_light(light)[0])}


def list_hexes(colours: list[dict]) -> list[str]:
    """The main colour and, if the garment is clearly two-tone, the second: what
    the list and filters show (colour_hex and colour_hex_2)."""
    if not colours:
        return []
    hexes = [colours[0]["hex"]]
    if len(colours) > 1 and colours[1]["share"] >= TWO_TONE_SHARE \
            and delta_e(hex_to_oklab(colours[0]["hex"]), hex_to_oklab(colours[1]["hex"])) > 100 * TWO_TONE_DELTA:
        hexes.append(colours[1]["hex"])
    return hexes


def main_colours(cutout_png: bytes) -> list[str]:
    """The main colour's hex, then a second one if the garment is two-tone, without
    taking out the room's light. Empty if the cutout has no solid pixels."""
    return list_hexes(measure(cutout_png)["colours"])


def colour_photo(item: dict) -> dict | None:
    """The photo to take the colour from: a shop photo's cutout if there is one
    (studio light, truer colour), otherwise the cover's."""
    ready = [p for p in item["photos"] if p["cutout"] and p["cutout_status"] == "ready"]
    shop = [p for p in ready if p["kind"] == "shop"]
    if shop:
        return shop[0]
    cover = item["photos"][0] if item["photos"] else None
    return cover if cover in ready else None


def refresh(store, item_id: str, upright=None) -> None:
    """Measure the item's colours again, e.g. when its cutout is ready or its cover
    changes. upright turns the stored original into the photo the cutout was cut
    from. A colour you picked by hand is kept (the store sees to that)."""
    item = store.get(item_id)
    photo = item and colour_photo(item)
    path = photo and store.image_path(photo["cutout"])
    if not path:
        return
    from_tidy = photo["kind"] == "worn" and photo["tidy"]
    if from_tidy and item["colours"] is not None:
        return  # its colour came from the tidied picture
    original = store.image_path(photo["original"])
    source = original.read_bytes() if original else None
    if source is not None and upright is not None:
        source = upright(source)
    measured = measure(path.read_bytes(), source) | {"source": photo["kind"]}
    # A worn garment's list colour stays the tidied picture's; its shares are still worth having.
    store.save_colour(item_id, None if from_tidy else list_hexes(measured["colours"]), measured)


def backfill(store, upright=None) -> int:
    """Measure every item that has a cutout but no measured colours yet. Returns how many got them."""
    done = 0
    for item in store.all(everyone=True):
        if item["colours"] is None:
            refresh(store, item["id"], upright)
            done += store.get(item["id"])["colours"] is not None
    return done
