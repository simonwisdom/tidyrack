from tidyrack import outfits


def piece(type_text, hexes, fabric=None):
    colours = [{"hex": h, "share": round(1 / len(hexes), 2)} for h in hexes]
    return outfits.describe(type_text, fabric, colours)


def score(*pieces):
    return outfits.score(list(pieces))[0]


def test_garments_are_sorted_into_top_bottom_layer_full_length_and_shoes():
    assert piece("t-shirt", ["#ffffff"])["slot"] == "top"
    assert piece("jeans", ["#3b4a6b"])["slot"] == "bottom"
    assert piece("blazer", ["#222222"])["slot"] == "layer"
    assert piece("dress", ["#aa2233"])["slot"] == "full"
    for shoes in ("trainers", "high-top trainers", "chelsea boots", "dress shoes", "loafers", "sandals"):
        assert piece(shoes, ["#222222"])["slot"] == "shoes", shoes
    assert piece("oxford shirt", ["#ffffff"])["slot"] == "top"
    assert piece("bootcut jeans", ["#3b4a6b"])["slot"] == "bottom"
    assert outfits.describe("tote bag", None, []) is None  # not something the scorer pairs


def test_shoes_count_for_formality_and_season():
    shirt, tailored = piece("shirt", ["#f4f4f2"]), piece("tailored trousers", ["#222222"])
    assert score(shirt, tailored, piece("loafers", ["#222222"], "leather")) > score(
        shirt, tailored, piece("flip flops", ["#222222"]))
    jumper, jeans = piece("jumper", ["#f4f4f2"], "wool"), piece("jeans", ["#3b4a6b"])
    assert score(jumper, jeans, piece("boots", ["#222222"])) > score(jumper, jeans, piece("sandals", ["#222222"]))


def test_neutrals_and_one_strong_colour_beat_a_clash():
    white_tee, jeans = piece("t-shirt", ["#f4f4f2"]), piece("jeans", ["#3b4a6b"])
    red_top, teal_trousers = piece("top", ["#c8202a"]), piece("trousers", ["#119c9a"])
    orange_top, purple_skirt = piece("top", ["#e07a10"]), piece("skirt", ["#8a3fb0"])
    assert outfits.is_neutral(white_tee["colours"][0][0]) and outfits.is_neutral(jeans["colours"][0][0])
    assert score(white_tee, jeans) > score(orange_top, purple_skirt)
    assert score(red_top, jeans) > score(orange_top, purple_skirt)
    _, reasons = outfits.score([orange_top, purple_skirt])
    assert "but the colours clash" in reasons
    assert outfits.features([red_top, teal_trousers])["strong_pair_go"] == 1  # opposites in OKLab


def test_dressy_with_casual_and_summer_with_winter_lose_points():
    shirt, tailored = piece("shirt", ["#f4f4f2"]), piece("tailored trousers", ["#222222"])
    hoodie = piece("hoodie", ["#f4f4f2"])
    assert score(shirt, tailored) > score(hoodie, tailored)
    linen_vest, shorts = piece("tank top", ["#f4f4f2"], "linen"), piece("shorts", ["#222222"])
    wool_jumper = piece("jumper", ["#f4f4f2"], "wool")
    assert score(linen_vest, shorts) > score(wool_jumper, shorts)


def test_a_colour_repeated_across_pieces_ties_them_together():
    print_top = piece("shirt", ["#f4f4f2", "#2a6fb0"])
    blue_skirt, red_skirt = piece("skirt", ["#2a6fb0"]), piece("skirt", ["#c8202a"])
    assert outfits.features([print_top, blue_skirt])["echo"] == 1
    assert outfits.features([print_top, red_skirt])["echo"] == 0


def test_an_item_without_measured_colours_uses_its_list_colour():
    item = {"type": "jeans", "fabric": "denim", "colour_hex": "#3b4a6b", "colours": None}
    assert outfits.from_item(item)["colours"][0][1] == 1.0
    assert outfits.from_item({"type": "jeans", "fabric": None, "colour_hex": None, "colours": None})["colours"] == []
