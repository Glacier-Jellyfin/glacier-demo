#!/usr/bin/env python3
"""Generates the AI images in prompts.toml with FLUX.1-schnell.

  ai.py candidates [--only TEXT] [--count 4]   four seeds per image, kept as PNG
  ai.py sheet                                  contact sheets to choose from
  ai.py pin                                    the chosen seeds into sources.toml

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
RELEASE = "https://github.com/Glacier-Jellyfin/glacier-demo/releases/download/ai-v1"
# About 1.5 megapixels: more detail in faces and hands than 1 MP, still fast.
SIZES = {"poster": (1024, 1536), "backdrop": (1824, 1024)}


def folder() -> Path:
    return demo.work() / "ai"


def entries() -> dict[str, dict]:
    prompts = demo.load("prompts.toml")
    catalog = demo.load("catalog.toml")
    shows = demo.shows(catalog)
    titles = {t["title"]: t for t in demo.movies(catalog) + shows}
    result = {}
    style, art = prompts["style"], prompts["art"]
    for key, entry in prompts["image"].items():
        title = titles[entry["title"]]
        genres = [g.lower() for g in title["genres"]]
        # Without a prompt of its own, the plot describes the picture.
        scene = entry.get("prompt") or f"a scene from a {', '.join(genres)} story: {' '.join(title['plot'].split())}"
        direction = next((text for genre, text in art.items() if genre in genres), "")
        shape = style[entry["shape"]]
        medium = entry.get("medium") or ("anime" if "anime" in genres else "cartoon" if {"animation", "kids"} <= set(genres)
                                         else "animation" if "animation" in genres else "photo")
        parts = [shape, direction, scene] + ([style["composition"]] if entry["shape"] == "poster" else []) + [prompts["medium"][medium]]
        full = ", ".join(p for p in parts if p)
        # The candidates belong to this exact prompt and size; a new prompt starts afresh.
        version = hashlib.sha1(f"{full}|{SIZES[entry['shape']]}".encode()).hexdigest()[:8]
        # CLIP reads 77 tokens: the shape and the scene; T5 reads all of it.
        result[key] = dict(entry, clip_prompt=f"{shape}, {scene}", full_prompt=full, version=version)
    return result


def candidate(key: str, entry: dict, seed: int) -> Path:
    return folder() / "candidates" / f"{key}-{entry['version']}-{seed}.png"


def candidates(only: str | None, count: int) -> None:
    import torch
    from diffusers import FluxPipeline

    todo = [(k, e, s) for k, e in entries().items() if not only or only in k
            for s in range(1, count + 1) if not candidate(k, e, s).exists()]
    if not todo:
        print("nothing to do")
        return
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
    for key, entry, seed in todo:
        width, height = SIZES[entry["shape"]]
        image = pipe(prompt=entry["clip_prompt"], prompt_2=entry["full_prompt"], width=width, height=height,
                     guidance_scale=0.0, num_inference_steps=4, max_sequence_length=256,
                     generator=torch.Generator("cpu").manual_seed(seed)).images[0]
        path = candidate(key, entry, seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        print(f"  {path.name}")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("candidates")
    make.add_argument("--only", help="only images whose key contains this text")
    make.add_argument("--count", type=int, default=4)
    commands.add_parser("sheet")
    commands.add_parser("pin")
    args = parser.parse_args()
    if args.command == "candidates":
        candidates(args.only, args.count)
    elif args.command == "sheet":
        sheet()
    else:
        pin()


if __name__ == "__main__":
    sys.exit(main())
