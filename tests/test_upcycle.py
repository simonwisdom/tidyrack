"""What if I…: edits drawn from the garment's tidied picture, and the Keep / Upcycle / Sell plan."""

import pytest
from test_app import FakePipeline, client, new_item, png_bytes, signed_in

from tidyrack import upcycle
from tidyrack.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


class EditPipeline(FakePipeline):
    def __init__(self, guesses=None):
        super().__init__(guesses or {"type": "blazer", "colour": "pink", "fabric": "linen"})
        self.redraws = []  # (prompt, OpenRouter key) of each edit
        self.howtos = []

    def redraw(self, jpeg, prompt, key=None):
        self.redraws.append((prompt, key))
        if self.error:
            raise self.error
        return png_bytes()

    def write_howto(self, jpegs, details, change, key=None):
        self.howtos.append((len(jpegs), details, change, key))
        return {"materials": ["Machine dye, navy, about £8"], "steps": ["Wash it", "Dye it"],
                "cost": "about £8", "time": "an hour"}


def pink_blazer(store, pipeline):
    c = client(store, pipeline)
    item = new_item(c)
    store.update(item["id"], {"colour_hex": "#a53f52"}, [])
    return c, item["id"]


# The rules for what's offered


def test_dye_only_offers_darker_colours_that_differ():
    for name in upcycle.dye_colours("#a53f52", []):
        assert upcycle.oklab(upcycle.DYES[name])[0] < upcycle.oklab("#a53f52")[0]
    assert upcycle.dye_colours("#111111", []) == []  # nothing is darker than near-black


def test_dye_colours_go_with_the_wardrobe():
    # A wardrobe of blues: a blue-green dye pairs with them, a rust one is their complement too,
    # but a plum one goes with neither.
    blues = ["#2b4a8a", "#3a5f9a", "#1e3a6e"]
    picked = upcycle.dye_colours("#f0e8d8", blues, count=4)
    assert picked[0] in upcycle.NEUTRAL_DYES
    assert "plum" not in picked


def test_fabric_rules():
    blazer = {"type": "blazer", "fabric": "linen", "colour_hex": "#a53f52"}
    assert {o["key"] for o in upcycle.options(blazer, [])} >= {"fade", "crop", "buttons"}
    silk = {**blazer, "fabric": "100% silk"}
    assert "fade" not in {o["key"] for o in upcycle.options(silk, [])}  # bleach ruins silk
    assert all("hob" in o["cost"] for o in upcycle.options(silk, []) if o["key"].startswith("dye"))
    leather = {"type": "jacket", "fabric": "leather", "colour_hex": "#a53f52"}
    assert not any(o["key"].startswith("dye") for o in upcycle.options(leather, []))
    tee = {"type": "t-shirt", "fabric": "cotton", "colour_hex": "#a53f52"}
    assert "buttons" not in {o["key"] for o in upcycle.options(tee, [])}
    assert upcycle.fibre("80% polyester, 20% cotton") == "synthetic"


def test_oklab_matches_the_reference_values():
    assert [round(v, 3) for v in upcycle.oklab("#ff0000")] == [0.628, 0.225, 0.126]
    assert upcycle.oklab_to_hex(upcycle.oklab("#a53f52")) == "#a53f52"


# The app


def test_edits_are_drawn_from_the_tidied_picture(store):
    pipeline = EditPipeline()
    c, item_id = pink_blazer(store, pipeline)
    view = c.get(f"/api/items/{item_id}/what-if").json()
    keys = [o["key"] for o in view["options"]]
    assert "dye-navy" in keys and "prompt" not in view["options"][0]
    view = c.post(f"/api/items/{item_id}/what-if", json={"option": "dye-navy"}).json()
    (edit,) = view["edits"]
    assert edit["status"] == "ready" and edit["image"].endswith("-edit.png")
    assert edit["label"] == "Dyed navy" and edit["target_hex"] == upcycle.DYES["navy"]
    assert "navy" in pipeline.redraws[0][0] and pipeline.redraws[0][1] is None  # Codex in the personal copy
    assert c.get("/images/" + edit["image"]).status_code == 200


def test_a_new_try_replaces_the_old_but_own_ideas_are_kept(store):
    pipeline = EditPipeline()
    c, item_id = pink_blazer(store, pipeline)
    first = c.post(f"/api/items/{item_id}/what-if", json={"option": "fade"}).json()["edits"][0]
    c.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "add elbow patches"})
    c.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "embroider a bee"})
    edits = c.post(f"/api/items/{item_id}/what-if", json={"option": "fade"}).json()["edits"]
    assert [e["option"] for e in edits] == ["own", "own", "fade"]
    assert not (store.images / first["image"]).exists()
    assert pipeline.redraws[1][0].endswith("add elbow patches")


def test_only_offered_edits_and_real_ideas(store):
    c, item_id = pink_blazer(store, EditPipeline())
    assert c.post(f"/api/items/{item_id}/what-if", json={"option": "dye-hot-pink"}).status_code == 400
    assert c.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "  "}).status_code == 400
    assert c.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "x" * 201}).status_code == 422


def test_a_failed_edit_says_why(store):
    pipeline = EditPipeline()
    c, item_id = pink_blazer(store, pipeline)
    pipeline.error = RuntimeError("Codex made no image")
    (edit,) = c.post(f"/api/items/{item_id}/what-if", json={"option": "crop"}).json()["edits"]
    assert edit["status"] == "failed" and "Codex made no image" in edit["error"]


def test_upcycle_takes_the_edit_as_its_goal_with_a_howto(store):
    pipeline = EditPipeline()
    c, item_id = pink_blazer(store, pipeline)
    edit = c.post(f"/api/items/{item_id}/what-if", json={"option": "dye-navy"}).json()["edits"][0]
    item = c.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle", "edit_id": edit["id"]}).json()
    assert item["plan"] == "upcycle"
    assert item["goal"]["image"] == edit["image"] and item["goal"]["label"] == "Dyed navy"
    assert item["goal"]["howto"]["steps"] == ["Wash it", "Dye it"]
    assert pipeline.howtos == [(2, "type: blazer, colour: pink, fabric: linen", "Dyed navy", None)]
    # The goal survives a new try of the same edit, and choosing it again doesn't rewrite the how-to.
    c.post(f"/api/items/{item_id}/what-if", json={"option": "dye-navy"})
    assert (store.images / edit["image"]).exists()
    c.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle", "edit_id": edit["id"]})
    assert len(pipeline.howtos) == 1
    item = c.post(f"/api/items/{item_id}/plan", json={"plan": "keep"}).json()
    assert item["plan"] == "keep" and item["goal"] is None


def test_export_has_the_plan_and_the_goal_picture(store):
    import io
    import zipfile

    from test_app import export_rows

    c, item_id = pink_blazer(store, EditPipeline())
    edit = c.post(f"/api/items/{item_id}/what-if", json={"option": "dye-navy"}).json()["edits"][0]
    c.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle", "edit_id": edit["id"]})
    (row,) = export_rows(c)
    assert row["plan"] == "upcycle" and row["goal"] == f"Dyed navy: images/{edit['image']}"
    assert f"images/{edit['image']}" in zipfile.ZipFile(io.BytesIO(c.get("/api/export").content)).namelist()


def test_upcycle_needs_a_finished_edit_of_this_item(store):
    c, item_id = pink_blazer(store, EditPipeline())
    assert c.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle"}).status_code == 400
    other = new_item(c)["id"]
    edit = c.post(f"/api/items/{other}/what-if", json={"option": "own", "text": "dye it"}).json()["edits"][0]
    assert c.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle", "edit_id": edit["id"]}).status_code == 400
    assert c.post(f"/api/items/{item_id}/plan", json={"plan": "bin"}).status_code == 400


def test_deleting_the_item_deletes_its_edits(store):
    c, item_id = pink_blazer(store, EditPipeline())
    edit = c.post(f"/api/items/{item_id}/what-if", json={"option": "fade"}).json()["edits"][0]
    c.delete(f"/api/items/{item_id}")
    assert not (store.images / edit["image"]).exists() and store.edits(item_id) == []


def test_public_copy_draws_edits_on_the_owners_key_and_hides_them_from_others(store):
    pipeline = EditPipeline()
    a, b = signed_in(store, pipeline, "user-a"), signed_in(store, pipeline, "user-b")
    item_id = new_item(a)["id"]
    edit = a.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "dye it navy"}).json()["edits"][0]
    assert pipeline.redraws[-1][1] == "sk-or-user-a"
    a.post(f"/api/items/{item_id}/plan", json={"plan": "upcycle", "edit_id": edit["id"]})
    assert pipeline.howtos[-1][3] == "sk-or-user-a"
    assert a.get("/images/" + edit["image"]).status_code == 200
    assert b.get("/images/" + edit["image"]).status_code == 404
    assert b.get(f"/api/items/{item_id}/what-if").status_code == 404
    assert b.post(f"/api/items/{item_id}/what-if", json={"option": "own", "text": "x"}).status_code == 404
    assert b.post(f"/api/items/{item_id}/plan", json={"plan": "keep"}).status_code == 404
