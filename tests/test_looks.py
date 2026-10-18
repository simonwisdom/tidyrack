from test_app import FakePipeline, client, new_item, photo_bytes, png_bytes, public_client, signed_in, store  # noqa: F401

from tidyrack import looks


class OutfitPipeline(FakePipeline):
    def __init__(self):
        super().__init__()
        self.drawn = []  # (prompt, number of reference pictures, key) of each drawing
        self.answer = {"pieces": []}
        self.today = {"high": 18, "low": 10, "rain": 10}

    def draw(self, prompt, jpegs, key=None):
        self.drawn.append((prompt, len(jpegs), key))
        return png_bytes()

    def ask_json(self, prompt, jpegs, key=None):
        return self.answer

    def weather(self, place):
        return self.today


# type, colour words, list colour, fabric, size
CLOTHES = [("t-shirt", "white", "#f2f2ef", "cotton", "M"), ("jumper", "navy", "#202b45", "wool", "M"),
           ("jeans", "mid blue", "#56708f", "denim", "32R"), ("tailored trousers", "black", "#1e1e20", "wool", "32R"),
           ("blazer", "navy", "#1e2840", "wool", "40R")]


def wardrobe(c):
    ids = {}
    for type_text, words, hex_code, fabric, size in CLOTHES:
        item = new_item(c)
        c.patch(f"/api/items/{item['id']}", json={"type": type_text, "colour": words, "colour_hex": hex_code,
                                                   "fabric": fabric, "size": size})
        c.post(f"/api/items/{item['id']}/finish")
        ids[type_text] = item["id"]
    return ids


def test_dress_me_shows_the_best_outfits_and_keeps_save_and_shuffle(store):
    c = client(store, OutfitPipeline())
    assert c.get("/api/looks/dress-me").status_code == 409  # nothing to dress yet
    ids = wardrobe(c)
    res = c.get("/api/looks/dress-me").json()
    # 2 tops x 2 bottoms x (no layer or the blazer)
    assert res["total"] == 8 and len(res["outfits"]) == 8
    first = res["outfits"][0]
    assert {p["id"] for p in first["pieces"]} == set(first["items"])
    assert all(p["image"].startswith("/images/") for p in first["pieces"])
    assert ids["blazer"] not in first["items"][1:]  # a layer comes first

    assert c.post("/api/looks/feedback", json={"items": first["items"], "action": "liked"}).status_code == 400
    assert c.post("/api/looks/feedback", json={"items": ["nope"], "action": "saved"}).status_code == 404
    c.post("/api/looks/feedback", json={"items": first["items"], "action": "shuffled", "score": first["score"]})
    c.post("/api/looks/feedback", json={"items": first["items"], "action": "saved", "score": first["score"]})
    (saved,) = c.get("/api/looks/saved").json()
    assert saved["items"] == first["items"]


def test_see_it_on_draws_the_outfit_from_its_pieces(store):
    pipeline = OutfitPipeline()
    c = client(store, pipeline)
    ids = wardrobe(c)
    outfit = [ids["blazer"], ids["t-shirt"], ids["jeans"]]
    look = c.post("/api/looks/draw", json={"items": outfit}).json()
    assert look["status"] == "ready" and look["image_url"] == f"/api/looks/{look['id']}/image"
    prompt, refs, key = pipeline.drawn[-1]
    assert refs == 3 and key is None
    assert "navy blazer; white t-shirt; mid blue jeans" in prompt
    assert c.get(look["image_url"]).status_code == 200
    assert c.get(f"/api/looks/{look['id']}").json()["status"] == "ready"

    pipeline.draw = lambda *a: (_ for _ in ()).throw(RuntimeError("Codex made no image"))
    failed = c.post("/api/looks/draw", json={"items": outfit}).json()
    assert failed["status"] == "failed" and "no image" in failed["error"]


def test_copy_this_look_matches_pieces_and_names_whats_missing(store):
    pipeline = OutfitPipeline()
    c = client(store, pipeline)
    ids = wardrobe(c)
    trainers = new_item(c)
    c.patch(f"/api/items/{trainers['id']}", json={"type": "trainers", "colour": "white", "colour_hex": "#f4f4f2"})
    c.post(f"/api/items/{trainers['id']}/finish")
    pipeline.answer = {"pieces": [
        {"part": "top", "type": "white tee", "colour": "white", "hex": "#f5f5f5"},
        {"part": "bottom", "type": "light blue jeans", "colour": "blue", "hex": "#6c86a8"},
        {"part": "layer", "type": "beige trench coat", "colour": "beige", "hex": "#c8ac80"},
        {"part": "shoes", "type": "white trainers", "colour": "white", "hex": "#ffffff"},
    ]}
    res = c.post("/api/looks/copy", files={"photo": ("look.jpg", photo_bytes(), "image/jpeg")}).json()
    assert [m["item"] for m in res["matched"]] == [ids["t-shirt"], ids["jeans"], trainers["id"]]
    (missing,) = res["missing"]
    assert missing["type"] == "beige trench coat" and "search_text=beige+trench+coat" in missing["vinted"]

    look = c.post(f"/api/looks/{res['look']['id']}/draw-copy").json()
    assert look["status"] == "ready"
    assert pipeline.drawn[-1][1] == 4  # the outfit photo and the three matched pieces


def test_every_outfit_wears_the_pair_of_shoes_that_suits_it_best(store):
    c = client(store, OutfitPipeline())
    ids = wardrobe(c)
    for type_text, words, hex_code in (("loafers", "black", "#1e1e20"), ("flip flops", "orange", "#e07a10")):
        item = new_item(c)
        c.patch(f"/api/items/{item['id']}", json={"type": type_text, "colour": words, "colour_hex": hex_code,
                                                   "fabric": "leather" if type_text == "loafers" else None})
        c.post(f"/api/items/{item['id']}/finish")
        ids[type_text] = item["id"]
    res = c.get("/api/looks/dress-me").json()
    assert res["total"] == 8  # the same 8 outfits of clothes; shoes don't multiply them
    smart = next(o for o in res["outfits"] if ids["tailored trousers"] in o["items"] and ids["blazer"] in o["items"])
    assert len(smart["items"]) == 4 and smart["items"][-1] == ids["loafers"]


def test_the_missing_piece_is_the_one_that_adds_most_outfits(store):
    pipeline = OutfitPipeline()
    c = client(store, pipeline)
    wardrobe(c)
    res = c.get("/api/looks/missing").json()
    assert res["tried"] == len(looks.EVERYDAY) == 44
    best = res["best"]
    assert len(best) == 3 and best[0]["adds"] >= best[1]["adds"] >= best[2]["adds"] >= 0
    assert best[0]["outfits"] == res["outfits_now"] + best[0]["adds"]
    size = {"top": "M", "bottom": "32R", "layer": "40R", "shoes": ""}[best[0]["slot"]]
    assert best[0]["vinted"].endswith("+" + size if size else best[0]["type"].replace(" ", "+"))
    look = c.post("/api/looks/missing/draw").json()
    assert look["status"] == "ready" and pipeline.drawn[-1][1] == 0  # drawn from words alone


def test_morning_card_dresses_for_the_weather_and_the_calendar(store):
    pipeline = OutfitPipeline()
    c = client(store, pipeline)
    ids = wardrobe(c)
    pipeline.today = {"high": 6, "low": 1, "rain": 70}
    cold = c.get("/api/looks/morning").json()
    assert ids["jumper"] in cold["items"] and "70% rain" in cold["why"]
    pipeline.today = {"high": 24, "low": 16, "rain": 0}
    warm = c.get("/api/looks/morning").json()
    assert ids["t-shirt"] in warm["items"] and ids["blazer"] not in warm["items"]
    pipeline.today = {"high": 14, "low": 8, "rain": 0}
    smart = c.get("/api/looks/morning", params={"events": "09:00 Client meeting\nLunch"}).json()
    assert smart["dressy"] and ids["tailored trousers"] in smart["items"]
    sketch = c.post("/api/looks/morning/sketch", json={"items": smart["items"]}).json()
    assert sketch["status"] == "ready" and "pencil sketch" in pipeline.drawn[-1][0]


def test_looks_belong_to_their_owner_in_the_public_copy(store):
    pipeline = OutfitPipeline()
    a = signed_in(store, pipeline, "user-a")
    item = new_item(a)
    a.patch(f"/api/items/{item['id']}", json={"type": "t-shirt", "colour_hex": "#ffffff"})
    a.post(f"/api/items/{item['id']}/finish")
    look = a.post("/api/looks/draw", json={"items": [item["id"]]}).json()
    assert look["status"] == "ready" and pipeline.drawn[-1][2] is not None  # on A's own key
    b = signed_in(store, pipeline, "user-b")
    assert b.get(f"/api/looks/{look['id']}").status_code == 404
    assert b.get(look["image_url"]).status_code == 404
    assert b.post("/api/looks/draw", json={"items": [item["id"]]}).status_code == 409  # B has no clothes
    assert public_client(store, pipeline).get("/api/looks/dress-me").status_code == 401

    a.delete("/api/account")
    assert not list(store.images.glob("look-*"))


def test_weather_falls_back_to_met_norway_when_open_meteo_is_busy(monkeypatch):
    import httpx

    hour = lambda t, rain: {"data": {"instant": {"details": {"air_temperature": t}},  # noqa: E731
                                     "next_1_hours": {"details": {"precipitation_amount": rain}}}}

    class Reply:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def get(url, **kwargs):
        if "open-meteo" in url:
            return Reply({"reason": "The service is overloaded", "error": True})
        assert kwargs["headers"]["User-Agent"].startswith("Tidyrack")
        return Reply({"properties": {"timeseries": [hour(8.4, 0), hour(13.6, 0.5), hour(11, 0)]}})

    monkeypatch.setattr(httpx, "get", get)
    assert looks.weather() == {"high": 14, "low": 8, "rain": 70}
