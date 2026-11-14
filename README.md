# Glacier demo server

A Jellyfin server with a made-up library, for testing
[Glacier](https://github.com/Glacier-Jellyfin/glacier-androidtv) and for the
pictures on its website. The titles, plots, people and studios are invented,
apart from Blender Studio's open films and an anime series after David
Revoy's webcomic Pepper&Carrot. The images are CC0 or CC BY (see
[CREDITS.md](CREDITS.md)), so screenshots of it may be published; leave out
the two photos marked there as showing a recognisable person.

The library is generated, not stored: this repository holds only the
catalog, the stream layouts and the list of sources with their checksums.

## What it covers

118 films, 20 series with 289 episodes and 5 collections with descriptions.
Seventeen films are worked out in detail; the other 101 are one line each in
catalog.toml, so the library scrolls and the A-Z rail has letters with ten
titles, letters with one and a letter with none.

Beside films and series there are a music library (five artists and a
compilation, 64 tracks in FLAC, AAC, MP3, Ogg Vorbis and Opus, some with
synced lyrics), four music videos, a photo library with five folders of
photos and home videos, and Live TV with eight channels and a guide.

- films with German and English audio, commentary and audio description
- subtitles that are forced, SDH (by flag and only by name), closed
  captions, sing-along and karaoke, ASS and SRT, external files next to the
  video, regional variants (es-419, pt-BR, Traditional Chinese)
- several versions of one film (2160p HDR10 HEVC and 1080p H.264), AV1,
  10-bit HEVC, 720p, 2160p50 HDR, 23.976, 24, 25 and 50 fps
- TrueHD, DTS, AC-3, E-AC-3, FLAC, Opus and AAC audio
- an anime release (Japanese original, ASS subtitles, forced signs, OP/ED
  karaoke), a nature documentary with audio description, a concert film,
  subtitles in right-to-left and CJK scripts
- drama, western, family, animation, anime, horror comedy and science fiction
  posters in different styles; people, animals, insects, landscapes
- series with seasons and a special, two collections, unrated titles,
  chapters, trickplay, resume points, watched episodes and favourites
- Big Buck Bunny, Sintel and Tears of Steel as the real films, with
  their surround audio, the community subtitles and named chapters
- showcase titles with real footage, intros, recaps and credits as media
  segments, so skip buttons appear
- three profiles as a family: Nora (administrator), Alex (password `demo`) and Kids
  (up to FSK 6)

## Use

Needs Python 3.11+ with Pillow, and ffmpeg with libx264, libx265,
libsvtav1, libopus and zscale on PATH (or `FFMPEG`). The server part uses the
portable Windows build of Jellyfin.

```sh
python demo.py build            # generate the library
python demo.py server setup     # fresh server on it: users, libraries, scan, watch state
python demo.py server start     # later runs
python demo.py server refresh   # after a new build: new titles, ratings and pictures into the running server
python demo.py server stop
```

The server answers on <http://localhost:8098>, from the Android emulator on
<http://10.0.2.2:8098>. Sign in as **Nora** without a password.

Downloads, the generated media and the server's data live in the work
directory, `%LOCALAPPDATA%\glacier-demo` unless `GLACIER_DEMO_DIR` says
otherwise. `server setup --reset` throws away the server's data and starts
over; `build` only regenerates what changed.

## How the files stay small

This applies to the made-up titles; the Blender films and the showcases
carry real footage.

Each chapter is a single still frame encoded as one 64-second GOP and
repeated with stream copy; audio is silence. A two-hour film comes to a few
megabytes of video plus its audio tracks. Two hours of 5.1 silence take
2.5 MB as FLAC, 8 MB as AAC, 30 MB as E-AC-3 and 86 MB as AC-3, but 200 MB
as TrueHD and 1.2 GB as DTS, so only the short film carries those two.

All files play: the picture just stands still, and the subtitles name their
track and the time, so a track switch is easy to see.

The one-line films go further: each video preset has one three-hour master
with neutral chapter frames, and a film is the first part of it, cut with
stream copy in under a second.

## Changing the library

- `catalog.toml`: titles, people, plots, which images and stream layouts
- `tracks.toml`: video presets and the audio and subtitle layouts
- `sources.toml`: every downloaded file with its sha256 and licence; only
  sources that may be used in advertising (CC0, public domain, CC-BY)

After adding a source, run `python demo.py credits` to update CREDITS.md.

## Generated images

Posters that need a person, a couple or a superhero come from FLUX.1-schnell
(Apache 2.0), because free photos of real people come without their consent
to advertising. `prompts.toml` holds one entry per image; `ai.py` (setup in
its docstring, it needs PyTorch with CUDA) makes four candidates each, the
chosen seed goes back into prompts.toml, and `ai.py pin` adds the image to
sources.toml. The images are attached to eight releases by kind: `ai-movies` and `ai-series` (with the
episode stills), their `-kids` twins for the titles the Kids profile can see, `ai-people` (cast
photos), `ai-profiles` (avatars), `ai-artists` and `ai-covers`. `ai.py releases --check` tells
when a title has to move, for example after a new rating, and `--list` prepares the uploads. demo.py puts
them in place of the title's photos and sets the title text itself; FLUX is
never asked for lettering, and candidates with made-up text or logos that
resemble real brands are left out.

Some titles have a complete poster instead, made with Qwen-Image (Apache 2.0,
4-bit GGUF from city96/Qwen-Image-gguf with the 8-step Qwen-Image-Lightning
LoRA, 1024x1536 in about 45 seconds on a 16 GB card). The model paints the
title into the picture in a lettering that fits the genre: entries with
`engine = "qwen"` and `titled = true` in prompts.toml, keys ending in
`-poster-titled`. demo.py uses such a poster as it is, without setting a
font title on it; only the season posters of a series get a small season
line. The matching backdrops (`-backdrop-qwen`, 16:9, no text) show the same
scene. Nearly every title has one: 121 complete posters, 10 composed ones (see above) and about 125 Qwen backdrops. Every candidate is checked by eye for misspellings, extra text, logos
and broken faces; a title without a clean candidate keeps its FLUX picture
and the font title. Each poster combines two typical story ideas of the
title's genre (`tropes` in the entry), and the plot in catalog.toml tells
that story.

Qwen-Image sometimes misspells a title or adds marks under it, however often it
is asked. For such a title there is a second way: a poster without text
(`-poster-qwen`, with `letters = "..."`) and the lettering alone on black
(`-lettering`, `shape = "letter"`). demo.py keys the lettering onto the foot
of the picture (Art.keyed: the brightest channel becomes the alpha), a
reproducible step from the two pinned images and their seeds. Only when that
fails too does a title keep its FLUX picture and the font title.

The Qwen-Image files (about 20 GB: the GGUF, the LoRA and the text encoder
quantized to nf4 once on the first run) are cached in the models directory:
`GLACIER_DEMO_MODELS` if set, else `models` in the work directory. Put it on
a fast SSD: loading reads all of it on every run, and from a SATA disk that
alone takes five minutes. A run of `candidates` stops starting new
Qwen images after about 9 minutes (`QWEN_BUDGET`, in seconds) and carries on with
the missing ones when it is run again; loading the model takes a few minutes.

Profile pictures, album covers and artist pictures come the same way.
`ai.py batch episodes` makes one still per episode of the one-line series
and `ai.py batch people` one headshot per made-up cast member; a picture
that came out wrong gets another seed under `[episodes.seeds]` or
`[people.seeds]`, and `batch ... pin` writes episodes.toml and people.toml.
Real people, like the directors of the Blender films, get no picture.

## Live TV

`server setup` adds an M3U tuner and an XMLTV guide that `demo.py livetv`
serves on port 8200; the server starts it. Radio Glacier shows up as a TV
channel, because Jellyfin's M3U tuner never marks a channel as radio.
