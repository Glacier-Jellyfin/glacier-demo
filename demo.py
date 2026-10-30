#!/usr/bin/env python3
"""Builds and runs the demo Jellyfin server for Glacier.

  demo.py build [--only TITLE]   generate the library into <work>/media
  demo.py credits                rewrite CREDITS.md from sources.toml
  demo.py server setup [--reset] set up a fresh server on the built library
  demo.py server start|stop|status

The library comes from catalog.toml, tracks.toml and sources.toml. Videos are
one still frame per chapter and the audio is silence, so a two-hour film takes
a few megabytes and a few seconds to build. Downloads are kept in
<work>/cache and checked against their sha256.

The work directory is GLACIER_DEMO_DIR, or %LOCALAPPDATA%\\glacier-demo
(~/.cache/glacier-demo elsewhere). The server runs on port 8098 (from the
Android emulator: http://10.0.2.2:8098), user "Demo" without a password.

Needs Python 3.11+, Pillow and ffmpeg with libx264, libx265, libsvtav1,
libopus and zscale (FFMPEG, or ffmpeg on PATH). The server itself is the
portable Windows build of Jellyfin.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
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

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
PORT = 8098
USER_AGENT = "GlacierDemo/1 (https://github.com/Glacier-Jellyfin/glacier-demo)"
# Bump when the generated files change for the same catalog entry.
RECIPE = 1
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
    # Jellyfin scans 8.3 short paths (JENSEB~1) as empty folders.
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
    with urllib.request.urlopen(request) as response, open(partial, "wb") as out:
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


def run_ffmpeg(*args) -> None:
    # SVT-AV1 prints its configuration on every run unless told otherwise.
    env = dict(os.environ, SVT_LOG="1")
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True, env=env)


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
        if key in pinned and entry["title"] in by_title:
            item = by_title[entry["title"]]
            if entry["shape"] == "poster":
                # Generated at 2:3 with the subject at the top and room for the title below.
                item.update(poster=key, focus=0.5, plain_poster=False, layout=item.get("layout") or "bottom")
            else:
                item["backdrop"] = key
    return titles


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
                # Thumbnails are crops along the series image, one step per episode.
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

    def __init__(self, sources: dict):
        self.sources = sources
        self.fonts: dict[str, Path] = {}
        self.photos: dict[str, Image.Image] = {}

    def font(self, style: str, size: int) -> ImageFont.FreeTypeFont:
        if style not in self.fonts:
            self.fonts[style] = fetch(self.sources["font"][style])
        font = ImageFont.truetype(str(self.fonts[style]), size)
        # Variable fonts open at their lightest weight.
        for weight in (b"Bold", b"SemiBold"):
            try:
                font.set_variation_by_name(weight)
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

    @staticmethod
    def spaced(text: str) -> str:
        return " ".join(text.upper())

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
            "size": pick(title, "size", [170, 150, 130, 110, 90])[0],
        }

    def poster(self, item: dict, key: str, focus: float, subtitle: str | None = None) -> Image.Image:
        """A poster with the title set over the image; subtitle names a season."""
        width, height, margin = 1000, 1500, 80
        layout = self.layout(item)
        place, tagline, align = layout["place"], layout["tagline"], layout["align"]
        image = self.cover(key, width, height, focus)
        # Dark behind the title, and a lighter shade on the edge where a tagline goes.
        if place == "bottom":
            image = self.shade(image, 0.42, 0.9, 0.1)
        elif place == "top":
            image = ImageOps.flip(self.shade(ImageOps.flip(image), 0.5, 0.9, 0.1))
        else:
            image = self.shade(image, 0.0, 0.0, 0.38)
        if tagline == "edge":
            image = ImageOps.flip(self.shade(ImageOps.flip(image), 0.82, 0.55))
        draw = ImageDraw.Draw(image)
        accent = item.get("accent", "white")
        x, anchor = {"center": (width / 2, "ma"), "left": (margin, "la"), "right": (width - margin, "ra")}[align]
        max_width = width - 2 * margin - (0 if align == "center" else 60)
        font, lines = self.fit(item["title"], item.get("style", "serif"), max_width, layout["size"])
        line_height = round(font.size * 1.08)
        below = []
        if subtitle:
            below.append((self.spaced(subtitle), self.font("sans", 34)))
        elif tagline == "below":
            below.append((item["tagline"].upper(), self.font("sans", 28)))
        block = line_height * len(lines) + sum(26 + f.size for _, f in below)
        # A centred title sits a little above or below the middle, never quite on it twice.
        shift = pick(item["title"], "shift", [-0.12, -0.07, -0.03, 0.03, 0.07, 0.12])[0] * height
        y = {"top": 120, "center": (height - block) / 2 + shift, "bottom": height - 120 - block}[place]
        for line in lines:
            draw.text((x, y), line, font=font, fill="white", anchor=anchor)
            y += line_height
        for text, small in below:
            draw.text((x, y + 26), text, font=small, fill=accent, anchor=anchor)
            y += 26 + small.size
        if tagline == "edge" and not subtitle:
            draw.text((x, 70), item["tagline"].upper(), font=self.font("sans", 26), fill=accent, anchor=anchor)
        return image

    def logo(self, item: dict) -> Image.Image:
        style = item.get("style", "serif")
        font, lines = self.fit(item["title"], style, 1100, 220, max_lines=2)
        line_height = round(font.size * 1.05)
        image = Image.new("RGBA", (1200, line_height * len(lines) + 120), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for n, line in enumerate(lines):
            draw.text((600, 60 + n * line_height), line, font=font, fill="white", anchor="ma")
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow.putalpha(image.getchannel("A").filter(ImageFilter.GaussianBlur(10)).point(lambda a: a * 0.6))
        result = Image.alpha_composite(shadow, image)
        return result.crop(result.getbbox())

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
        self.art = Art(self.sources)
        self.media = work() / "media"
        self.temp = work() / "build"

    # ---- one media file

    def video(self, spec: dict, runtime: float, frames) -> tuple[Path, list[tuple[float, float, str]]]:
        """The video stream: one still per chapter, each repeated as whole segments.

        frames(n, count) draws chapter n of count; without frames the video is cut
        from a shared master. Returns the file and the chapters.
        """
        if frames is None:
            return self.cut(spec, runtime)
        fps = Fraction(spec["fps"])
        per_segment = round(SEGMENT_SECONDS * fps)
        segment = per_segment / fps
        segments = max(1, round(runtime * 60 / segment))
        count = max(1, min(12, segments // 8)) if segments >= 8 else 1
        counts = [segments // count + (1 if n < segments % count else 0) for n in range(count)]
        work_dir = self.temp / "video"
        work_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha1(json.dumps([spec, runtime, frames.key, RECIPE], sort_keys=True).encode()).hexdigest()[:16]
        output = work_dir / f"{stem}.mkv"
        chapters, start = [], 0.0
        for n, length in enumerate(counts):
            end = start + float(length * segment)
            chapters.append((start, end, f"Chapter {n + 1}"))
            start = end
        if output.exists():
            return output, chapters
        listing = []
        for n, length in enumerate(counts):
            png = work_dir / f"{stem}-{n}.png"
            frames(n, count).save(png)
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
        for n in range(count):
            (work_dir / f"{stem}-{n}.mkv").unlink()
            (work_dir / f"{stem}-{n}.png").unlink()
        concat.unlink()
        return output, chapters

    def cut(self, spec: dict, runtime: float) -> tuple[Path, list[tuple[float, float, str]]]:
        """The first runtime minutes of a three-hour master with neutral frames."""
        art = self.art

        def blank(n: int, count: int) -> Image.Image:
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

    def encoder(self, spec: dict, gop: int) -> tuple[str, list]:
        """The filter that prepares the frame, and the encoder arguments."""
        scale = f"scale={spec['width']}:{spec['height']}"
        hdr = spec.get("hdr")
        ten_bit = hdr or spec.get("bits") == 10
        pix = "yuv420p10le" if ten_bit else "yuv420p"
        # The still is SDR; mapped into PQ it looks like itself on an HDR screen.
        pre = (f"{scale},zscale=tin=iec61966-2-1:pin=bt709:min=bt709:rin=full"
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
        recipe = hashlib.sha1(json.dumps([title, spec, template, runtime, key, RECIPE], sort_keys=True).encode()).hexdigest()
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
        run_ffmpeg(*args, *maps, "-map_chapters", len(inputs) - 1, "-map_metadata", -1, "-c", "copy",
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

    def frames_for(self, item: dict, image: str, focus: float, heading: list[str], spec: dict):
        art = self.art

        def draw(n: int, count: int) -> Image.Image:
            pan = focus if count == 1 else 0.15 + 0.7 * n / (count - 1)
            return art.frame(item, image, spec["width"], spec["height"], pan, heading + [f"Chapter {n + 1} of {count}"])

        draw.key = [item["title"], image, heading, item.get("style")]
        return draw

    def movie(self, item: dict) -> None:
        folder = self.media / "Movies" / folder_name(item)
        folder.mkdir(parents=True, exist_ok=True)
        self.title_images(folder, item)
        write_xml(folder / "movie.nfo", movie_nfo(item))
        for file in item["files"]:
            spec = self.tracks["video"][file["video"]]
            name = folder_name(item) + (f" - {file['label']}" if file.get("label") else "")
            frames = None if item.get("filler") else self.frames_for(item, item["backdrop"], 0.5, [item["title"]], spec)
            self.media_file(folder / f"{name}.mkv", item["title"], spec, self.tracks["template"][file["tracks"]],
                            item["runtime"], frames)

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
                frames = None if item.get("filler") else self.frames_for(item, episode["image"], 0.5, heading, spec)
                self.media_file(season_dir / f"{stem}.mkv", episode["title"], spec, template,
                                episode.get("runtime", item["runtime"]), frames)

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
    element(root, "lockdata", "true")


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
    element(root, "lockdata", "true")
    return root


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root)
    path.write_bytes(b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8"))


# ---------------------------------------------------------------- credits

def credits() -> None:
    sources = load("sources.toml")
    lines = ["# Credits", "",
             "The demo library is built from these works. The titles, plots, people and",
             "studios in catalog.toml are made up, except for Blender Studio's films and",
             "Pepper&Carrot, which appear under their own names.", "",
             "All images were changed: cropped, scaled, dimmed and lettered for posters,",
             "logos and video frames. Images marked * show a recognisable person; keep",
             "them off the website.", "",
             "## Images", "",
             "| Used as | Image | Author | Licence |", "|---|---|---|---|"]
    for key, image in sources["image"].items():
        if "File:" in image["page"]:
            name = urllib.request.unquote(image["page"].rsplit("File:", 1)[-1]).replace("_", " ")
        else:
            name = urllib.request.unquote(image["url"].rsplit("/", 1)[-1])
        page = image["page"] if "File:" in image["page"] else image["url"]
        mark = " *" if image.get("identifiable") else ""
        lines.append(f"| `{key}`{mark} | [{name}]({page}) | {image['author']} | {image['license']} |")
    lines += ["", "## Fonts", "", "All under the SIL Open Font License 1.1, from [google/fonts](https://github.com/google/fonts).", ""]
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
    {"name": "Demo", "password": "", "color": "#3d7bd9"},
    {"name": "Alex", "password": "demo", "color": "#c2410c"},
    {"name": "Kids", "password": "", "color": "#15803d", "max_rating": "FSK-6"},
]
# Watch state of user Demo: share watched for resume points, then played and favourites.
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
        for _ in range(180):
            if self.running():
                print(f"running on {self.url} (pid {process.pid})")
                return
            if process.poll() is not None:
                sys.exit(f"Jellyfin exited, see {work() / 'jellyfin.log'}")
            time.sleep(1)
        sys.exit("Jellyfin did not answer within three minutes")

    def stop(self) -> None:
        pid_file = work() / "jellyfin.pid"
        if not pid_file.exists():
            print("not started by demo.py")
            return
        pid = pid_file.read_text().strip()
        # Only this process: other Jellyfin servers may run on the same machine.
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

        self.library("Movies", "movies", media / "Movies", ["Movie"])
        self.library("Shows", "tvshows", media / "Shows", ["Series", "Season", "Episode"])
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
        for user in USERS:
            avatar = self.art().avatar(user["name"], user["color"])
            self.upload(f"/UserImage?userId={users[user['name']]}", avatar)
        self.collections()
        self.watch_state(users["Demo"])
        self.state_file.write_text(json.dumps({"url": self.url, "token": self.token, "users": users}, indent=2))
        print(f"ready: {self.url} (from the emulator http://10.0.2.2:{PORT}), user Demo without a password")

    def art(self) -> Art:
        if not hasattr(self, "_art"):
            self._art = Art(load("sources.toml"))
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

    def scan(self) -> None:
        print("  scanning (with trickplay and chapter images)")
        self.request("POST", "/Library/Refresh")
        time.sleep(5)
        while True:
            tasks = self.request("GET", "/ScheduledTasks?isHidden=false")
            busy = [t["Name"] for t in tasks if t["State"] != "Idle"]
            if not busy:
                return
            time.sleep(5)

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

    def collections(self) -> None:
        catalog = load("catalog.toml")
        items = self.items(self.user_id, "Movie,Series")
        groups: dict[str, list[dict]] = {}
        titles = movies(catalog) + shows(catalog)
        for item in titles:
            names = item.get("collection", [])
            for name in [names] if isinstance(names, str) else names:
                groups.setdefault(name, []).append(item)
        by_title = {item["title"]: item for item in titles}
        for listed in catalog.get("collection", []):
            groups.setdefault(listed["name"], []).extend(by_title[t] for t in listed["titles"])
        for name, members in groups.items():
            members.sort(key=lambda m: m["year"])
            ids = ",".join(items[m["title"]]["Id"] for m in members)
            query = urllib.parse.urlencode({"name": name, "ids": ids})
            created = self.request("POST", f"/Collections?{query}")
            # A poster that already carries its own lettering would get a second title.
            source = next((m for m in reversed(members) if not m.get("plain_poster")), members[0])
            poster = self.art().poster(dict(source, title=name), source["poster"], source.get("focus", 0.5))
            self.upload(f"/Items/{created['Id']}/Images/Primary", poster)
            self.upload(f"/Items/{created['Id']}/Images/Backdrop", self.art().cover(members[0]["backdrop"], 1920, 1080))

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


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--only", help="build only titles containing this text")
    commands.add_parser("credits")
    server = commands.add_parser("server")
    server.add_argument("action", choices=["setup", "start", "stop", "status"])
    server.add_argument("--reset", action="store_true", help="setup: delete the server's data first")
    args = parser.parse_args()
    if args.command == "build":
        Builder().build(args.only)
    elif args.command == "credits":
        credits()
    elif args.action == "setup":
        Server().setup(args.reset)
    elif args.action == "start":
        Server().start()
    elif args.action == "stop":
        Server().stop()
    else:
        s = Server()
        print(f"{s.url}: {'running' if s.running() else 'not running'}; work directory {work()}")


if __name__ == "__main__":
    main()
