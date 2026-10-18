<p align="center">
  <img src="static/favicon.svg" width="88" alt="Tidyrack's hanger logo">
</p>

<h1 align="center">Tidyrack</h1>

<p align="center">
  <b>Your whole wardrobe, photographed once and tidied for good.</b><br>
  Snap a shirt on your phone. Tidyrack cuts it out, redraws it as a catalogue shot and reads the label.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-1b2f6e" alt="MIT licence"></a>
  <a href="https://github.com/simonwisdom/tidyrack/actions/workflows/test.yml"><img src="https://github.com/simonwisdom/tidyrack/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/python-3.11%E2%80%933.13-1b2f6e" alt="Python 3.11 to 3.13">
</p>

<p align="center">
  <img src="screenshots/before-after.png" width="520" alt="A shirt photographed on a hanger, and the same shirt tidied into a catalogue shot">
</p>

<p align="center">
  <img src="screenshots/list.jpg" width="240" alt="The wardrobe list on a phone">
  &nbsp;
  <img src="screenshots/looks.jpg" width="240" alt="Looks: an outfit made from your clothes">
</p>

## What it does

- 🏷️ **Sell on Vinted**: writes the listing and shares the real photos.
- 👗 **Looks**: outfits from what you own, plus the one piece you're missing.
- 🎨 **What if I…**: see a garment dyed, faded or cut before you touch it.
- 🔗 **From link** and 🤳 **Outfit**: add clothes from a shop page or a mirror selfie.
- 📦 **Export**: everything in one zip. It's your data.

## Run it

You need a Mac, [uv](https://docs.astral.sh/uv/) and the [Codex CLI](https://github.com/openai/codex) signed in to a paid ChatGPT plan.

```bash
./run.sh install   # starts at login, at http://127.0.0.1:8760
./run.sh phone     # open it on your phone with Tailscale
```

🤖 Or hand this repo to Codex or Claude Code: [AGENTS.md](AGENTS.md) walks it through the setup. Everything else is in the [guide](GUIDE.md).

☁️ Rather not run it yourself? [tidyrack.simonwisdom.com](https://tidyrack.simonwisdom.com) is a hosted copy that runs on your own OpenRouter key.

## No support, all yours

Issues are off. Fork it, change it, make it yours.

[MIT](LICENSE). The Inter Tight font is under the [SIL Open Font License](static/fonts/OFL.txt).
