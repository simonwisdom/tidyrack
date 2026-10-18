"""The check for measured colour: how close does taking out the room's light get
back to a garment's true colour?

Studio product photos (Polyvore Outfits, white background) give the true colour.
Each garment is placed in a made-up room (a wall, a floor, a locker...), the
whole scene is lit like a warm lamp or shade, saved as a phone JPEG, and its
colours measured twice: as photographed, and with the room's light taken out.
The distance to the true colour is Delta E in OKLab (x100: ~2 is just
noticeable, over 20 a different colour).

    uv run --with pyarrow python tests/colour_check.py polyvore-items.parquet [count]

Not a pytest test: it needs the dataset and takes a few minutes.
"""

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tidyrack import colour  # noqa: E402

CLOTHES = {"tops", "bottoms", "outerwear", "all-body"}
# Room surfaces as (sRGB, texture): what's around a garment on a phone photo.
ROOMS = {
    "white wall": [((242, 242, 240), "flat")],
    "cream wall": [((236, 228, 212), "flat")],
    "grey duvet": [((190, 192, 196), "folds")],
    "oak floor": [((169, 130, 90), "wood")],
    "sage locker": [((167, 179, 154), "flat")],
    "wall and floor": [((236, 228, 212), "flat"), ((150, 112, 78), "wood")],
    "dark sofa": [((74, 74, 78), "folds")],
}
LIGHTS = {  # kelvin of the light left in the photo after the phone's own white balance
    "daylight": 6504,
    "warm lamp": 2700,
    "lamp, phone half-fixed": 3600,
    "shade": 7500,
    "deep shade": 10000,
}
SCENE = (1200, 1600)


def studio_cutout(image: Image.Image) -> Image.Image | None:
    """The garment off a white studio background, trimmed: pixels clearly not white,
    holes filled, the biggest patch."""
    rgb = np.asarray(image.convert("RGB"), dtype=float)
    garment = np.min(rgb, axis=2) < 235
    garment = ndimage.binary_fill_holes(ndimage.binary_opening(garment, iterations=2))
    labels, count = ndimage.label(garment)
    if count == 0:
        return None
    sizes = ndimage.sum(garment, labels, range(1, count + 1))
    garment = labels == 1 + int(np.argmax(sizes))
    if garment.mean() < 0.15:
        return None
    alpha = Image.fromarray((ndimage.binary_erosion(garment, iterations=2) * 255).astype(np.uint8))
    cutout = image.convert("RGB").convert("RGBA")
    cutout.putalpha(alpha)
    return cutout.crop(alpha.getbbox())


def room(name: str, rng) -> np.ndarray:
    """A made-up room behind the garment, in linear light."""
    w, h = SCENE
    out = np.zeros((h, w, 3))
    surfaces = ROOMS[name]
    for n, (rgb, texture) in enumerate(surfaces):
        top, bottom = (0, h) if len(surfaces) == 1 else ((0, int(0.65 * h)) if n == 0 else (int(0.65 * h), h))
        base = colour.linear(np.array(rgb, dtype=float))
        rows = bottom - top
        shade = np.ones((rows, w))
        if texture == "wood":
            shade *= 1 + 0.12 * np.sin(np.arange(w) / 9 + rng.uniform(0, 6))[None] \
                * rng.uniform(0.5, 1, (1, w))
        if texture == "folds":
            shade *= 1 + 0.2 * np.sin(np.arange(rows)[:, None] / 40 + np.arange(w)[None] / 70)
        shade *= np.linspace(1.1, 0.8, w)[None]  # light falls off across the room
        out[top:bottom] = base * shade[..., None]
    return out


def lit(scene: np.ndarray, kelvin: float) -> np.ndarray:
    """The scene under a light of this colour, same brightness, as the phone saves it."""
    tint = colour.light_rgb(kelvin) / colour.light_rgb(6504)
    tint /= colour._TO_XYZ[1] @ tint / (colour._TO_XYZ[1] @ np.ones(3))
    return scene * tint


def jpeg(linear_scene: np.ndarray, rng) -> bytes:
    rgb = colour.encoded(linear_scene) + rng.normal(0, 1.5, linear_scene.shape)
    out = io.BytesIO()
    Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(out, "JPEG", quality=90)
    return out.getvalue()


def png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def scene_with(garment: Image.Image, room_name: str, kelvin: float, rng) -> tuple[bytes, bytes]:
    """A phone photo of the garment in the room under this light, and its cutout."""
    g = garment.copy()
    g.thumbnail((int(SCENE[0] * 0.6), int(SCENE[1] * 0.6)))
    x, y = (SCENE[0] - g.width) // 2, (SCENE[1] - g.height) // 2
    scene = room(room_name, rng)
    rgba = np.asarray(g, dtype=float)
    a = (rgba[:, :, 3:] > colour.SOLID).astype(float)
    scene[y:y + g.height, x:x + g.width] = a * colour.linear(rgba[:, :, :3]) \
        + (1 - a) * scene[y:y + g.height, x:x + g.width]
    photo = jpeg(lit(scene, kelvin), rng)
    taken = Image.open(io.BytesIO(photo)).convert("RGBA")
    alpha = np.zeros((SCENE[1], SCENE[0]), np.uint8)
    alpha[y:y + g.height, x:x + g.width] = a[:, :, 0] * 255
    taken.putalpha(Image.fromarray(alpha))
    return photo, png(taken.crop((x, y, x + g.width, y + g.height)))


def distance(measured: dict, truth: dict) -> float:
    return colour.delta_e(colour.hex_to_oklab(measured["colours"][0]["hex"]),
                          colour.hex_to_oklab(truth["colours"][0]["hex"]))


def garments(parquet: str, count: int):
    import pyarrow.parquet as pq

    # The first batch is enough garments, and keeps the memory to a few hundred MB.
    batch = next(pq.ParquetFile(parquet).iter_batches(2000, columns=["item_id", "category", "image"])).to_pydict()
    rng = np.random.default_rng(1)
    picked = 0
    for i in rng.permutation(len(batch["item_id"])):
        if batch["category"][i] not in CLOTHES:
            continue
        image = batch["image"][i]
        image = Image.open(io.BytesIO(image["bytes"] if isinstance(image, dict) else image))
        cutout = studio_cutout(image)
        if cutout is None:
            continue
        yield batch["item_id"][i], cutout
        picked += 1
        if picked == count:
            return


def main(parquet: str, count: int = 60) -> None:
    rng = np.random.default_rng(2)
    rows = []  # (room, light, before, after)
    for n, (item_id, garment) in enumerate(garments(parquet, count)):
        truth = colour.measure(png(garment))
        for room_name in ROOMS:
            for light_name, kelvin in LIGHTS.items():
                photo, cutout = scene_with(garment, room_name, kelvin, rng)
                rows.append((room_name, light_name, distance(colour.measure(cutout), truth),
                             distance(colour.measure(cutout, photo), truth)))
        print(f"{n + 1}/{count}", file=sys.stderr, end="\r")
    report(rows, count)


def report(rows, count) -> None:
    def line(label, picked):
        before = np.array([r[2] for r in picked])
        after = np.array([r[3] for r in picked])
        print(f"| {label} | {np.median(before):.1f} | {np.median(after):.1f} "
              f"| {np.percentile(before, 90):.1f} | {np.percentile(after, 90):.1f} "
              f"| {np.mean(after > before + 2):.0%} |")

    print(f"\n{count} garments, {len(rows)} photos. Delta E (OKLab x100) of the main colour from the true one.\n")
    print("| | median before | median after | worst 10% before | worst 10% after | made worse by 2+ |")
    print("|---|---|---|---|---|---|")
    for light_name in LIGHTS:
        line(light_name, [r for r in rows if r[1] == light_name])
    print("| | | | | | |")
    for room_name in ROOMS:
        line(room_name, [r for r in rows if r[0] == room_name and r[1] != "daylight"])
    print("| | | | | | |")
    line("all but daylight", [r for r in rows if r[1] != "daylight"])


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60)
