"""The On me check: try garments on selfies with each model, then score the results blind.

Run from the repo:  PYTHONPATH=. uv run python tools/tryon_check.py run|judge|sheet SET_DIR OUT_DIR [models]
Run it where the photos are, so they never leave your own machines.
SET_DIR holds selfies/*.(jpg|png) and garments/<A|B|C>.(jpg|png) plus garments.json {"A": "a grey cardigan", ...}.
Results: OUT_DIR/<model>/<selfie>__<garment>.png, times.json, scores.json, sheet.jpg.
"""
import io
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from tidyrack import ai

MODELS = {"codex": lambda s, g, w: ai.try_on(s, g, w),
          "gemini": lambda s, g, w: ai.try_on(s, g, w, key=ai._api_key())}


def jpeg(path: Path, max_side=1600) -> bytes:
    image = Image.open(path)
    if image.mode in ("RGBA", "LA", "P"):  # a cutout: put it on white
        image = image.convert("RGBA")
        white = Image.new("RGB", image.size, "white")
        white.paste(image, mask=image.split()[3])
        image = white
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return ai.upright_jpeg(out.getvalue(), max_side)


def load(set_dir: Path):
    selfies = sorted(p for p in (set_dir / "selfies").iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    garments = {p.stem: p for p in sorted((set_dir / "garments").iterdir()) if p.suffix.lower() in (".jpg", ".jpeg", ".png")}
    what = json.loads((set_dir / "garments.json").read_text())
    return selfies, garments, what


def run(set_dir: Path, out: Path, models: list[str]):
    selfies, garments, what = load(set_dir)
    times_file = out / "times.json"
    times = json.loads(times_file.read_text()) if times_file.exists() else {}

    def one(model, selfie, letter):
        dest = out / model / f"{selfie.stem}__{letter}.png"
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        t = time.time()
        try:
            png = MODELS[model](jpeg(selfie), jpeg(garments[letter], 1024), what[letter])
        except Exception as exc:
            print(f"FAIL {dest.name} {model}: {exc}", flush=True)
            return
        dest.write_bytes(png)
        times[f"{model}/{dest.name}"] = round(time.time() - t, 1)
        print(f"{model} {dest.name} {times[f'{model}/{dest.name}']}s", flush=True)

    jobs = [(m, s, g) for m in models for s in selfies for g in garments]
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda j: one(*j), jobs))
    times_file.write_text(json.dumps(times, indent=1))


WHICH_PROMPT = """The first image shows a person. Each of the other images shows one garment, with a big red number printed in its corner.
Which numbered garment is the person wearing? Use 0 if they are wearing none of them. Describe it in a few words too.
Then compare what the person wears with that garment's picture. Be strict: a redrawn garment often loses or adds small details.
colours and pattern_or_print: "same", "different" or "not visible".
Reply with JSON only: {"garment": <number>, "description": ..., "colours": ..., "pattern_or_print": ..., "note": "<one short sentence on any difference>"}"""

# Asked of one picture at a time, so the reader can't copy one answer from the other.
READ_PROMPT = """Look only at the {what} in this picture{where}.
- text: every word, number and letter printed or sewn on it, as it appears (a mirror image stays reversed), "" if none.
- buttons: how many buttons you can count on it. pockets: how many pockets (count flaps and patch pockets).
Reply with JSON only: {{"text": ..., "buttons": <n>, "pockets": <n>}}"""


def numbered(jpeg_bytes: bytes, n: int) -> bytes:
    """The garment picture with a big red number in its corner, so the judge can't mix up the order."""
    from PIL import ImageDraw, ImageFont
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    size = max(40, image.width // 6)
    font = ImageFont.load_default(size=size)
    ImageDraw.Draw(image).text((size // 4, size // 4), str(n), fill=(220, 0, 0), font=font)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def words(text) -> str:
    """The words in a read, as a set: case, punctuation and order don't count."""
    return set("".join(ch if ch.isalnum() else " " for ch in str(text or "").upper()).split())


def text_check(which: dict) -> tuple[set, set]:
    """Words drawn that aren't on the real garment (invented or garbled), and words of it not drawn (lost)."""
    real, worn = words(which["garment_read"].get("text")), words(which["worn_read"].get("text"))
    return worn - real, real - worn

SAME_PROMPT = """Image 1 is a photo. Image 2 is an edited version of it, in which one garment was meant to change.
Ignoring the clothes, answer each with "yes" or "no": same_person (face, hair, skin and body look like the same person; "yes" if the face is hidden in both), same_room (background, furniture and light), same_pose_and_framing.
Reply with JSON only: {"same_person": ..., "same_room": ..., "same_pose_and_framing": ..., "other_changes": "<anything besides the clothes that changed, or 'none'>"}"""


def unchanged_share(before: Path, after: Path) -> dict:
    """No model: how much of the picture is the same. Both are scaled to 96 px wide,
    blurred a little (a redraw shifts pixels slightly), and compared in 8x8 px tiles."""
    a, b = Image.open(before).convert("L"), Image.open(after).convert("L")
    aspect_kept = abs(a.width / a.height - b.width / b.height) < 0.02
    size = (96, round(96 * a.height / a.width))
    a = np.asarray(a.resize(size).filter(ImageFilter.GaussianBlur(1)), dtype=float)
    b = np.asarray(b.resize(size).filter(ImageFilter.GaussianBlur(1)), dtype=float)
    h = size[1] // 8 * 8
    diff = np.abs(a[:h] - b[:h]).reshape(h // 8, 8, 12, 8).mean(axis=(1, 3))
    return {"aspect_kept": aspect_kept, "tiles_unchanged": round(float((diff < 12).mean()), 2)}


def judge(set_dir: Path, out: Path):
    selfies, garments, what = load(set_dir)
    scores_file = out / "scores.json"
    scores = json.loads(scores_file.read_text()) if scores_file.exists() else {}
    letters = sorted(garments)
    pics = {g: jpeg(garments[g], 768) for g in letters}

    def read(picture: bytes, letter: str, worn: bool) -> dict:
        prompt = READ_PROMPT.format(what=what[letter].split(" with ")[0],
                                    where=", which a person is wearing" if worn else "")
        return ai.json_object(ai.ask_codex(prompt, [picture]))

    with ThreadPoolExecutor(3) as pool:
        truth_reads = dict(zip(letters, pool.map(lambda g: read(pics[g], g, False), letters)))

    def one(result: Path):
        key = f"{result.parent.name}/{result.name}"
        if key in scores:
            return
        selfie_stem, truth = result.stem.split("__")
        selfie = next(s for s in selfies if s.stem == selfie_stem)
        order = letters[:]
        random.Random(key).shuffle(order)
        try:
            which = ai.json_object(ai.ask_codex(WHICH_PROMPT, [jpeg(result), *(numbered(pics[g], n) for n, g in enumerate(order, 1))]))
            same = ai.json_object(ai.ask_codex(SAME_PROMPT, [jpeg(selfie), jpeg(result)]))
            worn = read(jpeg(result), truth, True)
        except Exception as exc:
            print(f"FAIL judging {key}: {exc}", flush=True)
            return
        n = int(which.get("garment") or 0)
        which["picked"] = order[n - 1] if 1 <= n <= len(order) else None
        which["right"] = which["picked"] == truth
        # Judged by the script, not the model: the text it read on both, and the buttons it counted.
        real = truth_reads[truth]
        which.update(garment_read=real, worn_read=worn)
        which["buttons_kept"] = real.get("buttons") == worn.get("buttons") and real.get("pockets") == worn.get("pockets")
        scores[key] = {"which": which, "same": same, "pixels": unchanged_share(selfie, result)}
        print(key, "right" if which["right"] else f"WRONG ({which['picked']})", flush=True)

    results = sorted(p for p in out.glob("*/*.png"))
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(one, results))
    scores_file.write_text(json.dumps(scores, indent=1))
    summary(scores)


def summary(scores: dict):
    by_model = {}
    for key, s in scores.items():
        by_model.setdefault(key.split("/")[0], []).append(s)
    for model, rows in sorted(by_model.items()):
        n = len(rows)
        count = lambda f: sum(1 for r in rows if f(r))  # noqa: E731
        print(f"{model}: garment named right {count(lambda r: r['which']['right'])}/{n}; "
              f"no invented text {count(lambda r: not text_check(r['which'])[0])}; "
              f"no lost text {count(lambda r: not text_check(r['which'])[1])}; "
              f"pattern kept {count(lambda r: r['which'].get('pattern_or_print') == 'same')}; "
              f"buttons and pockets kept {count(lambda r: r['which'].get('buttons_kept'))}; "
              f"same person {count(lambda r: r['same'].get('same_person') == 'yes')}; "
              f"same room {count(lambda r: r['same'].get('same_room') == 'yes')}; "
              f"framing kept {count(lambda r: r['pixels']['aspect_kept'])}; "
              f"tiles unchanged {np.mean([r['pixels']['tiles_unchanged'] for r in rows]):.2f}")


def sheet(set_dir: Path, out: Path):
    """One row per selfie x garment: selfie, garment, then each model's result."""
    selfies, garments, _ = load(set_dir)
    models = sorted(p.name for p in out.iterdir() if p.is_dir())
    cell = (300, 400)
    rows = [(s, g) for s in selfies for g in sorted(garments)]
    canvas = Image.new("RGB", (cell[0] * (2 + len(models)), cell[1] * len(rows)), "white")
    for r, (s, g) in enumerate(rows):
        for c, path in enumerate([s, garments[g], *(out / m / f"{s.stem}__{g}.png" for m in models)]):
            if path.exists():
                im = Image.open(path).convert("RGB")
                im.thumbnail(cell)
                canvas.paste(im, (c * cell[0], r * cell[1]))
    canvas.save(out / "sheet.jpg", quality=85)
    print(out / "sheet.jpg", "columns: selfie, garment,", ", ".join(models))


if __name__ == "__main__":
    step, set_dir, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    if step == "run":
        run(set_dir, out, sys.argv[4].split(",") if len(sys.argv) > 4 else list(MODELS))
    elif step == "judge":
        judge(set_dir, out)
    else:
        sheet(set_dir, out)
