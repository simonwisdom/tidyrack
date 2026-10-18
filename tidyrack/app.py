"""The web app: add photos, browse items, fix the model's guesses."""

import io
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import segno
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tidyrack import ai, auth, colour, shoplink, upcycle
from tidyrack.export import export_zip
from tidyrack.store import BUSY, PHOTO_KINDS, SESSION_DAYS, Store
from tidyrack import looks  # the Looks button: outfits from the clothes

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(ai.env("DATA", ROOT / "data"))
# The https port `tailscale serve` publishes the app on; ./run.sh phone uses the same setting.
TAILSCALE_PORT = int(ai.env("TAILSCALE_PORT", "10000"))
# Photos sent to Codex in one call; more makes it slower and rarely helps.
MAX_PHOTOS_READ = 6
# Jobs of each kind run at once; the rest wait their turn, so a batch of photos
# can't swamp the Mac. Cutouts share one model (and one lock in ai.py), and each
# tidy is a 30-40 s Codex call.
LANES = {"cutout": 1, "tidy": 3, "details": 2, "tryon": 2}
HEX = r"^#[0-9a-fA-F]{6}$"
# The public copy: people sign in and see only their own items. the personal copy has no sign-in.
PUBLIC = ai.env("PUBLIC") == "1"
SESSION_COOKIE = "tidyrack_session"
# Holds the sign-in's state and PKCE secret while the person is away at OpenRouter.
SIGN_IN_COOKIE = "tidyrack_signin"
# Limits per person in the public copy: cutouts run on the server, so one person
# mustn't be able to fill its disk or keep it busy for everyone else.
MAX_UPLOAD = 25_000_000
DAILY_PHOTOS = int(ai.env("DAILY_PHOTOS", "100"))
MAX_PHOTOS_PER_USER = 2000
LINKS_AT_ONCE = 4  # Add from link reads running at once, for everyone together
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
# The public copy's security headers,
# without PostHog: the public copy has no analytics.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                               "connect-src 'self' https://feedback.simonwisdom.com; img-src 'self' data:; "
                               "style-src 'self' 'unsafe-inline'; font-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'self'; form-action 'self'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}

log = logging.getLogger("uvicorn.error")


def timed(step: str, fn, *args):
    """Run one model step and log how long it took, to see where time goes."""
    start = time.monotonic()
    try:
        return fn(*args)
    finally:
        log.info("%s took %.1fs", step, time.monotonic() - start)


class Jobs:
    """The model work, run in the background after each upload is saved, so a
    slow or failed model never loses a photo."""

    def __init__(self, store: Store, pipeline, run, key_for):
        """`key_for(owner)` is the OpenRouter key an owner's model calls run on (None: your own Codex)."""
        self.store, self.pipeline, self.run, self.key_for = store, pipeline, run, key_for
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def jpeg(self, photo: dict) -> bytes:
        return self.pipeline.upright_jpeg((self.store.images / photo["original"]).read_bytes())

    def new_photo(self, photo: dict) -> None:
        """Queue the cutout and tidy of a view photo. A shop photo gets only the
        cutout (it's already a catalogue shot); label close-ups get neither. A worn
        photo comes with its cutout from the clothing model, so it's only tidied."""
        steps = {"photo": ("cutout", "tidy"), "shop": ("cutout",), "worn": ("tidy",)}.get(photo["kind"], ())
        for step in steps:
            if not photo[step] and photo[f"{step}_status"] not in BUSY:
                self.queue(photo["id"], step)

    def queue(self, photo_id: str, step: str) -> None:
        self.store.photo_step(photo_id, step, "queued")
        self.run(step, self._image_step, photo_id, step)

    def _image_step(self, photo_id: str, step: str) -> None:
        photo = self.store.get_photo(photo_id)
        if photo is None:
            return
        self.store.photo_step(photo_id, step, "working")
        try:
            if step == "cutout":  # runs here, so it costs nobody anything
                png = timed(step, self.pipeline.remove_background, self.jpeg(photo))
            else:
                # A worn photo's tidy draws only its garment, not the rest of the outfit.
                part = photo["part"] if photo["kind"] == "worn" else None
                key = self.key_for(self.store.get(photo["item_id"])["owner"])
                png = timed(step, self.pipeline.tidy_up, self.jpeg(photo), part, key)
            self.store.save_photo_image(photo_id, step, png)
        except Exception as exc:  # shown on the photo so you can try again
            if self.store.get_photo(photo_id):
                self.store.photo_step(photo_id, step, "failed", failure(exc))
            return
        if step == "cutout":
            self.colour(photo["item_id"])
        elif photo["kind"] == "worn":
            self.colour_from_tidy(photo_id, png)

    def colour_from_tidy(self, photo_id: str, tidy_png: bytes) -> None:
        """A worn garment's colour from its tidied picture, which drops the room's
        shading: the tidy is cut out and its pixels grouped like any cutout's."""
        photo = self.store.get_photo(photo_id)
        item = photo and self.store.get(photo["item_id"])
        if not item or (colour.colour_photo(item) or {}).get("id") != photo_id:
            return
        try:
            jpeg = self.pipeline.upright_jpeg(tidy_png)
            cutout = timed("tidy cutout", self.pipeline.remove_background, jpeg)
            measured = colour.measure(cutout) | {"source": "tidy"}
            self.store.save_colour(item["id"], colour.list_hexes(measured["colours"]), measured)
        except Exception:  # the cutout's colour stays
            log.exception("Couldn't work out the colour of %s from its tidy", item["id"])

    def colour(self, item_id: str) -> None:
        """Work out the item's colour from its pixels again. It's quick, so it runs here and now."""
        try:
            colour.refresh(self.store, item_id, self.pipeline.upright_jpeg)
        except Exception:  # a colour that can't be worked out shouldn't break anything else
            log.exception("Couldn't work out the colour of %s", item_id)

    def backfill_colours(self) -> None:
        try:
            log.info("Worked out the colour of %d older items", colour.backfill(self.store, self.pipeline.upright_jpeg))
        except Exception:
            log.exception("Couldn't work out the colours of older items")

    def what_if(self, edit_id: str, picture: str) -> None:
        """Draw one What if I… edit from the item's picture, in the tidy lane."""
        self.run("tidy", self._what_if, edit_id, picture)

    def _what_if(self, edit_id: str, picture: str) -> None:
        edit = self.store.get_edit(edit_id)
        if edit is None:
            return
        self.store.edit_step(edit_id, "working")
        try:
            key = self.key_for(self.store.get(edit["item_id"])["owner"])
            jpeg = self.pipeline.upright_jpeg((self.store.images / picture).read_bytes())
            self.store.save_edit_image(edit_id, timed("what if", self.pipeline.redraw, jpeg, edit["prompt"], key))
        except Exception as exc:
            if self.store.get_edit(edit_id):
                self.store.edit_step(edit_id, "failed", failure(exc))

    def details(self, item_id: str) -> None:
        """Read the details from all the item's photos (again)."""
        self.store.mark_processing(item_id)
        self.run("details", self._details, item_id)

    def _details(self, item_id: str) -> None:
        # One read per item at a time, so a slow early read can't land after a later one.
        with self._locks_guard:
            lock = self._locks.setdefault(item_id, threading.Lock())
        with lock:
            item = self.store.get(item_id)
            if item is None:
                return
            try:
                jpegs = [self.jpeg(p) for p in item["photos"][:MAX_PHOTOS_READ]]
                # From an outfit photo: read only this garment, not the rest of the outfit in the crop.
                part = next((p["part"] for p in item["photos"] if p["kind"] == "worn"), None)
                guesses = timed("details", self.pipeline.guess_details, jpegs, self.key_for(item["owner"]), part)
                if any(p["kind"] == "shop" for p in item["photos"]):
                    # The shop's own details beat a guess from its photo: only fill the gaps.
                    guesses = {k: v for k, v in guesses.items() if not item.get(k)}
                self.store.save_guesses(item_id, guesses)
            except Exception as exc:  # shown on the item so you can retry
                if self.store.get(item_id):
                    self.store.mark_failed(item_id, failure(exc))

    def tryon(self, tryon_id: str) -> None:
        self.run("tryon", self._tryon, tryon_id)

    def _tryon(self, tryon_id: str) -> None:
        """On me: draw the item onto the selfie. The garment goes in as its cover's
        cutout on white (real pixels, nothing else in view), or the cover itself."""
        tryon = self.store.get_tryon(tryon_id)
        item = tryon and self.store.get(tryon["item_id"])
        selfie = tryon and self.store.get_selfie(tryon["selfie_id"])
        if not item or not selfie:
            if tryon:
                self.store.fail_tryon(tryon_id, "That Outfit photo has been deleted.")
            return
        try:
            cover = item["photos"][0]
            if cover["cutout"]:
                garment = self.pipeline.on_white((self.store.images / cover["cutout"]).read_bytes())
            else:
                garment = self.jpeg(cover)
            what = " ".join(v for v in (item["colour"], item["type"]) if v) or "garment"
            png = timed("try on", self.pipeline.try_on, (self.store.images / selfie["original"]).read_bytes(),
                        garment, what, self.key_for(item["owner"]))
            self.store.save_tryon(tryon_id, png)
        except Exception as exc:
            self.store.fail_tryon(tryon_id, failure(exc))


def failure(exc: Exception) -> str:
    """Why a model step failed, as shown on the item: plain words when the person can act on it."""
    return str(exc) if isinstance(exc, ai.ModelError) else f"{type(exc).__name__}: {exc}"


class ItemChanges(BaseModel):
    type: str | None = None
    colour: str | None = None
    fabric: str | None = None
    brand: str | None = None
    size: str | None = None
    amount_paid: float | None = None
    notes: str | None = None
    colour_hex: str | None = Field(None, pattern=HEX)
    colour_hex_2: str | None = Field(None, pattern=HEX)
    checked: list[str] = []


class PhotoChanges(BaseModel):
    kind: str


class ShopLink(BaseModel):
    url: str


class TryOnChoice(BaseModel):
    selfie_id: str | None = None  # None: the newest Outfit photo


NO_SELFIE = ("On me needs a photo of you wearing clothes. Tap Outfit and add a mirror selfie "
             "with your whole outfit in view; it's kept for trying things on.")


class WhatIf(BaseModel):
    option: str  # an offered edit's key, or "own" with the person's text
    text: str = Field("", max_length=upcycle.FREE_TEXT_MAX)


class Plan(BaseModel):
    plan: str | None  # keep | upcycle | sell; None: not decided
    edit_id: str | None = None  # an upcycle's goal


def create_app(store: Store, pipeline=ai, warm_up: bool = False, run=None,
               read_link=shoplink.read_link, public: bool = False, secret: str | None = None,
               public_url: str | None = None, exchange=auth.exchange) -> FastAPI:
    """`run(lane, fn, *args)` starts a background job in one of LANES; tests pass
    one that runs it at once. `read_link(url)` fetches a shop's product page;
    tests pass one that reads saved pages. `public` turns on Sign in with
    OpenRouter: `secret` encrypts the keys, `public_url` is the address
    OpenRouter sends people back to, and `exchange(code, verifier)` trades
    the code for (key, user id); tests pass a fake."""
    keys = auth.Keys(secret) if public else None

    def key_for(owner: str | None) -> str | None:
        """The owner's own OpenRouter key in the public copy; None in the personal copy, which uses Codex."""
        if not public:
            return None
        locked = owner and store.user_key(owner)
        key = locked and keys.unlock(locked)
        if not key:
            raise ai.ModelError("Tidyrack can't read your OpenRouter key. Sign out and sign in again.")
        return key

    pools = {}
    if run is None:
        pools = {lane: ThreadPoolExecutor(n, thread_name_prefix=lane) for lane, n in LANES.items()}
        run = lambda lane, fn, *args: pools[lane].submit(fn, *args)  # noqa: E731
    jobs = Jobs(store, pipeline, run, key_for)

    @asynccontextmanager
    async def lifespan(app):
        if warm_up:
            threading.Thread(target=pipeline.warm_up, daemon=True).start()
        # Items from before colours were worked out get theirs (once; it's quick).
        threading.Thread(target=jobs.backfill_colours, daemon=True).start()
        yield
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Tidyrack", lifespan=lifespan)

    def current_user(request: Request) -> str | None:
        """Who is signed in: their user id in the public copy, None in the personal copy."""
        if not public:
            return None
        user = store.session_user(request.cookies.get(SESSION_COOKIE) or "")
        if user is None:
            raise HTTPException(401, "Sign in first")
        return user

    def item_or_404(item_id: str, user: str | None) -> dict:
        """The item, if it's the user's. Someone else's item looks the same as a missing one."""
        item = store.get(item_id)
        if item is None or item["owner"] != user:
            raise HTTPException(404, "No such item")
        return item

    def photo_or_404(photo_id: str, user: str | None) -> dict:
        photo = store.get_photo(photo_id)
        item = photo and store.get(photo["item_id"])
        if not item or item["owner"] != user:
            raise HTTPException(404, "No such photo")
        return photo

    links_guard, links_running = threading.Lock(), set()

    @app.middleware("http")
    async def sign_in_before_upload(request: Request, call_next):
        """In the public copy, turn away a change from someone not signed in, or an
        upload that's too big, before its body is read: FastAPI would otherwise
        read the whole upload to disk first."""
        if public and request.method in ("POST", "PATCH", "DELETE") and request.url.path.startswith("/api/") \
                and request.url.path != "/api/sign-out":
            if not store.session_user(request.cookies.get(SESSION_COOKIE) or ""):
                return JSONResponse({"detail": "Sign in first"}, status_code=401)
            length = request.headers.get("content-length")
            if length is None and request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "Uploads need a length"}, status_code=411)
            if length and (not length.isdigit() or int(length) > MAX_UPLOAD + 1_000_000):
                return JSONResponse({"detail": f"That photo is over {MAX_UPLOAD // 1_000_000} MB"}, status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """The public copy sends security headers on every response (added
        last, so it wraps the sign-in check above and its refusals get them too)."""
        response = await call_next(request)
        if public:
            response.headers.update(SECURITY_HEADERS)
        return response

    def public_only() -> None:
        if not public:
            raise HTTPException(404, "Only in the public copy")

    @app.get("/api/me")
    def me(request: Request, response: Response):
        """Whether this copy has sign-in, whether this browser is signed in, and the
        OpenRouter page of an old key the person can now delete. Each visit restarts
        the sign-in's SESSION_DAYS, so only people who stay away that long sign in again
        (and each sign-in makes a new OpenRouter key)."""
        token = request.cookies.get(SESSION_COOKIE)
        user = public and token and store.session_user(token)
        if user:
            store.renew_session(token)
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                                httponly=True, secure=True, samesite="lax")
        old = user and store.old_key_hash(user)
        return {"public": public, "signed_in": not public or bool(user),
                "old_key_url": f"https://openrouter.ai/keys/{old}" if old else None}

    @app.post("/api/old-key/done", status_code=204)
    def old_key_done(user=Depends(current_user)):
        """The person has dealt with their old key: stop showing it."""
        public_only()
        store.set_old_key_hash(user, None)

    @app.get("/auth/openrouter")
    def sign_in(request: Request):
        """Send the person to OpenRouter to approve Tidyrack."""
        public_only()
        state, verifier = auth.new_verifier(), auth.new_verifier()
        callback = (public_url or str(request.base_url)).rstrip("/") + "/auth/callback"
        response = RedirectResponse(auth.sign_in_url(callback, verifier, state), status_code=303)
        response.set_cookie(SIGN_IN_COOKIE, f"{state}.{verifier}", max_age=600, path="/auth",
                            httponly=True, secure=True, samesite="lax")
        return response

    @app.get("/auth/callback")
    def signed_in_at_openrouter(request: Request, code: str = "", state: str = ""):
        """OpenRouter sends the person back here with a one-time code."""
        public_only()
        saved_state, _, verifier = (request.cookies.get(SIGN_IN_COOKIE) or "").partition(".")
        try:
            if not code:
                raise auth.SignInError("cancelled")
            if not verifier or not secrets.compare_digest(saved_state, state):
                raise auth.SignInError("expired")
            key, openrouter_id = exchange(code, verifier)
        except auth.SignInError as exc:
            response = RedirectResponse("/?" + urlencode({"sign_in_error": str(exc)}), status_code=303)
        else:
            # Each sign-in makes a new key at OpenRouter, and an app can't delete keys. So
            # a returning person's old key is shown to them, linked, to delete themselves.
            locked = store.key_of(openrouter_id)
            old_key = locked and keys.unlock(locked)
            user = store.save_user(openrouter_id, keys.lock(key))
            if old_key and old_key != key:
                store.set_old_key_hash(user, auth.key_hash(old_key))
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(SESSION_COOKIE, store.new_session(user), max_age=SESSION_DAYS * 86400,
                                httponly=True, secure=True, samesite="lax")
        response.delete_cookie(SIGN_IN_COOKIE, path="/auth")
        return response

    @app.post("/api/sign-out", status_code=204)
    def sign_out(request: Request):
        public_only()
        store.end_session(request.cookies.get(SESSION_COOKIE) or "")
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.delete("/api/account")
    def delete_account(user=Depends(current_user)):
        """Delete my account: everything Tidyrack keeps about the person, then sign them
        out. Their keys at OpenRouter are theirs to delete (an app can't), so the answer
        links to each one."""
        public_only()
        current = keys.unlock(store.user_key(user) or "")
        hashes = [auth.key_hash(current) if current else None, store.old_key_hash(user)]
        store.delete_user(user)
        looks.forget(store, user)
        response = JSONResponse({"key_urls": [f"https://openrouter.ai/keys/{h}" for h in hashes if h]})
        response.delete_cookie(SESSION_COOKIE)
        return response

    def check_kind(kind: str) -> None:
        if kind not in PHOTO_KINDS:
            raise HTTPException(400, f"kind must be one of {', '.join(PHOTO_KINDS)}")
        if kind == "worn":  # it needs its garment and cutout, which only an outfit photo gives
            raise HTTPException(400, "Worn photos only come from Add outfit")

    async def read_upload(photo: UploadFile, user: str | None) -> tuple[bytes, str]:
        if public:
            take_upload_slot(user)
        data = await photo.read(MAX_UPLOAD + 1 if public else -1)
        if not data:
            raise HTTPException(400, "Empty upload")
        suffix = Path(photo.filename or "").suffix.lower() or ".jpg"
        if public:
            # Pictures are served back with a type that follows their name, so a stranger's
            # "photo" called page.html must never be stored as one.
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"That photo is over {MAX_UPLOAD // 1_000_000} MB")
            if suffix not in PHOTO_SUFFIXES or not ai.is_image(data):
                raise HTTPException(400, "That file isn't a photo Tidyrack can read (JPEG, PNG or WebP, "
                                         f"up to {ai.MAX_PIXELS // 1_000_000} megapixels)")
        return data, suffix

    def take_upload_slot(user: str) -> None:
        """Count a photo, or a link, against the person's limits before any work starts.
        Tries count too, so failing on purpose doesn't get round the limit."""
        if store.photo_count(user) >= MAX_PHOTOS_PER_USER:
            raise HTTPException(429, f"You have {MAX_PHOTOS_PER_USER} photos, the most Tidyrack keeps for one "
                                     "person. Delete some to add more.")
        if not store.take_upload_slot(user, DAILY_PHOTOS, hours=24):
            raise HTTPException(429, f"You've added {DAILY_PHOTOS} photos today, the most for one day. "
                                     "Try again tomorrow.")

    @app.get("/api/items")
    def list_items(user=Depends(current_user)):
        return store.all(user)

    @app.post("/api/items", status_code=201)
    async def add_item(photo: UploadFile, kind: str = Form("photo"), user=Depends(current_user)):
        """Start a draft item from its first photo. Its cutout, tidy and a first
        read of the details start at once, while you take the next photo."""
        check_kind(kind)
        data, suffix = await read_upload(photo, user)
        item = store.create(data, suffix, kind, owner=user)
        jobs.new_photo(item["photos"][0])
        jobs.details(item["id"])
        return store.get(item["id"])

    @app.post("/api/items/from-outfit", status_code=201)
    async def add_from_outfit(photo: UploadFile, user=Depends(current_user)):
        """One item for each garment in a photo of someone wearing an outfit. The
        clothing model finds them and cuts each out; each gets a crop of the photo
        as its photo, then a tidy and a read of its details like any other."""
        data, _ = await read_upload(photo, user)
        try:
            jpeg = pipeline.upright_jpeg(data)
            parts = await run_in_threadpool(timed, "split outfit", pipeline.split_outfit, jpeg)
        except Exception as exc:
            log.exception("Splitting an outfit photo failed")
            raise HTTPException(502, f"Couldn't look for clothes in that photo ({type(exc).__name__}).")
        if not parts:
            raise HTTPException(422, "No clothes found in that photo. Try one with the whole outfit in view.")
        store.add_selfie(jpeg, owner=user)  # the whole photo, for On me
        added = []
        for part in parts:
            item = store.create(part["photo"], ".jpg", "worn", part["part"], owner=user)
            photo_id = item["photos"][0]["id"]
            store.save_photo_image(photo_id, "cutout", part["cutout"])
            store.finish(item["id"])
            jobs.colour(item["id"])
            jobs.new_photo(store.get_photo(photo_id))
            jobs.details(item["id"])
            added.append(item["id"])
        return [store.get(item_id) for item_id in added]

    @app.post("/api/items/from-link", status_code=201)
    def add_from_link(link: ShopLink, user=Depends(current_user)):
        """A new item from a shop's product page: its photo becomes a shop photo,
        its details go in as guesses, and Codex reads the type from the photo."""
        url = link.url.strip()
        if public:
            take_upload_slot(user)
            # Each link can take a minute; one at a time per person, and a few in all,
            # so strangers' slow links can't tie up the server for everyone.
            with links_guard:
                if user in links_running or len(links_running) >= LINKS_AT_ONCE:
                    raise HTTPException(429, "Another link is still being read. Try again in a minute.")
                links_running.add(user)
        try:
            product, image = read_link(url)
        except shoplink.ShopLinkError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:  # anything unexpected still gets a plain message, not a broken item
            log.exception("Add from link failed for %s", url)
            raise HTTPException(502, f"Couldn't read that page ({type(exc).__name__}). {shoplink.SAVE_IT_YOURSELF}")
        finally:
            links_running.discard(user)
        item = store.create(image, ".jpg", "shop", owner=user)
        store.save_guesses(item["id"], {k: product[k] for k in ("brand", "colour", "fabric")})
        store.update(item["id"], {"notes": shoplink.notes(product, url)}, [])
        store.finish(item["id"])
        jobs.new_photo(store.get(item["id"])["photos"][0])
        jobs.details(item["id"])
        return store.get(item["id"])

    @app.post("/api/items/{item_id}/photos", status_code=201)
    async def add_photo(item_id: str, photo: UploadFile, kind: str = Form("photo"), user=Depends(current_user)):
        item = item_or_404(item_id, user)
        check_kind(kind)
        data, suffix = await read_upload(photo, user)
        jobs.new_photo(store.add_photo(item_id, data, suffix, kind))
        if not item["draft"]:
            # A photo added to a finished item: read the details again with it.
            jobs.details(item_id)
        return store.get(item_id)

    @app.post("/api/items/{item_id}/finish")
    def finish_item(item_id: str, user=Depends(current_user)):
        """Done adding photos: read the details again from all of them."""
        item = item_or_404(item_id, user)
        store.finish(item_id)
        if len(item["photos"]) > 1:
            jobs.details(item_id)
        return store.get(item_id)

    @app.get("/api/items/{item_id}")
    def get_item(item_id: str, user=Depends(current_user)):
        return item_or_404(item_id, user)

    @app.patch("/api/items/{item_id}")
    def update_item(item_id: str, changes: ItemChanges, user=Depends(current_user)):
        item_or_404(item_id, user)
        fields = changes.model_dump(exclude_unset=True)
        checked = fields.pop("checked", [])
        item = store.update(item_id, fields, checked)
        if item is None:
            raise HTTPException(404, "No such item")
        return item

    @app.post("/api/items/{item_id}/retry")
    def retry_item(item_id: str, user=Depends(current_user)):
        """Read the details again, and redo any cutout that failed."""
        item = item_or_404(item_id, user)
        for photo in item["photos"]:
            if photo["kind"] == "photo" and photo["cutout_status"] == "failed":
                store.photo_step(photo["id"], "cutout", None)
                jobs.new_photo(store.get_photo(photo["id"]))
        jobs.details(item_id)
        return store.get(item_id)

    @app.post("/api/items/{item_id}/listing")
    def write_listing(item_id: str, user=Depends(current_user)):
        """Draft a Vinted listing from the photos and the details as they are now."""
        item = item_or_404(item_id, user)
        details = {f: item[f] for f in ("type", "brand", "colour", "fabric", "size", "amount_paid", "notes")}
        details["unchecked"] = item["unchecked"]
        try:
            jpegs = [jobs.jpeg(p) for p in item["photos"][:MAX_PHOTOS_READ]]
            listing = timed("listing", pipeline.write_listing, jpegs, details, key_for(user))
        except Exception as exc:
            raise HTTPException(502, f"Couldn't write the listing: {exc}") from exc
        store.save_listing(item_id, listing)
        return store.get(item_id)

    def offered(item: dict) -> list[dict]:
        """The edits offered for an item; dye colours go with the owner's other clothes."""
        wardrobe = [h for i in store.all(item["owner"]) if i["id"] != item["id"]
                    for h in (i["colour_hex"], i["colour_hex_2"]) if h]
        return upcycle.options(item, wardrobe)

    def what_if_view(item: dict) -> dict:
        """The edits offered for an item and the ones drawn so far (without the prompts)."""
        hide = lambda d: {k: v for k, v in d.items() if k != "prompt"}  # noqa: E731
        return {"options": [hide(o) for o in offered(item)], "edits": [hide(e) for e in store.edits(item["id"])]}

    @app.get("/api/items/{item_id}/what-if")
    def what_if(item_id: str, user=Depends(current_user)):
        return what_if_view(item_or_404(item_id, user))

    @app.post("/api/items/{item_id}/what-if")
    def ask_what_if(item_id: str, ask: WhatIf, user=Depends(current_user)):
        """Draw the garment with one edit: an offered one, or the person's own idea."""
        item = item_or_404(item_id, user)
        picture = upcycle.source(item)
        if picture is None:
            raise HTTPException(400, "This item has no picture of the garment to start from")
        if ask.option == "own":
            if not ask.text.strip():
                raise HTTPException(400, "Say what you'd change")
            option = upcycle.own_option(ask.text)
        else:
            option = next((o for o in offered(item) if o["key"] == ask.option), None)
        if option is None:
            raise HTTPException(400, "That edit isn't offered for this item")
        jobs.what_if(store.add_edit(item_id, option)["id"], picture)
        return what_if_view(store.get(item_id))

    @app.post("/api/items/{item_id}/plan")
    def set_plan(item_id: str, choice: Plan, user=Depends(current_user)):
        """Keep, Upcycle or Sell. Upcycle takes a drawn edit as its goal picture and
        gets a how-to (materials and steps) written for it, in about 10 seconds."""
        item = item_or_404(item_id, user)
        if choice.plan not in (None, "keep", "upcycle", "sell"):
            raise HTTPException(400, "plan must be keep, upcycle or sell")
        goal = None
        if choice.plan == "upcycle":
            edit = choice.edit_id and store.get_edit(choice.edit_id)
            if not edit or edit["item_id"] != item_id or not edit["image"]:
                raise HTTPException(400, "Pick a finished What if I… picture as the goal")
            goal = item["goal"] if (item["goal"] or {}).get("edit_id") == edit["id"] else None
            if goal is None:
                details = ", ".join(f"{f}: {item[f]}" for f in ("type", "colour", "fabric", "brand") if item[f])
                try:
                    jpegs = [pipeline.upright_jpeg((store.images / name).read_bytes())
                             for name in (upcycle.source(item), edit["image"])]
                    howto = timed("how-to", pipeline.write_howto, jpegs, details, edit["label"], key_for(user))
                except Exception as exc:
                    raise HTTPException(502, f"Couldn't write the how-to: {failure(exc)}") from exc
                goal = {"edit_id": edit["id"], "image": edit["image"], "label": edit["label"], "howto": howto}
        store.set_plan(item_id, choice.plan, goal)
        return store.get(item_id)

    @app.delete("/api/items/{item_id}", status_code=204)
    def delete_item(item_id: str, user=Depends(current_user)):
        item_or_404(item_id, user)
        if not store.delete(item_id):
            raise HTTPException(404, "No such item")

    @app.patch("/api/photos/{photo_id}")
    def update_photo(photo_id: str, changes: PhotoChanges, user=Depends(current_user)):
        """Mark a photo as a label close-up, or back as a view of the garment."""
        photo = photo_or_404(photo_id, user)
        check_kind(changes.kind)
        store.set_kind(photo_id, changes.kind)
        jobs.new_photo(store.get_photo(photo_id))
        jobs.colour(photo["item_id"])
        return store.get(photo["item_id"])

    @app.post("/api/photos/{photo_id}/cover")
    def make_cover(photo_id: str, user=Depends(current_user)):
        photo = photo_or_404(photo_id, user)
        store.make_cover(photo_id)
        jobs.colour(photo["item_id"])
        return store.get(photo["item_id"])

    @app.post("/api/photos/{photo_id}/tidy")
    def tidy_photo(photo_id: str, user=Depends(current_user)):
        photo = photo_or_404(photo_id, user)
        if photo["kind"] not in ("photo", "worn"):
            raise HTTPException(400, "Label photos aren't tidied")
        if photo["tidy_status"] not in BUSY:
            jobs.queue(photo_id, "tidy")
        return store.get(photo["item_id"])

    @app.delete("/api/photos/{photo_id}")
    def delete_photo(photo_id: str, user=Depends(current_user)):
        photo = photo_or_404(photo_id, user)
        if len(store.get(photo["item_id"])["photos"]) == 1:
            raise HTTPException(400, "This is the item's only photo; delete the item instead")
        store.delete_photo(photo_id)
        jobs.colour(photo["item_id"])
        return store.get(photo["item_id"])

    @app.get("/api/selfies")
    def list_selfies(user=Depends(current_user)):
        """The person's Outfit photos, newest first, for On me."""
        return store.selfies(user)

    @app.delete("/api/selfies/{selfie_id}", status_code=204)
    def delete_selfie(selfie_id: str, user=Depends(current_user)):
        selfie = store.get_selfie(selfie_id)
        if selfie is None or selfie["owner"] != user:
            raise HTTPException(404, "No such Outfit photo")
        store.delete_selfie(selfie_id)

    @app.post("/api/items/{item_id}/tryons", status_code=201)
    def try_on(item_id: str, choice: TryOnChoice, user=Depends(current_user)):
        """On me: draw the item onto an Outfit photo, the newest unless one is picked."""
        item = item_or_404(item_id, user)
        selfies = store.selfies(user)
        if not selfies:
            raise HTTPException(409, NO_SELFIE)
        selfie = next((s for s in selfies if s["id"] == choice.selfie_id), None) if choice.selfie_id else selfies[0]
        if selfie is None:
            raise HTTPException(404, "No such Outfit photo")
        tryon = store.add_tryon(item["id"], selfie["id"])
        jobs.tryon(tryon["id"])
        return store.get(item["id"])

    @app.delete("/api/tryons/{tryon_id}")
    def delete_tryon(tryon_id: str, user=Depends(current_user)):
        tryon = store.get_tryon(tryon_id)
        item = tryon and store.get(tryon["item_id"])
        if not item or item["owner"] != user:
            raise HTTPException(404, "No such picture")
        store.delete_tryon(tryon_id)
        return store.get(item["id"])

    @app.get("/images/{name}")
    def image(name: str, user=Depends(current_user)):
        path = store.image_path(name)
        if public:
            known, owner = store.image_owner(name)
            if not known or owner != user:
                path = None
        if path is None:
            raise HTTPException(404, "No such image")
        # Private in the public copy, so a shared browser or proxy doesn't keep it.
        cache = "private, max-age=31536000, immutable" if public else "max-age=31536000, immutable"
        return FileResponse(path, headers={"Cache-Control": cache})

    @app.get("/api/export")
    def export(user=Depends(current_user)):
        """The whole wardrobe as one zip: items.csv, items.json and every picture."""
        name = f"tidyrack-{date.today().isoformat()}.zip"
        return StreamingResponse(export_zip(store, user), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/phone")
    def phone_link():
        """The private Tailscale address your phone can open, with a QR code for it."""
        if public:
            raise HTTPException(404, "Not in the public copy")
        host = tailscale_host()
        if host is None:
            raise HTTPException(503, "Tailscale isn't running on this Mac")
        url = f"https://{host}:{TAILSCALE_PORT}/"
        svg = io.BytesIO()
        segno.make(url).save(svg, kind="svg", scale=5, border=2, dark="#222")
        return {"url": url, "qr_svg": svg.getvalue().decode()}

    looks.add_routes(app, store, pipeline, run, key_for, current_user, read_upload, ai.env)
    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app


def tailscale_host() -> str | None:
    """This Mac's name on your Tailscale network, or None if it's not running."""
    tailscale = shutil.which("tailscale") or "/opt/homebrew/bin/tailscale"
    try:
        out = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True, timeout=5)
        status = json.loads(out.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if status.get("BackendState") != "Running":
        return None
    return status["Self"]["DNSName"].rstrip(".") or None


if PUBLIC and not ai.env("PUBLIC_URL"):
    # Otherwise the address OpenRouter sends people back to would come from the request,
    # which behind a proxy is the wrong one (http, or whatever Host header was sent).
    raise RuntimeError("The public copy needs TIDYRACK_PUBLIC_URL, e.g. https://tidyrack.example.com")
app = create_app(Store(DATA_DIR), warm_up=True, public=PUBLIC, secret=ai.env("SECRET"),
                 public_url=ai.env("PUBLIC_URL"))
