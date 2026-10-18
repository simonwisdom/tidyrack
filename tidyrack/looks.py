"""Outfits from the wardrobe: Dress me, Copy this look, The one piece you're missing
and the Morning card.

Every top + bottom (+ an optional layer), and every full-length piece (+ an
optional layer), with the owner's best pair of shoes for it if they have any,
is scored by tidyrack/outfits.py. Pictures of an outfit on a
body are drawn by the image model (ai.draw) in the background, like a tidy.
Every Save, Shuffle and drawing is kept in the `looks` table, so the scorer can
learn the owner's taste later.
"""

import io
import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from itertools import product
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from tidyrack import colour, outfits

SCHEMA = """
create table if not exists looks (
    id text primary key,
    owner text,                      -- the user's id in the public copy; null in the personal one
    kind text not null,              -- saved | shuffled | drawn | copy | missing | morning
    items text not null,             -- JSON list of item ids, top first
    score real,
    extra text,                      -- JSON: what the look came from (a copied photo's pieces, a missing piece...)
    image text,                      -- file name under images/ of the drawn picture
    status text,                     -- null | queued | working | ready | failed
    error text,
    added_at text not null
);
"""
TOP_LOOKS = 40  # Dress me shuffles through this many of the best outfits
GOOD = 0.0  # an outfit scoring this or more counts as one that works
BODY = "a person"  # who wears the outfit in See it on (TIDYRACK_BODY, e.g. "a man in his thirties")
PLACE = (51.51, -0.13)  # where the morning card's weather is for (TIDYRACK_PLACE "lat,lon"); London
DRESSY = ("interview", "client", "meeting", "presentation", "wedding", "dinner", "office", "board",
          "conference", "funeral", "pitch", "date")
# Everyday pieces The one piece you're missing tries: (type, colour in words, hex, fabric).
EVERYDAY = [
    ("white t-shirt", "white", "#f2f2ef", "cotton"), ("black t-shirt", "black", "#1d1d1f", "cotton"),
    ("grey marl t-shirt", "grey", "#9a9a9c", "cotton"), ("navy t-shirt", "navy", "#1f2a44", "cotton"),
    ("white oxford shirt", "white", "#f4f4f0", "cotton"), ("light blue oxford shirt", "light blue", "#a9c1dd", "cotton"),
    ("denim shirt", "mid blue", "#5a7396", "denim"), ("linen shirt", "ecru", "#e6dcc8", "linen"),
    ("striped breton top", "navy and white", "#e9e6df", "cotton"), ("navy polo shirt", "navy", "#22304d", "cotton"),
    ("grey sweatshirt", "grey", "#8f9092", "cotton"), ("navy jumper", "navy", "#202b45", "wool"),
    ("cream jumper", "cream", "#ece2cc", "wool"), ("black roll neck", "black", "#1b1b1d", "merino"),
    ("forest green jumper", "forest green", "#2f4a36", "wool"), ("burgundy jumper", "burgundy", "#5c2230", "wool"),
    ("camel cardigan", "camel", "#b8905e", "wool"), ("grey hoodie", "grey", "#7f8083", "cotton"),
    ("dark indigo jeans", "indigo", "#27324a", "denim"), ("mid blue jeans", "mid blue", "#56708f", "denim"),
    ("black jeans", "black", "#202022", "denim"), ("light wash jeans", "light blue", "#9fb4cc", "denim"),
    ("beige chinos", "beige", "#c8b58f", "cotton"), ("navy chinos", "navy", "#26304a", "cotton"),
    ("olive chinos", "olive", "#5f6342", "cotton"), ("grey tailored trousers", "grey", "#6c6d70", "wool"),
    ("black tailored trousers", "black", "#1e1e20", "wool"), ("cream wide trousers", "cream", "#e7dfcd", "linen"),
    ("navy shorts", "navy", "#26304a", "cotton"), ("stone shorts", "stone", "#cbbd9d", "cotton"),
    ("black joggers", "black", "#1d1d1f", "cotton"), ("navy blazer", "navy", "#1e2840", "wool"),
    ("grey blazer", "grey", "#6d6f73", "wool"), ("denim jacket", "mid blue", "#5b7597", "denim"),
    ("black leather jacket", "black", "#1a1a1b", "leather"), ("beige trench coat", "beige", "#c3a97f", "cotton"),
    ("navy overcoat", "navy", "#1f2840", "wool"), ("olive field jacket", "olive", "#5a5d3e", "cotton"),
    ("black puffer jacket", "black", "#19191a", "down"), ("green overshirt", "khaki green", "#6b6a4a", "wool"),
    ("white trainers", "white", "#f1f1ee", "leather"), ("black chelsea boots", "black", "#1c1c1d", "leather"),
    ("brown loafers", "brown", "#5b3a26", "leather"), ("tan suede desert boots", "tan", "#a7774b", "suede"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Looks:
    """The looks table, beside the store's own (same database file)."""

    def __init__(self, store):
        self.store = store
        with self._connect() as db:
            db.executescript(SCHEMA)
            db.execute("update looks set status = 'failed', error = 'Interrupted by a restart'"
                       " where status in ('queued', 'working')")

    def _connect(self):
        db = sqlite3.connect(self.store.db_path)
        db.row_factory = sqlite3.Row
        return db

    def add(self, owner, kind: str, items: list[str], score=None, extra=None, status=None) -> dict:
        look_id = uuid.uuid4().hex[:12]
        with self._connect() as db:
            db.execute("insert into looks (id, owner, kind, items, score, extra, status, added_at)"
                       " values (?, ?, ?, ?, ?, ?, ?, ?)",
                       (look_id, owner, kind, json.dumps(items), score, json.dumps(extra or {}), status, _now()))
        return self.get(look_id)

    def get(self, look_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from looks where id = ?", (look_id,)).fetchone()
        return row and _look(row)

    def of(self, owner, kind: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("select * from looks where owner is ? and kind = ? order by added_at desc",
                              (owner, kind)).fetchall()
        return [_look(r) for r in rows]

    def set(self, look_id: str, **changes) -> None:
        with self._connect() as db:
            for field, value in changes.items():
                assert field in ("status", "error", "image")
                db.execute(f"update looks set {field} = ? where id = ?", (value, look_id))


def forget(store, owner) -> None:
    """Delete someone's looks and their pictures, when they delete their account."""
    with sqlite3.connect(store.db_path) as db:
        db.executescript(SCHEMA)
        rows = db.execute("select id, image from looks where owner = ?", (owner,)).fetchall()
        db.execute("delete from looks where owner = ?", (owner,))
    for look_id, image in rows:
        for name in (image, f"look-{look_id}-photo.jpg"):
            if name and (store.images / name).exists():
                (store.images / name).unlink()


def _look(row) -> dict:
    look = dict(row)
    look["items"] = json.loads(look["items"])
    look["extra"] = json.loads(look["extra"] or "{}")
    look["image_url"] = f"/api/looks/{look['id']}/image" if look["image"] else None
    return look


def wardrobe(store, owner) -> dict[str, dict]:
    """The owner's finished items the scorer understands: id -> (item, description)."""
    found = {}
    for item in store.all(owner):
        if item["draft"]:
            continue
        piece = outfits.from_item(item)
        if piece:
            found[item["id"]] = {"item": item, "piece": piece}
    return found


def combinations(pieces: dict[str, dict]):
    """Every outfit's clothes: top + bottom or a full-length piece, each with no layer or one layer.
    Ids, layer first. ranked() adds the shoes."""
    by = {s: [i for i, p in pieces.items() if p["slot"] == s] for s in outfits.SLOTS}
    layers = [None] + by["layer"]
    for top, bottom, layer in product(by["top"], by["bottom"], layers):
        yield [i for i in (layer, top, bottom) if i]
    for full, layer in product(by["full"], layers):
        yield [i for i in (layer, full) if i]


def ranked(pieces: dict[str, dict], adjust=None) -> list[dict]:
    """Every outfit, best first: {"items", "score", "reasons"}. `adjust(ids)` adds to a score.
    If the owner has shoes, each outfit wears the pair that suits it best, last in its ids."""
    shoes = [i for i, p in pieces.items() if p["slot"] == "shoes"] or [None]
    out = []
    for clothes in combinations(pieces):
        best = None
        for pair in shoes:
            ids = clothes + [pair] if pair else clothes
            score, reasons = outfits.score([pieces[i] for i in ids])
            if adjust:
                score += adjust(ids)
            if best is None or score > best["score"]:
                best = {"items": ids, "score": score, "reasons": reasons}
        out.append(best | {"score": round(best["score"], 3)})
    out.sort(key=lambda o: -o["score"])
    return out


def good_count(pieces: dict[str, dict]) -> int:
    return sum(1 for o in ranked(pieces) if o["score"] >= GOOD)


def missing_pieces(wardrobe_items: dict[str, dict], top: int = 3) -> tuple[int, list[dict]]:
    """How many good outfits there are now, and the everyday pieces that would add the most, best first."""
    pieces = {i: w["piece"] for i, w in wardrobe_items.items()}
    now = good_count(pieces)
    tried = []
    for type_text, colour_words, hex_code, fabric in EVERYDAY:
        piece = outfits.describe(type_text, fabric, [{"hex": hex_code, "share": 1.0}])
        with_it = good_count(pieces | {"new": piece})
        tried.append({"type": type_text, "colour": colour_words, "hex": hex_code, "fabric": fabric,
                      "slot": piece["slot"], "outfits": with_it, "adds": with_it - now})
    tried.sort(key=lambda t: (-t["adds"], t["type"]))
    return now, tried[:top]


def vinted_search(piece: dict, wardrobe_items: dict[str, dict]) -> str:
    """A Vinted search for the piece, in the size the owner's other pieces of that kind are."""
    sizes = [w["item"]["size"] for w in wardrobe_items.values()
             if w["piece"]["slot"] == piece["slot"] and w["item"]["size"]]
    size = max(set(sizes), key=sizes.count) if sizes else ""
    return "https://www.vinted.co.uk/catalog?" + urlencode({"search_text": f"{piece['type']} {size}".strip()})


COPY_PROMPT = """The photo shows an outfit someone is wearing. List each garment they wear.
Reply with JSON only: {"pieces": [{"part": one of "top", "bottom", "layer", "full", "shoes", "other",
"type": what it is in a few plain words (e.g. "oversized white shirt", "black wide-leg trousers"),
"colour": its main colour in plain words, "hex": its main colour as a #rrggbb code}]}.
"layer" is anything worn over a top (jacket, coat, blazer, cardigan); "full" is a dress, jumpsuit or similar."""


def closest(want: dict, wardrobe_items: dict[str, dict], taken: set) -> tuple[str | None, float]:
    """The owner's item nearest a piece seen in a photo: same slot, then colour, then shared words."""
    slot = "bottom" if want.get("part") == "bottom" else want.get("part")
    best, best_cost = None, None
    try:
        target = colour.hex_to_oklab(want.get("hex") or "")
    except (ValueError, IndexError):
        target = None
    words = set((want.get("type") or "").lower().split())
    for item_id, w in wardrobe_items.items():
        if w["piece"]["slot"] != slot or item_id in taken or not w["piece"]["colours"]:
            continue
        cost = colour.delta_e(w["piece"]["colours"][0][0], target) if target is not None else 30
        cost -= 8 * len(words & set((w["item"]["type"] or "").lower().split()))
        if best_cost is None or cost < best_cost:
            best, best_cost = item_id, cost
    return best, (best_cost if best_cost is not None else 999)


MATCH = 25  # a piece within this (Delta E less word bonus) counts as the owner's version of it


def weather(place=PLACE) -> dict:
    """Today's weather: highest and lowest temperature and chance of rain. From
    Open-Meteo, or MET Norway when Open-Meteo is busy (both free, no key)."""
    try:
        response = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place[0], "longitude": place[1], "timezone": "auto", "forecast_days": 1,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"}, timeout=10)
        daily = response.json()["daily"]
        return {"high": round(daily["temperature_2m_max"][0]), "low": round(daily["temperature_2m_min"][0]),
                "rain": daily["precipitation_probability_max"][0] or 0}
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    # MET Norway asks every app to name itself and a contact (TIDYRACK_CONTACT).
    response = httpx.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                         params={"lat": round(place[0], 2), "lon": round(place[1], 2)},
                         headers={"User-Agent": f"Tidyrack/0.1 {os.environ.get('TIDYRACK_CONTACT', 'github.com/simonwisdom/tidyrack')}"}, timeout=10)
    hours = response.json()["properties"]["timeseries"][:15]  # the day ahead, from about now
    temps = [h["data"]["instant"]["details"]["air_temperature"] for h in hours]
    wet = [h["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount", 0) for h in hours]
    return {"high": round(max(temps)), "low": round(min(temps)), "rain": 70 if max(wet) >= 0.3 else 10}


def morning_pick(wardrobe_items: dict[str, dict], today: dict, events: list[str]) -> dict | None:
    """Today's outfit: the best scorer once warmth fits the weather and dressiness fits the calendar."""
    pieces = {i: w["piece"] for i, w in wardrobe_items.items()}
    feels = (today["high"] + today["low"]) / 2
    want_warmth = 4.5 if feels < 8 else 3.5 if feels < 14 else 2.5 if feels < 20 else 1.5
    dressy = any(word in e.lower() for e in events for word in DRESSY)
    want_formality = 3.5 if dressy else 2.0

    def fit(ids):
        ps = [pieces[i] for i in ids]
        warmth = sum(p["warmth"] for p in ps) / len(ps) + (0.7 if any(p["slot"] == "layer" for p in ps) else 0)
        formality = sum(p["formality"] for p in ps) / len(ps)
        return -0.4 * abs(warmth - want_warmth) - 0.6 * abs(formality - want_formality)

    best = ranked(pieces, fit)
    if not best:
        return None
    pick = best[0]
    names = [wardrobe_items[i]["item"]["colour"] or "" for i in pick["items"]]
    line = ", ".join(f"{n} {wardrobe_items[i]['item']['type']}".strip() for n, i in zip(names, pick["items"]))
    why = f"{today['low']}–{today['high']}°" + (f", {today['rain']}% rain" if today["rain"] >= 30 else "")
    if dressy:
        why += ", something smart on today"
    return pick | {"line": line, "why": why, "weather": today, "dressy": dressy}


def item_jpeg(store, upright, item: dict) -> bytes:
    """The best picture of an item for the image model: its tidied picture, else its
    cutout on white, else its photo."""
    photo = colour.colour_photo(item) or (item["photos"][0] if item["photos"] else None)
    if photo is None:
        raise ValueError(f"{item['type'] or 'An item'} has no photo")
    for name in (photo["tidy"], photo["cutout"]):
        path = name and store.image_path(name)
        if path:
            image = Image.open(path).convert("RGBA")
            flat = Image.new("RGB", image.size, "white")
            flat.paste(image, mask=image.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=90)
            return out.getvalue()
    return upright(store.image_path(photo["original"]).read_bytes())


def describe_items(items: list[dict]) -> str:
    return "; ".join(f"{i['colour'] or ''} {i['type'] or 'garment'}".strip() for i in items)


ON_BODY_PROMPT = (
    "A full-length catalogue photo of {body} wearing exactly these garments together: {items}. "
    "The reference pictures show each garment: keep each one's exact colours, print, fabric and details, "
    "and don't add other clothes (plain shoes if none are listed). Plain light background, natural standing pose, "
    "head to toe."
)
COPY_DRAW_PROMPT = (
    "The first reference picture is an outfit someone wore. Draw {body} wearing the same kind of look, "
    "but made of the garments in the other reference pictures: {items}. Keep each garment's exact colours, print "
    "and details. Same mood and styling as the first picture. Plain light background, full length, head to toe."
)
PIECE_PROMPT = ("A clean catalogue photo of a {colour} {type} in {fabric}, laid flat and centred on a plain "
                "light grey background. No logos or labels.")
SKETCH_PROMPT = (
    "A simple pencil sketch, black graphite on white paper, of {body} wearing exactly these garments: {items}. "
    "Full length, clear outlines, light shading, lots of white space, no colour, no background, no text. "
    "It will be shown on a black and white e-ink screen."
)


class Draw(BaseModel):
    items: list[str]


class Feedback(BaseModel):
    items: list[str]
    action: str  # saved | shuffled
    score: float | None = None


def add_routes(app, store, pipeline, run, key_for, current_user, read_upload, env) -> None:
    """The outfit routes. `run(lane, fn, *args)` starts a background job, as for tidies."""
    looks = Looks(store)
    body = env("BODY", BODY)
    place = tuple(float(x) for x in env("PLACE", ",".join(map(str, PLACE))).split(","))

    def mine(user):
        found = wardrobe(store, user)
        if not found:
            raise HTTPException(409, "Add a few clothes first: a top and a bottom at least.")
        return found

    def look_or_404(look_id: str, user) -> dict:
        look = looks.get(look_id)
        if look is None or look["owner"] != user:
            raise HTTPException(404, "No such look")
        return look

    def show(ranked_outfit: dict, found: dict) -> dict:
        """An outfit as the page shows it: its items with their pictures and colours."""
        shown = []
        for item_id in ranked_outfit["items"]:
            item = found[item_id]["item"]
            photo = colour.colour_photo(item) or (item["photos"][0] if item["photos"] else {})
            picture = photo.get("tidy") or photo.get("cutout") or photo.get("original")
            shown.append({"id": item_id, "type": item["type"], "colour": item["colour"],
                          "hex": item["colour_hex"], "image": picture and f"/images/{picture}"})
        return ranked_outfit | {"pieces": shown}

    def start_drawing(look: dict, prompt: str, refs: list[bytes], user) -> dict:
        key = key_for(user)  # raises in the public copy if the key can't be read, before anything is queued
        looks.set(look["id"], status="queued")

        def job():
            looks.set(look["id"], status="working")
            try:
                png = pipeline.draw(prompt, refs, key)
                name = f"look-{look['id']}.png"
                (store.images / name).write_bytes(png)
                looks.set(look["id"], image=name, status="ready", error=None)
            except Exception as exc:  # shown on the page, with Try again
                looks.set(look["id"], status="failed", error=str(exc)[:300])

        run("tidy", job)
        return looks.get(look["id"])

    def refs_for(item_ids: list[str], found: dict) -> list[bytes]:
        return [item_jpeg(store, pipeline.upright_jpeg, found[i]["item"]) for i in item_ids]

    @app.get("/api/looks/dress-me")
    def dress_me(user=Depends(current_user)):
        """The best outfits, in a shuffled order that leans to the best."""
        found = mine(user)
        best = ranked({i: w["piece"] for i, w in found.items()})[:TOP_LOOKS]
        rng = random.Random()
        order = sorted(best, key=lambda o: -o["score"] - rng.random() * 0.8)
        return {"outfits": [show(o, found) for o in order], "total": sum(1 for _ in combinations(
            {i: w["piece"] for i, w in found.items()}))}

    @app.post("/api/looks/feedback", status_code=201)
    def feedback(choice: Feedback, user=Depends(current_user)):
        """Save or Shuffle on an outfit, kept so the scorer can learn the owner's taste."""
        if choice.action not in ("saved", "shuffled"):
            raise HTTPException(400, "action must be saved or shuffled")
        found = mine(user)
        if any(i not in found for i in choice.items):
            raise HTTPException(404, "No such item")
        return looks.add(user, choice.action, choice.items, choice.score)

    @app.get("/api/looks/saved")
    def saved(user=Depends(current_user)):
        found = wardrobe(store, user)
        out = []
        for look in looks.of(user, "saved"):
            if all(i in found for i in look["items"]):
                out.append(show({"items": look["items"], "score": look["score"], "reasons": []}, found) | {"look": look})
        return out

    @app.post("/api/looks/draw", status_code=202)
    def see_it_on(outfit: Draw, user=Depends(current_user)):
        """Draw the outfit on a body with the image model, in the background."""
        found = mine(user)
        if not outfit.items or any(i not in found for i in outfit.items):
            raise HTTPException(404, "No such item")
        items = [found[i]["item"] for i in outfit.items]
        look = looks.add(user, "drawn", outfit.items)
        return start_drawing(look, ON_BODY_PROMPT.format(body=body, items=describe_items(items)),
                             refs_for(outfit.items, found), user)

    @app.post("/api/looks/copy", status_code=201)
    async def copy_look(photo: UploadFile, user=Depends(current_user)):
        """Read an outfit photo, match each piece to the owner's closest item, and say what's missing."""
        found = mine(user)
        data, _ = await read_upload(photo, user)
        jpeg = pipeline.upright_jpeg(data)
        try:
            answer = await run_in_threadpool(pipeline.ask_json, COPY_PROMPT, [jpeg], key_for(user))
        except Exception as exc:
            raise HTTPException(502, f"Couldn't read that outfit: {exc}") from exc
        taken, matched, missing = set(), [], []
        for want in answer.get("pieces") or []:
            if want.get("part") not in outfits.SLOTS:
                continue
            item_id, cost = closest(want, found, taken)
            if item_id and cost <= MATCH:
                taken.add(item_id)
                matched.append({"want": want, "item": item_id})
            else:
                missing.append(want | {"vinted": "https://www.vinted.co.uk/catalog?"
                                       + urlencode({"search_text": want.get("type") or ""})})
        order = {"layer": 0, "top": 1, "full": 1, "bottom": 2, "shoes": 3}
        matched.sort(key=lambda m: order[m["want"]["part"]])
        ids = [m["item"] for m in matched]
        look = looks.add(user, "copy", ids, extra={"pieces": answer.get("pieces") or [], "missing": missing})
        name = f"look-{look['id']}-photo.jpg"
        (store.images / name).write_bytes(jpeg)
        shown = show({"items": ids, "score": None, "reasons": []}, found)
        return {"look": look, "matched": [m | {"piece": p} for m, p in zip(matched, shown["pieces"])],
                "missing": missing}

    @app.post("/api/looks/{look_id}/draw-copy", status_code=202)
    def draw_copy(look_id: str, user=Depends(current_user)):
        """Draw the owner's version of a copied look: their matched pieces in the photo's styling."""
        look = look_or_404(look_id, user)
        found = mine(user)
        if look["kind"] != "copy" or not look["items"] or any(i not in found for i in look["items"]):
            raise HTTPException(409, "None of your clothes matched that look")
        inspiration = store.image_path(f"look-{look_id}-photo.jpg")
        refs = ([inspiration.read_bytes()] if inspiration else []) + refs_for(look["items"], found)
        items = [found[i]["item"] for i in look["items"]]
        return start_drawing(look, COPY_DRAW_PROMPT.format(body=body, items=describe_items(items)), refs, user)

    @app.get("/api/looks/missing")
    def missing(user=Depends(current_user)):
        """The everyday piece that would add the most good outfits, and the next two."""
        found = mine(user)
        now, best = missing_pieces(found)
        for piece in best:
            piece["vinted"] = vinted_search(piece, found)
        return {"outfits_now": now, "tried": len(EVERYDAY), "best": best}

    @app.post("/api/looks/missing/draw", status_code=202)
    def draw_missing(user=Depends(current_user)):
        found = mine(user)
        _, best = missing_pieces(found, top=1)
        piece = best[0]
        look = looks.add(user, "missing", [], extra=piece)
        return start_drawing(look, PIECE_PROMPT.format(**piece), [], user)

    @app.get("/api/looks/morning")
    def morning(events: str = "", user=Depends(current_user)):
        """Today's outfit for the morning card. `events`: today's calendar, one per line."""
        found = mine(user)
        try:
            today = pipeline.weather(place) if hasattr(pipeline, "weather") else weather(place)
        except Exception as exc:
            raise HTTPException(502, f"Couldn't get today's weather: {exc}") from exc
        pick = morning_pick(found, today, [e for e in events.splitlines() if e.strip()])
        if pick is None:
            raise HTTPException(409, "Add a top and a bottom first")
        return show(pick, found)

    @app.post("/api/looks/morning/sketch", status_code=202)
    def morning_sketch(outfit: Draw, user=Depends(current_user)):
        """Draw today's outfit as a pencil sketch for the e-ink reader."""
        found = mine(user)
        if not outfit.items or any(i not in found for i in outfit.items):
            raise HTTPException(404, "No such item")
        items = [found[i]["item"] for i in outfit.items]
        look = looks.add(user, "morning", outfit.items)
        return start_drawing(look, SKETCH_PROMPT.format(body=body, items=describe_items(items)),
                             refs_for(outfit.items, found), user)

    # Last, so /api/looks/missing and the others above aren't taken for a look's id.
    @app.get("/api/looks/{look_id}")
    def get_look(look_id: str, user=Depends(current_user)):
        return look_or_404(look_id, user)

    @app.get("/api/looks/{look_id}/image")
    def look_image(look_id: str, user=Depends(current_user)):
        look = look_or_404(look_id, user)
        path = look["image"] and store.image_path(look["image"])
        if not path:
            raise HTTPException(404, "Not drawn yet")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    return looks
