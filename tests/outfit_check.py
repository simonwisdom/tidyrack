"""The check for the outfit scorer on Polyvore Outfits (Vasileva et al. 2018, the
disjoint split): do real outfits score above the same outfits with one piece
swapped at random for another of its kind?

Only the clothes count (tops, bottoms, outerwear, dresses), and with --shoes the
outfit's shoes too, as in Tidyrack. Each
piece is described the way Tidyrack describes an item: its type and fabric from
words (here the product's title and description) and its colours measured from
its studio photo. The weights are fitted on the training outfits and the AUC
(the chance a real outfit beats a swapped one; 0.5 is a coin toss) is on the
test outfits, which share no items with them.

    uv run --with pyarrow python tests/outfit_check.py ITEMS_DIR OUTFITS_DIR [outfits per split] [--shoes]

ITEMS_DIR holds owj0421/polyvore's parquet files (item photos and titles),
OUTFITS_DIR owj0421/polyvore-outfits' disjoint_default/{train,test}.json.
"""

import io
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from colour_check import png, studio_cutout  # noqa: E402

from tidyrack import colour, outfits  # noqa: E402

BATCH = 500  # photos read at a time
WORKERS = 3  # the Mac is shared: a few processes, not one per core
SLOT_OF = {"tops": "top", "bottoms": "bottom", "outerwear": "layer", "all-body": "full", "shoes": "shoes"}


def load_items(items_dir: Path) -> dict:
    """item_id -> (category, title + description, parquet file), for the clothes."""
    import pyarrow.parquet as pq

    found = {}
    for path in sorted(items_dir.glob("*.parquet")):
        t = pq.read_table(path, columns=["item_id", "category", "title", "description"]).to_pydict()
        for item_id, category, title, desc in zip(t["item_id"], t["category"], t["title"], t["description"]):
            if category in SLOT_OF:
                found[item_id] = (category, title or "", desc or "", path.name)
    return found


def outfits_of(path: Path, items: dict, limit: int, rng, shoes: bool) -> list[list[str]]:
    """Outfits (their clothes' ids) with a top and a bottom, or a full-length piece and a layer.
    With `shoes`, an outfit's shoes are added; the outfits picked are the same either way."""
    picked = []
    for outfit in json.loads(path.read_text()):
        clothes = [i["item_id"] for i in outfit["items"] if i["item_id"] in items]
        by = {}
        for item_id in clothes:
            by.setdefault(SLOT_OF[items[item_id][0]], []).append(item_id)
        if "full" in by:
            pieces = [by["full"][0]]
        elif "top" in by and "bottom" in by:
            pieces = [by["top"][0], by["bottom"][0]]
        else:
            continue
        if "layer" in by:
            pieces.append(by["layer"][0])
        if len(pieces) > 1:  # a dress on its own has nothing to go with: swapping it is a coin toss
            picked.append(pieces + (by.get("shoes", [])[:1] if shoes else []))
    rng.shuffle(picked)
    return picked[:limit]


def _measure(job):
    item_id, image_bytes = job
    from PIL import Image

    cutout = studio_cutout(Image.open(io.BytesIO(image_bytes)))
    return item_id, (colour.measure(png(cutout))["colours"] if cutout else None)


def measure_all(items_dir: Path, items: dict, wanted: set, cache: Path) -> dict:
    """item_id -> measured colours, kept in a cache file between runs. Reads the
    photos BATCH at a time with a few workers, so it stays well under a GB."""
    import pyarrow.parquet as pq

    done = json.loads(cache.read_text()) if cache.exists() else {}
    todo = wanted - done.keys()
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for name in sorted({items[i][3] for i in todo}):
            for batch in pq.ParquetFile(items_dir / name).iter_batches(BATCH, columns=["item_id", "image"]):
                jobs = [(i, im["bytes"] if isinstance(im, dict) else im)
                        for i, im in zip(batch.column("item_id").to_pylist(), batch.column("image").to_pylist())
                        if i in todo]
                for item_id, measured in pool.map(_measure, jobs, chunksize=16):
                    done[item_id] = measured
            cache.write_text(json.dumps(done))
            print(f"measured {len(done)} items", file=sys.stderr)
    return done


def piece(item_id: str, items: dict, colours: dict) -> dict | None:
    category, title, desc, _ = items[item_id]
    measured = colours.get(item_id)
    if not measured:
        return None
    return outfits.describe(title, f"{title} {desc}", measured, slot=SLOT_OF[category])


def pairs(outfit_list, items, colours, rng):
    """For each real outfit, it and a copy with one piece swapped for a random one of its kind."""
    pool = {}
    for o in outfit_list:
        for i in o:
            pool.setdefault(items[i][0], []).append(i)
    out = []
    for o in outfit_list:
        n = rng.integers(len(o))
        swapped = list(o)
        swapped[n] = pool[items[o[n]][0]][rng.integers(len(pool[items[o[n]][0]]))]
        real = [piece(i, items, colours) for i in o]
        fake = [piece(i, items, colours) for i in swapped]
        if None not in real and None not in fake and swapped != o:
            out.append((real, fake))
    return out


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """The chance a real outfit scores above a swapped one, any pair (ties count half)."""
    from scipy.stats import rankdata

    ranks = rankdata(np.concatenate([pos, neg]))
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def fit(train) -> dict:
    """Logistic regression on (real - swapped) features: which features tell them apart."""
    keys = list(outfits.WEIGHTS)
    x = np.array([[outfits.features(r)[k] - outfits.features(f)[k] for k in keys] for r, f in train])
    w = np.zeros(len(keys))
    for _ in range(3000):
        p = 1 / (1 + np.exp(-x @ w))
        w += 0.5 * (x.T @ (1 - p)) / len(x) - 1e-3 * w
    return dict(zip(keys, np.round(w, 2).tolist()))


def evaluate(test, weights) -> float:
    saved = dict(outfits.WEIGHTS)
    outfits.WEIGHTS.update(weights)
    pos = np.array([outfits.score(r)[0] for r, _ in test])
    neg = np.array([outfits.score(f)[0] for _, f in test])
    outfits.WEIGHTS.update(saved)
    return auc(pos, neg)


def main(items_dir: str, outfits_dir: str, limit: int = 4000, shoes: bool = False) -> None:
    items_dir, outfits_dir = Path(items_dir), Path(outfits_dir)
    rng = np.random.default_rng(3)
    items = load_items(items_dir)
    train = outfits_of(outfits_dir / "train.json", items, limit, rng, shoes)
    test = outfits_of(outfits_dir / "test.json", items, limit, rng, shoes)
    wanted = {i for o in train + test for i in o}
    colours = measure_all(items_dir, items, wanted, outfits_dir / "colours-cache.json")
    # Each test outfit gets several swapped copies, so the AUC doesn't hang on one draw.
    train_pairs = pairs(train, items, colours, rng)
    test_pairs = [p for _ in range(3) for p in pairs(test, items, colours, rng)]
    fitted = fit(train_pairs)
    print(f"{len(train_pairs)} training pairs, {len(test_pairs)} test pairs ({len(test)} outfits)")
    print("fitted weights:", fitted)
    print(f"AUC, hand-set weights: {evaluate(test_pairs, {}):.3f}")
    print(f"AUC, fitted weights:   {evaluate(test_pairs, fitted):.3f}")
    for keep in (["formality_gap", "weight_order", "season_gap"],
                 ["all_neutral", "one_strong", "strong_pair_go", "strong_clash", "light_dark", "same_shade",
                  "echo", "muted_match"]):
        only = {k: (v if k in keep else 0.0) for k, v in fitted.items()}
        print(f"AUC, only {'colour' if 'all_neutral' in keep else 'formality, weight, season'}: "
              f"{evaluate(test_pairs, only):.3f}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--shoes"]
    main(args[0], args[1], int(args[2]) if len(args) > 2 else 4000, shoes="--shoes" in sys.argv)
