"""Add from link: reading saved shop pages (offline) and the item it makes."""

import io
from pathlib import Path

import pytest
from PIL import Image

from test_app import FakePipeline, photo_bytes
from tidyrack import shoplink
from tidyrack.store import Store

PAGES = Path(__file__).parent / "shop_pages"
URLS = {
    "asos": "https://www.asos.com/bershka/bershka-textured-print-jumper-in-beige/prd/211886541",
    "mands": "https://www.marksandspencer.com/air-yarn-cable-knit-button-through-cardigan/p/clp61223928?color=ECRU",
    "next": "https://www.next.co.uk/style/su185450/k82235",
    "uniqlo": "https://www.uniqlo.com/uk/en/products/E460933-000/00",
    "vinted": "https://www.vinted.co.uk/items/1000000001-ralph-lauren-navy-striped-jumper",
    "zara-blocked": "https://www.zara.com/uk/en/cotton-wool-quarter-zip-jumper-p09325336.html",
}


def saved(shop):
    return shoplink.read_product((PAGES / f"{shop}.html").read_text(), URLS[shop])


@pytest.mark.parametrize("shop, name, brand, colour, price", [
    ("asos", "Bershka Textured print jumper in beige", "Bershka", "beige", None),
    ("mands", "Air Yarn Button Through Cardigan", "M&S", None, "£45.00"),
    ("next", "Regular Fit Cotton Rich Crew Jumper", "Next", "Navy Blue", "£6.00"),
    ("uniqlo", "Mens Merino Crew Neck Jumper", "UNIQLO", None, None),
    ("vinted", "Ralph Lauren navy striped jumper", "Ralph Lauren", "Navy", "£45.00"),
])
def test_reads_saved_shop_pages(shop, name, brand, colour, price):
    product = saved(shop)
    assert product["is_product"]
    assert product["image"].startswith("https://")
    assert (product["name"], product["brand"], product["colour"], product["price"]) == (name, brand, colour, price)


def test_next_lists_each_size_as_its_own_product():
    product = saved("next")
    assert product["fabric"] == "82% Cotton, 18% Nylon."
    assert product["sizes"][:3] == ["X-Small", "Small", "Medium"]


def test_asos_image_is_asked_for_full_size():
    assert saved("asos")["image"].endswith("211886541-1-beige?wid=1400")


def test_a_bot_check_page_is_not_a_product():
    assert not saved("zara-blocked")["is_product"]


def test_a_home_page_with_a_logo_is_not_a_product():
    html = '<meta property="og:title" content="Shop"><meta property="og:image" content="/logo.png">'
    assert not shoplink.read_product(html, "https://shop.example/en-gb/")["is_product"]
    assert shoplink.read_product(html, "https://shop.example/en-gb/products/123")["is_product"]


def test_only_web_links_are_fetched():
    with pytest.raises(shoplink.ShopLinkError, match="isn't a web link"):
        shoplink.read_link("file:///etc/passwd")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8760/api/items", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://100.101.102.103/",  # a Tailscale address
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
])
def test_links_to_the_servers_own_network_are_refused(url):
    with pytest.raises(shoplink.ShopLinkError, match="public website|shop's page"):
        shoplink.public_address(url)


@pytest.mark.parametrize("address", ["::7f00:1", "2002:7f00:1::", "64:ff9b::7f00:1", "2001::1"])
def test_ipv6_addresses_wrapping_private_ones_are_not_public(address):
    import ipaddress

    assert not shoplink.is_public(ipaddress.ip_address(address))


def test_addresses_with_non_ascii_letters_are_refused():
    with pytest.raises(shoplink.ShopLinkError, match="can't check"):
        shoplink.public_address("http://shopß.example/")


def test_requests_go_to_the_checked_address_not_a_fresh_dns_lookup():
    """The real _get, pinned to a local test server under a name that doesn't exist:
    the page only arrives if curl used the pin."""
    import http.server
    import threading

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pinned")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        res = shoplink._get(f"http://no-such-shop.invalid:{port}/", "no-such-shop.invalid", port, ["127.0.0.1"])
        assert res.text == "pinned"
    finally:
        server.shutdown()


def test_downloads_are_capped(monkeypatch):
    import http.server
    import threading

    class Huge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()  # no length given, so curl only finds out as it goes
            for _ in range(200):
                self.wfile.write(b"x" * 10_000)

        def log_message(self, *args):
            pass

    monkeypatch.setattr(shoplink, "MAX_DOWNLOAD", 100_000)
    server = http.server.HTTPServer(("127.0.0.1", 0), Huge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with pytest.raises(shoplink.requests.RequestsError):
            shoplink._get(f"http://big.invalid:{port}/", "big.invalid", port, ["127.0.0.1"])
    finally:
        server.shutdown()


def test_odd_ports_are_refused():
    with pytest.raises(shoplink.ShopLinkError, match="shop's page"):
        shoplink.public_address("https://www.asos.com:22/")


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    class Redirect:
        status_code, headers = 302, {"location": "http://127.0.0.1/admin"}

    import socket

    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(None, None, None, "", ("93.184.215.14", 443))]
                        if host == "shop.example" else real(host, *a, **k))
    fetched = []
    monkeypatch.setattr(shoplink, "_get", lambda url, host, port, addresses: (fetched.append(url), Redirect())[1])
    with pytest.raises(shoplink.ShopLinkError, match="public website"):
        shoplink.fetch("https://shop.example/product")
    assert fetched == ["https://shop.example/product"]  # the redirect itself was never followed


def test_see_through_shop_images_go_on_white():
    out = io.BytesIO()
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(out, "PNG")
    jpeg = Image.open(io.BytesIO(shoplink.as_jpeg(out.getvalue(), URLS["asos"])))
    assert jpeg.format == "JPEG" and jpeg.getpixel((0, 0)) == (255, 255, 255)
    with pytest.raises(shoplink.ShopLinkError, match="didn't download as an image"):
        shoplink.as_jpeg(b"<html>", URLS["asos"])


def test_notes_hold_the_name_price_sizes_and_link():
    assert shoplink.notes(saved("vinted"), URLS["vinted"]) == (
        f"Ralph Lauren navy striped jumper, £45.00 at vinted.co.uk.\n{URLS['vinted']}")
    assert "Sizes: X-Small, Small, Medium" in shoplink.notes(saved("next"), URLS["next"])


# The endpoint, with the saved pages standing in for the shops.

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def saved_link(url):
    shop = next(name for name, u in URLS.items() if u == url)
    product = saved(shop)
    if not product["is_product"]:
        raise shoplink.ShopLinkError("zara.com didn't send the product details.")
    return product, photo_bytes()


@pytest.fixture
def app_with_pages(store):
    from fastapi.testclient import TestClient
    from tidyrack.app import create_app

    def make(pipeline):
        return TestClient(create_app(store, pipeline, run=lambda lane, fn, *args: fn(*args), read_link=saved_link))
    return make


def test_a_link_makes_an_item_with_a_shop_photo(app_with_pages):
    pipeline = FakePipeline({"type": "jumper", "colour": "blue", "brand": "Polo"})
    res = app_with_pages(pipeline).post("/api/items/from-link", json={"url": URLS["vinted"]})
    assert res.status_code == 201
    item = res.json()
    [photo] = item["photos"]
    assert photo["kind"] == "shop"
    assert photo["cutout_status"] == "ready" and photo["tidy_status"] is None  # a cutout, no tidy
    assert not item["draft"] and item["status"] == "ready"
    # The shop's brand and colour beat the model's; the model fills in the type.
    assert (item["type"], item["brand"], item["colour"]) == ("jumper", "Ralph Lauren", "Navy")
    assert set(item["unchecked"]) == {"type", "brand", "colour"}
    assert item["amount_paid"] is None
    assert item["notes"].startswith("Ralph Lauren navy striped jumper, £45.00 at vinted.co.uk.")


def test_a_retry_keeps_the_shop_details(app_with_pages):
    c = app_with_pages(FakePipeline({"type": "jumper", "brand": "Polo", "fabric": "wool"}))
    item = c.post("/api/items/from-link", json={"url": URLS["next"]}).json()
    item = c.post(f"/api/items/{item['id']}/retry").json()
    assert (item["brand"], item["fabric"]) == ("Next", "82% Cotton, 18% Nylon.")


def test_a_blocked_shop_says_so_and_makes_no_item(app_with_pages, store):
    res = app_with_pages(FakePipeline()).post("/api/items/from-link", json={"url": URLS["zara-blocked"]})
    assert res.status_code == 422
    assert "didn't send the product details" in res.json()["detail"]
    assert store.all() == []
