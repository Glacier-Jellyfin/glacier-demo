#!/usr/bin/env python3
"""Generates the AI images in prompts.toml with FLUX.1-schnell.

  ai.py candidates [--only TEXT] [--count 4]   four seeds per image, kept as PNG
  ai.py sheet                                  contact sheets to choose from
  ai.py pin                                    the chosen seeds into sources.toml
  ai.py batch episodes|people make|sheet|pin   a still per episode of the one-line series, or a
                                               headshot per made-up person; one seed each

Runs in its own environment, because it needs PyTorch with CUDA:

  python -m venv %LOCALAPPDATA%\\glacier-demo\\ai\\venv
  ...\\venv\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cu128
  ...\\venv\\Scripts\\pip install diffusers transformers accelerate sentencepiece protobuf pillow bitsandbytes
  ...\\venv\\Scripts\\hf auth login      (once; accept the model's terms on Hugging Face first)

The model needs about 34 GB on disk; quantized to 4 bits it runs in 16 GB of
video memory. "pin" also puts the chosen images into the build cache, so
demo.py builds with them before they are uploaded to the ai-v1 release.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import demo

MODEL = "black-forest-labs/FLUX.1-schnell"
REVISION = "741f7c3ce8b383c54771c7003378a50191e9efe9"
RELEASE = demo.RELEASE
# About 1.5 megapixels: more detail in faces and hands than 1 MP, still fast.
SIZES = {"poster": (1024, 1536), "backdrop": (1824, 1024), "avatar": (1024, 1024), "cover": (1024, 1024),
         "artist": (1024, 1024)}


def folder() -> Path:
    return demo.work() / "ai"


def entries() -> dict[str, dict]:
    prompts = demo.load("prompts.toml")
    catalog = demo.load("catalog.toml")
    shows = demo.shows(catalog)
    titles = {t["title"]: t for t in demo.movies(catalog) + shows}
    result = {}
    style, art = prompts["style"], prompts["art"]
    groups = demo.collections_of(catalog)
    genres_of = {a["name"]: a["genre"] for a in catalog.get("artist", [])}
    for key, entry in prompts["image"].items():
        # A collection takes its genres (and so its art direction) from its first member.
        if "user" in entry:
            title = {"title": entry["user"], "genres": [], "plot": ""}
        elif "album" in entry or "artist" in entry:
            # Albums and artists take the art direction of the artist's genre.
            genre = genres_of.get(entry["artist"], "Lounge")
            title = {"title": entry.get("album", entry["artist"]), "genres": [genre], "plot": ""}
        elif "collection" in entry:
            title = dict(groups[entry["collection"]][0], title=entry["collection"])
        else:
            title = titles[entry["title"]]
        genres = [g.lower() for g in title["genres"]]
        # Without a prompt of its own, the plot describes the picture.
        scene = entry.get("prompt") or f"a scene from a {', '.join(genres)} story: {' '.join(title['plot'].split())}"
        direction = next((text for genre, text in art.items() if genre in genres), "")
        shape = style[entry["shape"]]
        medium = medium_for(genres, entry.get("medium"))
        parts = [shape, direction, scene] + ([style["composition"]] if entry["shape"] == "poster" else []) + [prompts["medium"][medium]]
        full = ", ".join(p for p in parts if p)
        # The candidates belong to this exact prompt and size; a new prompt starts afresh.
        version = hashlib.sha1(f"{full}|{SIZES[entry['shape']]}".encode()).hexdigest()[:8]
        # CLIP reads 77 tokens: the shape and the scene; T5 reads all of it.
        result[key] = dict(entry, clip_prompt=f"{shape}, {scene}", full_prompt=full, version=version)
    return result


def medium_for(genres: list[str], chosen: str | None = None) -> str:
    return chosen or ("anime" if "anime" in genres else "cartoon" if {"animation", "kids"} <= set(genres)
                      else "animation" if "animation" in genres else "photo")


def candidate(key: str, entry: dict, seed: int) -> Path:
    return folder() / "candidates" / f"{key}-{entry['version']}-{seed}.png"


def candidates(only: str | None, count: int) -> None:
    import torch

    todo = [(k, e, s) for k, e in entries().items() if not only or only in k
            for s in range(1, count + 1) if not candidate(k, e, s).exists()]
    if not todo:
        print("nothing to do")
        return
    pipe = pipeline()
    for key, entry, seed in todo:
        width, height = SIZES[entry["shape"]]
        image = pipe(prompt=entry["clip_prompt"], prompt_2=entry["full_prompt"], width=width, height=height,
                     guidance_scale=0.0, num_inference_steps=4, max_sequence_length=256,
                     generator=torch.Generator("cpu").manual_seed(seed)).images[0]
        path = candidate(key, entry, seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        print(f"  {path.name}")


def pipeline():
    import torch
    from diffusers import FluxPipeline
    from diffusers.quantizers import PipelineQuantizationConfig

    # In bf16 the model needs about 24 GB; spilled into system memory a step
    # takes 90 s. Quantized to 4 bits it fits in 16 GB and takes a second or two.
    quantization = PipelineQuantizationConfig(
        quant_backend="bitsandbytes_4bit",
        quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": torch.bfloat16},
        components_to_quantize=["transformer", "text_encoder_2"],
    )
    pipe = FluxPipeline.from_pretrained(MODEL, revision=REVISION, dtype=torch.bfloat16, quantization_config=quantization)
    pipe.to("cuda")
    return pipe


def sheet() -> None:
    out = folder() / "sheets"
    out.mkdir(parents=True, exist_ok=True)
    for key, entry in entries().items():
        files = sorted(folder().glob(f"candidates/{key}-{entry['version']}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        if not files:
            continue
        thumbs = []
        for f in files:
            im = Image.open(f)
            im.thumbnail((480, 480))
            thumbs.append((f.stem.rsplit("-", 1)[1], im))
        width = sum(im.width for _, im in thumbs) + 10 * len(thumbs)
        page = Image.new("RGB", (width, max(im.height for _, im in thumbs) + 30), "white")
        x = 0
        for seed, im in thumbs:
            page.paste(im, (x, 0))
            ImageDraw.Draw(page).text((x + 4, im.height + 6), f"{key} seed {seed}", fill="black")
            x += im.width + 10
        page.save(out / f"{key}.jpg", quality=85)
    print(out)


def pin() -> None:
    sources_path = demo.ROOT / "sources.toml"
    text = sources_path.read_text(encoding="utf-8")
    final = folder() / "final"
    final.mkdir(parents=True, exist_ok=True)
    for key, entry in entries().items():
        if "seed" not in entry:
            continue
        chosen = candidate(key, entry, entry["seed"])
        if not chosen.exists():
            print(f"  {key}: the prompt changed since seed {entry['seed']} was chosen; choose again")
            continue
        image = Image.open(chosen).convert("RGB")
        if entry.get("crop"):
            # Cuts off made-up lettering along the bottom; demo.py crops to the poster shape anyway.
            image = image.crop((0, 0, image.width, round(image.height * (1 - entry["crop"]))))
        path = final / f"{key}.jpg"
        image.save(path, quality=92)
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        block = (f"[image.{key}]\nurl = \"{RELEASE}/{key}.jpg\"\nsha256 = \"{sha}\"\n"
                 f"page = \"https://github.com/Glacier-Jellyfin/glacier-demo/blob/main/prompts.toml\"\n"
                 f"author = \"generated with FLUX.1-schnell, seed {entry['seed']}\"\nlicense = \"CC0\"\n")
        pattern = re.compile(rf"\[image\.{re.escape(key)}\]\n(?:[^\[\n].*\n)*")
        if pattern.search(text):
            text = pattern.sub(block, text)
        else:
            if "# Generated with FLUX" not in text:
                text = text.rstrip("\n") + "\n\n# Generated with FLUX.1-schnell from prompts.toml; nobody shown exists.\n"
            text = text.rstrip("\n") + "\n\n" + block
        # Into the build cache too, so demo.py finds it before the upload.
        cached = demo.work() / "cache" / f"{sha[:12]}-{key}.jpg"
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, cached)
        print(f"  {key} seed {entry['seed']}")
    sources_path.write_text(text, encoding="utf-8", newline="\n")
    print(f"upload: gh release upload ai-v1 {final}\\*.jpg --repo Glacier-Jellyfin/glacier-demo")


# ---- batches: one picture for each of many things, one seed each

# Shots for episode stills, one per episode in turn. The episode's title stays out of
# the prompt: the model sets a title it is given as lettering into the picture.
SHOTS = [
    "a wide establishing shot of the main location at dawn, without people",
    "two of the main characters in a tense conversation, over-the-shoulder shot",
    "a close-up of one character's face, lost in thought, shallow focus",
    "one character alone at night, lit by a single lamp",
    "a busy group scene with several characters",
    "a detail shot of an important object on a table",
    "a character walking away down a street or path, seen from behind",
    "an exterior at night in the rain",
    "a warm, funny moment with the characters laughing together",
    "a character running, urgent, motion blur",
    "an empty room with signs of what has just happened",
    "a golden hour exterior with the characters small in the frame",
]


def episode_entries() -> dict[str, dict]:
    """A still for every episode of the one-line series: the series' premise in another shot each time."""
    prompts = demo.load("prompts.toml")
    style, art = prompts["style"], prompts["art"]
    seeds = prompts.get("episodes", {}).get("seeds", {})
    result = {}
    for show in demo.shows(demo.load("catalog.toml")):
        if not show.get("filler"):
            continue
        genres = [g.lower() for g in show["genres"]]
        direction = next((text for genre, text in art.items() if genre in genres), "")
        # The first sentence: the premise, without the blurb that follows it.
        premise = re.split(r"(?<=[.!?])\s", " ".join(show["plot"].split()))[0]
        count = 0
        for season in show["season"]:
            for number, episode in enumerate(season["episodes"], 1):
                key = demo.still_key(show["title"], season["number"], number)
                shot = SHOTS[(count + len(show["title"])) % len(SHOTS)]
                count += 1
                medium = medium_for(genres)
                if medium == "photo":
                    look, scene = style["still"], f"{shot}, in a {', '.join(genres)} series: {premise}"
                else:
                    # Drawn styles turn a premise given as a sentence into a subtitle in the frame.
                    look, scene = style["drawing"], f"{shot}, {premise.rstrip('.!?').lower()}"
                full = ", ".join(p for p in (look, direction, scene, prompts["medium"][medium]) if p)
                # A seed of its own per episode, so even the same shot comes out different.
                seed = seeds.get(key, int(hashlib.sha1(key.encode()).hexdigest()[:6], 16))
                result[key] = {"group": show["title"], "clip_prompt": f"{look}, {scene}", "full_prompt": full,
                               "seed": seed}
    return result


def people_entries() -> dict[str, dict]:
    """A headshot for every made-up person described in prompts.toml; real people get none."""
    prompts = demo.load("prompts.toml")
    people = prompts.get("people", {})
    headshot, photo = prompts["style"]["headshot"], prompts["medium"]["photo"]
    return {demo.person_key(name): {"group": "people", "clip_prompt": f"{headshot}, {description}",
                                    "full_prompt": f"{headshot}, {description}, {photo}",
                                    # A seed per person, so not everyone strikes the same pose.
                                    "seed": people.get("seeds", {}).get(demo.person_key(name),
                                                                        int(hashlib.sha1(name.encode()).hexdigest()[:6], 16))}
            for name, description in people.get("describe", {}).items()}


# size: what the model makes; final: what goes into the release; manifest: the
# file that lists them for demo.py, under section.
BATCHES = {
    "episodes": {"entries": episode_entries, "size": (1344, 768), "final": (1280, 720),
                 "manifest": "episodes.toml", "section": "still",
                 "about": ["Episode stills for the one-line series, generated by \"ai.py batch episodes\"",
                           "with FLUX.1-schnell from the series' plot and the episode's title."]},
    "people": {"entries": people_entries, "size": (832, 1248), "final": (800, 1200),
               "manifest": "people.toml", "section": "person",
               "about": ["Headshots of the made-up cast and crew, generated by \"ai.py batch people\"",
                         "with FLUX.1-schnell from the descriptions in prompts.toml."]},
}


def batch_entries(name: str) -> dict[str, dict]:
    batch = BATCHES[name]
    entries = batch["entries"]()
    for entry in entries.values():
        # The pictures belong to this exact prompt and size; a new prompt starts afresh.
        entry["version"] = hashlib.sha1(f"{entry['full_prompt']}|{batch['size']}".encode()).hexdigest()[:8]
    return entries


def batch_file(name: str, key: str, entry: dict) -> Path:
    return folder() / name / f"{key}-{entry['version']}-{entry['seed']}.png"


def batch_make(name: str, only: str | None) -> None:
    import torch

    todo = [(k, e) for k, e in batch_entries(name).items()
            if (not only or only in k) and not batch_file(name, k, e).exists()]
    if not todo:
        print("nothing to do")
        return
    pipe = pipeline()
    width, height = BATCHES[name]["size"]
    for key, entry in todo:
        image = pipe(prompt=entry["clip_prompt"], prompt_2=entry["full_prompt"], width=width, height=height,
                     guidance_scale=0.0, num_inference_steps=4, max_sequence_length=256,
                     generator=torch.Generator("cpu").manual_seed(entry["seed"])).images[0]
        path = batch_file(name, key, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        print(f"  {path.name}")


def batch_sheet(name: str) -> None:
    """One contact sheet per group (a series, or all people), with the keys."""
    out = folder() / "sheets"
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, entry in batch_entries(name).items():
        groups.setdefault(entry["group"], []).append((key, entry))
    width, height = BATCHES[name]["size"]
    w, h = (336, round(336 * height / width)) if width > height else (round(260 * width / height), 260)
    columns = 4 if width > height else 8
    for group, members in groups.items():
        page = Image.new("RGB", (columns * (w + 6), ((len(members) + columns - 1) // columns) * (h + 22)), "white")
        for n, (key, entry) in enumerate(members):
            path = batch_file(name, key, entry)
            if path.exists():
                im = Image.open(path)
                im.thumbnail((w, h))
                x, y = (n % columns) * (w + 6), (n // columns) * (h + 22)
                page.paste(im, (x, y))
                ImageDraw.Draw(page).text((x + 2, y + h + 4), key.removeprefix("ai-"), fill="black")
        page.save(out / f"{name}-{demo.slug(group)}.jpg", quality=85)
    print(out)


def batch_pin(name: str) -> None:
    """Writes the pictures as JPEG and lists them with their checksums in the batch's manifest."""
    batch = BATCHES[name]
    final = folder() / "final"
    final.mkdir(parents=True, exist_ok=True)
    lines = [f"# {line}" for line in batch["about"]]
    lines += ["# Attached to the ai-v1 release as <key>.jpg, CC0; nobody shown exists. A picture",
              f"# that came out wrong gets another seed under [{name}.seeds] in prompts.toml.", "",
              f"[{batch['section']}]"]
    count = 0
    for key, entry in batch_entries(name).items():
        source = batch_file(name, key, entry)
        if not source.exists():
            continue
        image = Image.open(source).convert("RGB").resize(batch["final"], Image.LANCZOS)
        path = final / f"{key}.jpg"
        image.save(path, quality=86)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        cached = demo.work() / "cache" / f"{sha[:12]}-{key}.jpg"
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, cached)
        lines.append(f'{key} = {{ sha256 = "{sha}", seed = {entry["seed"]} }}')
        count += 1
    (demo.ROOT / batch["manifest"]).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{count} pictures in {batch['manifest']}; upload: gh release upload ai-v1 {final}\\*.jpg --clobber")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("candidates")
    make.add_argument("--only", help="only images whose key contains this text")
    make.add_argument("--count", type=int, default=4)
    commands.add_parser("sheet")
    commands.add_parser("pin")
    batch = commands.add_parser("batch")
    batch.add_argument("name", choices=list(BATCHES))
    batch.add_argument("action", choices=["make", "sheet", "pin"])
    batch.add_argument("--only", help="make: only pictures whose key contains this text")
    args = parser.parse_args()
    if args.command == "candidates":
        candidates(args.only, args.count)
    elif args.command == "sheet":
        sheet()
    elif args.command == "pin":
        pin()
    elif args.action == "make":
        batch_make(args.name, args.only)
    elif args.action == "sheet":
        batch_sheet(args.name)
    else:
        batch_pin(args.name)


if __name__ == "__main__":
    sys.exit(main())
