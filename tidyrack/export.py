"""The whole wardrobe as one zip: items.csv, items.json and every picture.

The zip is written in small pieces as it is sent, so a large wardrobe never
sits in memory whole.
"""

import csv
import io
import json
import zipfile

from tidyrack.store import Store

LISTING_FIELDS = ["title", "description", "category", "condition", "parcel_size", "price_gbp", "price_reason"]
CSV_COLUMNS = (
    ["id", "added_at", "type", "colour", "colour_hex", "colour_hex_2", "fabric", "brand", "size", "amount_paid", "notes", "unchecked",
     "status", "draft"]
    + [f"listing_{f}" for f in LISTING_FIELDS]
    + ["photos", "labels", "cutouts", "tidied", "plan", "goal"]
)
CHUNK = 256 * 1024


class _Pipe(io.RawIOBase):
    """A write-only stream that hands back whatever was written since the last take()."""

    def __init__(self):
        self.parts = []

    def writable(self):
        return True

    def write(self, data):
        self.parts.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        out, self.parts = b"".join(self.parts), []
        return out


def _files(store: Store, item: dict) -> list[dict]:
    """The item's photos with only the picture files that are really on disk."""
    photos = []
    for p in item["photos"]:
        photo = dict(p)
        for step in ("original", "cutout", "tidy"):
            if photo[step] and store.image_path(photo[step]) is None:
                photo[step] = None
        photos.append(photo)
    return photos


def csv_row(item: dict) -> dict:
    listing = item["listing"] or {}
    photos = item["photos"]

    def names(step, kinds=None):
        return "; ".join(f"images/{p[step]}" for p in photos if p[step] and (kinds is None or p["kind"] in kinds))

    row = {c: item.get(c) for c in CSV_COLUMNS if c in item}
    row["unchecked"] = ", ".join(item["unchecked"])
    row["draft"] = "yes" if item["draft"] else ""
    for f in LISTING_FIELDS:
        row[f"listing_{f}"] = listing.get(f)
    row.update(photos=names("original", ("photo", "worn")), labels=names("original", ("label",)),
               cutouts=names("cutout"), tidied=names("tidy"))
    goal = item.get("goal")
    row["goal"] = goal and f"{goal['label']}: images/{goal['image']}"  # the upcycle's AI-drawn goal picture
    return row


def export_zip(store: Store, owner: str | None = None):
    """Yield the zip's bytes of the owner's items, a piece at a time."""
    items = [{**item, "photos": _files(store, item)} for item in store.all(owner)]
    pipe = _Pipe()
    with zipfile.ZipFile(pipe, "w", zipfile.ZIP_DEFLATED) as zf:
        table = io.StringIO()
        writer = csv.DictWriter(table, CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_row(item) for item in items)
        # The byte-order mark tells Numbers and Excel the file is UTF-8.
        zf.writestr("items.csv", "﻿" + table.getvalue())
        zf.writestr("items.json", json.dumps(items, indent=2, ensure_ascii=False))
        yield pipe.take()
        for item in items:
            names = [photo[step] for photo in item["photos"] for step in ("original", "cutout", "tidy")]
            names += [t["image"] for t in item["tryons"]]  # On me pictures
            names.append((item.get("goal") or {}).get("image"))
            for name in names:
                if not name or store.image_path(name) is None:
                    continue
                # Photos are already compressed; storing them is faster and no bigger.
                info = zipfile.ZipInfo.from_file(store.image_path(name), f"images/{name}")
                info.compress_type = zipfile.ZIP_STORED
                with open(store.image_path(name), "rb") as src, zf.open(info, "w") as dest:
                    while chunk := src.read(CHUNK):
                        dest.write(chunk)
                        yield pipe.take()
    yield pipe.take()
