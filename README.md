# Tidyrack

A wardrobe catalogue for your phone. Photograph each piece of clothing; Tidyrack cuts it out, tidies the picture, reads the label, and helps you sell it on Vinted, upcycle it or put outfits together. It runs on your own computer, on your own ChatGPT plan through Codex, and you open it from your phone.

Free and MIT-licensed. There's no support: issues and pull requests are off. If you need something different, fork it and change it; `AGENTS.md` tells a coding agent (Codex, Claude Code) how to set it up for you.

A hosted copy runs at https://tidyrack.simonwisdom.com, where you sign in with your own OpenRouter account.

## What you need

- A Mac (Linux works for everything but `./run.sh install`, which uses launchd).
- [uv](https://docs.astral.sh/uv/) and Python 3.11–3.13.
- The [Codex CLI](https://github.com/openai/codex), signed in with a paid ChatGPT plan. Details, tidy-ups and drawings run on it at no per-photo cost. Image drawing isn't on the Free plan. Without Codex, set `TIDYRACK_DETAILS=openrouter` and `OPENROUTER_API_KEY` for the details; tidy-ups and drawings still need Codex.
- For your phone away from home: [Tailscale](https://tailscale.com/download) on the computer and the phone. On the same Wi-Fi you can skip it (see "From your phone").

## What it does

Photograph a piece of clothing: the front first, then the back, a label close-up or anything else. The first photo starts a draft item, and its cutout, tidied picture and a first guess at the details start straight away while you take the next photos. Each further photo gets its own cutout and tidied picture as it arrives; label close-ups are only read, not redrawn. When you tap **Done adding photos**, Codex reads the details again from all the photos together, which is how it picks up the size and the real fabric from a label. Fields you've already fixed or checked are kept; only unchecked guesses change.

**Several at once**: Add item, **+ Photo** and **+ Label** all take several photos from the library in one go (on the iPhone: Photo Library, then tap each one). From **Add item**, you then choose whether they are one item or one item each. Photos upload one at a time with a progress bar; if one fails, the rest still go in and **Try again** sends the failed ones. Their cutouts and tidies wait their turn: one cutout and three tidies run at a time, and the cards say "waiting its turn" until theirs starts.

**Outfit** (beside From link): one photo of you wearing clothes, such as a mirror selfie, becomes one item per garment. rembg's clothing model (`u2net_cloth_seg`, local, about 3 seconds) finds the top, the bottoms and any full-length piece like a dress, and cuts each out without skin, hands, phone or the other garments. Each item gets a **worn photo**: a crop of your photo around the garment (real pixels, shared by Save photos), with that cutout. Codex then tidies the crop, told which garment to draw, and reads the details as usual. The colour comes from the tidied picture, because the room's shade on a worn garment is strong: cream trousers in a shady room read greyish brown from the cutout and cream from the tidy. The tidy invents what the photo hides, such as a waistband under a hoodie, and keeps a mirror selfie's print reversed. The model only knows top, bottoms and full-length: a shirt under a jacket comes out as one top. The first outfit photo downloads the clothing model (176 MB, once).

**On me** (**Try it on me** on an item): see the item on you. It's drawn onto your newest Outfit photo, kept whole for this; a row of your Outfit photos picks another. The garment goes in as the cover's cutout on white, with its colour and type, and the model is told to change nothing else. The personal copy uses Codex's image tool (about a minute), a public copy Gemini on the person's key. Each picture is kept on the item until you delete it; it lives apart from the item's photos, so it's never a cover, never read for details and never in Save photos. An Outfit photo can be deleted from the same dialog. With no Outfit photo yet, it says how to add one.

Guessed fields (type, colour, fabric, brand, size) stay highlighted until you fix them or tap **All correct**. You fill in amount paid yourself. The first photo is the cover; any photo can be made the cover or deleted.

Above the list, a dropdown per field (type, colour, fabric, brand, size, status) narrows it: ticking two options in one dropdown shows either, and an item must pass every dropdown. Each option shows how many items it would leave. A two-tone colour counts under both colours, and a label's mix like "80% wool, 20% nylon" counts as its biggest share. **Sort** orders the list by date, type, colour, brand or amount paid, with empty values last. **Clear all** resets the filters, sort and search; each device remembers its own.

## Run it

It runs as a background service that starts at login and restarts if it crashes:

```bash
./run.sh install   # set up the service (once)
./run.sh restart   # pick up code changes
./run.sh stop      # stop it and stop it starting at login
./run.sh           # or run it in this terminal instead
```

Open http://127.0.0.1:8760. Logs go to `~/Library/Logs/tidyrack.log`. The first photo downloads the background-removal model (about 180 MB, once).

## From your phone

The app only listens on the computer itself. `./run.sh phone` shares it through Tailscale, to your own devices only, on any network, and Tailscale remembers this across restarts. It prints the address to open on the phone (with Tailscale on); the **Phone** button on the computer shows a QR code for it. It uses port 10000 (`TIDYRACK_TAILSCALE_PORT`, read by both). `./run.sh phone off` stops sharing. It needs the `tailscale` command on the PATH (the Mac app installs it from its settings) and HTTPS turned on for your tailnet in Tailscale's admin console (the first `tailscale serve` says how).

Without Tailscale, on the same Wi-Fi only: `./run.sh stop` first (two servers on one `data/` would clash), then run `uv run uvicorn tidyrack.app:app --host 0.0.0.0 --port 8760` and open `http://<the computer's local address>:8760` on the phone. Anyone on that network can then open it, so only do this at home. Over plain http the phone's browser won't share files or copy for the app: **Save photos** downloads the photos instead of opening the share sheet, and Copy selects the text for you to copy.

## Run a public copy

`TIDYRACK_PUBLIC=1` turns on **Sign in with OpenRouter**, for a copy other people use: people sign in with their OpenRouter account, each sees only their own items, photos and export, and their model calls run on their own OpenRouter key: details and the Vinted listing with `google/gemini-3.8-flash` (`TIDYRACK_MODEL`), the tidy-up with `google/gemini-3.1-flash-image` (`TIDYRACK_TIDY_MODEL`). Cutouts still run on the server. Gemini's tidy-up keeps colours and stripes but can garble label text. Measured in October 2026: reading an item's details $0.002, a Vinted listing $0.006, and each tidy-up $0.068, which runs by itself on every view photo, so tidy-ups are most of what a person pays. Public uploads must be JPEG, PNG or WebP photos under 25 MB and 50 megapixels, at most 100 a day per person (`TIDYRACK_DAILY_PHOTOS`, failed tries and deleted photos included) and 2,000 kept in all; uploads from someone not signed in, or too big, are turned away before they're read. Add from link reads one link at a time per person and four at once in all.

It also needs `TIDYRACK_SECRET`, which encrypts the stored keys (make one with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; changing it signs everyone's keys out), and `TIDYRACK_PUBLIC_URL`, the public address OpenRouter sends people back to (for example `https://tidyrack.example.com`; the public copy won't start without it). Keep the data on a disk that survives restarts: `TIDYRACK_DATA` for the database and pictures, `U2NET_HOME` for the cutout models. Behind a proxy, also limit request bodies there (about 26 MB) as a second guard. `railpack.json` and `railway.json` are set up for [Railway](https://railway.com).

The public copy has a privacy page (`static/privacy.html`, linked from the sign-in screen and the footer), **Delete my account** in the footer, security headers on every response (`SECURITY_HEADERS` in `tidyrack/app.py`), a feedback button (`static/launch/`) and no analytics. Before you run your own public copy, put your own name and contact in `static/privacy.html`, and point `feedbackUrl` in `static/launch/config.js` at your own feedback service or set it to `""` (and drop it from `connect-src` in `tidyrack/app.py`).

Without `TIDYRACK_PUBLIC`, there's no sign-in and everything runs on the computer's own Codex login: the personal copy.

## How it works

- **Cutout**: [rembg](https://github.com/danielgatis/rembg) with `birefnet-general-lite`, run locally. It only removes pixels and never redraws the garment. It takes about 9 seconds a photo on an Apple-silicon Mac. Set `TIDYRACK_REMBG_MODEL` to try another model.
- **Details**: `codex exec` with `gpt-5.6-luna` on your ChatGPT login, so there's no per-photo cost (about 6 seconds). Set `TIDYRACK_CODEX_MODEL` to change the model. To use OpenRouter instead, set `TIDYRACK_DETAILS=openrouter` (`google/gemini-3.8-flash`, or `TIDYRACK_MODEL`; key from `OPENROUTER_API_KEY`). The brand is only guessed when a logo or label shows.
- **Tidy up** (runs by itself on every view photo, not on label close-ups; **Tidy again** redoes it): Codex's image tool redraws the original photo as a catalogue shot on plain grey, in 30–40 seconds, at no per-photo cost. It's a new picture, not the real pixels: labels and sizes can come out wrong. So it sits beside the cutout and the original, never replaces them, and the details are always read from the original. In a test against three Gemini image models, Codex was the most faithful; the cheaper Gemini models changed colours and invented labels.
- **Sell on Vinted** (a button on each item): Codex writes a title, description, category, condition, parcel size and suggested price from the photo and the details, in about 10 seconds. You copy them into Vinted yourself, and **Save photos** puts the real photos in the iPhone share sheet. It can't post for you: Vinted's listing API is only for approved Vinted Pro businesses, and its terms ban bots on personal accounts. Vinted needs a size: a label close-up usually gives it, or fill in **Size** yourself. **Save photos** shares only real pixels, because Vinted can suspend accounts for misleading photos and dislikes edited ones: the cover photo as taken, then the other photos and label close-ups, never cutouts. The AI-tidied picture is left out unless you tick **Include the AI-tidied picture**, and then it goes last. Only `photo` and `label` kinds are shared, so shop photos never are.
- **What if I…** (a button on each item): see the garment dyed, faded, shortened or with new buttons before choosing **Keep**, **Upcycle** or **Sell** (the **Plan** row on the item, also in the Status filter and the export). Tap **Show me** on an edit and the image model redraws the tidied picture with that one change, in 30–40 seconds (Codex, free; Gemini on the person's key in the public copy, about $0.07 each). Which edits are offered follows plain rules in `tidyrack/upcycle.py`, not a model: dye only goes darker (home dye can't lighten), and its colours go with the rest of the wardrobe (a dark neutral, then colours whose hue is close or opposite to the other clothes'); no fading for wool, silk or synthetics; no dye for leather; new buttons only on garments that have them; a crop to suit the type. Each shows a rough cost and effort. A box takes your own idea. **Upcycle to this** keeps that picture as the item's goal and has the model write materials and steps (about 10 seconds). The pictures are AI drawings, so they never go in Save photos. A garment marked Sell, or with a listing drafted, gets a quiet "Before you sell it" link; so does one not seen in an Outfit photo for six months (counted from when it was added if it has none). The check is `checks/what_if.py`.
- **From link** (a button beside Add item): paste a shop's product page and the app makes an item from it. It reads the product data shops put in their pages for search engines (JSON-LD and Open Graph tags): name, brand, colour, fabric, sizes, price and the main image. The image becomes a **shop photo**: it gets a cutout but no tidy, and is left out of Save photos. Brand, colour and fabric go in as guesses; Codex reads the type from the photo and only fills fields the shop left empty. The price, sizes and link go in Notes, not Amount paid. Many shops block apps like this; then it says so and suggests saving the shop's photo and adding it as a photo. It only fetches ordinary public websites (ports 80 and 443, never a private, Tailscale or local address, checked again at each redirect), and pages or images up to 15 MB. Tried on 11 October 2026 (the page is fetched with curl_cffi posing as Firefox, which got through at more shops than Chrome):

  | Works | Blocked |
  |---|---|
  | ASOS, M&S, Uniqlo (title and image only: type, colour and fabric are guessed from the photo), Vinted | Zara, COS, Arket ("check you're human" page), H&M, Mango (refused), Next (stopped sending product details by 14 October) |

  Shops change their blocking often, so this list will drift.
- **Colour swatch**: each item's main colour as a hex code (a second one if it's two-tone), worked out from the pixels of the cover's cutout (a shop photo's if there is one), not by a model: k-means in CIE Lab, merging light and shadow of one colour. It's the colour as photographed, so the room's lighting shows in it. A worn photo's colour comes from its tidied picture once that's ready (see **Outfit**). Tap the swatch in the item view to pick the right colour; a picked one is kept. Older items get theirs at startup.
- **Storage**: `data/tidyrack.db` (SQLite): an `items` table for the details and a `photos` table, one row per photo; `data/images/` holds each photo's original, cutout and tidied picture. Set `TIDYRACK_DATA` to keep them somewhere else. Only `tidyrack/store.py` touches storage, so moving to a hosted database or file storage later means changing that one file.
- Processing runs after the upload is saved, so a failed model call never loses a photo. A failed item shows **Try again**.

## Looks

**Looks** (top bar) puts your clothes together into outfits. Every top + bottom (+ an optional jacket, blazer or coat) is scored by `tidyrack/outfits.py` on colour (measured from the photos), formality, fabric weight and season. If you have shoes (any item whose type says trainers, boots, loafers, sandals...), each outfit wears the pair that suits it best. Accessories aren't counted.

- **Dress me**: the best outfits, Shuffle through them, Save the ones you like. **See it on** draws the outfit on a person with the image model (Codex on your login; Gemini on the person's key in the public copy), in about a minute. Every Save and Shuffle is kept (`looks` table) so the scorer can learn your taste later.
- **Copy a look**: choose a photo of an outfit you like. A vision model names each piece, Tidyrack finds your closest item by kind and colour, lists what's missing with a Vinted search, and **Draw my version** draws it in your clothes.
- **Missing piece**: tries 44 everyday pieces (4 of them shoes) and shows the one that adds the most good outfits, with a Vinted search in your size and **Draw it**.
- **Morning card**: today's weather (Open-Meteo, or MET Norway when it's busy; London unless `TIDYRACK_PLACE=lat,lon`; MET Norway gets the contact in `TIDYRACK_CONTACT`, the repo's address unless you set your own) and, if you type them, today's events pick one outfit; **Pencil sketch** draws it for the e-ink reader.

Set `TIDYRACK_BODY` (e.g. "a man in his thirties") for who wears the outfit in the drawings; the default is "a person". The checks behind the colour and the scorer are `tests/colour_check.py` and `tests/outfit_check.py`.

## The look

The hanger logo (`static/logo.svg`; `favicon.svg` and `apple-touch-icon.png` are it in white on a blue square), ink blue `#1b2f6e` as the accent (`#a3b6f0` in dark mode), cool near-white and grey for the rest, and Inter Tight for the name and headings (`static/fonts`, served from the app itself, under the SIL Open Font License in `OFL.txt`). Body text stays the device's own font. The colours are the variables at the top of `static/style.css`.

## Export

**Export** (top right, on the phone too) downloads everything as `tidyrack-YYYY-MM-DD.zip`:

- `items.csv`: one row per item, for Numbers or Excel: every detail, `colour_hex` and `colour_hex_2`, `unchecked` (fields still a model guess), the last Vinted listing (`listing_...` columns), and the picture files as `images/...` names: `photos` (views of the garment, cover first), `labels` (label close-ups), `cutouts` and `tidied`.
- `items.json`: the same items in full, each with its list of photos, as the app keeps them.
- `images/`: every original photo, cutout and tidied picture.

The zip is built while it downloads, so a big wardrobe doesn't fill the computer's memory. On the iPhone it goes to Downloads in the Files app. There's no import yet.

## Tests

```bash
uv run pytest -q
```

The tests use a fake model, so they run offline and cost nothing. GitHub runs them on every push (`.github/workflows/test.yml`).

## Keeping a server on master

`deploy/` is how the hosted copy's sibling, a personal copy on a small Linux server, follows this repo: `tidyrack-follow.timer` runs `deploy/follow.sh` every 2 minutes as the server's `agent` user. It fetches master, runs the tests, installs, restarts `tidyrack.service` and checks the page answers; a commit that fails its tests stays out, and one that doesn't come up is rolled back. Install steps are at the top of `deploy/tidyrack-follow.timer`. From your computer, `./run.sh deploy` tests, pushes master and waits until the server runs it (`TIDYRACK_VPS` is the server's SSH name). `deploy/counts.py` prints read-only usage counts from a public copy's database.
