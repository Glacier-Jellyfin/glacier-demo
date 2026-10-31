#!/usr/bin/env python3
"""Builds and runs the demo Jellyfin server for Glacier.

  demo.py build [--only TITLE]   generate the library into <work>/media
  demo.py credits                rewrite CREDITS.md from sources.toml
  demo.py server setup [--reset] set up a fresh server on the built library
  demo.py server start|stop|status   (start and stop include Live TV on port 8200)
  demo.py server collections     redraw the collections' pictures on a running server
  demo.py server avatars         upload the profile pictures again
  demo.py server people          upload the cast and crew headshots

The library comes from catalog.toml, tracks.toml and sources.toml. Videos are
one still frame per chapter and the audio is silence, so a two-hour film takes
a few megabytes and a few seconds to build. Downloads are kept in
<work>/cache and checked against their sha256.

The work directory is GLACIER_DEMO_DIR, or %LOCALAPPDATA%\\glacier-demo
(~/.cache/glacier-demo elsewhere). The server runs on port 8098 (from the
Android emulator: http://10.0.2.2:8098), user "Nora" without a password.

Needs Python 3.11+, Pillow and ffmpeg with libx264, libx265, libsvtav1,
libopus and zscale (FFMPEG, or ffmpeg on PATH). The server itself is the
portable Windows build of Jellyfin.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
PORT = 8098
USER_AGENT = "GlacierDemo/1 (https://github.com/Glacier-Jellyfin/glacier-demo)"
# Bump when the generated files change for the same catalog entry.
RECIPE = 1
# Bump when only the muxing changes, so the encoded video is reused.
MUX = 2
# Video is built from segments of this length, each a single GOP of one still frame.
SEGMENT_SECONDS = 64
# Films in the bulk of the library are cut from a master this long.
MASTER_MINUTES = 180

NOTE = "Glacier demo media · not a real film"


# ---------------------------------------------------------------- basics

def load(name: str) -> dict:
    with open(ROOT / name, "rb") as f:
        return tomllib.load(f)


def work() -> Path:
    if os.environ.get("GLACIER_DEMO_DIR"):
        path = Path(os.environ["GLACIER_DEMO_DIR"])
    elif os.name == "nt":
        path = Path(os.environ["LOCALAPPDATA"]) / "glacier-demo"
    else:
        path = Path.home() / ".cache" / "glacier-demo"
    # Jellyfin scans 8.3 short paths (like USERNA~1) as empty folders.
    return path.resolve()


def fetch(entry: dict) -> Path:
    """The cached copy of a source, downloaded and verified on first use."""
    name = entry["url"].split("?")[0].rsplit("/", 1)[-1]
    path = work() / "cache" / f"{entry['sha256'][:12]}-{urllib.request.unquote(name)}"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  download {entry['url']}")
    request = urllib.request.Request(entry["url"], headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    partial = path.with_suffix(path.suffix + ".part")
    # Wikimedia answers a run of downloads with 429 now and then; wait and try again.
    for attempt in range(6):
        try:
            response = urllib.request.urlopen(request)
            break
        except urllib.error.HTTPError as error:
            if error.code not in (429, 502, 503, 504) or attempt == 5:
                raise
            wait = int(error.headers.get("Retry-After") or 0) or 10 * 2 ** attempt
            print(f"  {error.code}, again in {wait} s")
            time.sleep(wait)
    with response, open(partial, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != entry["sha256"]:
        partial.unlink()
        sys.exit(f"{entry['url']}: sha256 {digest.hexdigest()} does not match sources.toml")
    partial.replace(path)
    return path


def ffmpeg() -> str:
    found = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not found:
        sys.exit("ffmpeg not found: put it on PATH or set FFMPEG")
    return found


def duration(path: Path) -> float:
    tool = Path(ffmpeg())
    probe = subprocess.run([str(tool.with_name(tool.name.replace("ffmpeg", "ffprobe"))), "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(probe.stdout.strip())


def run_ffmpeg(*args) -> None:
    # SVT-AV1 prints its configuration on every run unless told otherwise.
    env = dict(os.environ, SVT_LOG="1")
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True, env=env)


RELEASE = "https://github.com/Glacier-Jellyfin/glacier-demo/releases/download/ai-v1"


def slug(title: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in title.lower()).split())


def still_key(show: str, season: int, episode: int) -> str:
    return f"ai-ep-{slug(show)}-s{season:02}e{episode:02}"


def person_key(name: str) -> str:
    return f"ai-person-{slug(name)}"


def generated(manifest: str, section: str) -> dict[str, dict]:
    """Pictures that ai.py made in a batch (episodes.toml, people.toml), as image sources."""
    if not (ROOT / manifest).exists():
        return {}
    return {key: {"url": f"{RELEASE}/{key}.jpg", "sha256": entry["sha256"], "license": "CC0",
                  "author": f"generated with FLUX.1-schnell, seed {entry['seed']}"}
            for key, entry in load(manifest).get(section, {}).items()}


def episode_stills() -> dict[str, dict]:
    return generated("episodes.toml", "still")


def file_title(title: str) -> str:
    """The title as part of a file name. Windows forbids some characters (and a
    colon would write an alternate data stream); the real title is in the NFO."""
    return "".join(c for c in title.replace(":", " -") if c not in '<>"/\\|?*')


def folder_name(item: dict) -> str:
    return f"{file_title(item['title'])} ({item['year']})"


# Made up, for the bulk of the film library.
STUDIOS = ["Northwind Pictures", "Highline Films", "Spree Filmproduktion", "Kestrel Television", "Little Kite Animation",
           "Harbourlight Studios", "Blue Meridian", "Saltmarsh Films", "Copperfield Pictures", "Nine Bridges Media"]
DIRECTORS = ["Mara Lindqvist", "Paul Ferreira", "Kira Holtz", "Lukas Reinholt", "Eleni Doukas", "Ruth Calloway",
             "Jonas Ek", "Miriam Sandberg", "Teodor Varga", "Amara Nwosu", "Felix Brandauer", "Ines Carvalho"]
ACTORS = ["Elin Strand", "Tomas Berge", "Sanna Moe", "Daniel Okafor", "Ingrid Vale", "Nadia Kerr", "Samuel Achterberg",
          "Leah Moreno", "Johanna Keller", "Ahmet Yilmaz", "Oskar Brandt", "Mila Sorensen", "Nikos Arvanitis",
          "Clara Weiss", "Rafael Ortega", "Priya Raman", "Jakob Lund", "Yara Haddad", "Malte Fischer", "Zoe Lambert",
          "Kenji Ota", "Freya Holm", "Marco Bellini", "Ada Kowalska"]
GENRE_STYLE = {"Drama": "serif", "Romance": "serif", "War": "serif", "Mystery": "serif", "Western": "display",
               "Action": "display", "Thriller": "display", "Crime": "display", "Horror": "display",
               "Science Fiction": "sans", "Documentary": "sans", "Sport": "sans", "Adventure": "sans",
               "Comedy": "soft", "Family": "soft", "Music": "soft", "Superhero": "display"}
ACCENTS = ["#f2c879", "#9fd3ff", "#ff8a6b", "#ffd166", "#c3f0ca", "#e0b3ff", "#ffb38a", "#a0e7e5", "#ffffff"]
# Every this many films in the bulk, one is 720p, for a second resolution in the grid.
SD_EVERY = 7


def pick(title: str, salt: str, choices: list, count: int = 1) -> list:
    """count different entries of choices, the same for the same title every time."""
    seed = int(hashlib.sha1(f"{salt}:{title}".encode()).hexdigest(), 16)
    pool, result = list(choices), []
    for _ in range(count):
        seed, index = divmod(seed, len(pool))
        result.append(pool.pop(index))
    return result


def with_ai_images(titles: list[dict]) -> list[dict]:
    """Puts the generated images that sources.toml has pinned in place of a title's photos."""
    pinned = load("sources.toml")["image"]
    by_title = {t["title"]: t for t in titles}
    for key, entry in load("prompts.toml")["image"].items():
        if key in pinned and entry.get("title") in by_title:
            item = by_title[entry["title"]]
            if entry["shape"] == "poster":
                # Generated at 2:3 with the subject at the top and room for the title below.
                bottom = {"top-tagline-below": "bottom-tagline-below", "bottom-tagline-below": "bottom-tagline-below",
                          "bottom-tagline-top": "bottom-tagline-top"}.get(item.get("layout"), "bottom")
                item.update(poster=key, focus=0.5, plain_poster=False, layout=bottom)
            else:
                item["backdrop"] = key
    return titles


def collections_of(catalog: dict) -> dict[str, list[dict]]:
    """Each collection with its films and series, oldest first: from "collection" on a
    title and from [[collection]] lists."""
    titles = movies(catalog) + shows(catalog)
    groups: dict[str, list[dict]] = {}
    for item in titles:
        names = item.get("collection", [])
        for name in [names] if isinstance(names, str) else names:
            groups.setdefault(name, []).append(item)
    by_title = {item["title"]: item for item in titles}
    for listed in catalog.get("collection", []):
        groups.setdefault(listed["name"], []).extend(by_title[t] for t in listed["titles"])
    for members in groups.values():
        members.sort(key=lambda m: m["year"])
    return groups


def movies(catalog: dict) -> list[dict]:
    """The films of the catalog: the [[movie]] tables, then the one-line films."""
    result = list(catalog.get("movie", []))
    known = {m["title"] for m in result}
    rows = [row for row in catalog.get("fillers", {}).get("films", []) if row[0] not in known]
    for n, (title, year, runtime, rating, genres, image, focus, plot) in enumerate(rows):
        genre_list = genres.split("/")
        roles = ["Lead", "Friend", "Stranger"]
        result.append({
            "title": title, "year": year, "runtime": runtime, "rating": rating, "genres": genre_list,
            "plot": plot, "poster": image, "backdrop": image, "focus": focus,
            "score": round(5.5 + int(hashlib.sha1(title.encode()).hexdigest()[:4], 16) % 33 / 10, 1),
            "studios": pick(title, "studio", STUDIOS), "directors": pick(title, "director", DIRECTORS),
            "cast": [[name, role] for name, role in zip(pick(title, "cast", ACTORS, 3), roles)],
            "style": GENRE_STYLE.get(genre_list[-1] if genre_list[-1] == "Superhero" else genre_list[0], "serif"),
            "accent": pick(title, "accent", ACCENTS)[0],
            "files": [{"video": "720p" if n % SD_EVERY == SD_EVERY - 1 else "1080p", "tracks": "light"}],
            "filler": True,
        })
    return with_ai_images(result)


def shows(catalog: dict) -> list[dict]:
    """The series of the catalog: the [[show]] tables, then the one-line series."""
    result = list(catalog.get("show", []))
    known = {s["title"] for s in result}
    pool = catalog.get("fillers", {}).get("episode_titles", [])
    stills = episode_stills()
    for title, year, status, rating, genres, image, focus, seasons, runtime, plot in catalog.get("fillers", {}).get("shows", []):
        if title in known:
            continue
        genre_list = genres.split("/")
        names = iter(pick(title, "episodes", pool, sum(seasons)))
        season_list = []
        for number, count in enumerate(seasons, 1):
            episodes = []
            for e in range(count):
                aired = f"{year + number - 1}-{3 + e // 4:02}-{1 + e % 4 * 7:02}"
                # A generated still where there is one, else a crop along the series image.
                key = still_key(title, number, e + 1)
                if key in stills:
                    episodes.append({"title": next(names), "image": key, "aired": aired})
                else:
                    episodes.append({"title": next(names), "image": image, "focus": (0.1 + 0.13 * e) % 0.9 + 0.05, "aired": aired})
            season_list.append({"number": number, "episodes": episodes})
        kids = "Kids" in genre_list
        result.append({
            "title": title, "year": year, "status": status, "rating": rating, "genres": genre_list, "plot": plot,
            "runtime": runtime, "poster": image, "backdrop": image, "focus": focus,
            "score": round(6.0 + int(hashlib.sha1(title.encode()).hexdigest()[:4], 16) % 30 / 10, 1),
            "studios": pick(title, "studio", STUDIOS),
            "cast": [[name, role] for name, role in zip(pick(title, "cast", ACTORS, 4), ["Lead", "Partner", "Rival", "Neighbour"])],
            "style": GENRE_STYLE.get(genre_list[-1] if genre_list[-1] == "Superhero" else genre_list[0], "soft" if kids else "serif"),
            "accent": pick(title, "accent", ACCENTS)[0],
            "video": "720p" if kids else "1080p",
            "tracks": "anime" if "Anime" in genre_list else "light",
            "season": season_list,
            "filler": True,
        })
    return with_ai_images(result)


# ---------------------------------------------------------------- images

class Art:
    """Posters, logos and frames drawn from the source photos."""

    def __init__(self, sources: dict, billing: bool = False):
        self.sources = dict(sources, image={**sources["image"], **episode_stills(), **generated("people.toml", "person")})
        # The credits block at the foot of the posters; catalog.toml [posters] billing.
        self.billing_block = billing
        self.fonts: dict[str, Path] = {}
        self.photos: dict[str, Image.Image] = {}

    def font(self, style: str, size: int, weight: str | None = None) -> ImageFont.FreeTypeFont:
        if style not in self.fonts:
            self.fonts[style] = fetch(self.sources["font"][style])
        font = ImageFont.truetype(str(self.fonts[style]), max(8, round(size)))
        # Variable fonts open at their lightest weight; static ones ignore this.
        for name in ([weight] if weight else []) + ["Bold", "SemiBold"]:
            try:
                font.set_variation_by_name(name)
                break
            except (OSError, ValueError):
                continue
        return font

    def photo(self, key: str) -> Image.Image:
        if key not in self.photos:
            image = Image.open(fetch(self.sources["image"][key]))
            self.photos[key] = ImageOps.exif_transpose(image).convert("RGB")
        return self.photos[key]

    def cover(self, key: str, width: int, height: int, focus: float = 0.5) -> Image.Image:
        image = self.photo(key)
        scale = max(width / image.width, height / image.height)
        scaled = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        left = round((scaled.width - width) * focus)
        top = (scaled.height - height) // 2
        return scaled.crop((left, top, left + width, top + height))

    @staticmethod
    def shade(image: Image.Image, start: float, strength: float, base: float = 0.0) -> Image.Image:
        """Darkens towards the bottom from start (0..1 of the height)."""
        column = Image.new("L", (1, 256))
        for y in range(256):
            t = max(0.0, (y / 256 - start) / (1 - start))
            column.putpixel((0, y), round(255 * min(1.0, base + strength * t ** 1.4)))
        mask = column.resize(image.size, Image.BILINEAR)
        return Image.composite(Image.new("RGB", image.size, "black"), image, mask)

    def fit(self, text: str, style: str, max_width: int, size: int, max_lines: int = 3):
        """The largest font at which text wraps into max_lines lines of max_width."""
        if style == "display":
            text = text.upper()
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        while size > 12:
            font = self.font(style, size)
            lines, line = [], ""
            for word in text.split():
                candidate = f"{line} {word}".strip()
                if probe.textlength(candidate, font=font) <= max_width or not line:
                    line = candidate
                else:
                    lines.append(line)
                    line = word
            lines.append(line)
            if len(lines) <= max_lines and all(probe.textlength(l, font=font) <= max_width for l in lines):
                return font, lines
            size -= 4
        return self.font(style, size), [text]

    # ---- poster typography

    # How a kind of film sets its title: font and weight, letter spacing as a
    # share of the size, the largest size, the finish of the letters, and
    # thin rules above and below.
    TREATMENTS = {
        "blockbuster": {"font": "impact", "tracking": 0.03, "size": 230, "finish": "metal"},
        "prestige": {"font": "garamond", "weight": "Medium", "tracking": 0.16, "size": 130, "finish": "plain"},
        "thriller": {"font": "condensed", "weight": "Light", "tracking": 0.32, "size": 140, "finish": "plain",
                     "rules": True},
        "horror": {"font": "skinny", "tracking": 0.12, "size": 320, "finish": "plain"},
        "scifi": {"font": "shoulders", "weight": "Light", "tracking": 0.42, "size": 140, "finish": "glow"},
        "western": {"font": "roman", "weight": "Bold", "tracking": 0.10, "size": 150, "finish": "gold"},
        "documentary": {"font": "wide", "weight": "Light", "tracking": 0.40, "size": 90, "finish": "plain", "rules": True},
        "indie": {"font": "gothic", "tracking": 0.05, "size": 230, "finish": "plain"},
        "family": {"font": "soft", "weight": "Black", "tracking": 0.0, "size": 200, "finish": "cartoon", "case": "title"},
    }
    # The first genre here that a title has picks its treatment; dramas and the rest are prestige.
    GENRE_TREATMENTS = [("Superhero", "blockbuster"), ("Horror", "horror"), ("Science Fiction", "scifi"),
                        ("Animation", "family"), ("Anime", "family"), ("Kids", "family"), ("Family", "family"),
                        ("Action", "blockbuster"), ("Western", "western"), ("Thriller", "thriller"),
                        ("Crime", "thriller"), ("Mystery", "thriller"), ("Medical", "thriller"), ("Documentary", "documentary"),
                        ("Comedy", "indie"), ("Music", "indie"), ("Sport", "indie")]

    def treatment(self, item: dict) -> dict:
        genres = item.get("genres", [])
        name = item.get("treatment") or next((t for g, t in self.GENRE_TREATMENTS if g in genres), "prestige")
        return self.TREATMENTS[name]

    @staticmethod
    def tiers(title: str) -> list[tuple[str, str]]:
        """The title in levels: a sequel's subtitle below, a leading "The" small above."""
        if ":" in title:
            main, sub = title.split(":", 1)
            return [("main", main.strip()), ("sub", sub.strip())]
        words = title.split()
        if len(words) > 1 and words[0].lower() == "the":
            return [("small", words[0]), ("main", " ".join(words[1:]))]
        return [("main", title)]

    def letters(self, text: str, font: ImageFont.FreeTypeFont, tracking: float) -> Image.Image:
        """One line of text as a mask, with tracking added between the letters."""
        gap = tracking * font.size
        width = sum(font.getlength(c) for c in text) + gap * max(0, len(text) - 1)
        ascent, descent = font.getmetrics()
        mask = Image.new("L", (max(1, round(width) + 8), ascent + descent + 8))
        draw, x = ImageDraw.Draw(mask), 4.0
        for c in text:
            draw.text((x, 4), c, font=font, fill=255)
            x += font.getlength(c) + gap
        box = mask.getbbox()
        return mask.crop(box) if box else mask

    def wrapped(self, text: str, style: dict, size: float, max_width: int, max_lines: int) -> list[Image.Image] | None:
        """text in lines no wider than max_width at this size, or None if it needs more lines."""
        font = self.font(style["font"], size, style.get("weight"))
        lines, line = [], ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if not line or self.letters(candidate, font, style["tracking"]).width <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
        masks = [self.letters(l, font, style["tracking"]) for l in lines]
        if len(masks) > max_lines or any(m.width > max_width for m in masks):
            return None
        return masks

    def title_block(self, item: dict, max_width: int, scale: float, align: str, extra: list[str]) -> Image.Image:
        """The title with its finish, and lines below it (season, tagline), as one RGBA image."""
        style = self.treatment(item)
        upper = style.get("case") != "title"
        rows = []  # (mask, gap above)
        for level, text in self.tiers(item["title"]):
            text = text.upper() if upper else text
            if level == "main":
                size = style["size"] * scale
                while (masks := self.wrapped(text, style, size, max_width, 2)) is None and size > 30:
                    size *= 0.93
                font = self.font(style["font"], size, style.get("weight"))
                masks = masks or [self.letters(text, font, style["tracking"])]
                rows += [(m, round(size * 0.14)) for m in masks]
            else:
                # A sub-title or a small "The" in the same font, smaller and spaced wider.
                small = dict(style, tracking=style["tracking"] + 0.25)
                masks = self.wrapped(text.upper(), small, style["size"] * scale * 0.32, max_width, 1)
                rows += [(m, round(style["size"] * scale * 0.12)) for m in masks or []]
        line_font = self.font("condensed", 34, "Regular")
        for text in extra:
            rows.append((self.letters(text.upper(), line_font, 0.3), 30))
        width = max(m.width for m, _ in rows)
        height = sum(m.height + gap for m, gap in rows) - rows[0][1]
        pad = 40
        mask = Image.new("L", (width + 2 * pad, height + 2 * pad))
        extra_mask = Image.new("L", mask.size)
        y, title_rows = pad, len(rows) - len(extra)
        for n, (m, gap) in enumerate(rows):
            y += gap if n else 0
            x = {"left": pad, "right": pad + width - m.width}.get(align, pad + (width - m.width) // 2)
            (mask if n < title_rows else extra_mask).paste(m, (x, y), m)
            y += m.height
        return self.finish_letters(mask, extra_mask, style["finish"], item.get("accent", "#ffffff"))

    @staticmethod
    def gradient(size: tuple[int, int], stops: list[str]) -> Image.Image:
        column = Image.new("RGB", (1, len(stops)))
        for n, colour in enumerate(stops):
            column.putpixel((0, n), ImageColor.getrgb(colour))
        return column.resize(size, Image.BICUBIC)

    def finish_letters(self, mask: Image.Image, extra: Image.Image, finish: str, accent: str) -> Image.Image:
        """Colours the letters (metal, gold, glow, cartoon or plain white) over a soft shadow."""
        size = mask.size
        shadow = Image.new("RGBA", size, (0, 0, 0, 255))
        shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(14)).point(lambda a: a * 0.7))
        layer = Image.alpha_composite(Image.new("RGBA", size, (0, 0, 0, 0)), shadow)
        if finish == "glow":
            glow = Image.new("RGBA", size, ImageColor.getrgb(accent) + (255,))
            glow.putalpha(mask.filter(ImageFilter.GaussianBlur(18)))
            layer = Image.alpha_composite(layer, glow)
        if finish == "cartoon":
            thick = mask.filter(ImageFilter.MaxFilter(11))
            drop = Image.new("RGBA", size, (0, 0, 0, 0))
            drop.paste(Image.new("RGBA", size, (20, 20, 40, 220)), (6, 9), thick)
            outline = Image.new("RGBA", size, (255, 255, 255, 255))
            outline.putalpha(thick)
            layer = Image.alpha_composite(Image.alpha_composite(layer, drop), outline)
        fills = {"metal": ["#ffffff", "#dfe5ea", "#9aa4ae", "#eef2f5"], "gold": ["#fff1c9", "#e7c27a", "#b9853c"],
                 "cartoon": [accent, accent]}
        fill = self.gradient(size, fills.get(finish, ["#ffffff", "#ffffff"])).convert("RGBA")
        fill.putalpha(mask)
        layer = Image.alpha_composite(layer, fill)
        lines = Image.new("RGBA", size, ImageColor.getrgb(accent) + (255,))
        lines.putalpha(extra)
        return Image.alpha_composite(layer, lines)

    def billing(self, item: dict, width: int) -> Image.Image:
        """The block of credits at the foot of a poster: small words, bigger names."""
        title = item["title"]
        crew = pick(title, "crew", DIRECTORS + ACTORS, 5)
        director = (item.get("directors") or crew[:1])[0]
        tokens = [(item["studios"][0], True), ("presents", False), ("a", False), (director, True), ("film", False)]
        tokens += [(name, True) for name, _ in item.get("cast", [])]
        for role, name in zip(["music by", "edited by", "director of photography", "produced by"], crew[1:]):
            tokens += [(role, False), (name, True)]
        tokens += [("written by", False), ((item.get("writers") or [director])[0], True),
                   ("directed by", False), (director, True)]
        big, small = self.font("billing", 30), self.font("billing", 18)
        rows, row, row_width = [], [], 0.0
        for text, name in tokens:
            font = big if name else small
            w = font.getlength(text.upper()) + 14
            if row and row_width + w > width:
                rows.append(row)
                row, row_width = [], 0.0
            row.append((text.upper(), font))
            row_width += w
        rows.append(row)
        image = Image.new("L", (width, 40 * len(rows)))
        draw = ImageDraw.Draw(image)
        for n, row in enumerate(rows):
            total = sum(f.getlength(t) + 14 for t, f in row) - 14
            x = (width - total) / 2
            for text, font in row:
                draw.text((x, 32 + n * 40), text, font=font, fill=190, anchor="ls")
                x += font.getlength(text) + 14
        return image

    @staticmethod
    def film_look(image: Image.Image, seed: str) -> Image.Image:
        """A vignette and fine grain, so a photo reads as a printed poster."""
        width, height = image.size
        vignette = ImageOps.invert(Image.radial_gradient("L")).resize((width, height))
        vignette = vignette.point(lambda v: 255 - round((255 - v) * 0.55))
        image = Image.composite(image, Image.new("RGB", image.size, "black"), vignette)
        random.seed(seed)
        noise = Image.frombytes("L", (width // 2, height // 2),
                                bytes(random.randint(108, 148) for _ in range((width // 2) * (height // 2))))
        noise = noise.resize(image.size, Image.BILINEAR).convert("RGB")
        return Image.blend(image, ImageChops.soft_light(image, noise), 0.6)

    # Fixed layouts for "layout" in the catalog: where the title sits and the tagline.
    LAYOUTS = {
        "bottom": ("bottom", None),
        "top": ("top", None),
        "center": ("center", None),
        "bottom-tagline-below": ("bottom", "below"),
        "top-tagline-below": ("top", "below"),
        "bottom-tagline-top": ("bottom", "edge"),
    }

    def layout(self, item: dict) -> dict:
        """Where the title goes, its alignment and size, and the tagline.

        Picked from the title, so every poster differs and stays the same between
        builds; most go without a tagline. "layout" and "align" in the catalog win.
        """
        title = item["title"]
        place = pick(title, "place", ["bottom", "bottom", "top", "top", "center"])[0]
        tagline = pick(title, "tagline", [None, None, None, "below", "edge"])[0]
        if item.get("layout"):
            place, tagline = self.LAYOUTS[item["layout"]]
        return {
            "place": place,
            "tagline": tagline if item.get("tagline") else None,
            # Mostly centred; now and then flush left or right.
            "align": item.get("align") or pick(title, "align", ["center"] * 6 + ["left", "right"])[0],
            "scale": pick(title, "scale", [1.0, 0.92, 0.84, 0.76])[0],
        }

    def collection(self, name: str, members: list[dict]) -> tuple[Image.Image, Image.Image]:
        """Poster and backdrop of a collection: generated ones from prompts.toml, else a
        collage of its members, lettered in the style of its first member."""
        pinned = self.sources["image"]
        generated = {e["shape"]: key for key, e in load("prompts.toml")["image"].items()
                     if e.get("collection") == name and key in pinned}
        item = dict(members[0], title=name, layout="bottom", align="center", tagline=None)
        if "poster" in generated:
            poster = self.poster(item, generated["poster"], 0.5)
        else:
            # A busy collage needs quiet lettering, whatever its members are.
            poster = self.poster(dict(item, treatment="documentary"), None, 0.5, base=self.collage(members))
        backdrop = self.cover(generated.get("backdrop") or members[0]["backdrop"], 1920, 1080)
        return poster, backdrop

    def collage(self, members: list[dict]) -> Image.Image:
        """The members' backdrops side by side in tall strips, for a collection without its own picture."""
        width, height = 1000, 1500
        image = Image.new("RGB", (width, height))
        columns = min(3, len(members))
        rows = (len(members) + columns - 1) // columns
        for n, member in enumerate(members):
            w, h = width // columns, height // rows
            image.paste(self.cover(member["backdrop"], w - 4, h - 4, 0.5), ((n % columns) * w + 2, (n // columns) * h + 2))
        return self.shade(image, 0.0, 0.0, 0.3)

    def poster(self, item: dict, key: str | None, focus: float, subtitle: str | None = None,
               base: Image.Image | None = None) -> Image.Image:
        """A poster: the image (or base), then title, tagline and (if wanted) credits set like a film's one-sheet."""
        width, height, margin = 1000, 1500, 80
        layout = self.layout(item)
        place, tagline, align = layout["place"], layout["tagline"], layout["align"]
        image = base if base is not None else self.cover(key, width, height, focus)
        # Dark behind the title, and a lighter shade along the top for a tagline.
        if place == "bottom":
            image = self.shade(image, 0.45, 0.92, 0.05)
        elif place == "top":
            image = ImageOps.flip(self.shade(ImageOps.flip(image), 0.5, 0.92, 0.05))
        else:
            image = self.shade(image, 0.0, 0.0, 0.32)
        image = ImageOps.flip(self.shade(ImageOps.flip(image), 0.85, 0.5))
        image = self.film_look(image, item["title"])
        accent = item.get("accent", "#ffffff")
        canvas = image.convert("RGBA")

        if tagline == "edge" and not subtitle:
            line = self.letters(item["tagline"].upper(), self.font("condensed", 26, "Regular"), 0.25)
            colour = Image.new("RGBA", line.size, ImageColor.getrgb(accent) + (255,))
            canvas.paste(colour, ((width - line.width) // 2, 56), line)

        foot = 0
        if self.billing_block and not subtitle:
            block = self.billing(item, width - 2 * margin)
            foot = block.height + 40
            canvas.paste(Image.new("RGBA", block.size, (210, 210, 210, 255)), (margin, height - foot), block)

        extra = [subtitle] if subtitle else ([item["tagline"]] if tagline == "below" else [])
        title = self.title_block(item, width - 2 * margin, layout["scale"], align, extra)
        # A centred title sits a little above or below the middle, never quite on it twice.
        shift = pick(item["title"], "shift", [-0.12, -0.07, -0.03, 0.03, 0.07, 0.12])[0] * height
        y = {"top": 90,
             "center": (height - title.height) / 2 + shift,
             "bottom": height - 80 - foot - title.height}[place]
        x = {"left": margin - 40, "right": width - margin + 40 - title.width}.get(align, (width - title.width) // 2)
        canvas.alpha_composite(title, (round(x), round(y)))
        if self.treatment(item).get("rules"):
            draw = ImageDraw.Draw(canvas)
            box = title.getbbox()
            for ry in (y + box[1] - 22, y + box[3] + 22):
                draw.line([(x + box[0], ry), (x + box[2], ry)], fill=accent, width=2)
        return canvas.convert("RGB")

    def logo(self, item: dict) -> Image.Image:
        block = self.title_block(item, 1100, 1.0, "center", [])
        return block.crop(block.getbbox())

    def landscape(self, item: dict) -> Image.Image:
        image = self.shade(self.cover(item["backdrop"], 1280, 720), 0.35, 0.8)
        logo = self.logo(item)
        logo.thumbnail((620, 260))
        image.paste(logo, (64, 720 - 64 - logo.height), logo)
        return image

    def frame(self, item: dict, key: str, width: int, height: int, focus: float, lines: list[str]) -> Image.Image:
        """A video frame: the photo dimmed, with the title and where in the title this is."""
        image = self.shade(self.cover(key, width, height, focus), 0.0, 0.0, 0.35)
        draw = ImageDraw.Draw(image)
        unit = height / 1080
        x, y = round(96 * unit), round(height - 96 * unit)
        draw.text((x, y), NOTE.upper(), font=self.font("sans", round(22 * unit)), fill="#9a9a9a", anchor="ls")
        for n, line in enumerate(reversed(lines)):
            y -= round((54 if n == 0 else 48) * unit)
            size = 64 if n == len(lines) - 1 else 34
            draw.text((x, y), line, font=self.font("sans" if n < len(lines) - 1 else item.get("style", "serif"), round(size * unit)),
                      fill="white", anchor="ls")
        return image

    def blank(self, width: int, height: int, n: int, count: int) -> Image.Image:
        """A neutral frame for films that share one video: dark, with the chapter."""
        hue = ACCENTS[n % len(ACCENTS)]
        image = Image.new("RGB", (width, height), "#101418")
        glow = Image.new("RGB", (width, height), hue)
        # Brightest in the middle, a quarter of the accent at most.
        mask = ImageOps.invert(Image.radial_gradient("L")).resize((width, height)).point(lambda v: v // 4)
        image = Image.composite(glow, image, mask)
        draw = ImageDraw.Draw(image)
        unit = height / 1080
        draw.text((width / 2, height / 2), f"Chapter {n + 1} of {count}", font=self.font("sans", round(72 * unit)),
                  fill="white", anchor="mm")
        draw.text((width / 2, height / 2 + 80 * unit), NOTE.upper(), font=self.font("sans", round(24 * unit)),
                  fill="#9a9a9a", anchor="mm")
        return image

    def avatar(self, name: str, color: str) -> Image.Image:
        image = Image.new("RGB", (512, 512), color)
        ImageDraw.Draw(image).text((256, 270), name[0], font=self.font("sans", 280), fill="white", anchor="mm")
        return image


# ---------------------------------------------------------------- video

AUDIO = {
    "aac": lambda ch: ["-c:a", "aac", "-b:a", "64k" if ch > 2 else "32k"],
    "ac3": lambda ch: ["-c:a", "ac3", "-b:a", "96k"],
    "eac3": lambda ch: ["-c:a", "eac3", "-b:a", "32k"],
    "flac": lambda ch: ["-c:a", "flac"],
    "opus": lambda ch: ["-c:a", "libopus", "-b:a", "16k"] + (["-mapping_family", "1"] if ch > 2 else []),
    "truehd": lambda ch: ["-c:a", "truehd", "-strict", "-2"],
    "dts": lambda ch: ["-c:a", "dca", "-strict", "-2"],
}
LAYOUT = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
FLAGS = {"default": "default", "original": "original", "commentary": "comment", "visual_impaired": "visual_impaired",
         "forced": "forced", "hearing_impaired": "hearing_impaired"}

DIALOGUE = [
    "Did you see the light last night?", "It has not turned in thirty years.", "Then who keeps it running?",
    "Nobody goes out there after dark.", "Give me the logbook.", "The storm is coming in early.",
    "We can still make it back before the tide.", "You should not have come back.", "I had to see it for myself.",
    "Listen. Do you hear that?", "Keep the lamp burning.", "We leave at first light.",
]
SOUNDS = ["[waves crashing]", "[wind howling]", "[door creaks]", "[distant foghorn]", "[footsteps]"]
SIGNS = ["HARBOUR OFFICE", "NO ENTRY", "PLATFORM 4", "CLOSED FOR THE WINTER", "MISSING", "LAST FERRY 18:40"]
LYRICS = ["Fold the paper, fold it twice,", "hang it where the night is nice,", "little moon, don't drift away,",
          "stay with me until the day."]


def timestamp(seconds: float, ass: bool = False) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if ass:
        return f"{int(h)}:{int(m):02}:{s:05.2f}"
    return f"{int(h):02}:{int(m):02}:{int(s):02},{round((s % 1) * 1000):03}"


def cues(track: dict, duration: float) -> list[tuple[float, float, str]]:
    kind = track.get("text", "dialogue")
    label = track.get("title", track["lang"])
    result = []
    if kind == "signs":
        for n, start in enumerate(range(45, int(duration) - 5, 150)):
            result.append((start, start + 3.5, f"{SIGNS[n % len(SIGNS)]}\n({label})"))
    elif kind == "lyrics":
        for verse_start in range(120, int(duration) - 20, 600):
            for n, line in enumerate(LYRICS):
                start = verse_start + n * 4
                result.append((start, start + 3.8, f"♪ {line} ♪\n({label})"))
    else:
        sdh = track.get("hearing_impaired") or "SDH" in label or "CC" in label.split()
        for n, start in enumerate(range(8, int(duration) - 5, 9)):
            text = DIALOGUE[n % len(DIALOGUE)]
            if sdh and n % 3 == 0:
                text = f"{SOUNDS[n // 3 % len(SOUNDS)]} {text}"
            result.append((start, start + 4.5, f"{text}\n({label} · {timestamp(start)[:8]})"))
    return result


def write_subtitle(path: Path, track: dict, duration: float) -> None:
    lines = []
    if track.get("format") == "ass":
        lines += ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "",
                  "[V4+ Styles]",
                  "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginV",
                  "Style: Default,Arial,58,&H00FFFFFF,&H00000000,1,3,0,2,60", "",
                  "[Events]", "Format: Layer, Start, End, Style, Text"]
        for start, end, text in cues(track, duration):
            text = text.replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{timestamp(start, True)},{timestamp(end, True)},Default,{text}")
    else:
        for n, (start, end, text) in enumerate(cues(track, duration), 1):
            lines += [str(n), f"{timestamp(start)} --> {timestamp(end)}", text, ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Builder:
    def __init__(self):
        self.sources = load("sources.toml")
        self.tracks = load("tracks.toml")
        self.art = Art(self.sources, billing=load("catalog.toml").get("posters", {}).get("billing", False))
        self.media = work() / "media"
        self.temp = work() / "build"

    # ---- one media file

    def video(self, spec: dict, runtime: float, frames) -> tuple[Path, list[tuple[float, float, str]]]:
        """The video stream: one still per chapter, each repeated as whole segments.

        frames(n, count, label) draws chapter n of count; without frames the video
        is cut from a shared master. frames.lead and frames.tail name sections
        before and after the numbered chapters, as (name, segments): "Recap",
        "Intro", "Credits", "Preview", which the Chapter Segments Provider turns
        into media segments. Returns the file and the chapters.
        """
        if frames is None:
            return self.cut(spec, runtime)
        if getattr(frames, "footage", None):
            return self.footage_video(spec, runtime, frames)
        fps = Fraction(spec["fps"])
        per_segment = round(SEGMENT_SECONDS * fps)
        segment = per_segment / fps
        segments = max(1, round(runtime * 60 / segment))
        lead, tail = list(getattr(frames, "lead", [])), list(getattr(frames, "tail", []))
        # Named sections only where the story keeps at least two segments.
        while lead + tail and segments - sum(n for _, n in lead + tail) < 2:
            (tail if tail else lead).pop()
        story = segments - sum(n for _, n in lead + tail)
        count = max(1, min(12, story // 8)) if story >= 8 else 1
        parts = lead + [(f"Chapter {n + 1}", story // count + (1 if n < story % count else 0)) for n in range(count)] + tail
        work_dir = self.temp / "video"
        work_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha1(json.dumps([spec, runtime, frames.key, parts, RECIPE], sort_keys=True).encode()).hexdigest()[:16]
        output = work_dir / f"{stem}.mkv"
        chapters, start = [], 0.0
        for name, length in parts:
            end = start + float(length * segment)
            chapters.append((start, end, name))
            start = end
        if output.exists():
            return output, chapters
        listing = []
        counts = [length for _, length in parts]
        for n, (name, length) in enumerate(parts):
            png = work_dir / f"{stem}-{n}.png"
            label = f"{name} of {count}" if name.startswith("Chapter ") else name
            frames(n, len(parts), label).save(png)
            part = work_dir / f"{stem}-{n}.mkv"
            # The loop filter repeats the converted frame in memory; reading the
            # picture again for every frame takes longer than encoding it.
            pre, codec = self.encoder(spec, per_segment)
            repeat = f"loop=loop={per_segment - 1}:size=1:start=0,setpts=N*{fps.denominator}/{fps.numerator}/TB"
            run_ffmpeg("-i", png, "-vf", f"{pre},{repeat}", "-r", spec["fps"], "-frames:v", per_segment, *codec, part)
            listing += [f"file '{part.name}'"] * length
        concat = work_dir / f"{stem}.txt"
        concat.write_text("\n".join(listing) + "\n", encoding="utf-8")
        run_ffmpeg("-f", "concat", "-safe", 0, "-i", concat, "-c", "copy", output)
        for n in range(len(counts)):
            (work_dir / f"{stem}-{n}.mkv").unlink()
            (work_dir / f"{stem}-{n}.png").unlink()
        concat.unlink()
        return output, chapters

    def cut(self, spec: dict, runtime: float) -> tuple[Path, list[tuple[float, float, str]]]:
        """The first runtime minutes of a three-hour master with neutral frames."""
        art = self.art

        def blank(n: int, count: int, label: str) -> Image.Image:
            return art.blank(spec["width"], spec["height"], n, count)

        blank.key = ["master"]
        master, chapters = self.video(spec, MASTER_MINUTES, blank)
        fps = Fraction(spec["fps"])
        segment = round(SEGMENT_SECONDS * fps) / fps
        duration = float(max(1, round(runtime * 60 / segment)) * segment)
        output = master.with_name(f"{master.stem}-{round(duration)}.mkv")
        if not output.exists():
            run_ffmpeg("-i", master, "-t", f"{duration:.3f}", "-map", 0, "-c", "copy", output)
        return output, [(start, min(end, duration), title) for start, end, title in chapters if start < duration]

    # ---- real footage

    # How long the named sections run, in seconds.
    SECTION_SECONDS = {"Recap": 45, "Intro": 50, "Credits": 60, "Preview": 30}

    def footage_video(self, spec: dict, runtime: float, frames) -> tuple[Path, list[tuple[float, float, str]]]:
        """The video from real clips: named sections first and last, the reel repeated in between.

        The clips are cut into an SDR reel once; every piece is encoded from it with the
        same settings (and the HDR mapping, where wanted), then joined by stream copy.
        """
        item, variant = frames.footage["item"], frames.footage["variant"]
        clips = item["footage"]
        clips = clips[variant % len(clips):] + clips[:variant % len(clips)]
        sections = [(name, self.SECTION_SECONDS[name] * (2 if name == "Credits" and n > 1 else 1))
                    for name, n in frames.lead + frames.tail]
        lead, tail = sections[:len(frames.lead)], sections[len(frames.lead):]
        work_dir = self.temp / "footage"
        work_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha1(json.dumps([spec, item["title"], clips, sections, runtime, frames.key, RECIPE],
                                       sort_keys=True).encode()).hexdigest()[:16]
        output = work_dir / f"{stem}.mkv"
        fps = Fraction(spec["fps"])
        gop = round(2 * fps)
        reel = self.reel(spec, clips, work_dir)
        reel_length = duration(reel)
        story = runtime * 60 - sum(s for _, s in sections)
        count = max(1, min(12, round(story / 600)))
        chapters, start = [], 0.0
        for name, seconds in lead:
            chapters.append((start, start + seconds, name))
            start += seconds
        for n in range(count):
            chapters.append((start, start + story / count, f"Chapter {n + 1}"))
            start += story / count
        for name, seconds in tail:
            chapters.append((start, start + seconds, name))
            start += seconds
        if output.exists():
            return output, chapters

        pre, codec = self.encoder(spec, gop, video=True)
        story_file = work_dir / f"{stem}-story.mkv"
        run_ffmpeg("-i", reel, "-vf", pre, *codec, "-an", story_file)
        listing = []
        offsets = {"Intro": 0.0, "Recap": reel_length / 3, "Preview": reel_length * 2 / 3}
        pieces = {}
        for name, seconds in sections:
            piece = work_dir / f"{stem}-{name.lower()}.mkv"
            if name == "Credits":
                self.credits_piece(item, spec, seconds, pre, codec, piece)
            else:
                self.titled_piece(item, spec, reel, offsets[name] % max(1.0, reel_length - seconds), seconds,
                                  name, frames.footage.get("heading"), pre, codec, piece)
            pieces[name] = piece
        listing += [f"file '{pieces[name].name}'" for name, _ in lead]
        story_length, left = duration(story_file), story
        while left > 0.5:
            listing.append(f"file '{story_file.name}'")
            if left < story_length:
                listing.append(f"outpoint {left:.3f}")
            left -= story_length
        listing += [f"file '{pieces[name].name}'" for name, _ in tail]
        concat = work_dir / f"{stem}.txt"
        concat.write_text("\n".join(listing) + "\n", encoding="utf-8")
        run_ffmpeg("-f", "concat", "-safe", 0, "-i", concat, "-c", "copy", output)
        for piece in [story_file, concat, *pieces.values()]:
            piece.unlink()
        return output, chapters

    def reel(self, spec: dict, clips: list[dict], work_dir: Path) -> Path:
        """The clips cut, scaled and joined into one SDR video at the target size and rate."""
        key = hashlib.sha1(json.dumps([spec["width"], spec["height"], spec["fps"], clips], sort_keys=True).encode())
        output = work_dir / f"reel-{key.hexdigest()[:16]}.mkv"
        if output.exists():
            return output
        width, height = spec["width"], spec["height"]
        inputs, filters = [], []
        for n, clip in enumerate(clips):
            inputs += ["-ss", clip.get("start", 0), "-t", clip["length"], "-i", fetch(self.sources["video"][clip["clip"]])]
            crop = f"crop=iw:ih*{1 - clip['crop_bottom']}:0:0," if clip.get("crop_bottom") else ""
            filters.append(f"[{n}:v]{crop}scale={width}:{height}:force_original_aspect_ratio=increase,"
                           f"crop={width}:{height},setsar=1,fps={spec['fps']},format=yuv420p[v{n}]")
        joined = "".join(f"[v{n}]" for n in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[out]"
        # Nearly lossless: every piece of the film is encoded from this.
        run_ffmpeg(*inputs, "-filter_complex", ";".join(filters + [joined]), "-map", "[out]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", 16, "-an", output)
        return output

    def titled_piece(self, item: dict, spec: dict, reel: Path, start: float, seconds: float, name: str,
                     heading: str | None, pre: str, codec: list, output: Path) -> None:
        """A stretch of the reel with the title over it: the intro, or "Previously on" and "Next time on"."""
        width, height = spec["width"], spec["height"]
        logo = self.art.logo(item)
        if name == "Intro":
            logo.thumbnail((round(width * 0.55), round(height * 0.4)))
            card, x, y = logo, "(W-w)/2", "(H-h)/2"
        else:
            logo.thumbnail((round(width * 0.25), round(height * 0.16)))
            words = {"Recap": "Previously on", "Preview": "Next time on"}[name]
            line = self.art.letters(words.upper(), self.art.font("condensed", height * 0.032, "Regular"), 0.3)
            card = Image.new("RGBA", (max(line.width, logo.width), line.height + 18 + logo.height), (0, 0, 0, 0))
            card.paste(Image.new("RGBA", line.size, (255, 255, 255, 255)), (0, 0), line)
            card.alpha_composite(logo, (0, line.height + 18))
            x, y = f"{round(width * 0.06)}", f"H-h-{round(height * 0.08)}"
        png = output.with_suffix(".png")
        card.save(png)
        fade = (f"[1:v]format=rgba,fade=in:st=1:d=1.5:alpha=1,fade=out:st={seconds - 3}:d=1.5:alpha=1[t];"
                f"[0:v][t]overlay={x}:{y}:shortest=1,{pre}")
        run_ffmpeg("-ss", f"{start:.3f}", "-t", seconds, "-i", reel, "-loop", 1, "-t", seconds, "-i", png,
                   "-filter_complex", fade, *codec, "-an", output)
        png.unlink()

    def credits_piece(self, item: dict, spec: dict, seconds: float, pre: str, codec: list, output: Path) -> None:
        """End credits rolling up over black."""
        width, height = spec["width"], spec["height"]
        unit = height / 1080
        crew = pick(item["title"], "crew", DIRECTORS + ACTORS, 6)
        lines = [("", None)]
        for label, names in (("Directed by", item.get("directors") or crew[:1]), ("Written by", item.get("writers") or crew[1:2])):
            lines += [(label, None)] + [(n, "name") for n in names] + [("", None)]
        lines += [("Cast", None)] + [(f"{name}   {role}", "name") for name, role in item.get("cast", [])] + [("", None)]
        for label, name in zip(["Director of Photography", "Editor", "Music", "Production Design"], crew[2:]):
            lines += [(label, None), (name, "name"), ("", None)]
        lines += [("", None), (NOTE, None)]
        small, big = self.art.font("condensed", 26 * unit, "Light"), self.art.font("condensed", 38 * unit, "Regular")
        logo = self.art.logo(item)
        logo.thumbnail((round(width * 0.4), round(height * 0.3)))
        roll = Image.new("RGBA", (round(width * 0.7), logo.height + round(60 * unit) * len(lines)), (0, 0, 0, 0))
        roll.alpha_composite(logo, ((roll.width - logo.width) // 2, 0))
        draw, y = ImageDraw.Draw(roll), logo.height + round(80 * unit)
        for text, kind in lines:
            if text:
                draw.text((roll.width / 2, y), text if kind else text.upper(), anchor="ma",
                          font=big if kind else small, fill=(255, 255, 255, 255) if kind else (170, 170, 170, 255))
            y += round(60 * unit) if kind or text else round(30 * unit)
        roll = roll.crop((0, 0, roll.width, y))
        png = output.with_suffix(".png")
        roll.save(png)
        speed = (height + roll.height) / seconds
        run_ffmpeg("-f", "lavfi", "-i", f"color=black:s={width}x{height}:r={spec['fps']}:d={seconds}",
                   "-loop", 1, "-t", seconds, "-i", png,
                   "-filter_complex", f"[0:v][1:v]overlay=(W-w)/2:H-t*{speed:.3f}:shortest=1,format=yuv420p,{pre}",
                   *codec, "-an", output)
        png.unlink()

    def encoder(self, spec: dict, gop: int, video: bool = False) -> tuple[str, list]:
        """The filter that prepares the frame, and the encoder arguments."""
        scale = f"scale={spec['width']}:{spec['height']}"
        hdr = spec.get("hdr")
        ten_bit = hdr or spec.get("bits") == 10
        pix = "yuv420p10le" if ten_bit else "yuv420p"
        # The still is SDR; mapped into PQ it looks like itself on an HDR screen.
        source = "tin=bt709:pin=bt709:min=bt709:rin=limited" if video else "tin=iec61966-2-1:pin=bt709:min=bt709:rin=full"
        pre = (f"{scale},zscale={source}"
               f":t=smpte2084:p=bt2020:m=bt2020nc:r=limited:npl=203,format={pix}") if hdr else f"{scale},format={pix}"
        colour = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"] if hdr else []
        if spec["codec"] == "h264":
            return pre, ["-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", 26,
                         "-g", gop, "-keyint_min", gop, "-sc_threshold", 0]
        if spec["codec"] == "av1":
            return pre, ["-c:v", "libsvtav1", "-preset", 10, "-crf", 40, "-g", gop, "-svtav1-params", "scd=0"]
        if spec["codec"] == "hevc":
            if self.nvenc():
                # Six times faster than x265 at 2160p, but without the mastering display SEI;
                # the server tells HDR10 by the transfer and primaries anyway.
                return pre, ["-c:v", "hevc_nvenc", "-preset", "p1", "-profile:v", "main10" if ten_bit else "main",
                             "-rc", "constqp", "-qp", 28, "-g", gop, *colour]
            params = f"keyint={gop}:min-keyint={gop}:scenecut=0:log-level=error"
            if hdr:
                params += (":hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
                           ":master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
                           ":max-cll=1000,400")
            return pre, ["-c:v", "libx265", "-preset", "ultrafast", "-crf", 26, "-x265-params", params, *colour]
        sys.exit(f"unknown video codec {spec['codec']}")

    def nvenc(self) -> bool:
        if not hasattr(self, "_nvenc"):
            probe = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "color=black:s=256x256:d=0.1", "-c:v", "hevc_nvenc", "-f", "null", "-"],
                                   capture_output=True)
            self._nvenc = probe.returncode == 0
        return self._nvenc

    def audio(self, track: dict, duration: float) -> Path:
        codec, channels = track["codec"], track.get("channels", 2)
        work_dir = self.temp / "audio"
        work_dir.mkdir(parents=True, exist_ok=True)
        base = work_dir / f"{codec}-{channels}.mka"
        if not base.exists():
            run_ffmpeg("-f", "lavfi", "-i", f"anullsrc=r=48000:cl={LAYOUT[channels]}", "-t", 60,
                       *AUDIO[codec](channels), base)
        output = work_dir / f"{codec}-{channels}-{duration:.3f}.mka"
        if not output.exists():
            run_ffmpeg("-stream_loop", -1, "-i", base, "-t", f"{duration:.3f}", "-c", "copy", output)
        return output

    def media_file(self, path: Path, title: str, spec: dict, template: dict, runtime: float, frames) -> None:
        """Muxes video, audio, subtitles and chapters into path, unless it is up to date."""
        key = frames.key if frames else "master"
        recipe = hashlib.sha1(json.dumps([title, spec, template, runtime, key, RECIPE, MUX], sort_keys=True).encode()).hexdigest()
        stamp = self.temp / "done" / (hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".txt")
        if path.exists() and stamp.exists() and stamp.read_text() == recipe:
            return
        print(f"  {path.relative_to(self.media)}")
        video, chapters = self.video(spec, runtime, frames)
        duration = chapters[-1][1]
        inputs, maps, meta = [video], ["-map", "0:v"], []
        meta += ["-metadata:s:v:0", f"title={spec['height']}p", "-disposition:v:0", "default"]
        for n, track in enumerate(template.get("audio", [])):
            inputs.append(self.audio(track, duration))
            maps += ["-map", f"{len(inputs) - 1}:a"]
            meta += self.stream_meta("a", n, track)
        subtitle_dir = self.temp / "subtitles"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        for n, track in enumerate(template.get("subtitles", [])):
            sub = subtitle_dir / f"{recipe[:12]}-{n}.{track.get('format', 'srt')}"
            write_subtitle(sub, track, duration)
            inputs.append(sub)
            maps += ["-map", f"{len(inputs) - 1}:s"]
            meta += self.stream_meta("s", n, track)
        chapter_file = subtitle_dir / f"{recipe[:12]}.ffmeta"
        chapter_file.write_text(";FFMETADATA1\n" + "".join(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={round(s * 1000)}\nEND={round(e * 1000)}\ntitle={t}\n"
            for s, e, t in chapters), encoding="utf-8")
        inputs.append(chapter_file)
        args = []
        for source in inputs:
            args += ["-i", source]
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.stem + ".part.mkv")
        run_ffmpeg(*args, *maps, "-map_chapters", len(inputs) - 1, "-map_metadata:g", -1, "-c", "copy",
                   "-metadata", f"title={title}", *meta, partial)
        partial.replace(path)
        for track in template.get("external", []):
            suffix = "".join(f".{mark}" for mark, flag in (("forced", "forced"), ("sdh", "hearing_impaired")) if track.get(flag))
            write_subtitle(path.with_name(f"{path.stem}.{track['lang']}{suffix}.srt"), track, duration)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(recipe)

    @staticmethod
    def stream_meta(kind: str, n: int, track: dict) -> list:
        flags = "+".join(value for key, value in FLAGS.items() if track.get(key)) or "0"
        meta = [f"-metadata:s:{kind}:{n}", f"language={track['lang']}", f"-disposition:{kind}:{n}", flags]
        if track.get("title"):
            meta += [f"-metadata:s:{kind}:{n}", f"title={track['title']}"]
        return meta

    # ---- titles

    def frames_for(self, item: dict, image: str, focus: float, heading: list[str], spec: dict,
                   lead: list | None = None, tail: list | None = None):
        art = self.art

        def draw(n: int, count: int, label: str) -> Image.Image:
            pan = focus if count == 1 else 0.15 + 0.7 * n / (count - 1)
            return art.frame(item, image, spec["width"], spec["height"], pan, heading + [label])

        draw.lead, draw.tail = lead or [], tail or []
        # Everything that changes the picture: a file whose key changed is built again.
        draw.key = [item["title"], image, heading, item.get("style"), draw.lead, draw.tail]
        return draw

    @staticmethod
    def episode_sections(item: dict, season: int, episode: int) -> tuple[list, list]:
        """A recap from the second episode on, an intro, credits, and for anime a preview of the next one."""
        if season == 0:
            return [], [("Credits", 1)]
        recap = [] if (season, episode) == (1, 1) else [("Recap", 1)]
        preview = [("Preview", 1)] if "Anime" in item["genres"] else []
        return recap + [("Intro", 1)], [("Credits", 1)] + preview

    def movie(self, item: dict) -> None:
        folder = self.media / "Movies" / folder_name(item)
        folder.mkdir(parents=True, exist_ok=True)
        self.title_images(folder, item)
        write_xml(folder / "movie.nfo", movie_nfo(item))
        if item.get("film"):
            self.film(folder / f"{folder_name(item)}.mkv", item)
            return
        for file in item["files"]:
            spec = self.tracks["video"][file["video"]]
            name = folder_name(item) + (f" - {file['label']}" if file.get("label") else "")
            # Opening titles and end credits, for skipping them.
            frames = None if item.get("filler") else self.frames_for(item, item["backdrop"], 0.5, [item["title"]], spec,
                                                                        [("Intro", 1)], [("Credits", 2)])
            if item.get("footage"):
                frames.footage = {"item": item, "variant": 0}
                frames.key = frames.key + [item["footage"], 0]
            self.media_file(folder / f"{name}.mkv", item["title"], spec, self.tracks["template"][file["tracks"]],
                            item["runtime"], frames)

    def film(self, path: Path, item: dict) -> None:
        """A real film (an open movie) remuxed with the audio, subtitles and chapters from the catalog."""
        film = item["film"]
        recipe = hashlib.sha1(json.dumps([item["title"], film, RECIPE, MUX], sort_keys=True).encode()).hexdigest()
        stamp = self.temp / "done" / (hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".txt")
        if path.exists() and stamp.exists() and stamp.read_text() == recipe:
            return
        print(f"  {path.relative_to(self.media)}")
        main = self.film_source(film["source"])
        inputs, maps, meta = [main], ["-map", "0:v:0"], ["-disposition:v:0", "default"]
        # Audio: the film's own tracks by index, or another source file.
        for n, track in enumerate(film.get("audio", [])):
            if "source" in track:
                inputs.append(self.film_source(track["source"]))
                maps += ["-map", f"{len(inputs) - 1}:a:0"]
            else:
                maps += ["-map", f"0:a:{track.get('index', 0)}"]
            meta += self.stream_meta("a", n, track)
        charsets = {}
        for n, track in enumerate(film.get("subtitles", [])):
            if "source" in track:
                inputs.append(self.film_source(track["source"]))
                if track.get("charset"):
                    charsets[inputs[-1]] = track["charset"]
                maps += ["-map", f"{len(inputs) - 1}:s:0"]
            else:
                maps += ["-map", f"0:s:{track['index']}"]
            meta += self.stream_meta("s", n, track)
        # Chapters named Intro or Credits become media segments to skip.
        marks = film.get("chapters", [])
        end = duration(main)
        chapter_file = self.temp / "subtitles" / f"{recipe[:12]}.ffmeta"
        chapter_file.parent.mkdir(parents=True, exist_ok=True)
        chapter_file.write_text(";FFMETADATA1\n" + "".join(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={round(start * 1000)}\n"
            f"END={round((marks[i + 1][0] if i + 1 < len(marks) else end) * 1000)}\ntitle={name}\n"
            for i, (start, name) in enumerate(marks)), encoding="utf-8")
        inputs.append(chapter_file)
        # Some subtitle files probe badly (a stray line breaks the detection); their format is known.
        args = []
        for source in inputs:
            if source.suffix == ".srt":
                args += ["-f", "srt"] + (["-sub_charenc", charsets[source]] if source in charsets else [])
            args += ["-i", source]
        partial = path.with_name(path.stem + ".part.mkv")
        run_ffmpeg(*args, *maps, "-map_chapters", len(inputs) - 1, "-map_metadata:g", -1, "-c:v", "copy", "-c:a", "copy",
                   "-c:s", "srt", "-metadata", f"title={item['title']}", *meta, partial)
        partial.replace(path)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(recipe)

    def film_source(self, key: str) -> Path:
        """A file from sources.toml [film], unpacked from its zip archive once if it is in one."""
        entry = self.sources["film"][key]
        path = fetch(entry)
        if not entry.get("member"):
            return path
        target = self.temp / "films" / f"{entry['sha256'][:12]}-{entry['member']}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as archive, archive.open(entry["member"]) as source, \
                    open(target.with_suffix(".part"), "wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
            target.with_suffix(".part").replace(target)
        return target

    def show(self, item: dict) -> None:
        folder = self.media / "Shows" / folder_name(item)
        folder.mkdir(parents=True, exist_ok=True)
        self.title_images(folder, item)
        write_xml(folder / "tvshow.nfo", show_nfo(item))
        spec = self.tracks["video"][item["video"]]
        template = self.tracks["template"][item["tracks"]]
        for season in item["season"]:
            number = season["number"]
            label = "Specials" if number == 0 else f"Season {number}"
            poster = self.art.poster(item, item["poster"], item.get("focus", 0.5), subtitle=label)
            poster.save(folder / ("season-specials-poster.jpg" if number == 0 else f"season{number:02}-poster.jpg"), quality=88)
            season_dir = folder / ("Specials" if number == 0 else f"Season {number:02}")
            for e, episode in enumerate(season["episodes"], 1):
                stem = f"{file_title(item['title'])} S{number:02}E{e:02}"
                season_dir.mkdir(parents=True, exist_ok=True)
                self.art.cover(episode["image"], 1280, 720, episode.get("focus", 0.5)).save(season_dir / f"{stem}-thumb.jpg", quality=88)
                write_xml(season_dir / f"{stem}.nfo", episode_nfo(item, number, e, episode))
                heading = [item["title"], f"S{number:02}E{e:02} · {episode['title']}"]
                frames = None if item.get("filler") else self.frames_for(
                    item, episode["image"], 0.5, heading, spec, *self.episode_sections(item, number, e))
                if item.get("footage"):
                    # Each episode starts the reel at another clip.
                    frames.footage = {"item": item, "variant": e - 1 + 3 * number}
                    frames.key = frames.key + [item["footage"], e - 1 + 3 * number]
                self.media_file(season_dir / f"{stem}.mkv", episode["title"], spec, template,
                                episode.get("runtime", item["runtime"]), frames)

    # ---- music

    # Made-up tracks are silence in these formats: file extension and encoder.
    AUDIO_FORMATS = {"flac": ("flac", ["-c:a", "flac"]), "mp3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
                     "aac": ("m4a", ["-c:a", "aac", "-b:a", "256k"]), "opus": ("opus", ["-c:a", "libopus", "-b:a", "160k"])}

    def music(self, catalog: dict) -> None:
        root = self.media / "Music"
        artists = {a["name"]: a for a in catalog.get("artist", [])}
        for artist in artists.values():
            folder = root / file_title(artist["name"])
            folder.mkdir(parents=True, exist_ok=True)
            write_xml(folder / "artist.nfo", artist_nfo(artist))
            picture = artist.get("image") or f"ai-artist-{slug(artist['name'])}"
            if picture in self.art.sources["image"]:
                self.art.cover(picture, 1000, 1000).save(folder / "folder.jpg", quality=90)
            for album in artist["album"]:
                discs = album.get("discs") or [album["tracks"]]
                tracks = [dict(self.track(t), disc=d + 1, number=n + 1, artist=artist["name"])
                          for d, disc in enumerate(discs) for n, t in enumerate(disc)]
                self.album(folder, album, artist["name"], artist["genre"], tracks, len(discs))
        for compilation in catalog.get("compilation", []):
            tracks = [dict(title=title, seconds=seconds, disc=1, number=n + 1, artist=name)
                      for n, (name, title, seconds) in enumerate(compilation["tracks"])]
            self.album(root / "Various Artists", compilation, "Various Artists", "Lounge", tracks, 1)

    @staticmethod
    def track(entry) -> dict:
        """A track as a dict: a real recording ({title, audio}) or made-up silence ([title, seconds])."""
        return dict(entry) if isinstance(entry, dict) else {"title": entry[0], "seconds": entry[1]}

    def album(self, parent: Path, album: dict, album_artist: str, genre: str, tracks: list[dict], discs: int) -> None:
        folder = parent / file_title(album["title"])
        folder.mkdir(parents=True, exist_ok=True)
        cover = f"ai-cover-{slug(album_artist)}-{slug(album['title'])}"
        if cover in self.art.sources["image"]:
            self.art.cover(cover, 1000, 1000).save(folder / "folder.jpg", quality=90)
        root = ET.Element("album")
        for tag, value in (("title", album["title"]), ("artist", album_artist), ("albumartist", album_artist),
                           ("year", album.get("year")), ("genre", genre)):
            if value is not None:
                element(root, tag, value)
        write_xml(folder / "album.nfo", root)
        lyrics = album.get("lyrics", {})
        for track in tracks:
            number = f"{track['disc']}-{track['number']:02}" if discs > 1 else f"{track['number']:02}"
            stem = f"{number} {file_title(track['title'])}"
            ext = "ogg" if track.get("audio") else self.AUDIO_FORMATS[album.get("format", "flac")][0]
            target = folder / f"{stem}.{ext}"
            tags = {"title": track["title"], "artist": track["artist"], "album_artist": album_artist,
                    "album": album["title"], "genre": genre, "track": f"{track['number']}/{sum(t['disc'] == track['disc'] for t in tracks)}",
                    "disc": f"{track['disc']}/{discs}", "date": album.get("year", ""), "comment": album.get("performer", "")}
            if not target.exists():
                meta = [arg for key, value in tags.items() if value != "" for arg in ("-metadata", f"{key}={value}")]
                if track.get("audio"):
                    source = fetch(self.sources["audio"][track["audio"]])
                    run_ffmpeg("-i", source, "-map", "0:a", "-map_metadata", -1, "-c", "copy", *meta, target)
                else:
                    _, codec = self.AUDIO_FORMATS[album.get("format", "flac")]
                    run_ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", track["seconds"], *codec, *meta, target)
            if track["title"] in lyrics:
                # A line every six seconds from the first verse on, with a pause in the middle.
                lines = lyrics[track["title"]]
                times = [12 + 6 * n + (8 if n >= len(lines) // 2 else 0) for n in range(len(lines))]
                lrc = [f"[ti:{track['title']}]", f"[ar:{track['artist']}]", f"[al:{album['title']}]"]
                lrc += [f"[{t // 60:02}:{t % 60:02}.00]{line}" for t, line in zip(times, lines)]
                (folder / f"{stem}.lrc").write_text("\n".join(lrc) + "\n", encoding="utf-8")

    def music_videos(self, catalog: dict) -> None:
        """Music videos cut from footage, with the artist and title over the first seconds."""
        folder = self.media / "Music Videos"
        folder.mkdir(parents=True, exist_ok=True)
        spec = {"width": 1920, "height": 1080, "fps": "25", "codec": "h264"}
        for video in catalog.get("music_video", []):
            stem = file_title(f"{video['artist']} - {video['title']}")
            target = folder / f"{stem}.mkv"
            root = ET.Element("musicvideo")
            for tag in ("title", "artist", "album", "year"):
                element(root, tag, video[tag])
            write_xml(folder / f"{stem}.nfo", root)
            if not target.exists():
                reel = self.reel(spec, video["footage"], self.temp / "footage")
                card = Image.new("RGBA", (900, 200), (0, 0, 0, 0))
                artist = self.art.letters(video["artist"].upper(), self.art.font("condensed", 34, "Regular"), 0.3)
                title = self.art.letters(video["title"], self.art.font("garamond", 76, "Medium"), 0.02)
                card.paste(Image.new("RGBA", artist.size, (255, 255, 255, 255)), (0, 0), artist)
                card.paste(Image.new("RGBA", title.size, (255, 255, 255, 255)), (0, artist.height + 18), title)
                png = target.with_suffix(".png")
                card.save(png)
                overlay = ("[1:v]format=rgba,fade=in:st=1.5:d=1:alpha=1,fade=out:st=7:d=1:alpha=1[t];"
                           "[0:v][t]overlay=96:H-h-96:shortest=1,format=yuv420p[v]")
                run_ffmpeg("-i", reel, "-loop", 1, "-t", 10, "-i", png, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                           "-filter_complex", overlay.replace("shortest=1", "eof_action=pass"), "-map", "[v]", "-map", "2:a",
                           "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", 21, "-c:a", "aac", "-b:a", "192k",
                           "-metadata", f"title={video['title']}", "-metadata", f"artist={video['artist']}", target)
                png.unlink()
                run_ffmpeg("-ss", 20, "-i", target, "-frames:v", 1, "-vf", "scale=1280:-2", folder / f"{stem}-thumb.jpg")

    def photo_folder(self, folder: dict, parent: Path) -> None:
        """Photos with the dates they were "taken" in their EXIF data, and home videos cut from the footage."""
        path = parent / folder["name"]
        path.mkdir(parents=True, exist_ok=True)
        start = datetime.datetime.fromisoformat(folder["date"]).replace(hour=9)
        # Four shots a day, a few hours apart, over as many days as it takes.
        moments = (start + datetime.timedelta(days=n // 4, hours=(n % 4) * 2.5, minutes=n * 7 % 60) for n in range(10_000))
        for key in folder.get("photos", []):
            taken = next(moments)
            target = path / f"IMG_{taken:%Y%m%d_%H%M%S}.jpg"
            if not target.exists():
                image = self.art.photo(key).copy()
                image.thumbnail((2400, 2400))
                exif = Image.Exif()
                exif[0x0132] = f"{taken:%Y:%m:%d %H:%M:%S}"  # DateTime
                exif.get_ifd(0x8769)[0x9003] = f"{taken:%Y:%m:%d %H:%M:%S}"  # DateTimeOriginal
                image.save(target, quality=90, exif=exif)
            os.utime(target, (taken.timestamp(), taken.timestamp()))
        for clip in folder.get("clips", []):
            taken = next(moments)
            target = path / f"VID_{taken:%Y%m%d_%H%M%S}.mp4"
            if not target.exists():
                source = fetch(self.sources["video"][clip["clip"]])
                run_ffmpeg("-ss", clip.get("start", 0), "-t", clip["length"], "-i", source, "-map", "0:v:0", "-map", "0:a:0?",
                           "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,setsar=1", "-c:v", "libx264",
                           "-preset", "veryfast", "-crf", 22, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                           "-metadata", f"title={clip['name']}", "-metadata", f"creation_time={taken:%Y-%m-%dT%H:%M:%S}Z",
                           "-movflags", "+faststart", target)
            os.utime(target, (taken.timestamp(), taken.timestamp()))
        for inner in folder.get("folders", []):
            self.photo_folder(inner, path)

    def title_images(self, folder: Path, item: dict) -> None:
        if item.get("plain_poster"):
            poster = self.art.cover(item["poster"], 1000, 1500, item.get("focus", 0.5))
        else:
            poster = self.art.poster(item, item["poster"], item.get("focus", 0.5))
        poster.save(folder / "poster.jpg", quality=88)
        self.art.cover(item["backdrop"], 1920, 1080).save(folder / "fanart.jpg", quality=88)
        self.art.logo(item).save(folder / "logo.png")
        self.art.landscape(item).save(folder / "landscape.jpg", quality=88)

    def build(self, only: str | None) -> None:
        catalog = load("catalog.toml")
        titles = {"movie": movies(catalog), "show": shows(catalog)}
        for kind, handler in (("movie", self.movie), ("show", self.show)):
            for item in titles[kind]:
                if only and only.lower() not in item["title"].lower():
                    continue
                print(f"{kind} {folder_name(item)}")
                handler(item)
        if not only or only.lower() in ("music", "music videos"):
            print("music")
            self.music(catalog)
            self.music_videos(catalog)
        for folder in catalog.get("photo_folder", []):
            if not only or only.lower() in folder["name"].lower():
                print(f"photos {folder['name']}")
                self.photo_folder(folder, self.media / "Photos")
        size = sum(f.stat().st_size for f in self.media.rglob("*") if f.is_file())
        print(f"library: {self.media} ({size / 1e6:.0f} MB)")


# ---------------------------------------------------------------- NFO

def element(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    child = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        child.text = str(text)
    return child


def common_nfo(root: ET.Element, item: dict) -> None:
    element(root, "title", item["title"])
    if item.get("original_title"):
        element(root, "originaltitle", item["original_title"])
    element(root, "year", item["year"])
    element(root, "runtime", item["runtime"])
    for tag, key in (("premiered", "premiered"), ("mpaa", "rating"), ("rating", "score"), ("tagline", "tagline")):
        if key in item:
            element(root, tag, item[key])
    element(root, "plot", " ".join(item["plot"].split()))
    for tag, key in (("genre", "genres"), ("studio", "studios"), ("country", "countries"),
                     ("director", "directors"), ("credits", "writers")):
        for value in item.get(key, []):
            element(root, tag, value)
    for order, (name, role) in enumerate(item.get("cast", [])):
        actor = element(root, "actor")
        element(actor, "name", name)
        element(actor, "role", role)
        element(actor, "type", "Actor")
        element(actor, "sortorder", order)


def movie_nfo(item: dict) -> ET.Element:
    root = ET.Element("movie")
    common_nfo(root, item)
    if item.get("collection"):
        names = item["collection"]
        element(element(root, "set"), "name", names if isinstance(names, str) else names[0])
    return root


def show_nfo(item: dict) -> ET.Element:
    root = ET.Element("tvshow")
    common_nfo(root, item)
    element(root, "status", item["status"])
    return root


def episode_nfo(show: dict, season: int, number: int, episode: dict) -> ET.Element:
    root = ET.Element("episodedetails")
    element(root, "title", episode["title"])
    element(root, "showtitle", show["title"])
    element(root, "season", season)
    element(root, "episode", number)
    element(root, "aired", episode["aired"])
    element(root, "runtime", episode.get("runtime", show["runtime"]))
    if episode.get("plot"):
        element(root, "plot", episode["plot"])
    if episode.get("before_season"):
        element(root, "airsbefore_season", episode["before_season"])
        element(root, "airsbefore_episode", 1)
    return root


def artist_nfo(artist: dict) -> ET.Element:
    root = ET.Element("artist")
    element(root, "name", artist["name"])
    element(root, "genre", artist["genre"])
    if artist.get("about"):
        element(root, "biography", artist["about"])
    return root


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root)
    path.write_bytes(b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8"))


# ---------------------------------------------------------------- credits

def credits() -> None:
    sources = load("sources.toml")
    lines = ["# Credits", "",
             "The demo library is built from these works. The titles, plots, people and",
             "studios in catalog.toml are made up, except for Blender Studio's films,",
             "Pepper&Carrot and the two composers, which appear under their own names.", "",
             "All images were changed: cropped, scaled, dimmed and lettered for posters,",
             "logos and video frames. Images marked * show a recognisable person; keep",
             "them off the website.", ""]

    def table(heading: str, entries: dict, intro: list[str] = ()) -> None:
        nonlocal lines
        lines += [f"## {heading}", "", *intro, *([""] if intro else []),
                  "| Used as | Work | Author | Licence |", "|---|---|---|---|"]
        for key, entry in entries.items():
            page = entry.get("page") or entry["url"]
            name = page.rsplit("File:", 1)[-1] if "File:" in page else entry["url"].split("?")[0].rsplit("/", 1)[-1]
            name = urllib.request.unquote(name).replace("_", " ")
            mark = " *" if entry.get("identifiable") else ""
            lines.append(f"| `{key}`{mark} | [{name}]({page}) | {entry['author']} | {entry['license']} |")
        lines.append("")

    images = sources["image"]
    table("Images", {k: v for k, v in images.items() if not k.startswith("ai-")})
    table("Films", sources.get("film", {}), ["Blender Studio's open movies and their subtitles, remuxed unchanged."])
    table("Video", sources.get("video", {}), ["Footage for the showcase titles, music videos, home videos and Live TV."])
    table("Audio", sources.get("audio", {}))
    generated_count = sum(k.startswith("ai-") for k in images)
    stills, people = generated("episodes.toml", "still"), generated("people.toml", "person")
    lines += ["## Generated images", "",
              f"{generated_count} posters, backdrops, covers, artist pictures and avatars, {len(stills)} episode",
              f"stills and {len(people)} headshots of made-up people were generated with",
              "[FLUX.1-schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell) (Apache 2.0)",
              "from the prompts in prompts.toml. They show no real person and are released",
              "under CC0, like the rest of this repository's own content.", ""]
    lines += ["## Fonts", "", "All under the SIL Open Font License 1.1, from [google/fonts](https://github.com/google/fonts).", ""]
    for font in sources["font"].values():
        lines.append(f"- {font['name']}")
    lines += ["", "## Server", "", f"[Jellyfin](https://jellyfin.org) {sources['jellyfin']['version']}, GPL-2.0.", ""]
    (ROOT / "CREDITS.md").write_text("\n".join(lines), encoding="utf-8")
    print(ROOT / "CREDITS.md")


# ---------------------------------------------------------------- server

# The wizard's administrator: Jellyfin insists on a password for it, so it stays
# off the login screen and the profiles below are the ones to use.
ADMIN = {"name": "Admin", "password": "glacier-demo"}
USERS = [
    {"name": "Nora", "password": "", "color": "#3d7bd9", "languages": ("eng", "eng")},
    {"name": "Alex", "password": "demo", "color": "#c2410c", "languages": ("deu", "deu")},
    {"name": "Kids", "password": "", "color": "#15803d", "max_rating": "FSK-6", "languages": ("deu", "deu")},
]
# Watch state of user Nora: share watched for resume points, then played and favourites.
RESUME = {"The Last Lighthouse": 0.35, "Glass Mountain": 0.7, "Belle Tout": 0.6}
PLAYED = ["Empty Nets", "Undertow", "Paper Moons"]
FAVORITES = ["Glass Mountain", "Harbor Lights", "Paper Moons 2"]


class Server:
    def __init__(self):
        self.root = work() / "server"
        self.data = work() / "data"
        self.url = f"http://localhost:{PORT}"
        self.token = None
        self.state_file = work() / "server.json"

    # ---- process

    def exe(self) -> Path:
        exe = self.root / "jellyfin" / "jellyfin.exe"
        if not exe.exists():
            if os.name != "nt":
                sys.exit("the server part only knows the portable Windows build of Jellyfin")
            archive = fetch(load("sources.toml")["jellyfin"])
            print(f"  unpack {archive.name}")
            with zipfile.ZipFile(archive) as z:
                z.extractall(self.root)
        return exe

    def running(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/System/Info/Public", timeout=3) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def start(self) -> None:
        if self.running():
            print(f"running on {self.url}")
            return
        exe = self.exe()
        network = self.data / "config" / "network.xml"
        if not network.exists():
            network.parent.mkdir(parents=True, exist_ok=True)
            network.write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n<NetworkConfiguration>\n'
                f"  <InternalHttpPort>{PORT}</InternalHttpPort>\n  <PublicHttpPort>{PORT}</PublicHttpPort>\n"
                "</NetworkConfiguration>\n", encoding="utf-8")
        log = open(work() / "jellyfin.log", "ab")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen([exe, "--datadir", self.data, "--ffmpeg", exe.parent / "ffmpeg.exe"],
                                   cwd=exe.parent, stdout=log, stderr=log, creationflags=flags)
        (work() / "jellyfin.pid").write_text(str(process.pid))
        self.start_livetv()
        for _ in range(180):
            if self.running():
                print(f"running on {self.url} (pid {process.pid})")
                return
            if process.poll() is not None:
                sys.exit(f"Jellyfin exited, see {work() / 'jellyfin.log'}")
            time.sleep(1)
        sys.exit("Jellyfin did not answer within three minutes")

    def start_livetv(self) -> None:
        """The Live TV service, in its own process; it prepares its loops first, which takes a while once."""
        pid_file = work() / "livetv.pid"
        try:
            with urllib.request.urlopen(LiveTV.url("channels.m3u"), timeout=2):
                return
        except (urllib.error.URLError, OSError):
            pass
        log = open(work() / "livetv.log", "ab")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "livetv"], cwd=ROOT,
                                   stdout=log, stderr=log, creationflags=flags)
        pid_file.write_text(str(process.pid))

    def stop(self) -> None:
        for name in ("livetv.pid", "jellyfin.pid"):
            pid_file = work() / name
            if not pid_file.exists():
                continue
            pid = pid_file.read_text().strip()
            # Only these processes: other Jellyfin servers may run on the same machine.
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"] if os.name == "nt" else ["kill", pid])
            pid_file.unlink()
        for _ in range(30):
            if not self.running():
                return
            time.sleep(1)

    # ---- API

    def request(self, method: str, path: str, body=None, raw: bytes | None = None, content_type: str = "application/json"):
        auth = 'MediaBrowser Client="Glacier Demo", Device="demo.py", DeviceId="glacier-demo-setup", Version="1"'
        if self.token:
            auth += f', Token="{self.token}"'
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Authorization": auth, "Content-Type": content_type})
        # The server answers before it has finished loading, with 503 until then.
        for _ in range(90):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    text = response.read()
                break
            except urllib.error.HTTPError as e:
                if e.code != 503:
                    sys.exit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")
                time.sleep(2)
        else:
            sys.exit(f"{method} {path}: the server is still loading after three minutes")
        return json.loads(text) if text.strip() else None

    def items(self, user: str, types: str) -> dict[str, dict]:
        result = self.request("GET", f"/Items?userId={user}&Recursive=true&IncludeItemTypes={types}"
                                     "&CollapseBoxSetItems=false&Fields=Path")
        return {i["Name"]: i for i in result["Items"]}

    # ---- setup

    def setup(self, reset: bool) -> None:
        media = work() / "media"
        if not (media / "Movies").exists():
            sys.exit("build the library first: demo.py build")
        if self.data.exists():
            if not reset:
                sys.exit(f"{self.data} exists already; demo.py server setup --reset starts over")
            self.stop()
            shutil.rmtree(self.data)
        self.start()
        self.request("POST", "/Startup/Configuration",
                     {"UICulture": "en-US", "MetadataCountryCode": "DE", "PreferredMetadataLanguage": "en"})
        self.request("GET", "/Startup/User")
        self.request("POST", "/Startup/User", {"Name": ADMIN["name"], "Password": ADMIN["password"]})
        self.request("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        self.request("POST", "/Startup/Complete")
        self.login(ADMIN)
        policy = self.request("GET", f"/Users/{self.user_id}")["Policy"]
        self.request("POST", f"/Users/{self.user_id}/Policy", dict(policy, IsHidden=True))

        config = self.request("GET", "/System/Configuration")
        config["ServerName"] = "Glacier Demo"
        # Nothing may come from the internet: the people and studios are made up,
        # and a lookup by name would find real ones.
        for options in config.get("MetadataOptions", []):
            options["DisabledMetadataFetchers"] = ["TheMovieDb", "The Open Movie Database", "MusicBrainz", "TheAudioDB", "TheTVDB"]
            options["DisabledImageFetchers"] = ["TheMovieDb", "The Open Movie Database", "MusicBrainz", "TheAudioDB",
                                                "Studio Images", "TheTVDB", "Screen Grabber"]
        # Our videos have one keyframe per segment, so trickplay from keyframes
        # alone looks the same and takes seconds instead of a minute per file.
        config.setdefault("TrickplayOptions", {}).update({"EnableKeyFrameOnlyExtraction": True, "EnableHwAcceleration": True})
        self.request("POST", "/System/Configuration", config)

        # Turns the chapters named Intro, Recap, Credits and Preview into media segments.
        self.request("POST", "/Packages/Installed/Chapter%20Segments%20Provider?assemblyGuid="
                             "698b6f3314ca49b59d79fc3c0ab941f5&version=5.0.0.0")
        self.stop()
        self.start()
        self.login(ADMIN)

        self.library("Movies", "movies", media / "Movies", ["Movie"])
        self.library("Shows", "tvshows", media / "Shows", ["Series", "Season", "Episode"])
        self.library("Photos", "homevideos", media / "Photos", ["Photo", "Video"])
        self.library("Music", "music", media / "Music", ["MusicArtist", "MusicAlbum", "Audio"])
        self.library("Music Videos", "musicvideos", media / "Music Videos", ["MusicVideo"])
        self.live_tv()
        self.segment_names()
        self.scan()

        users = {}
        for user in USERS:
            created = self.request("POST", "/Users/New", {"Name": user["name"], "Password": user["password"]})
            users[user["name"]] = created["Id"]
            # New accounts are hidden from the login screen; these are the profiles to pick.
            policy = self.request("GET", f"/Users/{created['Id']}")["Policy"]
            self.request("POST", f"/Users/{created['Id']}/Policy", dict(policy, IsHidden=False))
            if user.get("max_rating"):
                self.limit(created["Id"], user["max_rating"])
            self.languages(created["Id"], *user["languages"])
        self.avatars()
        self.people()
        self.collections()
        self.watch_state(users["Nora"])
        self.state_file.write_text(json.dumps({"url": self.url, "token": self.token, "users": users}, indent=2))
        print(f"ready: {self.url} (from the emulator http://10.0.2.2:{PORT}), user Nora without a password")

    def segment_names(self) -> None:
        """Whole chapter names only: the plugin's defaults match "ad" in "Meadow" and
        "credits" in "After the Credits", which would skip the scene after the credits."""
        plugin = next(p for p in self.request("GET", "/Plugins") if p["Name"] == "Chapter Segments Provider")
        self.request("POST", f"/Plugins/{plugin['Id']}/Configuration", {
            "CustomMappingIntro": "^(intro|opening|op)$",
            "CustomMappingRecap": "^(recap|previously on|last time on)$",
            "CustomMappingOutro": "^(outro|closing|credits|ending|ed)$",
            "CustomMappingPreview": "^(preview|next time on|sneak peek)$",
            "CustomMappingCommercial": "^(break|ad|advertisement|intermission|commercial)$",
        })

    def art(self) -> Art:
        if not hasattr(self, "_art"):
            self._art = Art(load("sources.toml"), billing=load("catalog.toml").get("posters", {}).get("billing", False))
        return self._art

    def login(self, user: dict) -> None:
        result = self.request("POST", "/Users/AuthenticateByName", {"Username": user["name"], "Pw": user["password"]})
        self.token, self.user_id = result["AccessToken"], result["User"]["Id"]

    def library(self, name: str, kind: str, path: Path, types: list[str]) -> None:
        options = {
            "Enabled": True,
            "EnableRealtimeMonitor": False,
            "EnableChapterImageExtraction": True,
            "ExtractChapterImagesDuringLibraryScan": True,
            "EnableTrickplayImageExtraction": True,
            "ExtractTrickplayImagesDuringLibraryScan": True,
            "SaveLocalMetadata": False,
            "MetadataSavers": [],
            "AutomaticallyAddToCollection": False,
            "PreferredMetadataLanguage": "en",
            "MetadataCountryCode": "DE",
            "SubtitleDownloadLanguages": [],
            "DisabledSubtitleFetchers": [],
            "TypeOptions": [{"Type": t, "MetadataFetchers": [], "MetadataFetcherOrder": [], "ImageFetchers": [],
                             "ImageFetcherOrder": [], "ImageOptions": []} for t in types],
            "PathInfos": [{"Path": str(path)}],
        }
        query = urllib.parse.urlencode({"name": name, "collectionType": kind, "refreshLibrary": "false"})
        self.request("POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})

    def live_tv(self) -> None:
        """The M3U tuner and the XMLTV guide of the Live TV service (it must be running)."""
        for _ in range(600):
            try:
                with urllib.request.urlopen(LiveTV.url("guide.xml"), timeout=2):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(2)
        self.request("POST", "/LiveTv/TunerHosts", {"Type": "m3u", "Url": LiveTV.url("channels.m3u"),
                                                    "FriendlyName": "Glacier Demo", "ImportFavoritesOnly": False,
                                                    "AllowHWTranscoding": False, "TunerCount": 0})
        self.request("POST", "/LiveTv/ListingProviders", {"Type": "xmltv", "Path": LiveTV.url("guide.xml"),
                                                          "EnableAllTuners": True})

    def scan(self) -> None:
        print("  scanning (with trickplay and chapter images)")
        self.request("POST", "/Library/Refresh")
        self.wait_idle()
        # A first scan leaves media segments and part of the trickplay to their tasks.
        tasks = self.request("GET", "/ScheduledTasks?isHidden=false")
        for task in tasks:
            if task["Key"] in ("TaskExtractMediaSegments", "RefreshTrickplayImages"):
                print(f"  {task['Name'].lower()}")
                self.request("POST", f"/ScheduledTasks/Running/{task['Id']}")
                self.wait_idle()

    def wait_idle(self) -> None:
        time.sleep(5)
        while True:
            tasks = self.request("GET", "/ScheduledTasks?isHidden=false")
            busy = [t["Name"] for t in tasks if t["State"] != "Idle"]
            if not busy:
                return
            time.sleep(5)

    def languages(self, user: str, audio: str, subtitles: str) -> None:
        """Preferred audio and subtitle language; "Smart" shows subtitles only for foreign audio.
        Without it Jellyfin picks the first external subtitle file as the default."""
        config = self.request("GET", f"/Users/{user}")["Configuration"]
        config.update(AudioLanguagePreference=audio, SubtitleLanguagePreference=subtitles, SubtitleMode="Smart")
        self.request("POST", f"/Users/{user}/Configuration", config)

    def limit(self, user: str, rating: str) -> None:
        ratings = self.request("GET", "/Localization/ParentalRatings")
        match = next((r for r in ratings if r["Name"] == rating), None)
        if not match:
            sys.exit(f"rating {rating} unknown to the server")
        policy = self.request("GET", f"/Users/{user}")["Policy"]
        score = match.get("Value")
        if score is None:
            score = match["RatingScore"]["score"]
        policy["MaxParentalRating"] = score
        policy["BlockUnratedItems"] = ["Movie", "Series"]
        self.request("POST", f"/Users/{user}/Policy", policy)

    def upload(self, path: str, image: Image.Image) -> None:
        from io import BytesIO
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=90)
        # The server wants the picture base64 encoded, with its real type.
        self.request("POST", path, raw=base64.b64encode(buffer.getvalue()), content_type="image/jpeg")

    def avatars(self) -> None:
        """Profile pictures: the generated ones from prompts.toml, else the initial on the user's colour."""
        ids = {u["Name"]: u["Id"] for u in self.request("GET", "/Users")}
        for user in USERS:
            key = f"ai-avatar-{slug(user['name'])}"
            if key in self.art().sources["image"]:
                picture = self.art().cover(key, 512, 512)
            else:
                picture = self.art().avatar(user["name"], user["color"])
            self.upload(f"/UserImage?userId={ids[user['name']]}", picture)
            print(f"  avatar {user['name']}")

    def people(self) -> None:
        """Headshots for the made-up cast and crew; real people keep no picture."""
        pictures = self.art().sources["image"]
        for person in self.request("GET", f"/Persons?userId={self.user_id}")["Items"]:
            key = person_key(person["Name"])
            if key in pictures:
                self.upload(f"/Items/{person['Id']}/Images/Primary", self.art().cover(key, 600, 900))
                print(f"  person {person['Name']}")

    def collections(self) -> None:
        """Creates the collections that do not exist yet and gives all of them their pictures."""
        catalog = load("catalog.toml")
        items = self.items(self.user_id, "Movie,Series")
        existing = {i["Name"]: i["Id"] for i in self.request(
            "GET", f"/Items?userId={self.user_id}&Recursive=true&IncludeItemTypes=BoxSet")["Items"]}
        for name, members in collections_of(catalog).items():
            if name not in existing:
                ids = ",".join(items[m["title"]]["Id"] for m in members)
                existing[name] = self.request("POST", f"/Collections?{urllib.parse.urlencode({'name': name, 'ids': ids})}")["Id"]
            overview = catalog.get("collection_overviews", {}).get(name)
            if overview:
                item = self.request("GET", f"/Users/{self.user_id}/Items/{existing[name]}")
                self.request("POST", f"/Items/{existing[name]}", dict(item, Overview=" ".join(overview.split())))
            poster, backdrop = self.art().collection(name, members)
            self.upload(f"/Items/{existing[name]}/Images/Primary", poster)
            self.upload(f"/Items/{existing[name]}/Images/Backdrop", backdrop)
            print(f"  collection {name}")

    def watch_state(self, user: str) -> None:
        items = self.items(user, "Movie,Series,Episode")
        now = time.time()
        for n, (name, share) in enumerate(RESUME.items()):
            item = items[name]
            when = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 3600 * (n + 1)))
            self.request("POST", f"/UserItems/{item['Id']}/UserData?userId={user}",
                         {"PlaybackPositionTicks": int(item["RunTimeTicks"] * share), "Played": False, "LastPlayedDate": when})
        for name in PLAYED:
            self.request("POST", f"/UserPlayedItems/{items[name]['Id']}?userId={user}")
        for name in FAVORITES:
            self.request("POST", f"/UserFavoriteItems/{items[name]['Id']}?userId={user}")


# ---------------------------------------------------------------- live tv

LIVETV_PORT = 8200


class LiveTV:
    """The channels in catalog.toml as HLS streams looping real footage, with an M3U and an XMLTV guide.

    Every channel's loop is encoded once (720p, its logo in the corner); streaming copies it, so
    eight channels cost little CPU. Served on port 8200 for the Jellyfin server next door.
    """

    def __init__(self):
        self.catalog = load("catalog.toml")
        self.channels = self.catalog.get("channel", [])
        self.root = work() / "livetv"
        self.builder = Builder()
        self.art = self.builder.art

    @staticmethod
    def url(path: str) -> str:
        return f"http://127.0.0.1:{LIVETV_PORT}/{path}"

    def prepare(self) -> None:
        for name in ("streams", "loops", "logos", "art"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for channel in self.channels:
            self.logo(channel).save(self.root / "logos" / f"{channel['id']}.png")
            self.loop(channel)
        self.write_m3u()
        self.write_guide()

    def logo(self, channel: dict) -> Image.Image:
        """The channel's name on a rounded tile of its colour."""
        size = 512
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(image).rounded_rectangle((16, 16, size - 16, size - 16), radius=90, fill=channel["colour"])
        words = channel["name"].upper().split()
        masks = [self.art.letters(w, self.art.font("impact", 150), 0.02) for w in words]
        scale = min(1.0, 400 / max(m.width for m in masks), 360 / sum(m.height + 24 for m in masks))
        masks = [m.resize((max(1, round(m.width * scale)), max(1, round(m.height * scale))), Image.LANCZOS) for m in masks]
        y = (size - sum(m.height for m in masks) - 24 * scale * (len(masks) - 1)) / 2
        for m in masks:
            image.paste(Image.new("RGBA", m.size, (255, 255, 255, 255)), (round((size - m.width) / 2), round(y)), m)
            y += m.height + 24 * scale
        return image

    def loop(self, channel: dict) -> Path:
        """The channel's picture and sound, encoded once for streaming by copy."""
        key = hashlib.sha1(json.dumps([channel, RECIPE], sort_keys=True).encode()).hexdigest()[:12]
        path = self.root / "loops" / f"{channel['id']}-{key}.mp4"
        if path.exists():
            return path
        print(f"  live tv loop {channel['name']}")
        if channel.get("radio"):
            # A quiet chord, so the player has something to play.
            run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=300",
                       "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=300",
                       "-filter_complex", "[0:a][1:a]amix=inputs=2,volume=0.15", "-c:a", "aac", "-b:a", "96k", path)
            return path
        spec = {"width": 1280, "height": 720, "fps": "25"}
        reel = self.builder.reel(spec, channel["footage"], self.root / "loops")
        logo = self.root / "logos" / f"{channel['id']}.png"
        bug = "[1:v]scale=110:-1,format=rgba,colorchannelmixer=aa=0.85[l];[0:v][l]overlay=W-w-36:32,format=yuv420p[v]"
        run_ffmpeg("-i", reel, "-i", logo, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex", bug,
                   "-map", "[v]", "-map", "2:a", "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", 23,
                   "-g", 50, "-keyint_min", 50, "-sc_threshold", 0, "-c:a", "aac", "-b:a", "96k", path)
        return path

    def write_m3u(self) -> None:
        lines = ["#EXTM3U"]
        for c in self.channels:
            radio = ' radio="true"' if c.get("radio") else ""
            lines.append(f'#EXTINF:-1 tvg-id="{c["id"]}" tvg-chno="{c["number"]}" tvg-name="{c["name"]}" '
                         f'tvg-logo="{self.url("logos/" + c["id"] + ".png")}" group-title="Glacier Demo"{radio},{c["name"]}')
            lines.append(self.url(f"streams/{c['id']}/index.m3u8"))
        (self.root / "channels.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def programmes(self, channel: dict) -> list[dict]:
        """What the channel can show: catalog titles of its genres, or its own made-up formats."""
        if channel.get("shows"):
            return [{"title": name, "minutes": 60 if n % 3 == 0 else 30, "desc": f"{name} on {channel['name']}.",
                     "category": channel.get("category")} for n, name in enumerate(channel["shows"])]
        media = self.builder.media
        result = []
        genres = set(channel.get("genres", []))
        for film in movies(self.catalog):
            if genres & set(film["genres"]) and film["runtime"] >= 60:
                folder = media / "Movies" / folder_name(film)
                result.append({"title": film["title"], "minutes": film["runtime"], "desc": " ".join(film["plot"].split()),
                               "category": "Movie", "year": film["year"], "image": folder / "landscape.jpg"})
        if not channel.get("films_only"):
            for show in shows(self.catalog):
                if not genres & set(show["genres"]):
                    continue
                folder = media / "Shows" / folder_name(show)
                for season in show["season"]:
                    if season["number"] == 0:
                        continue
                    for e, episode in enumerate(season["episodes"], 1):
                        stem = f"{file_title(show['title'])} S{season['number']:02}E{e:02}"
                        result.append({"title": show["title"], "subtitle": episode["title"], "season": season["number"],
                                       "episode": e, "minutes": show["runtime"],
                                       "desc": episode.get("plot") or " ".join(show["plot"].split()),
                                       "category": channel.get("category") or "Series",
                                       "image": folder / f"Season {season['number']:02}" / f"{stem}-thumb.jpg"})
        return result

    def write_guide(self) -> None:
        """Seven days of programme from six hours ago, the same every time for the same day."""
        now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
        start, end = now - datetime.timedelta(hours=6), now + datetime.timedelta(days=7)
        stamp = lambda t: t.strftime("%Y%m%d%H%M%S +0000")
        root = ET.Element("tv", {"generator-info-name": "glacier-demo"})
        for c in self.channels:
            channel = element(root, "channel", id=c["id"])
            element(channel, "display-name", c["name"])
            element(channel, "display-name", c["number"])
            element(channel, "icon", src=self.url(f"logos/{c['id']}.png"))
        for c in self.channels:
            pool = self.programmes(c)
            rng = random.Random(f"{c['id']}{start:%Y%m%d}")
            rng.shuffle(pool)
            t, n = start, 0
            while t < end and pool:
                show = pool[n % len(pool)]
                # Programme slots end on the quarter hour, like real ones.
                minutes = -(-show["minutes"] // 15) * 15
                stop = t + datetime.timedelta(minutes=minutes)
                programme = element(root, "programme", start=stamp(t), stop=stamp(stop), channel=c["id"])
                element(programme, "title", show["title"])
                if show.get("subtitle"):
                    element(programme, "sub-title", show["subtitle"])
                    element(programme, "episode-num", f"{show['season'] - 1}.{show['episode'] - 1}.", system="xmltv_ns")
                element(programme, "desc", show["desc"])
                if show.get("category"):
                    element(programme, "category", show["category"])
                if show.get("year"):
                    element(programme, "date", show["year"])
                image = show.get("image")
                if image and image.exists():
                    name = f"{slug(show['title'])}-{slug(show.get('subtitle', ''))}.jpg".replace("-.jpg", ".jpg")
                    target = self.root / "art" / name
                    if not target.exists():
                        shutil.copyfile(image, target)
                    element(programme, "icon", src=self.url(f"art/{name}"))
                if rng.random() < 0.15:
                    element(programme, "new")
                t, n = stop, n + 1
        write_xml(self.root / "guide.xml", root)

    def serve(self) -> None:
        import functools
        import http.server

        self.prepare()
        processes = []
        for c in self.channels:
            out = self.root / "streams" / c["id"]
            out.mkdir(parents=True, exist_ok=True)
            for old in out.iterdir():
                old.unlink()
            processes.append(subprocess.Popen(
                [ffmpeg(), "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", str(self.loop(c)),
                 "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "10",
                 "-hls_flags", "delete_segments+omit_endlist", str(out / "index.m3u8")]))
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.root))
        handler.log_message = lambda *a: None
        server = http.server.ThreadingHTTPServer(("0.0.0.0", LIVETV_PORT), handler)
        print(f"live tv on {self.url('channels.m3u')}", flush=True)
        try:
            server.serve_forever()
        finally:
            for process in processes:
                process.terminate()


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--only", help="build only titles containing this text")
    commands.add_parser("credits")
    commands.add_parser("livetv", help="serve the Live TV channels (started with the server)")
    server = commands.add_parser("server")
    server.add_argument("action", choices=["setup", "start", "stop", "status", "collections", "avatars", "people"])
    server.add_argument("--reset", action="store_true", help="setup: delete the server's data first")
    args = parser.parse_args()
    if args.command == "build":
        Builder().build(args.only)
    elif args.command == "credits":
        credits()
    elif args.command == "livetv":
        LiveTV().serve()
    elif args.action == "setup":
        Server().setup(args.reset)
    elif args.action == "start":
        Server().start()
    elif args.action in ("collections", "avatars", "people"):
        s = Server()
        s.login(ADMIN)
        if args.action == "collections":
            s.collections()
        elif args.action == "avatars":
            s.avatars()
        else:
            s.people()
    elif args.action == "stop":
        Server().stop()
    else:
        s = Server()
        print(f"{s.url}: {'running' if s.running() else 'not running'}; work directory {work()}")


if __name__ == "__main__":
    main()
