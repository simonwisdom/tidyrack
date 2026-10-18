"""Read a product from a shop's web page: its name, brand, colour, fabric,
sizes, price and main image.

Shops put this in the page for search engines, as JSON-LD (a schema.org
`Product`) and Open Graph meta tags. Many shops block downloads that don't come
from a real browser, so pages are fetched with curl_cffi, which looks like
Firefox to them (in October 2026 Firefox got through at more shops than Chrome). Those that still block fail with a ShopLinkError saying so in
plain words.
"""

import io
import ipaddress
import json
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from curl_cffi import CurlOpt, requests
from PIL import Image

from tidyrack.ai import MAX_PIXELS

HEADERS = {"Accept-Language": "en-GB,en;q=0.9"}
TIMEOUT = 20
MAX_REDIRECTS = 5
MAX_DOWNLOAD = 15_000_000  # bytes, for a page or an image
IMAGE_FORMATS = ["JPEG", "PNG", "WEBP", "AVIF"]
CURRENCY_SIGNS = {"GBP": "£", "EUR": "€", "USD": "$"}
SAVE_IT_YOURSELF = "Save the shop's photo from the page and add it as a photo instead."


class ShopLinkError(Exception):
    """A link that gave no product, with a message you can read."""


def shop_name(url: str) -> str:
    host = urlparse(url).hostname or url
    return host.removeprefix("www.")


def public_address(url: str) -> tuple[str, int, list[str]]:
    """(host, port, IP addresses) to fetch a link from, if it's an ordinary public website.

    Anyone can paste a link into the public copy, so the server must never fetch
    its own or its network's addresses (localhost, Tailscale, the cloud's metadata
    service) for them.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ShopLinkError("That isn't a web link. Paste the whole address, starting with https://")
    if not parts.hostname.isascii():
        # Python and curl turn non-ASCII names into different ASCII ones, so curl
        # would look up a name that was never checked.
        raise ShopLinkError("That link's address has letters Tidyrack can't check. Paste the shop's usual address.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = None
    if port not in (80, 443):
        raise ShopLinkError("That link doesn't look like a shop's page. Paste the address from the shop's website.")
    try:
        found = {info[4][0] for info in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)}
    except (socket.gaierror, UnicodeError) as exc:
        raise ShopLinkError(f"Couldn't find {shop_name(url)}. Check the link.") from exc
    ips = [ipaddress.ip_address(address.split("%")[0]) for address in found]
    if not all(is_public(ip) for ip in ips):
        raise ShopLinkError("That link doesn't point to a public website.")
    # All of them, IPv4 first: curl tries the next if one can't be reached, as it would after its own lookup.
    return parts.hostname, port, [str(ip) for ip in sorted(ips, key=lambda ip: (ip.version, ip))]


# IPv6 ranges that carry an IPv4 address inside, which Python still calls global.
IPV6_WRAPPING_IPV4 = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96")]


def is_public(ip) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo or any(ip in net for net in IPV6_WRAPPING_IPV4):
            return False
    return ip.is_global


def _get(url: str, host: str, port: int, addresses: list[str]):
    """One request, sent only to the addresses that were checked, whatever DNS says by now."""
    pinned = ",".join(f"[{a}]" if ":" in a else a for a in addresses)
    session = requests.Session(curl_options={CurlOpt.RESOLVE: [f"{host}:{port}:{pinned}"],
                                             CurlOpt.MAXFILESIZE_LARGE: MAX_DOWNLOAD})
    return session.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="firefox", allow_redirects=False)


def fetch(url: str, what: str = "page"):
    """Download the page or image, or raise a ShopLinkError saying why not.
    Each redirect is checked like the first link."""
    shop = shop_name(url)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            response = _get(url, *public_address(url))
            location = response.headers.get("location")
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                break
            url = urljoin(url, location)
        else:
            raise ShopLinkError(f"{shop} kept redirecting. {SAVE_IT_YOURSELF}")
    except requests.RequestsError as exc:
        raise ShopLinkError(f"Couldn't reach {shop} ({type(exc).__name__}). {SAVE_IT_YOURSELF}") from exc
    if response.status_code in (401, 403, 429):
        raise ShopLinkError(f"{shop} blocked the download (error {response.status_code}). {SAVE_IT_YOURSELF}")
    if response.status_code != 200:
        raise ShopLinkError(f"{shop} sent no {what} (error {response.status_code}). {SAVE_IT_YOURSELF}")
    return response


def read_link(url: str) -> tuple[dict, bytes]:
    """Fetch a product page and its main image, as a JPEG."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ShopLinkError("That isn't a web link. Paste the whole address, starting with https://")
    page = fetch(url)
    product = read_product(page.text, str(page.url))
    if not product["is_product"] or not product["image"]:
        # Often a "check you're human" page: the shop hides the product from apps.
        raise ShopLinkError(f"{shop_name(url)} didn't send the product details (shops often hide them "
                            f"from apps like this one). {SAVE_IT_YOURSELF}")
    return product, as_jpeg(fetch(product["image"], "image").content, url)


def as_jpeg(data: bytes, url: str) -> bytes:
    """Shops send WebP, AVIF or PNG; the rest of the app expects a JPEG-like photo."""
    try:
        image = Image.open(io.BytesIO(data), formats=IMAGE_FORMATS)
        if image.width * image.height > MAX_PIXELS:
            raise ValueError(f"{image.width}x{image.height} is too big")
        image.load()
    except Exception as exc:
        raise ShopLinkError(f"The {shop_name(url)} photo didn't download as an image. {SAVE_IT_YOURSELF}") from exc
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        white = Image.new("RGB", image.size, "white")  # see-through shop images go on white
        white.paste(image, mask=image.getchannel("A"))
        image = white
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def notes(product: dict, url: str) -> str:
    """What the shop said that has no field of its own: name, price, sizes and the link."""
    name = product["name"] or "Product"
    price = f", {product['price']}" if product["price"] else ""
    lines = [f"{name}{price} at {shop_name(url)}."]
    if product["sizes"]:
        lines.append("Sizes: " + ", ".join(product["sizes"]) + ".")
    lines.append(url)
    return "\n".join(lines)


class _PageParser(HTMLParser):
    """Collects JSON-LD blocks and meta tags; ignores everything else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.meta: dict[str, str] = {}
        self._in = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._in = "json_ld"
            self.json_ld.append("")
        elif tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content") and key not in self.meta:
                self.meta[key] = attrs["content"].strip()

    def handle_endtag(self, tag):
        if tag == "script":
            self._in = None

    def handle_data(self, data):
        if self._in == "json_ld":
            self.json_ld[-1] += data


def _types(node: dict) -> list[str]:
    kind = node.get("@type", [])
    return [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)]


def _products(data) -> list[dict]:
    """Every Product (or ProductGroup) in a JSON-LD block, wherever it is nested."""
    found = []
    if isinstance(data, list):
        for entry in data:
            found += _products(entry)
    elif isinstance(data, dict):
        if {"Product", "ProductGroup"} & set(_types(data)):
            found.append(data)
        else:
            for key in ("@graph", "mainEntity", "itemListElement", "item"):
                found += _products(data.get(key))
    return found


def _text(value) -> str | None:
    """A plain string from a JSON-LD value: a string, a {name: ...} object or a list of them."""
    if isinstance(value, list):
        parts = [t for t in (_text(v) for v in value) if t]
        return ", ".join(dict.fromkeys(parts)) or None
    if isinstance(value, dict):
        value = value.get("name") or value.get("value")
    if isinstance(value, (int, float)):
        value = str(value)
    if isinstance(value, str):
        value = unescape(value).strip()
        return value or None
    return None


def _image(value) -> str | None:
    if isinstance(value, list):
        return next((i for i in (_image(v) for v in value) if i), None)
    if isinstance(value, dict):
        return _image(value.get("contentUrl") or value.get("url"))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _offer(offers) -> tuple[str | None, str | None]:
    """(price, currency) from the first offer that has a price."""
    for offer in offers if isinstance(offers, list) else [offers]:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price") or offer.get("lowPrice")
        spec = offer.get("priceSpecification")
        if price is None and isinstance(spec, dict):
            price, currency = spec.get("price"), spec.get("priceCurrency")
            if price not in (None, ""):
                return str(price), currency or offer.get("priceCurrency")
        if price is None and offer.get("offers"):
            price, currency = _offer(offer["offers"])
            if price:
                return price, currency or offer.get("priceCurrency")
        if price not in (None, ""):
            return str(price), offer.get("priceCurrency")
    return None, None


def _price(amount, currency) -> str | None:
    if amount in (None, ""):
        return None
    try:
        amount = f"{float(str(amount).replace(',', '.')):.2f}"
    except ValueError:
        return str(amount)
    sign = CURRENCY_SIGNS.get((currency or "").upper())
    return f"{sign}{amount}" if sign else f"{amount} {currency or ''}".strip()


def _from_json_ld(product: dict) -> dict:
    variants = product.get("hasVariant") or []
    variants = variants if isinstance(variants, list) else [variants]
    variants = [v for v in variants if isinstance(v, dict)]
    sizes = [_text(v.get("size")) for v in [product] + variants]
    colours = [_text(v.get("color")) for v in [product] + variants]
    price, currency = _offer(product.get("offers"))
    if price is None:
        for variant in variants:
            price, currency = _offer(variant.get("offers"))
            if price:
                break
    return {
        "name": _text(product.get("name")),
        "brand": _text(product.get("brand")) or _text(product.get("manufacturer")),
        "colour": next((c for c in colours if c), None),
        "fabric": _text(product.get("material")),
        "sizes": [s for s in dict.fromkeys(sizes) if s],
        "price": _price(price, currency),
        "image": _image(product.get("image")) or next((_image(v.get("image")) for v in variants if v.get("image")), None),
        "category": _text(product.get("category")),
    }


def read_product(html: str, url: str) -> dict:
    """The product on a page, from its JSON-LD, filled in from its Open Graph
    tags. Missing fields are None (sizes: an empty list)."""
    page = _PageParser()
    page.feed(html)
    product = {"name": None, "brand": None, "colour": None, "fabric": None,
               "sizes": [], "price": None, "image": None, "category": None, "is_product": False}
    for block in page.json_ld:
        try:
            data = json.loads(block)
        except ValueError:
            continue  # some shops ship broken JSON-LD; the meta tags may still do
        for found in _products(data):
            product["is_product"] = True
            for key, value in _from_json_ld(found).items():
                if key == "sizes":  # some shops (Next) list each size as its own Product
                    product[key] += [s for s in value if s not in product[key]]
                elif value and not product[key]:
                    product[key] = value
    meta = page.meta
    if not product["is_product"]:
        # Some shops (Uniqlo) give only a title and an image. A page that calls
        # itself a product counts; so does a titled image below the home page,
        # so a home page's logo never becomes an item.
        deep = len([part for part in urlparse(url).path.split("/") if part]) >= 2
        product["is_product"] = ("product" in meta.get("og:type", "").lower()
                                 or any(k.startswith("product:") for k in meta)
                                 or (deep and "og:title" in meta and "og:image" in meta))
        # A shop's own name is the best brand guess there is without product data.
        product["brand"] = meta.get("og:site_name")
    fallbacks = {
        "name": meta.get("og:title") or meta.get("twitter:title"),
        "brand": meta.get("product:brand") or meta.get("og:brand"),
        "colour": meta.get("product:color") or meta.get("og:color"),
        "fabric": meta.get("product:material"),
        "price": _price(meta.get("product:price:amount") or meta.get("og:price:amount"),
                        meta.get("product:price:currency") or meta.get("og:price:currency")),
        "image": meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image"),
    }
    for key, value in fallbacks.items():
        if value and not product[key]:
            product[key] = unescape(value)
    if not product["sizes"] and meta.get("product:size"):
        product["sizes"] = [meta["product:size"]]
    if product["name"]:
        product["name"] = product["name"].split(" | ")[0].strip()  # "Merino Jumper | UNIQLO UK"
    if product["colour"] and product["colour"].isupper():
        product["colour"] = product["colour"].lower()  # ASOS shouts: "BEIGE"
    if product["image"]:
        product["image"] = urljoin(url, product["image"])
        if urlparse(product["image"]).hostname == "images.asos-media.com" and "?" not in product["image"]:
            product["image"] += "?wid=1400"  # ASOS sends 313 px wide unless asked for more
    return product
