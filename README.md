<!--
  Developed by Mohammad Rameez Imdad (Rameez Scripts)
  WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
  YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)
-->

# AI Shorts Editor

Local web app that turns a raw vertical short into a finished one: word-timed karaoke captions, a 3-second hook, Impact-style headlines, neon badges, animated alpha stickers, sound effects, voice emphasis, **AI-found B-roll cutaways (images and video), camera zooms and flash cuts** – all driven by a two-pass LLM edit plan you can tweak and re-render for free.

## The editor's toolbox (what the AI decides, what ffmpeg renders)

| Layer | Effect | How it's rendered |
|---|---|---|
| Captions | 2-4 word chunks, **karaoke word highlight** in the accent colour, pop-in, emphasis chunks bigger + accent | ASS per-word lines via libass |
| Hook / Headline / Sub | top-zone text, headline pops from 60 % → 100 % | ASS `\t` scale + fade |
| Neon badge | opaque box + blurred halo glow, red/green/blue | two ASS layers |
| Stickers | 2 s looping alpha WebM badges, tilt-bounce | VP9 alpha overlay |
| **B-roll full** | full-screen cutaway, Ken Burns zoom/pan, fade in/out, **white flash on the cut** | image/video input → `zoompan` → alpha `fade` → `overlay` |
| **B-roll pip** | 820×500 card with a white border that slides down above the face | `pad` + animated `overlay` y |
| **Zooms** | `punch` snap on a beat · `in` slow push-in then release · `out` start tight and pull back | one `zoompan` expression on the speaker |
| Voice | louder / brighter / deeper / softer delivery per moment | gated `volume` ramp + `treble`/`bass` |
| SFX | 13 kinds (whoosh, pop, ding, boom, riser, click, cash, notification, drum, bass drop, glitch, typing, reverse swoosh) on beats | `adelay` + `amix` |
| **Code window** | editor-style card (traffic lights, title), code typed out with a cursor, Dracula syntax colours, typing sound | ~1 ASS line per character, JetBrains Mono |
| **Cuts** | manual In→Out trims from the timeline, optional auto jump-cuts on silences over 1 s | `select` / `aselect` applied last, so every effect stays in sync |
| **Voice cleanup (ENC)** | high-pass → RNNoise neural denoise → FFT residual → de-esser → voice compressor (measured: noise floor −18 dB, speech level unchanged) | `arnndn` with `assets/audio/rnnoise.rnnn`, falls back to `afftdn` |

Every layer is on its own toggle in the upload form and editable after the first render - row by row in the inspector, or on the **timeline** (drag to move, drag edges to trim, double-click a track to add, `I`/`O` mark In/Out, **Cut In→Out**, `Del` removes, `Space` plays). **Ask AI to refine** sends the current plan plus your instruction ("make captions 5-12 s punchier", "swap the first b-roll for a money scene") to the model, optionally limited to the In/Out range, and re-renders; media that didn't change is reused.

**Sound effects and stickers are placeholders you can replace.** 13 synthesized SFX (`whoosh swoosh_out pop ding boom riser click cash notification drum bass_drop glitch typing`) and 25 libass-rendered sticker badges with 5 animation styles ship out of the box. Drop real files with the same names into `assets/sfx/` and `assets/stickers/`, or import a pack with `python scripts/setup_assets.py --sfx-pack path/to/folder` (normalised to 48 kHz / −16 LUFS). Free packs: Pixabay Sound Effects, Mixkit, Freesound (check licences).

### B-roll sources

| Source | Needs | Gives |
|---|---|---|
| **Pexels** (recommended) | free key from <https://www.pexels.com/api/> → `PEXELS_API_KEY` | real portrait stock **video** + photos |
| Pixabay | free key from <https://pixabay.com/api/docs/> → `PIXABAY_API_KEY` | stock video + photos |
| OpenAI images | your `OPENAI_API_KEY` (default when no stock key) | on-topic generated stills (`gpt-image-1-mini`, ≈ $0.006 each), animated with Ken Burns motion in place of video |
| Sora 2 | `BROLL_VIDEO_SOURCE=generate` | generated 4 s clips – ≈ $0.40 each and slow, opt-in only |
| Wikimedia Commons | nothing | keyless fallback, hit-or-miss quality, licence in the credit |

The picked file, its credit and source link are stored in the plan and shown in the **B-roll & Zooms** tab. Edit the query, change image↔video, or press shuffle to get the next result on the following re-render.

```
upload .mp4/.mov/.webm
  └─ ffmpeg   extract 16 kHz mono wav
  └─ whisper  word timestamps (faster-whisper, VAD, auto language)
  └─ LLM      EditPlan via OpenAI structured outputs (one call, ~1-3k tokens)
  └─ validate clamp / snap to real word times / one top-zone element at a time
  └─ ASS      1080x1920 subtitle file: Cap · Emph · Hook · Head · Sub · Neon
  └─ ffmpeg   ONE pass: scale+crop 9:16 → sticker overlays → subtitles → audio + SFX mix
edit the plan in the browser → PUT → ASS + render only (no whisper, no LLM)
```

## Requirements

- Python **3.11+** (tested on 3.12)
- **ffmpeg + ffprobe on PATH**, built with `libass` (subtitles filter), `libx264`, `libvpx` (VP9 alpha stickers) and `aac`. Every mainstream build qualifies: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) / winget on Windows, `brew install ffmpeg` on macOS, the [johnvansickle static build](https://johnvansickle.com/ffmpeg/) or `apt install ffmpeg` on Linux. If ffmpeg lives elsewhere set `FFMPEG_BIN` / `FFPROBE_BIN` in `.env`.
- An OpenAI API key.

## Quick start (one click)

- **Windows:** double-click `run.bat` – creates `.venv`, installs requirements, fetches assets, starts the server and opens the browser. Close the window to stop.
- **WSL / Linux / macOS:** `./run.sh` – same thing in a terminal, Ctrl+C stops it.

Both need Python 3.11+ and ffmpeg installed first, and your key in `.env`.

## Setup (manual)

```bash
cd "AI SHORTS MAKER"
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then put your OPENAI_API_KEY in .env
python scripts/setup_assets.py   # fonts + placeholder SFX + placeholder stickers (one time)
python scripts/smoke_test.py     # optional: proves ffmpeg/libass/render work, no API key needed
```

`setup_assets.py` downloads **Montserrat ExtraBold** and **Anton** (the Impact look-alike) from GitHub and downloads JetBrains Mono (code windows) and the RNNoise model (voice cleanup), and synthesizes 13 SFX and 25 two-second looping alpha WebM stickers with ffmpeg. Already-present files are kept, so drop your own files in and re-run any time (`--force` regenerates, `--skip-fonts` works offline).

## Run

```bash
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Drop a vertical clip, pick options, **Generate**. The first run downloads the whisper model (`large-v3` ≈ 3 GB; set `WHISPER_MODEL=small` or `medium` on a laptop CPU) – the progress card shows downloaded / total MB, speed and time left while it happens. Progress, step name and the live ffmpeg log stream in the page. When it finishes you get a 9:16 player, a download button and the editor: inspector tabs (Captions · Text & Stickers · B-roll & Zooms · Code & Cuts · SFX & Voice), the timeline and the refine box. Change anything, hit **Re-render**: only the ASS file and the ffmpeg pass run again, no tokens spent.

### Downloading the whisper model yourself

The weights are ordinary files on Hugging Face, so a browser download works fine (handy on a slow or flaky connection – the browser can resume, the in-app download can't):

1. Open the model page → **Files and versions**: <https://huggingface.co/Systran/faster-whisper-large-v3/tree/main> (or `faster-whisper-small`, `-medium`, `-base`, `-tiny`).
2. Download every file except `README.md` / `.gitattributes`: `model.bin` (3.09 GB for large-v3, 484 MB for small), `config.json`, `tokenizer.json`, `vocabulary.json` (or `vocabulary.txt`) and, for large-v3, `preprocessor_config.json`.
3. Put them in **`models/large-v3/`** inside the project (folder name = the value of `WHISPER_MODEL`). `models/faster-whisper-large-v3/` or an absolute path in `WHISPER_MODEL` also work.

The app checks that folder first and skips the download entirely when `model.bin` is there.

## Environment (`.env`)

| Var | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | – | required for uploads |
| `LLM_MODEL` | `gpt-5.5` | any model with structured outputs. `gpt-5.x` think before answering (richer plans, ~80 s); `gpt-4o-2024-08-06` answers in ~10 s with simpler plans |
| `WHISPER_MODEL` | `large-v3` | `tiny` `base` `small` `medium` `large-v3-turbo` `large-v3` – **`large-v3-turbo` is the CPU sweet spot** (large-v3 quality, ~6x faster, 1.6 GB) |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have CTranslate2 GPU support |
| `WHISPER_COMPUTE` | `int8` | `float16` on GPU |
| `WHISPER_THREADS` | `4` (`6` on 12+ core CPUs) | more is usually *slower* on hyper-threaded / P+E core CPUs – measured 4 threads 4x faster than 16 |
| `WHISPER_BEAM` | `5` | `1` = greedy decoding, about 2x faster |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | full path if not on PATH |
| `X264_PRESET` / `X264_CRF` | `medium` / `18` | speed vs quality |
| `MAX_UPLOAD_MB` | `200` | |
| `MAX_CONCURRENT_JOBS` | `1` | whisper jobs running at once |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | per model | USD per 1M tokens, only for the cost estimate shown in the UI |

## API

| Method | Path | What |
|---|---|---|
| `POST` | `/api/jobs` | multipart `file` + `caption_font` (montserrat/anton/impact), `accent_color` (#RRGGBB), `enable_stickers`, `enable_sfx`, `enable_voice` → job |
| `GET` | `/api/jobs/{id}` | `status` queued/running/done/error, `progress` 0-100, `step`, `log[]`, `error`, `usage` |
| `GET` | `/api/jobs/{id}/plan` | current plan.json (`?original=1` = untouched AI plan) |
| `PUT` | `/api/jobs/{id}/plan` | edited `EditPlan` → validate → ASS → render only |
| `GET` | `/api/jobs/{id}/output` | final mp4 (`?download=1` for attachment) |
| `GET` | `/api/jobs/{id}/transcript` | `{language, duration, words:[{w,s,e}]}` |
| `DELETE` | `/api/jobs/{id}` | remove the job + upload + work dir + output |
| `GET` | `/api/jobs` · `/api/health` | list · ffmpeg capabilities, models, assets |

Jobs live in memory; files live under `storage/` (`uploads/`, `work/<job_id>/`, `outputs/`). Restarting the server forgets the job list, the folder is safe to wipe.

## Edit plan schema

```jsonc
{
  "hook": "STOP WASTING MONEY",            // top, 0-3s
  "captions":  [{"text":"THIS ONE TRICK","start":0.32,"end":1.10,"emphasis":true}],
  "sfx":       [{"at":1.10,"kind":"pop"}],                       // whoosh|pop|ding|boom|riser, max 1 per 4s
  "stickers":  [{"kind":"money","start":4.0,"end":6.0,"position":"top"}],  // top|top_left|top_right
  "headlines": [{"main":"NOTION","sub":"free note app","start":8.0,"end":11.0}],
  "badges":    [{"text":"WARNING","start":14.0,"end":16.0,"color":"red"}],   // red|green|blue
  "voice":     [{"start":0.32,"end":1.10,"effect":"boost"}],                 // boost|excited|deep|soft
  "broll":     [{"query":"person overwhelmed by sticky notes desk","kind":"image","mode":"full","motion":"zoom_in",
                 "start":4.0,"end":6.2,"asset":{"provider":"openai","file":"b0_ai_0.png","kind":"image", "...":"..."},"skip":0}],
  "zooms":     [{"at":0.32,"end":0.72,"scale":1.2,"kind":"punch"},{"at":8.4,"end":11.0,"scale":1.3,"kind":"in"}],
  "music_mood": "upbeat lo-fi"
}
```

**Planning is two-pass.** Pass 1 asks the model for an editorial brief (topic, tone, the strongest beats with timestamps and what to show at each, retention risks, hook options – saved as `brief.txt` in the job folder). Pass 2 turns brief + transcript into the structured plan. Set `LLM_TWO_PASS=0` to skip the brief. On gpt-5 / o-series models `LLM_REASONING` sets the thinking effort.

**Voice emphasis** works on the speaker's own audio, driven by the text: the LLM marks the moments where the delivery should hit harder (punch lines, numbers, warnings, intimate lines) and ffmpeg applies gain + EQ there. `boost` = +5 dB, `excited` = +4 dB and brighter (treble +4 dB), `deep` = +2 dB and warmer (bass +6 dB), `soft` = −5 dB. Gain changes ramp over 60 ms so nothing clicks, effects never overlap, and a limiter keeps the mix from clipping. If the LLM returns no voice plan, every caption chunk marked `emphasis` gets an automatic `boost`. Toggle "Voice emphasis" off in the upload form to skip it, or edit the segments in the SFX & Voice tab.

`validate_plan` (runs on the AI plan **and** on every PUT) clamps everything into `[0, duration]`, enforces `start < end`, snaps caption times to the nearest real word boundary, trims overlapping captions, spaces SFX ≥ 4 s apart and keeps **one** top-zone element at a time (hook → headlines → badges → stickers, earliest start wins). Dropped items are named in the job log.

## Project layout

```
app/
  main.py        FastAPI routes: jobs, plan PUT, refine POST, media files, health, static index
  pipeline.py    extract_audio · ensure_whisper_model · transcribe · plan_edit (brief + plan) · validate_plan · resolve_broll
                 · to_ass (karaoke, code windows, flashes) · render (zooms, b-roll, stickers, cleanup, sfx, cuts) · process/rerender/refine jobs
  media.py       b-roll sources: Pexels, Pixabay, Wikimedia, OpenAI images, Sora
  schemas.py     EditPlanLLM (strict structured output) / EditPlan (stored, with media) + parts, RenderOptions, Usage
  jobs.py        in-memory JobStore + asyncio.to_thread runner behind a semaphore
  config.py      paths, .env, model/tool discovery, price tables
assets/fonts | sfx | stickers | audio   (filled by scripts/setup_assets.py)
frontend/index.html                      vanilla JS, Navy theme, timeline editor - no build step
scripts/setup_assets.py · smoke_test.py
storage/uploads | work/<job_id> | outputs
```

Inside `storage/work/<job_id>/` you'll find `audio.wav`, `transcript.json`, `plan.json`, `plan_ai.json`, `subs.ass`, `filter_complex.txt` and `ffmpeg_render.log` – everything needed to debug a render by hand.

## Replacing the placeholder assets

Everything in `assets/` is looked up **by file name**, so replacing an asset is just overwriting the file.

**Stickers** (`assets/stickers/<kind>.webm`, kinds: `question_marks` `user_question` `money` `fire` `warning` `arrow_down`)
- The renderer needs a **VP9 WebM with an alpha channel** (`yuva420p`). It is decoded with `libvpx-vp9`, scaled to 420 px wide, looped for the whole on-screen window and placed in the top zone.
- **LottieFiles**: open the animation → Download → *WebM* (alpha, transparent background) → save as `<kind>.webm`.
- **Canva** (Pro): design on a transparent canvas → Share → Download → *MP4 Video* with *Transparent background* checked → convert:
  ```bash
  ffmpeg -i sticker.mov -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 0 -crf 30 -an assets/stickers/fire.webm
  ```
  (also the command for any ProRes 4444 / PNG sequence: `-i frames_%03d.png -framerate 30 …`).
- Any square-ish size works; 400–600 px keeps files small. 1–3 s loops look best.

**SFX** (`assets/sfx/<kind>.wav`, kinds: `whoosh` `pop` `ding` `boom` `riser`) – any wav/short clip, 48 kHz stereo preferred. Per-kind gain lives in `SFX_GAIN` in `pipeline.py`.

**Fonts** (`assets/fonts/*.ttf`) – the ASS styles reference the font *family names* `Montserrat ExtraBold`, `Anton` and `Impact`. Drop another TTF in and change `FONT_NAMES` / the `Head` style in `to_ass()` if you want a different face. Fonts are loaded via `fontsdir`, nothing has to be installed system-wide.

## Smoke test

```bash
python scripts/smoke_test.py          # add --keep to inspect the job folder afterwards
```

Synthesizes a 10 s test clip, swaps whisper and the LLM for fakes, runs the real validate → ASS → render path, checks the mp4 is 1080x1920 with audio, then re-renders an edited plan. Runs in a few seconds and needs no API key or model download.

## Troubleshooting

- **"no 'subtitles' filter"** – your ffmpeg lacks libass. Install a full build (see Requirements).
- **Stickers skipped** – `assets/stickers/` is empty: run `python scripts/setup_assets.py`; or the build has no `libvpx-vp9` encoder (only needed to *generate* placeholders, real WebM files still play).
- **Captions in a fallback font** – fonts failed to download (offline). Re-run `setup_assets.py` online or copy the TTFs into `assets/fonts/`.
- **Whisper is slow** – `large-v3` on CPU is roughly real-time or slower. Use `small`/`medium`, or `WHISPER_DEVICE=cuda` + `WHISPER_COMPUTE=float16` on an NVIDIA GPU.
- **Upload rejected: OPENAI_API_KEY not set** – edit `.env` and restart uvicorn.
- **Blank UI** – Tailwind loads from a CDN; the page needs internet the first time.
- **Windows + OneDrive** – keep `storage/` out of a synced folder if OneDrive locks files mid-render, or set the project up outside OneDrive.

## Cost

One LLM call per video: the compact `word|start|end` transcript in, the plan out. A 60 s short is typically 1.5–3k prompt tokens and 1–2k completion tokens, well under a cent on `gpt-4o-2024-08-06` (the exact usage and estimate show in the result panel). Re-renders cost nothing.

---

📩 **Let's Work Together**
💬 WhatsApp: <https://whatsapp.rameezscripts.com> · 📱 Telegram: <https://t.me/rameezscripts> · 📧 Email: <Contact@rameezscripts.com>

_Built by Rameez Scripts._
