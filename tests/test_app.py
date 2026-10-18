import io
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tidyrack import ai, colour
from tidyrack.app import create_app
from tidyrack.store import Store


def photo_bytes(colour="navy"):
    out = io.BytesIO()
    Image.new("RGB", (40, 30), colour).save(out, "JPEG")
    return out.getvalue()


def png_bytes(image=None):
    out = io.BytesIO()
    (image or Image.new("RGBA", (10, 10), (0, 0, 0, 0))).save(out, "PNG")
    return out.getvalue()


def cutout(*colours, size=200):
    """A garment on a transparent background: a square in the middle, split
    into side-by-side bands of the given colours."""
    image = Image.new("RGBA", (size, size), (255, 0, 0, 0))  # red, but see-through
    band = (size // 2) // len(colours)
    for n, rgb in enumerate(colours):
        left = size // 4 + n * band
        image.paste((*rgb, 255), (left, size // 4, left + band, size * 3 // 4))
    return png_bytes(image)


class FakePipeline:
    upright_jpeg = staticmethod(ai.upright_jpeg)

    def __init__(self, guesses=None):
        self.guesses = guesses or {"type": "jumper", "colour": "navy", "fabric": "wool"}
        self.fail = False
        self.fail_tidy = False
        self.reads = []  # how many photos each details read was given
        self.cutout = png_bytes()
        self.keys = []  # (step, OpenRouter key) of each model call; None is the personal copy's Codex
        self.error = None  # raised by every model call, when set
        self.outfit = []  # what split_outfit finds
        self.tidied_parts = []  # the garment each tidy was told to draw
        self.read_parts = []  # the garment each details read was told to describe

    def remove_background(self, jpeg):
        return self.cutout

    def tidy_up(self, jpeg, part=None, key=None):
        self.keys.append(("tidy", key))
        self.tidied_parts.append(part)
        if self.error:
            raise self.error
        if self.fail_tidy:
            raise RuntimeError("Codex made no image")
        return png_bytes()

    def split_outfit(self, jpeg):
        return self.outfit

    on_white = staticmethod(ai.on_white)

    def try_on(self, selfie, garment, what, key=None):
        self.keys.append(("try on", key))
        self.tried_on = getattr(self, "tried_on", []) + [(selfie, garment, what)]
        if self.error:
            raise self.error
        return png_bytes(Image.new("RGB", (6, 8), "green"))

    def guess_details(self, jpegs, key=None, part=None):
        self.keys.append(("details", key))
        self.read_parts.append(part)
        self.reads.append(len(jpegs))
        if self.error:
            raise self.error
        if self.fail:
            raise RuntimeError("model down")
        return self.guesses

    def write_listing(self, jpegs, details, key=None):
        self.keys.append(("listing", key))
        if self.error:
            raise self.error
        self.listing_details = details
        self.listing_photos = len(jpegs)
        return {"title": "Navy wool jumper", "description": "Soft navy jumper.", "category": "Men > Clothing > Jumpers",
                "condition": "Very good", "parcel_size": "Small", "price_gbp": 18, "price_reason": "Plain wool, good nick."}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def client(store, pipeline):
    # Background jobs run straight away, so each response already shows their result.
    return TestClient(create_app(store, pipeline, run=lambda lane, fn, *args: fn(*args)))


def new_item(c, kind="photo"):
    res = c.post("/api/items", files={"photo": ("front.JPG", photo_bytes(), "image/jpeg")}, data={"kind": kind})
    assert res.status_code == 201
    return res.json()


def add_photo(c, item_id, kind="photo"):
    res = c.post(f"/api/items/{item_id}/photos", files={"photo": ("p.jpg", photo_bytes("red"), "image/jpeg")},
                 data={"kind": kind})
    assert res.status_code == 201
    return res.json()


def test_first_photo_starts_a_draft_and_all_its_work_at_once(store):
    pipeline = FakePipeline()
    item = new_item(client(store, pipeline))
    assert item["draft"] is True
    assert item["status"] == "ready"
    (photo,) = item["photos"]
    assert photo["original"].endswith(".jpg")
    assert photo["cutout_status"] == "ready" and photo["cutout"].endswith("-cutout.png")
    assert photo["tidy_status"] == "ready" and photo["tidy"].endswith("-tidy.png")
    assert (item["type"], item["colour"], item["fabric"], item["brand"]) == ("jumper", "navy", "wool", None)
    assert item["unchecked"] == ["type", "colour", "fabric"]
    assert pipeline.reads == [1]


def test_more_photos_then_done_reads_details_from_all_of_them(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    add_photo(c, item_id)
    item = add_photo(c, item_id, kind="label")
    # While still a draft, adding photos doesn't re-read the details.
    assert pipeline.reads == [1]
    back, label = item["photos"][1], item["photos"][2]
    assert back["tidy_status"] == "ready"
    assert label["kind"] == "label" and label["tidy"] is None and label["cutout"] is None

    pipeline.guesses = {"type": "jumper", "colour": "navy", "fabric": "80% wool, 20% nylon", "size": "M", "brand": "Uniqlo"}
    item = c.post(f"/api/items/{item_id}/finish").json()
    assert item["draft"] is False
    assert pipeline.reads == [1, 3]
    assert item["fabric"] == "80% wool, 20% nylon" and item["size"] == "M" and item["brand"] == "Uniqlo"
    assert set(item["unchecked"]) == {"type", "colour", "fabric", "size", "brand"}


def test_second_read_keeps_what_you_already_fixed(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    c.patch(f"/api/items/{item_id}", json={"colour": "midnight blue", "checked": ["type"]})
    add_photo(c, item_id)

    pipeline.guesses = {"type": "cardigan", "colour": "black", "fabric": "cotton"}
    item = c.post(f"/api/items/{item_id}/finish").json()
    assert item["type"] == "jumper"            # checked: kept
    assert item["colour"] == "midnight blue"   # edited: kept
    assert item["fabric"] == "cotton"          # still a guess: replaced
    assert item["unchecked"] == ["fabric"]


def test_one_photo_done_doesnt_read_again(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    c.post(f"/api/items/{item_id}/finish")
    assert pipeline.reads == [1]


def test_photo_added_to_a_finished_item_reads_again(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    c.post(f"/api/items/{item_id}/finish")
    add_photo(c, item_id, kind="label")
    assert pipeline.reads == [1, 2]


def test_cover_kind_and_delete_photo(store):
    c = client(store, FakePipeline())
    item_id = new_item(c)["id"]
    item = add_photo(c, item_id, kind="label")
    front, label = item["photos"]

    item = c.post(f"/api/photos/{label['id']}/cover").json()
    assert [p["id"] for p in item["photos"]] == [label["id"], front["id"]]

    # Marking the label as a view photo starts its cutout and tidy.
    item = c.patch(f"/api/photos/{label['id']}", json={"kind": "photo"}).json()
    assert item["photos"][0]["tidy_status"] == "ready"

    item = c.delete(f"/api/photos/{label['id']}").json()
    assert [p["id"] for p in item["photos"]] == [front["id"]]
    assert item["photos"][0]["position"] == 0
    assert not (store.images / label["original"]).exists()
    assert c.delete(f"/api/photos/{front['id']}").status_code == 400


def test_editing_or_confirming_clears_the_guess_flag(store):
    c = client(store, FakePipeline())
    item_id = new_item(c)["id"]

    item = c.patch(f"/api/items/{item_id}", json={"fabric": "cashmere", "amount_paid": 45.5}).json()
    assert item["fabric"] == "cashmere"
    assert item["amount_paid"] == 45.5
    assert item["unchecked"] == ["type", "colour"]

    item = c.patch(f"/api/items/{item_id}", json={"checked": ["type", "colour"]}).json()
    assert item["unchecked"] == []


def test_model_failure_keeps_the_photo_and_can_be_retried(store):
    pipeline = FakePipeline()
    pipeline.fail = True
    c = client(store, pipeline)
    item = new_item(c)
    assert item["status"] == "failed" and "model down" in item["error"]
    assert c.get(f"/images/{item['photos'][0]['original']}").status_code == 200

    pipeline.fail = False
    assert c.post(f"/api/items/{item['id']}/retry").json()["status"] == "ready"


def test_tidy_failure_is_shown_and_can_be_retried(store):
    pipeline = FakePipeline()
    pipeline.fail_tidy = True
    c = client(store, pipeline)
    photo = new_item(c)["photos"][0]
    assert photo["tidy_status"] == "failed" and "Codex made no image" in photo["error"]
    assert photo["cutout_status"] == "ready"

    pipeline.fail_tidy = False
    item = c.post(f"/api/photos/{photo['id']}/tidy").json()
    assert item["photos"][0]["tidy_status"] == "ready"


def test_delete_removes_item_and_all_its_files(store):
    c = client(store, FakePipeline())
    item_id = new_item(c)["id"]
    add_photo(c, item_id)
    assert c.delete(f"/api/items/{item_id}").status_code == 204
    assert c.get("/api/items").json() == []
    assert list(store.images.iterdir()) == []


def test_listing_uses_all_photos_and_checked_details(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    add_photo(c, item_id)
    c.patch(f"/api/items/{item_id}", json={"size": "M", "checked": ["type"]})

    item = c.post(f"/api/items/{item_id}/listing").json()
    assert item["listing"]["title"] == "Navy wool jumper"
    assert pipeline.listing_photos == 2
    assert pipeline.listing_details["size"] == "M"
    assert pipeline.listing_details["unchecked"] == ["colour", "fabric"]


def test_listing_failure_is_reported_not_saved(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    pipeline.write_listing = lambda jpegs, details, key: (_ for _ in ()).throw(ValueError("no JSON"))

    res = c.post(f"/api/items/{item_id}/listing")
    assert res.status_code == 502 and "no JSON" in res.json()["detail"]
    assert c.get(f"/api/items/{item_id}").json()["listing"] is None


def test_images_outside_the_folder_are_not_served(store):
    c = client(store, FakePipeline())
    assert c.get("/images/..%2Ftidyrack.db").status_code == 404


def test_one_photo_items_from_before_move_into_the_photos_table(tmp_path):
    db = sqlite3.connect(tmp_path / "tidyrack.db")
    db.execute(
        "create table items (id text primary key, added_at text not null, status text not null, error text,"
        " original text not null, cutout text, type text, colour text, fabric text, brand text,"
        " amount_paid real, notes text, unchecked text not null default '[]', tidy text, tidy_status text,"
        " tidy_error text, size text, listing text)"
    )
    db.execute("insert into items (id, added_at, status, original, cutout, tidy, tidy_status, type)"
               " values ('a', '2026-10-10', 'ready', 'a-original.jpg', 'a-cutout.png', 'a-tidy.png', 'ready', 'blazer')")
    db.execute("insert into items (id, added_at, status, original) values ('b', '2026-10-10', 'processing', 'b.jpg')")
    db.commit()
    db.close()

    store = Store(tmp_path)
    a = store.get("a")
    assert a["type"] == "blazer" and a["draft"] is False
    (photo,) = a["photos"]
    assert (photo["original"], photo["cutout"], photo["tidy"]) == ("a-original.jpg", "a-cutout.png", "a-tidy.png")
    assert photo["cutout_status"] == "ready" and photo["tidy_status"] == "ready"
    assert "original" not in a  # the old one-photo columns aren't shown
    # A job cut off by a restart shows as failed rather than spinning forever.
    assert store.get("b")["status"] == "failed"
    # Opening the store again doesn't add the photo twice.
    assert len(Store(tmp_path).get("a")["photos"]) == 1


def test_parse_guesses_handles_fences_and_nulls():
    text = '```json\n{"type": "Jeans", "colour": "blue", "fabric": "denim", "brand": null, "size": "32W"}\n```'
    assert ai.parse_guesses(text) == {"type": "Jeans", "colour": "blue", "fabric": "denim", "size": "32W"}
    assert ai.parse_guesses('{"type": "coat", "brand": "unknown"}') == {"type": "coat"}


def test_photo_steps_wait_their_turn_in_their_own_lane(store):
    jobs = []  # held back, to look at the photo while its jobs wait
    c = TestClient(create_app(store, FakePipeline(), run=lambda lane, fn, *args: jobs.append((lane, fn, args))))
    item = new_item(c)
    (photo,) = item["photos"]
    assert (photo["cutout_status"], photo["tidy_status"]) == ("queued", "queued")
    assert sorted(lane for lane, _, _ in jobs) == ["cutout", "details", "tidy"]
    # A queued tidy isn't queued twice.
    c.post(f"/api/photos/{photo['id']}/tidy")
    assert len(jobs) == 3
    for _, fn, args in jobs:
        fn(*args)
    photo = c.get(f"/api/items/{item['id']}").json()["photos"][0]
    assert (photo["cutout_status"], photo["tidy_status"]) == ("ready", "ready")
    # Jobs still waiting when the server stops show as failed after a restart.
    store.photo_step(photo["id"], "tidy", "queued")
    assert Store(store.root).get_photo(photo["id"])["tidy_status"] == "failed"


def test_export_zip_matches_the_store(store):
    import csv
    import json
    import zipfile
    from datetime import date

    c = client(store, FakePipeline())
    jumper = new_item(c)
    add_photo(c, jumper["id"])
    add_photo(c, jumper["id"], kind="label")
    c.post(f"/api/items/{jumper['id']}/finish")
    c.patch(f"/api/items/{jumper['id']}", json={"amount_paid": 12.5, "notes": 'Café, "good" one'})
    c.post(f"/api/items/{jumper['id']}/listing")
    new_item(c)

    res = c.get("/api/export")
    assert res.status_code == 200
    assert f"tidyrack-{date.today().isoformat()}.zip" in res.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert zf.testzip() is None

    items = store.all()
    assert json.loads(zf.read("items.json")) == items
    rows = list(csv.DictReader(io.StringIO(zf.read("items.csv").decode("utf-8-sig"))))
    assert [r["id"] for r in rows] == [i["id"] for i in items]
    row = next(r for r in rows if r["id"] == jumper["id"])
    assert row["notes"] == 'Café, "good" one' and row["amount_paid"] == "12.5"
    assert row["type"] == "jumper" and row["listing_title"] == "Navy wool jumper"
    assert len(row["photos"].split("; ")) == 2 and len(row["labels"].split("; ")) == 1
    assert len(row["cutouts"].split("; ")) == 2 and len(row["tidied"].split("; ")) == 2

    # Every picture is in the zip, byte for byte, and every name in the CSV is a file in the zip.
    pictures = {p[step] for i in items for p in i["photos"] for step in ("original", "cutout", "tidy") if p[step]}
    images = {n for n in zf.namelist() if n.startswith("images/")}
    assert images == {f"images/{name}" for name in pictures}
    for name in pictures:
        assert zf.read(f"images/{name}") == (store.images / name).read_bytes()
    named = {n for r in rows for col in ("photos", "labels", "cutouts", "tidied") for n in r[col].split("; ") if n}
    assert named == images


def test_export_leaves_out_a_picture_missing_from_disk(store):
    import zipfile

    c = client(store, FakePipeline())
    photo = new_item(c)["photos"][0]
    (store.images / photo["tidy"]).unlink()
    zf = zipfile.ZipFile(io.BytesIO(c.get("/api/export").content))
    assert f"images/{photo['tidy']}" not in zf.namelist()
    assert photo["tidy"] not in zf.read("items.csv").decode() + zf.read("items.json").decode()
    assert f"images/{photo['original']}" in zf.namelist()


NAVY, CREAM, BLACK = (0, 0, 128), (240, 230, 210), (20, 20, 20)


def close(hex_code, rgb, within=3):
    return colour.delta_e(colour.hex_to_oklab(hex_code), colour.rgb_to_oklab(np.array([rgb], dtype=float))[0]) < within


def test_colour_of_a_solid_cutout():
    (main,) = colour.main_colours(cutout(NAVY))
    assert close(main, NAVY)


def test_two_tone_cutout_gives_two_colours():
    hexes = colour.main_colours(cutout(CREAM, BLACK))
    assert len(hexes) == 2
    assert {close(h, CREAM) for h in hexes} == {True, False}
    assert {close(h, BLACK) for h in hexes} == {True, False}


def test_see_through_pixels_are_ignored():
    image = Image.open(io.BytesIO(cutout(NAVY))).convert("RGBA")
    # Background half-see-through at the edges still doesn't count; a fully see-through cutout has no colour.
    image.paste((255, 255, 0, 120), (0, 0, 200, 40))
    assert [close(h, NAVY) for h in colour.main_colours(png_bytes(image))] == [True]
    assert colour.main_colours(png_bytes()) == []


def test_colour_is_worked_out_when_the_cutout_is_ready_and_a_hand_picked_one_is_kept(store):
    import csv
    import zipfile

    pipeline = FakePipeline()
    pipeline.cutout = cutout(NAVY)
    c = client(store, pipeline)
    item = new_item(c)
    assert close(item["colour_hex"], NAVY) and item["colour_hex_2"] is None
    assert item["colour_hex_set_by_hand"] is False

    assert c.patch(f"/api/items/{item['id']}", json={"colour_hex": "not a colour"}).status_code == 422
    item = c.patch(f"/api/items/{item['id']}", json={"colour_hex": "#1A2B3C"}).json()
    assert item["colour_hex"] == "#1a2b3c" and item["colour_hex_set_by_hand"] is True

    # A new cover with another colour doesn't overwrite the hand-picked one.
    pipeline.cutout = cutout(CREAM, BLACK)
    back = add_photo(c, item["id"])["photos"][1]
    item = c.post(f"/api/photos/{back['id']}/cover").json()
    colour.refresh(store, item["id"])
    assert store.get(item["id"])["colour_hex"] == "#1a2b3c"

    zf = zipfile.ZipFile(io.BytesIO(c.get("/api/export").content))
    (row,) = csv.DictReader(io.StringIO(zf.read("items.csv").decode("utf-8-sig")))
    assert row["colour_hex"] == "#1a2b3c" and row["colour_hex_2"] == ""


def test_new_cover_recomputes_and_backfill_fills_older_items(store):
    pipeline = FakePipeline()
    pipeline.cutout = cutout(NAVY)
    c = client(store, pipeline)
    item_id = new_item(c)["id"]
    pipeline.cutout = cutout(CREAM, BLACK)
    back = add_photo(c, item_id)["photos"][1]
    item = c.post(f"/api/photos/{back['id']}/cover").json()
    assert item["colour_hex"] and item["colour_hex_2"]  # the new cover is two-tone

    # An item from before colours were measured: backfill measures it, once.
    store.save_colour(item_id, [])
    with sqlite3.connect(store.db_path) as db:
        db.execute("update items set colours = null")
    assert store.get(item_id)["colour_hex"] is None
    assert colour.backfill(store) == 1
    assert store.get(item_id)["colour_hex_2"] is not None
    assert [c["share"] for c in store.get(item_id)["colours"]["colours"]] == [0.5, 0.5]
    assert colour.backfill(store) == 0


def shop_scene(rgb, room_rgb=(180, 175, 165), kelvin=6504, size=(300, 400)):
    """A phone photo of a garment of this colour against a wall, under a light of
    this colour, and the garment's cutout trimmed to its edges, as rembg gives it."""
    w, h = size
    scene = np.empty((h, w, 3))
    scene[:] = colour.linear(np.array(room_rgb, float))
    scene[h // 4:h * 3 // 4, w // 4:w * 3 // 4] = colour.linear(np.array(rgb, float))
    tint = colour.light_rgb(kelvin) / colour.light_rgb(6504)
    photo = Image.fromarray(colour.encoded(scene * tint / (colour._TO_XYZ[1] @ tint)).round().astype(np.uint8))
    out = io.BytesIO()
    photo.save(out, "JPEG", quality=95)
    garment = photo.convert("RGBA").crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4))
    return out.getvalue(), png_bytes(garment)


def test_cutout_is_found_in_its_photo():
    photo, cut = shop_scene(NAVY)
    image = Image.open(io.BytesIO(photo)).convert("RGB")
    assert colour.locate(image, Image.open(io.BytesIO(cut))) == (75, 100)
    # A cutout that isn't from this photo isn't placed anywhere.
    assert colour.locate(image, Image.open(io.BytesIO(cutout((200, 30, 30), size=100)))) is None


def test_room_light_is_taken_out():
    photo, cut = shop_scene(NAVY, kelvin=2700)
    before = colour.measure(cut)
    after = colour.measure(cut, photo)
    truth = colour.rgb_to_oklab(np.array([NAVY], float))[0]
    far = colour.delta_e(colour.hex_to_oklab(before["colours"][0]["hex"]), truth)
    near = colour.delta_e(colour.hex_to_oklab(after["colours"][0]["hex"]), truth)
    assert before["kelvin"] is None and after["kelvin"] < 3500
    assert far > 10 and near < far / 2
    # In daylight there's next to nothing to take out.
    photo, cut = shop_scene(NAVY)
    assert close(colour.measure(cut, photo)["colours"][0]["hex"], NAVY, within=3)


# The public copy: people sign in, and each sees only their own items.

SECRET = "kUjF1mwuT9dK8sM4R2a7mDq5yV0cXe3bN6hZ1pW9sLo="  # gitleaks:allow (made for the tests, used nowhere else)


def public_client(store, pipeline, exchange=None):
    """The public copy, over https so its secure cookies are sent."""
    app = create_app(store, pipeline, run=lambda lane, fn, *args: fn(*args), public=True, secret=SECRET,
                     **({"exchange": exchange} if exchange else {}))
    return TestClient(app, base_url="https://testserver")


def signed_in(store, pipeline, openrouter_id):
    """A client for the public copy, signed in as a new user."""
    from tidyrack import auth

    c = public_client(store, pipeline)
    user = store.save_user(openrouter_id, auth.Keys(SECRET).lock(f"sk-or-{openrouter_id}"))
    c.cookies.set("tidyrack_session", store.new_session(user))
    return c


def export_rows(c):
    import csv
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(c.get("/api/export").content))
    return list(csv.DictReader(io.StringIO(zf.read("items.csv").decode("utf-8-sig"))))


def test_public_copy_needs_sign_in(store):
    c = public_client(store, FakePipeline())
    assert c.get("/api/me").json() == {"public": True, "signed_in": False, "old_key_url": None}
    assert c.get("/api/items").status_code == 401
    assert c.post("/api/items", files={"photo": ("f.jpg", photo_bytes(), "image/jpeg")}).status_code == 401
    assert c.get("/api/export").status_code == 401
    c.cookies.set("tidyrack_session", "made-up")
    assert c.get("/api/items").status_code == 401
    assert c.get("/").status_code == 200  # the page itself loads, to show the sign-in button
    assert c.get("/api/phone").status_code == 404


def test_users_see_only_their_own_items(store):
    pipeline = FakePipeline()
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    item = new_item(alice)
    photo = add_photo(alice, item["id"])["photos"][1]
    alice.post(f"/api/items/{item['id']}/finish")
    picture = store.get(item["id"])["photos"][0]

    assert [i["id"] for i in alice.get("/api/items").json()] == [item["id"]]
    assert bob.get("/api/items").json() == []
    # Every way in to Alice's item or photos is closed to Bob, as if it didn't exist.
    for method, path in [
        ("GET", f"/api/items/{item['id']}"), ("PATCH", f"/api/items/{item['id']}"),
        ("POST", f"/api/items/{item['id']}/finish"), ("POST", f"/api/items/{item['id']}/retry"),
        ("POST", f"/api/items/{item['id']}/listing"), ("DELETE", f"/api/items/{item['id']}"),
        ("PATCH", f"/api/photos/{photo['id']}"), ("POST", f"/api/photos/{photo['id']}/cover"),
        ("POST", f"/api/photos/{photo['id']}/tidy"), ("DELETE", f"/api/photos/{photo['id']}"),
        ("GET", f"/images/{picture['original']}"), ("GET", f"/images/{picture['cutout']}"),
    ]:
        body = {"kind": "label"} if method == "PATCH" else None
        res = bob.request(method, path, json=body)
        assert res.status_code == 404, (method, path, res.status_code)
    res = bob.post(f"/api/items/{item['id']}/photos", files={"photo": ("b.jpg", photo_bytes(), "image/jpeg")})
    assert res.status_code == 404
    after = store.get(item["id"])
    assert len(after["photos"]) == 2 and after["photos"][1]["kind"] == "photo"  # nothing Bob tried changed it

    assert alice.get(f"/images/{picture['original']}").status_code == 200
    assert "private" in alice.get(f"/images/{picture['cutout']}").headers["cache-control"]
    assert export_rows(bob) == []
    assert [r["id"] for r in export_rows(alice)] == [item["id"]]


def test_personal_copy_items_stay_out_of_the_public_copy(store):
    pipeline = FakePipeline()
    own = new_item(client(store, pipeline))  # made in the personal copy: no owner
    alice = signed_in(store, pipeline, "user_alice")
    assert alice.get("/api/items").json() == []
    assert alice.get(f"/api/items/{own['id']}").status_code == 404
    assert alice.get(f"/images/{own['photos'][0]['original']}").status_code == 404
    assert [i["id"] for i in client(store, pipeline).get("/api/items").json()] == [own["id"]]


def test_sign_in_with_openrouter(store):
    from urllib.parse import parse_qs, urlparse

    from tidyrack import auth

    exchanged = []

    def fake_exchange(code, verifier):
        exchanged.append((code, verifier))
        return "sk-or-v1-alice", "user_alice"

    c = public_client(store, FakePipeline(), fake_exchange)
    res = c.get("/auth/openrouter", follow_redirects=False)
    assert res.status_code == 303
    sent = urlparse(res.headers["location"])
    query = {k: v[0] for k, v in parse_qs(sent.query).items()}
    assert f"{sent.scheme}://{sent.netloc}{sent.path}" == "https://openrouter.ai/auth"
    assert query["callback_url"] == "https://testserver/auth/callback"
    assert query["code_challenge_method"] == "S256"

    # A callback whose state doesn't match this browser's sign-in is turned away.
    res = c.get("/auth/callback", params={"code": "abc", "state": "forged"}, follow_redirects=False)
    assert "sign_in_error" in res.headers["location"] and exchanged == []

    res = c.get("/auth/openrouter", follow_redirects=False)  # start again: the failed try cleared the cookie
    query = {k: v[0] for k, v in parse_qs(urlparse(res.headers["location"]).query).items()}
    res = c.get("/auth/callback", params={"code": "abc", "state": query["state"]}, follow_redirects=False)
    assert res.headers["location"] == "/"
    ((code, verifier),) = exchanged
    assert code == "abc" and auth.challenge(verifier) == query["code_challenge"]
    assert c.get("/api/me").json()["signed_in"] is True
    assert c.get("/api/items").status_code == 200

    # The key is stored encrypted, and can be read back with the secret.
    with sqlite3.connect(store.db_path) as db:
        (locked,) = db.execute("select key from users").fetchone()
    assert "sk-or-v1-alice" not in locked
    assert auth.Keys(SECRET).unlock(locked) == "sk-or-v1-alice"

    assert c.post("/api/sign-out").status_code == 204
    assert c.get("/api/items").status_code == 401


def test_signing_in_again_points_to_the_old_key_to_delete(store):
    import hashlib
    from urllib.parse import parse_qs, urlparse

    keys = iter(["sk-or-v1-first", "sk-or-v1-second"])
    c = public_client(store, FakePipeline(), lambda code, verifier: (next(keys), "user_alice"))

    def sign_in():
        res = c.get("/auth/openrouter", follow_redirects=False)
        state = parse_qs(urlparse(res.headers["location"]).query)["state"][0]
        c.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False)

    sign_in()
    assert c.get("/api/me").json()["old_key_url"] is None  # a first sign-in has nothing old
    sign_in()
    old = hashlib.sha256(b"sk-or-v1-first").hexdigest()
    assert c.get("/api/me").json()["old_key_url"] == f"https://openrouter.ai/keys/{old}"
    assert c.post("/api/old-key/done").status_code == 204
    assert c.get("/api/me").json()["old_key_url"] is None


def test_sign_in_problems_go_back_to_the_page_with_a_message(store):
    from tidyrack import auth

    def refused(code, verifier):
        raise auth.SignInError("refused")

    c = public_client(store, FakePipeline(), refused)
    res = c.get("/auth/callback", follow_redirects=False)  # the person said no at OpenRouter
    assert res.status_code == 303 and "cancelled" in res.headers["location"]
    c.get("/auth/openrouter", follow_redirects=False)
    state = c.cookies.get("tidyrack_signin").strip('"').split(".")[0]
    res = c.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert res.headers["location"] == "/?sign_in_error=refused"
    assert c.get("/api/me").json()["signed_in"] is False


def test_model_calls_run_on_the_owners_own_key(store):
    pipeline = FakePipeline()
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    item = new_item(alice)
    alice.post(f"/api/items/{item['id']}/listing")
    new_item(bob)
    assert pipeline.keys == [("tidy", "sk-or-user_alice"), ("details", "sk-or-user_alice"),
                             ("listing", "sk-or-user_alice"),
                             ("tidy", "sk-or-user_bob"), ("details", "sk-or-user_bob")]

    pipeline.keys = []
    new_item(client(store, pipeline))  # the personal copy: Codex, no key
    assert pipeline.keys == [("tidy", None), ("details", None)]


def test_a_dead_key_says_what_to_do(store):
    pipeline = FakePipeline()
    alice = signed_in(store, pipeline, "user_alice")
    pipeline.error = ai.ModelError("Your OpenRouter key no longer works. Sign out and sign in again.")
    item = new_item(alice)
    assert item["status"] == "failed"
    assert item["error"] == "Your OpenRouter key no longer works. Sign out and sign in again."
    assert item["photos"][0]["error"] == item["error"]
    res = alice.post(f"/api/items/{item['id']}/listing")
    assert res.status_code == 502 and "Sign out and sign in again" in res.json()["detail"]


def test_a_key_that_cant_be_read_asks_to_sign_in_again(store):
    pipeline = FakePipeline()
    alice = signed_in(store, pipeline, "user_alice")
    with sqlite3.connect(store.db_path) as db:
        db.execute("update users set key = 'locked-with-an-old-secret'")
    item = new_item(alice)
    assert item["status"] == "failed" and "Sign out and sign in again" in item["error"]
    assert pipeline.keys == []  # no model was called without a key


@pytest.mark.parametrize("status, body, says", [
    (401, {"error": {"code": 401, "message": "User not found."}}, "no longer works"),
    (402, {"error": {"code": 402, "message": "Insufficient credits"}}, "out of credit"),
    (403, {"error": {"code": 403, "message": "Key limit exceeded", "metadata": {"limit_source": "openrouter_key_limit"}}},
     "out of credit"),
    (200, {"error": {"code": 402, "message": "Insufficient credits"}}, "out of credit"),  # an error inside a 200
])
def test_openrouter_errors_become_plain_messages(monkeypatch, status, body, says):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(status, json=body))
    with pytest.raises(ai.ModelError, match=says):
        ai.guess_details([photo_bytes()], key="sk-or-x")


def test_tidy_through_openrouter_sends_the_photo_and_returns_a_png(monkeypatch):
    import base64

    import httpx

    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, json=json)
        jpeg = io.BytesIO()
        Image.new("RGB", (8, 8), "grey").save(jpeg, "JPEG")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(jpeg.getvalue()).decode()}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    png = ai.tidy_up(photo_bytes(), key="sk-or-x")
    assert png.startswith(b"\x89PNG")
    assert sent["url"] == "https://openrouter.ai/api/v1/images"
    assert sent["headers"]["Authorization"] == "Bearer sk-or-x"
    assert sent["json"]["input_references"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_try_on_through_openrouter_sends_the_selfie_then_the_garment(monkeypatch):
    import base64

    import httpx

    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, json=json)
        jpeg = io.BytesIO()
        Image.new("RGB", (8, 8), "grey").save(jpeg, "JPEG")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(jpeg.getvalue()).decode()}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    selfie, garment = photo_bytes(), png_bytes()
    png = ai.try_on(selfie, garment, "a brown corduroy jacket", key="sk-or-x")
    assert png.startswith(b"\x89PNG")
    refs = [r["image_url"]["url"] for r in sent["json"]["input_references"]]
    assert refs == ["data:image/jpeg;base64," + base64.b64encode(b).decode() for b in (selfie, garment)]
    assert "a brown corduroy jacket" in sent["json"]["prompt"]


def test_public_uploads_must_be_photos(store):
    alice = signed_in(store, FakePipeline(), "user_alice")
    for name, data in [("page.html", b"<script>alert(1)</script>"), ("x.jpg", b"<html>not a photo</html>"),
                       ("photo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")]:
        res = alice.post("/api/items", files={"photo": (name, data, "image/jpeg")})
        assert res.status_code == 400, name
    assert alice.post("/api/items", files={"photo": ("ok.png", png_bytes(), "image/png")}).status_code == 201
    assert len(store.all(everyone=True)) == 1


def test_public_uploads_have_a_size_and_daily_limit(store, monkeypatch):
    from tidyrack import app as app_module

    alice = signed_in(store, FakePipeline(), "user_alice")
    monkeypatch.setattr(app_module, "MAX_UPLOAD", 500)
    res = alice.post("/api/items", files={"photo": ("big.png", png_bytes(Image.new("RGB", (300, 300), "red")) + b"0" * 600,
                                                    "image/png")})
    assert res.status_code == 413

    monkeypatch.setattr(app_module, "MAX_UPLOAD", 25_000_000)
    monkeypatch.setattr(app_module, "DAILY_PHOTOS", 3)  # the too-big try above counts as one
    item = new_item(alice)
    alice.delete(f"/api/photos/{add_photo(alice, item['id'])['photos'][1]['id']}")  # deleting doesn't give it back
    res = alice.post(f"/api/items/{item['id']}/photos", files={"photo": ("c.jpg", photo_bytes(), "image/jpeg")})
    assert res.status_code == 429 and "Try again tomorrow" in res.json()["detail"]
    assert alice.post("/api/items/from-link", json={"url": "https://shop.example/p"}).status_code == 429
    bob = signed_in(store, FakePipeline(), "user_bob")
    new_item(bob)  # the limit is per person


def test_photos_that_would_decode_huge_are_refused(store):
    # A plain 9000x9000 PNG compresses to a few kB but needs ~240 MB to open.
    out = io.BytesIO()
    Image.new("L", (9000, 9000)).save(out, "PNG")
    assert len(out.getvalue()) < 1_000_000
    alice = signed_in(store, FakePipeline(), "user_alice")
    res = alice.post("/api/items", files={"photo": ("bomb.png", out.getvalue(), "image/png")})
    assert res.status_code == 400 and "megapixels" in res.json()["detail"]
    gif = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif, "GIF")
    assert alice.post("/api/items", files={"photo": ("x.jpg", gif.getvalue(), "image/jpeg")}).status_code == 400


def test_uploads_from_strangers_are_turned_away_before_they_are_read(store):
    c = public_client(store, FakePipeline())
    # Without signing in: refused at once, so a huge body is never written to disk.
    many = [("photo", (f"{n}.jpg", b"x", "image/jpeg")) for n in range(1001)]
    assert c.post("/api/items", files=many).status_code == 401
    alice = signed_in(store, FakePipeline(), "user_alice")
    res = alice.post("/api/items", content=b"x", headers={"content-length": "999999999", "content-type": "image/jpeg"})
    assert res.status_code == 413


def test_parallel_uploads_cant_beat_the_daily_limit(store):
    from concurrent.futures import ThreadPoolExecutor

    user = store.save_user("user_alice", "x")
    with ThreadPoolExecutor(20) as pool:
        taken = list(pool.map(lambda _: store.take_upload_slot(user, 5, hours=24), range(50)))
    assert taken.count(True) == 5


def test_one_link_at_a_time_per_person(store):
    import threading

    pipeline = FakePipeline()
    started, release = threading.Event(), threading.Event()

    def slow_link(url):
        started.set()
        release.wait(5)
        raise shoplink.ShopLinkError("blocked")

    from tidyrack import shoplink

    app = create_app(store, pipeline, run=lambda lane, fn, *args: fn(*args), public=True, secret=SECRET,
                     read_link=slow_link)
    alice = TestClient(app, base_url="https://testserver")
    alice.cookies = signed_in(store, pipeline, "user_alice").cookies
    first = threading.Thread(target=lambda: alice.post("/api/items/from-link", json={"url": "https://a.example/"}))
    first.start()
    started.wait(5)
    res = alice.post("/api/items/from-link", json={"url": "https://b.example/"})
    release.set()
    first.join()
    assert res.status_code == 429 and "still being read" in res.json()["detail"]
    assert alice.post("/api/items/from-link", json={"url": "https://c.example/"}).status_code == 422  # free again


def test_personal_copy_has_no_sign_in(store):
    c = client(store, FakePipeline())
    assert c.get("/api/me").json() == {"public": False, "signed_in": True, "old_key_url": None}
    assert c.get("/auth/openrouter", follow_redirects=False).status_code == 404
    assert c.post("/api/sign-out").status_code == 404


def test_using_the_app_keeps_you_signed_in(store):
    from datetime import datetime, timedelta, timezone

    c = signed_in(store, FakePipeline(), "user_alice")
    with sqlite3.connect(store.db_path) as db:  # signed in 89 days ago
        db.execute("update sessions set added_at = ?",
                   ((datetime.now(timezone.utc) - timedelta(days=89)).isoformat(timespec="seconds"),))
    res = c.get("/api/me")
    assert res.json()["signed_in"] is True
    assert "max-age=7776000" in res.headers["set-cookie"].lower()  # the cookie's 90 days start again
    with sqlite3.connect(store.db_path) as db:
        (added,) = db.execute("select added_at from sessions").fetchone()
    assert datetime.fromisoformat(added) > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_old_sessions_expire(store):
    user = store.save_user("user_alice", "x")
    token = store.new_session(user)
    with sqlite3.connect(store.db_path) as db:
        db.execute("update sessions set added_at = '2020-01-01T00:00:00+00:00'")
    assert store.session_user(token) is None


def test_signing_in_again_finds_the_same_user(store):
    first = store.save_user("user_alice", "sk-or-old")
    assert store.save_user("user_alice", "sk-or-new") == first
    assert store.user_key(first) == "sk-or-new"
    token = store.new_session(first)
    assert store.session_user(token) == first
    store.end_session(token)
    assert store.session_user(token) is None


def test_delete_my_account_removes_only_that_persons_data(store):
    from tidyrack import auth

    pipeline = FakePipeline()
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    for c in (alice, bob):
        add_photo(c, new_item(c)["id"], kind="label")
        new_item(c)
    alice_id, bob_id = (store.session_user(c.cookies["tidyrack_session"]) for c in (alice, bob))
    store.set_old_key_hash(alice_id, "oldhash")
    alice_files = {p[s] for i in store.all(alice_id) for p in i["photos"] for s in ("original", "cutout", "tidy")}
    bob_items = bob.get("/api/items").json()
    bob_files = {p[s] for i in bob_items for p in i["photos"] for s in ("original", "cutout", "tidy")} - {None}
    assert len(alice_files - {None}) == 7 and len(bob_files) == 7  # each: photo + cutout + tidy, a label, again

    token = alice.cookies["tidyrack_session"]
    res = alice.delete("/api/account")
    assert res.status_code == 200
    # Her keys at OpenRouter are hers to delete, so she's sent to each: this one and the one it replaced.
    assert res.json()["key_urls"] == [f"https://openrouter.ai/keys/{auth.key_hash('sk-or-user_alice')}",
                                      "https://openrouter.ai/keys/oldhash"]
    assert 'tidyrack_session=""' in res.headers["set-cookie"]  # signed out

    on_disk = {f.name for f in store.images.iterdir()}
    assert not alice_files & on_disk
    assert store.all(alice_id) == [] and store.key_of("user_alice") is None
    with sqlite3.connect(store.db_path) as db:
        for table, column in (("users", "id"), ("sessions", "user_id"), ("uploads", "user_id")):
            assert db.execute(f"select count(*) from {table} where {column} = ?", (alice_id,)).fetchone() == (0,)
    alice.cookies.set("tidyrack_session", token)  # her old cookie no longer signs anyone in
    assert alice.get("/api/items").status_code == 401

    # Bob's items, pictures, key and sign-in are all as they were.
    assert bob.get("/api/items").json() == bob_items
    assert bob_files <= on_disk
    assert store.key_of("user_bob") is not None and store.session_user(bob.cookies["tidyrack_session"]) == bob_id
    assert bob.get(f"/images/{bob_items[0]['photos'][0]['original']}").status_code == 200


def test_delete_my_account_needs_sign_in_and_the_public_copy(store):
    assert public_client(store, FakePipeline()).delete("/api/account").status_code == 401
    c = client(store, FakePipeline())
    item_id = new_item(c)["id"]
    assert c.delete("/api/account").status_code == 404
    assert store.get(item_id) is not None  # the personal copy keeps everything


def test_a_picture_finished_after_its_photo_was_deleted_leaves_no_file(store):
    item = store.create(photo_bytes(), ".jpg")
    photo_id = item["photos"][0]["id"]
    store.delete(item["id"])
    store.save_photo_image(photo_id, "tidy", png_bytes())  # a tidy that was still running
    assert list(store.images.iterdir()) == []


def test_public_copy_sends_security_headers_on_every_response(store):
    c = public_client(store, FakePipeline())
    for res in (c.get("/"), c.get("/privacy.html"), c.get("/api/me"), c.get("/api/items"),
                c.post("/api/items"), c.get("/images/nope.png")):
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"], res.request.url
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["strict-transport-security"].startswith("max-age=")
        assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in res.headers["permissions-policy"]
    personal = client(store, FakePipeline()).get("/")
    assert "content-security-policy" not in personal.headers  # the personal copy is as it was


def test_privacy_page_is_linked_from_the_page(store):
    c = public_client(store, FakePipeline())
    assert 'href="/privacy.html"' in c.get("/").text
    page = c.get("/privacy.html").text
    for words in ("Railway", "OpenRouter", "Delete my account", "encrypted"):
        assert words in page


SHADED_CREAM = (152, 138, 126)  # cream trousers in room shade, as the clothing model's cutout gives them


def add_outfit(c):
    return c.post("/api/items/from-outfit", files={"photo": ("me.jpg", photo_bytes(), "image/jpeg")})


def test_outfit_photo_makes_one_item_per_garment_with_colour_from_the_tidy(store):
    pipeline = FakePipeline()
    pipeline.outfit = [{"part": "top", "photo": photo_bytes("black"), "cutout": cutout(BLACK)},
                       {"part": "bottoms", "photo": photo_bytes("white"), "cutout": cutout(SHADED_CREAM)}]
    pipeline.cutout = cutout(CREAM)  # the tidied pictures, cut out (the fake gives every one this)
    c = client(store, pipeline)
    res = add_outfit(c)
    assert res.status_code == 201
    top, bottoms = res.json()

    for item, part in ((top, "top"), (bottoms, "bottoms")):
        (photo,) = item["photos"]
        assert photo["kind"] == "worn" and photo["part"] == part
        assert photo["cutout_status"] == "ready" and photo["tidy_status"] == "ready"
        assert item["draft"] is False and item["type"] == "jumper"
        # The cutout is the clothing model's, not a background removal of the crop.
        assert store.image_path(photo["cutout"]).read_bytes() != pipeline.cutout
    assert pipeline.tidied_parts == ["top", "bottoms"]
    # The tidy's colour wins over the shaded cutout's, and a later recompute leaves it.
    assert close(bottoms["colour_hex"], CREAM)
    colour.refresh(store, bottoms["id"])
    colour.backfill(store)
    assert close(store.get(bottoms["id"])["colour_hex"], CREAM)

    # Tidy again keeps drawing only that garment.
    c.post(f"/api/photos/{bottoms['photos'][0]['id']}/tidy")
    assert pipeline.tidied_parts[-1] == "bottoms"


def test_outfit_colour_stays_the_cutouts_when_the_tidy_fails(store):
    pipeline = FakePipeline()
    pipeline.outfit = [{"part": "bottoms", "photo": photo_bytes("white"), "cutout": cutout(SHADED_CREAM)}]
    pipeline.fail_tidy = True
    (item,) = add_outfit(client(store, pipeline)).json()
    assert item["photos"][0]["tidy_status"] == "failed"
    assert close(item["colour_hex"], SHADED_CREAM)


def test_outfit_photo_with_no_clothes_or_a_failed_split_adds_nothing(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    res = add_outfit(c)
    assert res.status_code == 422 and "No clothes" in res.json()["detail"]
    pipeline.split_outfit = lambda jpeg: 1 / 0
    assert add_outfit(c).status_code == 502
    assert store.all() == []


def test_outfits_in_the_public_copy_belong_to_whoever_added_them(store):
    pipeline = FakePipeline()
    pipeline.outfit = [{"part": "top", "photo": photo_bytes("black"), "cutout": cutout(BLACK)}]
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    (item,) = add_outfit(alice).json()
    assert [i["id"] for i in alice.get("/api/items").json()] == [item["id"]]
    assert bob.get("/api/items").json() == []
    assert bob.get(f"/api/items/{item['id']}").status_code == 404
    assert ("tidy", "sk-or-user_alice") in pipeline.keys and pipeline.tidied_parts == ["top"]
    assert pipeline.read_parts == ["top"]  # the details reader is told which garment, too
    assert public_client(store, pipeline).post(
        "/api/items/from-outfit", files={"photo": ("me.jpg", photo_bytes(), "image/jpeg")}).status_code == 401


def test_specks_of_a_garment_dont_stretch_its_crop_or_count_as_one():
    # A top as one solid patch, with its specks scattered down the trousers below,
    # and "bottoms" found only as specks: the case that made two trousers on 14 Oct.
    top, bottoms = np.zeros((400, 300), np.uint8), np.zeros((400, 300), np.uint8)
    top[40:120, 60:240] = 255
    for y in range(150, 400, 25):
        top[y:y + 2, 100:102] = 255
        bottoms[y:y + 3, 200:203] = 255
    kept = ai.main_patches(Image.fromarray(top))
    assert kept.getbbox() == (60, 40, 240, 120)
    assert ai.main_patches(Image.fromarray(bottoms)).getbbox() is not None  # specks alone are kept...
    assert np.count_nonzero(np.array(ai.main_patches(Image.fromarray(bottoms)))) < 0.01 * 400 * 300  # ...but too few to count


def test_details_of_a_worn_garment_name_the_part():
    sent = []
    ai_details = ai.guess_details_codex
    try:
        ai.guess_details_codex = lambda jpegs, prompt: sent.append(prompt) or {}
        ai.guess_details([b""], part="top")
        ai.guess_details([b""])
    finally:
        ai.guess_details_codex = ai_details
    assert "Describe only the top they are wearing" in sent[0]
    assert "wearing an outfit" not in sent[1]


def test_worn_photos_cant_be_uploaded_or_set_by_hand(store):
    c = client(store, FakePipeline())
    res = c.post("/api/items", files={"photo": ("p.jpg", photo_bytes(), "image/jpeg")}, data={"kind": "worn"})
    assert res.status_code == 400
    photo = new_item(c)["photos"][0]
    assert c.patch(f"/api/photos/{photo['id']}", json={"kind": "worn"}).status_code == 400


# On me: an item drawn onto one of the person's own Outfit photos.

def outfit_item(c, pipeline, colour_name="black"):
    """Add an Outfit photo of one garment; returns its item and the whole photo as kept."""
    pipeline.outfit = [{"part": "top", "photo": photo_bytes(colour_name), "cutout": cutout(BLACK)}]
    (item,) = add_outfit(c).json()
    return item


def test_outfit_photo_is_kept_whole_for_on_me(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    outfit_item(c, pipeline)
    (selfie,) = c.get("/api/selfies").json()
    assert c.get(f"/images/{selfie['original']}").content == ai.upright_jpeg(photo_bytes())


def test_on_me_without_an_outfit_photo_says_how_to_make_one(store):
    c = client(store, FakePipeline())
    res = c.post(f"/api/items/{new_item(c)['id']}/tryons", json={})
    assert res.status_code == 409 and "Tap Outfit" in res.json()["detail"]


def test_on_me_draws_the_item_on_the_newest_outfit_photo_or_a_picked_one(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    outfit_item(c, pipeline)
    item = new_item(c)  # a navy wool jumper, by the fake details
    pipeline.outfit = [{"part": "top", "photo": photo_bytes(), "cutout": cutout(BLACK)}]
    add_outfit(c)
    newest, older = c.get("/api/selfies").json()

    after = c.post(f"/api/items/{item['id']}/tryons", json={}).json()
    (tryon,) = after["tryons"]
    assert tryon["status"] == "ready" and tryon["selfie_id"] == newest["id"]
    assert c.get(f"/images/{tryon['image']}").status_code == 200
    selfie, garment, what = pipeline.tried_on[-1]
    assert selfie == (store.images / newest["original"]).read_bytes()
    assert garment == ai.on_white(pipeline.cutout)  # the cover's cutout: real pixels, on white
    assert what == "navy jumper"
    # It's a model's picture, so it never joins the item's photos (and so never Save photos or Vinted).
    assert after["photos"] == c.get(f"/api/items/{item['id']}").json()["photos"] == item["photos"]

    picked = c.post(f"/api/items/{item['id']}/tryons", json={"selfie_id": older["id"]}).json()["tryons"][0]
    assert picked["selfie_id"] == older["id"]
    assert c.post(f"/api/items/{item['id']}/tryons", json={"selfie_id": "nope"}).status_code == 404


def test_a_try_on_can_be_deleted_and_goes_with_its_item(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item = outfit_item(c, pipeline)
    first, second = (c.post(f"/api/items/{item['id']}/tryons", json={}).json()["tryons"][0] for _ in range(2))
    after = c.delete(f"/api/tryons/{first['id']}").json()
    assert [t["id"] for t in after["tryons"]] == [second["id"]]
    assert not (store.images / first["image"]).exists()
    c.delete(f"/api/items/{item['id']}")
    assert not (store.images / second["image"]).exists()
    assert c.delete(f"/api/tryons/{second['id']}").status_code == 404


def test_a_failed_try_on_says_why(store):
    pipeline = FakePipeline()
    c = client(store, pipeline)
    item = outfit_item(c, pipeline)
    pipeline.error = ai.ModelError("Your OpenRouter account is out of credit.")
    (tryon,) = c.post(f"/api/items/{item['id']}/tryons", json={}).json()["tryons"]
    assert tryon["status"] == "failed" and tryon["error"] == "Your OpenRouter account is out of credit."


def test_on_me_in_the_public_copy_uses_only_your_own_outfit_photos_and_key(store):
    pipeline = FakePipeline()
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    alice_item = outfit_item(alice, pipeline)
    (alice_selfie,) = alice.get("/api/selfies").json()
    bob_item = new_item(bob)
    assert bob.get("/api/selfies").json() == []
    assert bob.get(f"/images/{alice_selfie['original']}").status_code == 404
    assert bob.post(f"/api/items/{bob_item['id']}/tryons", json={"selfie_id": alice_selfie["id"]}).status_code == 409
    assert bob.post(f"/api/items/{alice_item['id']}/tryons", json={}).status_code == 404

    (tryon,) = alice.post(f"/api/items/{alice_item['id']}/tryons", json={}).json()["tryons"]
    assert ("try on", "sk-or-user_alice") in pipeline.keys
    assert bob.get(f"/images/{tryon['image']}").status_code == 404
    assert bob.delete(f"/api/tryons/{tryon['id']}").status_code == 404

    alice.delete("/api/account")
    assert not (store.images / alice_selfie["original"]).exists()
    assert not (store.images / tryon["image"]).exists()


def test_an_outfit_photo_can_be_deleted_and_its_pictures_are_exported(store):
    import zipfile

    pipeline = FakePipeline()
    c = client(store, pipeline)
    item = outfit_item(c, pipeline)
    (tryon,) = c.post(f"/api/items/{item['id']}/tryons", json={}).json()["tryons"]
    zf = zipfile.ZipFile(io.BytesIO(c.get("/api/export").content))
    assert f"images/{tryon['image']}" in zf.namelist()

    (selfie,) = c.get("/api/selfies").json()
    assert c.delete(f"/api/selfies/{selfie['id']}").status_code == 204
    assert c.get("/api/selfies").json() == [] and not (store.images / selfie["original"]).exists()
    assert c.get(f"/api/items/{item['id']}").json()["tryons"][0]["image"] == tryon["image"]  # the picture stays
    assert c.delete(f"/api/selfies/{selfie['id']}").status_code == 404


def test_only_you_can_delete_your_outfit_photo(store):
    pipeline = FakePipeline()
    alice, bob = signed_in(store, pipeline, "user_alice"), signed_in(store, pipeline, "user_bob")
    outfit_item(alice, pipeline)
    (selfie,) = alice.get("/api/selfies").json()
    assert bob.delete(f"/api/selfies/{selfie['id']}").status_code == 404
    assert alice.get("/api/selfies").json() == [selfie]
