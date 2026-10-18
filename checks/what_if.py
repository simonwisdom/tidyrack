"""The check for What if I…: are the edited pictures still the same garment, and did the colour move?

    uv run python checks/what_if.py DATA OUT

DATA is a Tidyrack data folder (read only; copy the live one first). Pictures and
results go in OUT; pictures already there are reused, so a run can be resumed.

1. Every offered edit of every item is drawn by both copies' image models: Codex
   (the personal copy) and Gemini on OpenRouter (the public copy).
2. Blind test: a vision model from neither maker (JUDGE) gets a numbered lineup
   of the wardrobe's pictures as they are, plus a decoy, and one edited picture,
   and picks which garment it is. It's never told what edit was asked for. The
   wardrobe has one garment of each type, so type alone would give the answer: the
   decoy is a different garment of the same type and colour (drawn by Codex from
   the original, told to change the brand, cut, collar, pockets and buttons), so the
   judge must recognise the garment itself.
3. Colour: for dye and fade edits, the edited garment is cut out and its main colour
   worked out from the pixels, as the app does for a photo. ΔE in OKLab (x100) to
   the colour asked for, and how much of the way from the garment's colour it moved.

Needs OPENROUTER_API_KEY and the codex CLI signed in.
"""

import json
import random
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tidyrack import ai, colour, upcycle  # noqa: E402

JUDGE = "anthropic/claude-sonnet-5.5"
MODELS = ("codex", "gemini")
DECOY_PROMPT = ("Draw a different {what}, as a clean catalogue photo on the same plain light grey background: "
                "the same kind of garment in the same colour and fabric, but clearly another garment from another "
                "brand: a different cut, collar, pockets, buttons, seams and label.")
JUDGE_PROMPT = """Pictures 1 to {n} are a lineup of garments. The last picture shows one of them, but the garment may have been altered since: dyed another colour, faded, shortened, or given different buttons.
Which numbered garment is it? Look at what alterations don't change: the shape, collar, seams, pockets, label and other small details.
Reply with JSON only: {{"number": <1 to {n}>, "why": "<one short sentence>"}}"""
CHANGES = {"dye": "dyed another colour", "fade": "faded or bleached", "crop": "shortened or cropped",
           "buttons": "different buttons"}
CHANGE_PROMPT = """The first picture is a garment; the second is the same garment after one change. Which change was made?
Choose exactly one of: {choices}.
Reply with JSON only: {{"change": "<one of the choices, word for word>"}}"""


def items_of(data: Path) -> list[dict]:
    db_path = data / "tidyrack.db" if (data / "tidyrack.db").exists() else data / "wardrobe.db"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    items = []
    for row in db.execute("select * from items order by added_at"):
        item = dict(row)
        item["photos"] = [dict(p) for p in db.execute(
            "select * from photos where item_id = ? order by position", (item["id"],))]
        item["source"] = data / "images" / upcycle.source(item)
        items.append(item)
    return items


def draw(path: Path, source: Path, prompt: str, key: str | None) -> str | None:
    """Draw once into path (kept between runs). Returns why it failed, or None."""
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(ai.redraw(ai.upright_jpeg(source.read_bytes()), prompt, key))
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def main_hex(png: Path) -> str:
    return colour.main_colours(ai.remove_background(ai.upright_jpeg(png.read_bytes())))[0]


def judge(lineup: list[Path], edited: Path, key: str) -> int | None:
    prompt = JUDGE_PROMPT.format(n=len(lineup))
    jpegs = [ai.upright_jpeg(p.read_bytes(), 768) for p in [*lineup, edited]]
    body = ai.call_openrouter(ai.OPENROUTER_URL, key, {
        "model": JUDGE,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            *[c for n, j in enumerate(jpegs, 1) for c in (
                {"type": "text", "text": f"Picture {n}" if n <= len(lineup) else "The last picture"},
                {"type": "image_url", "image_url": {"url": ai._data_url(j)}})]]}],
    }, timeout=120)
    try:
        return int(ai.json_object(body["choices"][0]["message"]["content"])["number"])
    except (ValueError, KeyError, TypeError):
        return None


def name_change(before: Path, after: Path, key: str) -> str | None:
    """The judge names the one change between two pictures, from CHANGES or "nothing changed"."""
    choices = [*CHANGES.values(), "nothing changed"]
    body = ai.call_openrouter(ai.OPENROUTER_URL, key, {
        "model": JUDGE,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CHANGE_PROMPT.format(choices="; ".join(choices))},
            *[{"type": "image_url", "image_url": {"url": ai._data_url(ai.upright_jpeg(p.read_bytes(), 768))}}
              for p in (before, after)]]}],
    }, timeout=120)
    try:
        said = ai.json_object(body["choices"][0]["message"]["content"])["change"]
    except (ValueError, KeyError, TypeError):
        return None
    return next((k for k, v in CHANGES.items() if v == said), "nothing" if said == "nothing changed" else None)


def main(data: Path, out: Path) -> None:
    key = ai._api_key()
    items = items_of(data)
    jobs = []
    for item in items:
        wardrobe = [i["colour_hex"] for i in items if i["id"] != item["id"] and i["colour_hex"]]
        for option in upcycle.options(item, wardrobe):
            for model in MODELS:
                jobs.append({"model": model, "item": item, "option": option,
                             "image": out / model / f"{item['id']}-{option['key']}.png"})

    # 1. The edits, and a decoy for each item.
    with ThreadPoolExecutor(3) as codex, ThreadPoolExecutor(4) as gemini:
        drawn = [(codex if j["model"] == "codex" else gemini).submit(
            draw, j["image"], j["item"]["source"], j["option"]["prompt"], key if j["model"] == "gemini" else None)
            for j in jobs]
        decoys = {i["id"]: codex.submit(draw, out / "decoys" / f"{i['id']}.png", i["source"], DECOY_PROMPT.format(
            what=" ".join(filter(None, [i["colour"], i["fabric"], i["type"]]))), None) for i in items}
        for j, future in zip(jobs, drawn):
            j["error"] = future.result()
        for item_id, future in decoys.items():
            if future.result():
                print("decoy failed", item_id, future.result())

    # 2. The blind lineup: the wardrobe as it is plus this item's decoy, in a fixed shuffled order.
    def blind(j):
        if j["error"]:
            return
        decoy = out / "decoys" / f"{j['item']['id']}.png"
        lineup = [i["source"] for i in items] + ([decoy] if decoy.exists() else [])
        random.Random(f"{j['item']['id']}{j['option']['key']}{j['model']}").shuffle(lineup)
        pick = judge(lineup, j["image"], key)
        j["picked"] = None if pick is None or not 1 <= pick <= len(lineup) else (
            "right" if lineup[pick - 1] == j["item"]["source"] else "decoy" if lineup[pick - 1] == decoy else "other")
        j["decoy"] = decoy.exists()
        j["named"] = name_change(j["item"]["source"], j["image"], key)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(blind, jobs))

    # 3. Colour, measured the same way for the picture before and after.
    before = {i["id"]: main_hex(i["source"]) for i in items}
    for j in jobs:
        target = j["option"].get("target_hex")
        if target and not j["error"]:
            now, after = before[j["item"]["id"]], main_hex(j["image"])
            start = upcycle.delta_e(now, target)
            j.update(before_hex=now, after_hex=after, delta_e=round(upcycle.delta_e(after, target), 1),
                     moved=round(1 - upcycle.delta_e(after, target) / start, 2) if start else None)

    rows = [{"model": j["model"], "item": j["item"]["id"], "type": j["item"]["type"], "edit": j["option"]["key"],
             **{k: j.get(k) for k in ("error", "picked", "decoy", "named", "before_hex", "after_hex", "delta_e", "moved")}}
            for j in jobs]
    (out / "results.json").write_text(json.dumps(rows, indent=1))
    summary(rows)


def summary(rows: list[dict]) -> None:
    for model in MODELS:
        mine = [r for r in rows if r["model"] == model]
        judged = [r for r in mine if r["picked"]]
        right = sum(r["picked"] == "right" for r in judged)
        print(f"\n{model}: {len(mine)} edits, {sum(bool(r['error']) for r in mine)} failed to draw")
        print(f"  blind test: {right}/{len(judged)} picked the right garment, "
              f"{sum(r['picked'] == 'decoy' for r in judged)} the decoy, {sum(r['picked'] == 'other' for r in judged)} another")
        by_edit = {}
        for r in judged:
            kind = r["edit"].split("-")[0]
            by_edit.setdefault(kind, []).append(r["picked"] == "right")
        print("  by edit:", ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in by_edit.items()))
        named = {}
        for r in mine:
            if not r["error"]:
                named.setdefault(r["edit"].split("-")[0], []).append(r.get("named"))
        print("  change named blind:", ", ".join(f"{k} {v.count(k)}/{len(v)}" for k, v in named.items()))
        for r in mine:
            kind = r["edit"].split("-")[0]
            if not r["error"] and r.get("named") != kind:
                print("    named", r.get("named"), "for", r["type"], r["edit"])
        coloured = [r for r in mine if r.get("delta_e") is not None]
        for kind in ("dye", "fade"):
            rs = [r for r in coloured if r["edit"].split("-")[0] == kind]
            if rs:
                des = sorted(r["delta_e"] for r in rs)
                print(f"  {kind}: ΔE to the colour asked for, median {des[len(des) // 2]} (range {des[0]}-{des[-1]}); "
                      f"moved a median {sorted(r['moved'] for r in rs)[len(rs) // 2]:.0%} of the way")
        for r in mine:
            if r["picked"] not in ("right", None) or r["error"]:
                print("   ", r["type"], r["edit"], r["picked"] or r["error"])


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
