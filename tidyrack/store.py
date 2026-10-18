"""Where items and their photos live.

Metadata is in one SQLite file; photos are plain files beside it. Everything
else talks to this module, so moving to a hosted database or object storage
later means changing this file, not the app.

An item has one or more photos. The first (position 0) is its cover. A photo
is either a view of the garment ("photo": front, back...), which gets a cutout
and a tidied version, a close-up of a label ("label"), which is only read,
the shop's own picture from an Add from link ("shop"), which gets a cutout only,
or one garment cropped from a photo of someone wearing an outfit ("worn"), which
comes with its cutout from the clothing model and gets a tidied version.
"""

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fields the model guesses, and that you can edit.
GUESSED_FIELDS = ["type", "colour", "fabric", "brand", "size"]
# Fields only you can fill in.
MANUAL_FIELDS = ["amount_paid"]
EDITABLE_FIELDS = GUESSED_FIELDS + MANUAL_FIELDS + ["notes"]
PHOTO_KINDS = ("photo", "label", "shop", "worn")
# A photo step waiting for its turn or running now.
BUSY = ("queued", "working")
# How long a sign-in lasts in the public copy.
SESSION_DAYS = 90

SCHEMA = """
create table if not exists items (
    id text primary key,
    added_at text not null,
    status text not null,            -- details: processing | ready | failed
    error text,
    original text not null,          -- before photos had their own table: the one photo
    cutout text,                     -- (old, see original)
    type text,
    colour text,
    fabric text,
    brand text,
    amount_paid real,
    notes text,
    unchecked text not null default '[]', -- JSON list of fields still a model guess
    tidy text,                       -- (old, see original)
    tidy_status text,                -- (old, see original)
    tidy_error text,                 -- (old, see original)
    size text,
    listing text,                    -- JSON: the last Vinted listing Codex wrote
    draft integer not null default 0, -- 1 while you're still adding photos
    colour_hex text,                 -- main colour from the cover's pixels, e.g. #1f2a44
    colour_hex_2 text,               -- the second colour if it's two-tone
    colour_hex_set_by_hand integer not null default 0 -- 1 once you pick it: never recomputed
);
create table if not exists photos (
    id text primary key,
    item_id text not null references items(id),
    position integer not null,       -- 0 is the cover
    kind text not null default 'photo',  -- photo | label | shop | worn
    added_at text not null,
    original text not null,          -- file names under images/
    cutout text,
    cutout_status text,              -- null | queued | working | ready | failed
    tidy text,
    tidy_status text,                -- null | queued | working | ready | failed
    error text,                      -- why the last cutout or tidy failed
    part text                        -- a worn photo's garment: top | bottoms | full-length piece
);
create table if not exists users (    -- only in the public copy, where people sign in
    id text primary key,
    openrouter_id text not null unique, -- the user_id OpenRouter gives at sign-in
    key text,                        -- their OpenRouter API key, encrypted
    added_at text not null
);
create table if not exists sessions (
    token_hash text primary key,     -- sha256 of the cookie, so a copied database can't sign anyone in
    user_id text not null references users(id),
    added_at text not null
);
create table if not exists uploads (  -- each photo a user adds, for the daily limit; kept when the photo is deleted
    user_id text not null references users(id),
    added_at text not null
);
create table if not exists selfies (  -- a whole Outfit photo, kept for On me (the items get crops of it)
    id text primary key,
    owner text,                      -- the user's id in the public copy; null in the personal one
    added_at text not null,
    original text not null           -- file name under images/
);
create table if not exists tryons (   -- On me: an item drawn onto one of the selfies. A model's picture, never a Vinted photo
    id text primary key,
    item_id text not null references items(id),
    selfie_id text not null,         -- may since have been deleted
    added_at text not null,
    image text,                      -- file name under images/, once drawn
    status text not null,            -- working | ready | failed
    error text
);
create table if not exists edits (   -- What if I…: the garment redrawn dyed, faded, shortened...
    id text primary key,
    item_id text not null references items(id),
    option text not null,            -- which edit: dye-navy, fade, crop, buttons, own
    label text not null,             -- as shown, e.g. "Dyed navy"
    cost text,                       -- rough cost and effort in plain words
    effort text,
    prompt text not null,            -- what the image model was told
    target_hex text,                 -- the colour asked for, for a dye or fade
    image text,                      -- file name under images/; never shared as a Vinted photo
    status text not null,            -- queued | working | ready | failed
    error text,
    added_at text not null
);
"""

# Columns added to items after the first version, for databases made before them.
ADDED_COLUMNS = {"tidy": "text", "tidy_status": "text", "tidy_error": "text", "size": "text",
                 "listing": "text", "draft": "integer not null default 0", "colour_hex": "text",
                 "colour_hex_2": "text", "colour_hex_set_by_hand": "integer not null default 0",
                 "owner": "text"}  # the user's id in the public copy; null in the personal one
ADDED_PHOTO_COLUMNS = {"part": "text", "tidy_note": "text"}  # tidy_note: the owner's words for its tidy
# old_key_hash: sha256 of the key a returning user's sign-in replaced, until they've
# deleted it at OpenRouter (an app can't delete keys itself).
ADDED_USER_COLUMNS = {"old_key_hash": "text"}
# Old one-photo columns, kept in the table but not shown.
OLD_ITEM_COLUMNS = ("original", "cutout", "tidy", "tidy_status", "tidy_error")
# What if I… (tidyrack/upcycle.py): the plan, and for an upcycle its goal as JSON
# (the chosen edit's picture and label, and the how-to).
ADDED_COLUMNS.update({"plan": "text", "goal": "text"})  # plan: keep | upcycle | sell


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# The measured colours: JSON from colour.measure, the main colours with their shares.
ADDED_COLUMNS["colours"] = "text"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.images = self.root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "tidyrack.db"
        if (self.root / "wardrobe.db").exists() and not self.db_path.exists():
            self.db_path = self.root / "wardrobe.db"  # an install from before the rename keeps its file
        with self._connect() as db:
            db.executescript(SCHEMA)
            have = {row["name"] for row in db.execute("pragma table_info(items)")}
            for name, kind in ADDED_COLUMNS.items():
                if name not in have:
                    db.execute(f"alter table items add column {name} {kind}")
            have = {row["name"] for row in db.execute("pragma table_info(photos)")}
            for name, kind in ADDED_PHOTO_COLUMNS.items():
                if name not in have:
                    db.execute(f"alter table photos add column {name} {kind}")
            have = {row["name"] for row in db.execute("pragma table_info(users)")}
            for name, kind in ADDED_USER_COLUMNS.items():
                if name not in have:
                    db.execute(f"alter table users add column {name} {kind}")
            # Items from before the photos table: their one photo becomes the cover.
            for row in db.execute(
                "select * from items where id not in (select item_id from photos)"
            ).fetchall():
                db.execute(
                    "insert into photos (id, item_id, position, kind, added_at, original, cutout,"
                    " cutout_status, tidy, tidy_status, error) values (?, ?, 0, 'photo', ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), row["id"], row["added_at"], row["original"], row["cutout"],
                     "ready" if row["cutout"] else None, row["tidy"], row["tidy_status"], row["tidy_error"]),
                )
            # Jobs run inside the server, so any still marked running were cut off by a restart.
            db.execute("update items set status = 'failed', error = 'Interrupted by a restart'"
                       " where status = 'processing'")
            for step in ("cutout", "tidy"):
                db.execute(f"update photos set {step}_status = 'failed', error = 'Interrupted by a restart'"
                           f" where {step}_status in ('queued', 'working')")
            db.execute("update tryons set status = 'failed', error = 'Interrupted by a restart' where status = 'working'")
            db.execute("update edits set status = 'failed', error = 'Interrupted by a restart'"
                       " where status in ('queued', 'working')")

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    # Items

    def create(self, photo: bytes, suffix: str, kind: str = "photo", part: str | None = None,
               owner: str | None = None) -> dict:
        """Start a draft item from its first photo."""
        item_id = _new_id()
        with self._connect() as db:
            db.execute(
                "insert into items (id, added_at, status, original, draft, owner)"
                " values (?, ?, 'processing', '', 1, ?)",
                (item_id, _now(), owner),
            )
        self.add_photo(item_id, photo, suffix, kind, part)
        return self.get(item_id)

    def finish(self, item_id: str) -> None:
        with self._connect() as db:
            db.execute("update items set draft = 0 where id = ?", (item_id,))

    def save_guesses(self, item_id: str, guesses: dict) -> None:
        """Store the model's guesses. A field you have filled in or checked is
        kept; anything still a guess (or empty) takes the new guess."""
        item = self.get(item_id)
        if item is None:
            return
        unchecked = list(item["unchecked"])
        values = {}
        for field in GUESSED_FIELDS:
            guess = guesses.get(field) or None
            confirmed = item[field] is not None and field not in unchecked
            if guess is None or confirmed:
                continue
            values[field] = guess
            if field not in unchecked:
                unchecked.append(field)
        with self._connect() as db:
            for field, value in values.items():
                db.execute(f"update items set {field} = ? where id = ?", (value, item_id))
            db.execute(
                "update items set unchecked = ?, status = 'ready', error = null where id = ?",
                (json.dumps(unchecked), item_id),
            )

    def save_listing(self, item_id: str, listing: dict) -> None:
        with self._connect() as db:
            db.execute("update items set listing = ? where id = ?", (json.dumps(listing), item_id))

    def save_colour(self, item_id: str, hexes: list[str] | None, measured: dict | None = None) -> None:
        """Store the colour worked out from the pixels, unless you picked it (or
        hexes is None). The measured colours with their shares are kept either way."""
        with self._connect() as db:
            if hexes is not None:
                main, second = (hexes + [None, None])[:2]
                db.execute("update items set colour_hex = ?, colour_hex_2 = ? where id = ?"
                           " and colour_hex_set_by_hand = 0", (main, second, item_id))
            if measured is not None:
                db.execute("update items set colours = ? where id = ?", (json.dumps(measured), item_id))

    def mark_failed(self, item_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute("update items set status = 'failed', error = ? where id = ?", (error, item_id))

    def mark_processing(self, item_id: str) -> None:
        with self._connect() as db:
            db.execute("update items set status = 'processing', error = null where id = ?", (item_id,))

    def update(self, item_id: str, changes: dict, checked: list[str]) -> dict | None:
        """Apply your edits. An edited field, or one you tick as checked,
        is no longer a guess."""
        item = self.get(item_id)
        if item is None:
            return None
        hexes = {k: v for k, v in changes.items() if k in ("colour_hex", "colour_hex_2")}
        changes = {k: v for k, v in changes.items() if k in EDITABLE_FIELDS}
        confirmed = set(changes) | set(checked)
        unchecked = [f for f in item["unchecked"] if f not in confirmed]
        with self._connect() as db:
            for field, value in changes.items():
                if isinstance(value, str):
                    value = value.strip() or None
                db.execute(f"update items set {field} = ? where id = ?", (value, item_id))
            db.execute("update items set unchecked = ? where id = ?", (json.dumps(unchecked), item_id))
            # A colour you pick is kept: later recomputes leave it alone.
            for field, value in hexes.items():
                db.execute(f"update items set {field} = ?, colour_hex_set_by_hand = 1 where id = ?",
                           (value.lower() if value else None, item_id))
        return self.get(item_id)

    def delete(self, item_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("select * from items where id = ?", (item_id,)).fetchone()
            if row is None:
                return False
            photos = db.execute("select * from photos where item_id = ?", (item_id,)).fetchall()
            tryons = db.execute("select image from tryons where item_id = ?", (item_id,)).fetchall()
            edits = db.execute("select image from edits where item_id = ?", (item_id,)).fetchall()
            db.execute("delete from photos where item_id = ?", (item_id,))
            db.execute("delete from tryons where item_id = ?", (item_id,))
            db.execute("delete from edits where item_id = ?", (item_id,))
            db.execute("delete from items where id = ?", (item_id,))
        names = [row[c] for c in ("original", "cutout", "tidy")]
        names += [p[c] for p in photos for c in ("original", "cutout", "tidy")]
        names += [t["image"] for t in tryons]
        names += [e["image"] for e in edits]
        self._unlink(names)
        return True

    def get(self, item_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from items where id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            photos = db.execute(
                "select * from photos where item_id = ? order by position", (item_id,)
            ).fetchall()
            tryons = db.execute("select * from tryons where item_id = ? order by added_at desc, rowid desc", (item_id,)).fetchall()
        return _item_dict(row, photos, tryons)

    def all(self, owner: str | None = None, everyone: bool = False) -> list[dict]:
        """The owner's items (the personal copy has no owners), or everyone's."""
        where, args = ("", ()) if everyone else ("where owner is ?", (owner,))
        with self._connect() as db:
            rows = db.execute(f"select * from items {where} order by added_at desc, rowid desc", args).fetchall()
            photos = db.execute(f"select * from photos where item_id in (select id from items {where})"
                                " order by position", args).fetchall()
            tryons = db.execute(f"select * from tryons where item_id in (select id from items {where})"
                                " order by added_at desc, rowid desc", args).fetchall()
        by_item, tryons_by_item = {}, {}
        for p in photos:
            by_item.setdefault(p["item_id"], []).append(p)
        for t in tryons:
            tryons_by_item.setdefault(t["item_id"], []).append(t)
        return [_item_dict(r, by_item.get(r["id"], []), tryons_by_item.get(r["id"], [])) for r in rows]

    # Photos

    def add_photo(self, item_id: str, photo: bytes, suffix: str, kind: str = "photo",
                  part: str | None = None) -> dict:
        photo_id = _new_id()
        original = f"{photo_id}-original{suffix}"
        (self.images / original).write_bytes(photo)
        with self._connect() as db:
            position = db.execute(
                "select coalesce(max(position) + 1, 0) from photos where item_id = ?", (item_id,)
            ).fetchone()[0]
            db.execute(
                "insert into photos (id, item_id, position, kind, added_at, original, part)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (photo_id, item_id, position, kind, _now(), original, part),
            )
        return self.get_photo(photo_id)

    def take_upload_slot(self, user_id: str, limit: int, hours: int) -> bool:
        """Record one more upload, unless the user already has `limit` in the last `hours`
        (deleted photos and failed tries included). Counts and records in one step, so
        uploads sent all at once can't all slip under the limit."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        db = self._connect()
        try:
            db.execute("begin immediate")  # holds the write lock between the count and the insert
            used = db.execute("select count(*) from uploads where user_id = ? and added_at > ?",
                              (user_id, since)).fetchone()[0]
            if used >= limit:
                db.rollback()
                return False
            db.execute("insert into uploads (user_id, added_at) values (?, ?)", (user_id, _now()))
            db.commit()
            return True
        finally:
            db.close()

    def photo_count(self, owner: str | None) -> int:
        with self._connect() as db:
            return db.execute("select count(*) from photos where item_id in (select id from items where owner is ?)",
                              (owner,)).fetchone()[0]

    def get_photo(self, photo_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from photos where id = ?", (photo_id,)).fetchone()
        return dict(row) if row else None

    def set_tidy_note(self, photo_id: str, note: str | None) -> None:
        """What the owner asked the tidy to do, such as keep the sleeves rolled; kept for every tidy after."""
        with self._connect() as db:
            db.execute("update photos set tidy_note = ? where id = ?", (note, photo_id))

    def photo_step(self, photo_id: str, step: str, status: str, error: str | None = None) -> None:
        """Set where the cutout or tidy of a photo has got to."""
        assert step in ("cutout", "tidy")
        with self._connect() as db:
            db.execute(f"update photos set {step}_status = ?, error = ? where id = ?", (status, error, photo_id))

    def save_photo_image(self, photo_id: str, step: str, png: bytes) -> None:
        assert step in ("cutout", "tidy")
        name = f"{photo_id}-{step}.png"
        (self.images / name).write_bytes(png)
        with self._connect() as db:
            saved = db.execute(
                f"update photos set {step} = ?, {step}_status = 'ready', error = null where id = ?",
                (name, photo_id),
            ).rowcount
        if not saved:  # the photo was deleted while its cutout or tidy ran: don't leave the file behind
            self._unlink([name])

    def set_kind(self, photo_id: str, kind: str) -> None:
        assert kind in PHOTO_KINDS
        with self._connect() as db:
            db.execute("update photos set kind = ? where id = ?", (kind, photo_id))

    def make_cover(self, photo_id: str) -> None:
        photo = self.get_photo(photo_id)
        with self._connect() as db:
            ids = [r["id"] for r in db.execute(
                "select id from photos where item_id = ? order by position", (photo["item_id"],))]
            ids.remove(photo_id)
            for position, pid in enumerate([photo_id] + ids):
                db.execute("update photos set position = ? where id = ?", (position, pid))

    def delete_photo(self, photo_id: str) -> None:
        photo = self.get_photo(photo_id)
        with self._connect() as db:
            db.execute("delete from photos where id = ?", (photo_id,))
            ids = [r["id"] for r in db.execute(
                "select id from photos where item_id = ? order by position", (photo["item_id"],))]
            for position, pid in enumerate(ids):
                db.execute("update photos set position = ? where id = ?", (position, pid))
        self._unlink([photo[c] for c in ("original", "cutout", "tidy")])

    # Selfies and try-ons, for On me

    def add_selfie(self, jpeg: bytes, owner: str | None = None) -> dict:
        """Keep a whole Outfit photo."""
        selfie_id = _new_id()
        original = f"{selfie_id}-selfie.jpg"
        (self.images / original).write_bytes(jpeg)
        with self._connect() as db:
            db.execute("insert into selfies (id, owner, added_at, original) values (?, ?, ?, ?)",
                       (selfie_id, owner, _now(), original))
        return self.get_selfie(selfie_id)

    def get_selfie(self, selfie_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from selfies where id = ?", (selfie_id,)).fetchone()
        return dict(row) if row else None

    def selfies(self, owner: str | None = None) -> list[dict]:
        """The owner's Outfit photos, newest first."""
        with self._connect() as db:
            rows = db.execute("select * from selfies where owner is ? order by added_at desc, rowid desc",
                              (owner,)).fetchall()
        return [dict(r) for r in rows]

    def delete_selfie(self, selfie_id: str) -> None:
        """Delete an Outfit photo. Its items keep their crops, and On me pictures made from it stay."""
        selfie = self.get_selfie(selfie_id)
        with self._connect() as db:
            db.execute("delete from selfies where id = ?", (selfie_id,))
        self._unlink([selfie["original"]])

    def add_tryon(self, item_id: str, selfie_id: str) -> dict:
        tryon_id = _new_id()
        with self._connect() as db:
            db.execute("insert into tryons (id, item_id, selfie_id, added_at, status) values (?, ?, ?, ?, 'working')",
                       (tryon_id, item_id, selfie_id, _now()))
        return self.get_tryon(tryon_id)

    def get_tryon(self, tryon_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from tryons where id = ?", (tryon_id,)).fetchone()
        return dict(row) if row else None

    def save_tryon(self, tryon_id: str, png: bytes) -> None:
        name = f"{tryon_id}-tryon.png"
        (self.images / name).write_bytes(png)
        with self._connect() as db:
            saved = db.execute("update tryons set image = ?, status = 'ready', error = null where id = ?",
                               (name, tryon_id)).rowcount
        if not saved:  # deleted while it was being drawn
            self._unlink([name])

    def fail_tryon(self, tryon_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute("update tryons set status = 'failed', error = ? where id = ?", (error, tryon_id))

    def delete_tryon(self, tryon_id: str) -> None:
        tryon = self.get_tryon(tryon_id)
        with self._connect() as db:
            db.execute("delete from tryons where id = ?", (tryon_id,))
        self._unlink([tryon["image"]])

    # What if I… edits, and the plan for an item

    def add_edit(self, item_id: str, option: dict) -> dict:
        """Queue an edit. A new try of the same edit replaces the old one (unless it's
        the item's goal); the person's own ideas are each kept."""
        edit_id = _new_id()
        goal = (self.get(item_id) or {}).get("goal") or {}
        with self._connect() as db:
            old = [] if option["key"] == "own" else db.execute(
                "select id, image from edits where item_id = ? and option = ? and id is not ?",
                (item_id, option["key"], goal.get("edit_id"))).fetchall()
            db.executemany("delete from edits where id = ?", [(r["id"],) for r in old])
            db.execute(
                "insert into edits (id, item_id, option, label, cost, effort, prompt, target_hex, status, added_at)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (edit_id, item_id, option["key"], option["label"], option.get("cost"), option.get("effort"),
                 option["prompt"], option.get("target_hex"), _now()))
        self._unlink([r["image"] for r in old])
        return self.get_edit(edit_id)

    def get_edit(self, edit_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("select * from edits where id = ?", (edit_id,)).fetchone()
        return dict(row) if row else None

    def edits(self, item_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "select * from edits where item_id = ? order by added_at, rowid", (item_id,))]

    def edit_step(self, edit_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute("update edits set status = ?, error = ? where id = ?", (status, error, edit_id))

    def save_edit_image(self, edit_id: str, png: bytes) -> None:
        name = f"{edit_id}-edit.png"
        (self.images / name).write_bytes(png)
        with self._connect() as db:
            saved = db.execute("update edits set image = ?, status = 'ready', error = null where id = ?",
                               (name, edit_id)).rowcount
        if not saved:  # replaced or deleted while it was drawn
            self._unlink([name])

    def set_plan(self, item_id: str, plan: str | None, goal: dict | None = None) -> None:
        """Keep, upcycle or sell (None: not decided). Only an upcycle has a goal."""
        with self._connect() as db:
            db.execute("update items set plan = ?, goal = ? where id = ?",
                       (plan, json.dumps(goal) if goal else None, item_id))

    # Users and sessions, in the public copy

    def save_user(self, openrouter_id: str, key: str) -> str:
        """Add someone signing in for the first time, or store their new key. Returns their id."""
        with self._connect() as db:
            row = db.execute("select id from users where openrouter_id = ?", (openrouter_id,)).fetchone()
            if row:
                db.execute("update users set key = ? where id = ?", (key, row["id"]))
                return row["id"]
            user_id = _new_id()
            db.execute("insert into users (id, openrouter_id, key, added_at) values (?, ?, ?, ?)",
                       (user_id, openrouter_id, key, _now()))
            return user_id

    def key_of(self, openrouter_id: str) -> str | None:
        """The stored (encrypted) key of someone who has signed in before."""
        with self._connect() as db:
            row = db.execute("select key from users where openrouter_id = ?", (openrouter_id,)).fetchone()
        return row["key"] if row else None

    def set_old_key_hash(self, user_id: str, key_hash: str | None) -> None:
        with self._connect() as db:
            db.execute("update users set old_key_hash = ? where id = ?", (key_hash, user_id))

    def old_key_hash(self, user_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("select old_key_hash from users where id = ?", (user_id,)).fetchone()
        return row["old_key_hash"] if row else None

    def user_key(self, user_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("select key from users where id = ?", (user_id,)).fetchone()
        return row["key"] if row else None

    def new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute("insert into sessions (token_hash, user_id, added_at) values (?, ?, ?)",
                       (_hash(token), user_id, _now()))
        return token

    def session_user(self, token: str) -> str | None:
        """Who the session cookie belongs to, if it's less than SESSION_DAYS old."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        with self._connect() as db:
            row = db.execute("select user_id from sessions where token_hash = ? and added_at > ?",
                             (_hash(token), cutoff)).fetchone()
        return row["user_id"] if row else None

    def renew_session(self, token: str) -> None:
        """Count the session's SESSION_DAYS from now: someone who keeps using the app stays signed in."""
        with self._connect() as db:
            db.execute("update sessions set added_at = ? where token_hash = ?", (_now(), _hash(token)))

    def end_session(self, token: str) -> None:
        with self._connect() as db:
            db.execute("delete from sessions where token_hash = ?", (_hash(token),))

    def delete_user(self, user_id: str) -> None:
        """Delete my account: the person's items and their pictures, their stored key,
        their sign-ins and their upload counts. Nobody else's are touched."""
        with self._connect() as db:
            item_ids = [r["id"] for r in db.execute("select id from items where owner = ?", (user_id,))]
        for item_id in item_ids:
            self.delete(item_id)
        selfies = self.selfies(user_id)
        with self._connect() as db:
            db.execute("delete from selfies where owner = ?", (user_id,))
        self._unlink([s["original"] for s in selfies])
        with self._connect() as db:
            for table, column in (("sessions", "user_id"), ("uploads", "user_id"), ("users", "id")):
                db.execute(f"delete from {table} where {column} = ?", (user_id,))

    # Files

    def image_owner(self, name: str) -> tuple[bool, str | None]:
        """Whether a picture belongs to an item or a selfie, and whose it is."""
        with self._connect() as db:
            row = db.execute(
                "select owner from items where ? in (original, cutout, tidy) or id in"
                " (select item_id from photos where ? in (original, cutout, tidy))"
                " or id in (select item_id from tryons where image = ?)"
                " or id in (select item_id from edits where image = ?)"
                " union all select owner from selfies where original = ?", (name, name, name, name, name)).fetchone()
        return (True, row["owner"]) if row else (False, None)

    def image_path(self, name: str) -> Path | None:
        path = (self.images / name).resolve()
        if path.parent != self.images.resolve() or not path.is_file():
            return None
        return path

    def _unlink(self, names) -> None:
        for name in names:
            if name:
                (self.images / name).unlink(missing_ok=True)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _item_dict(row: sqlite3.Row, photos, tryons=()) -> dict:
    item = {k: row[k] for k in row.keys() if k not in OLD_ITEM_COLUMNS}
    item["unchecked"] = json.loads(item["unchecked"])
    item["listing"] = json.loads(item["listing"]) if item["listing"] else None
    item["draft"] = bool(item["draft"])
    item["colour_hex_set_by_hand"] = bool(item["colour_hex_set_by_hand"])
    item["colours"] = json.loads(item["colours"]) if item["colours"] else None
    item["photos"] = [dict(p) for p in photos]
    item["tryons"] = [dict(t) for t in tryons]  # On me pictures, newest first: never Vinted photos
    item["goal"] = json.loads(item["goal"]) if item["goal"] else None
    return item
