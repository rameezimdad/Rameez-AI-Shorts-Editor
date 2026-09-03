"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

Pipeline: extract_audio -> transcribe -> plan_edit -> validate_plan -> to_ass -> render.
process_job() / rerender_job() drive it for the job store on a worker thread.
"""
from __future__ import annotations

import bisect
import functools
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from collections.abc import Callable

from . import config, media
from .jobs import store
from .schemas import (SFX, BRoll, Caption, CodeBlock, Cut, EditPlan, EditPlanLLM, Headline, NeonBadge, RenderOptions, Sticker,
                      Usage, VoiceFx, Zoom, empty_plan)

log = logging.getLogger("shorts.pipeline")

Logger = Callable[[str], None]
Progress = Callable[[float], None]  # 0..1 inside one stage
W, H = config.OUT_W, config.OUT_H


class Word(TypedDict):
    w: str
    s: float
    e: float


class FFmpegError(RuntimeError):
    pass


# ---------------------------------------------------------------- ffmpeg utils
def _q(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


@functools.lru_cache(maxsize=1)
def ffmpeg_caps() -> dict[str, Any]:
    """What this ffmpeg build can do - cached for the process lifetime."""
    def listing(flag: str) -> str:
        try:
            return subprocess.run([config.FFMPEG, "-hide_banner", flag], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def has(text: str, name: str) -> bool:
        return re.search(rf"\s{re.escape(name)}\s", text) is not None

    filters, enc, dec = listing("-filters"), listing("-encoders"), listing("-decoders")
    ver = listing("-version").splitlines()[:1]
    return {
        "ffmpeg": shutil.which(config.FFMPEG) is not None,
        "ffprobe": shutil.which(config.FFPROBE) is not None,
        "version": ver[0] if ver else "",
        "subtitles": has(filters, "subtitles"),
        "drawtext": has(filters, "drawtext"),
        "libx264": has(enc, "libx264"),
        "vp9_alpha_dec": has(dec, "libvpx-vp9"),
        "vp9_enc": has(enc, "libvpx-vp9"),
    }


def run_ffmpeg(
    cmd: list[str],
    logger: Logger,
    cwd: Path | None = None,
    duration: float = 0.0,
    on_progress: Progress | None = None,
    stderr_file: Path | None = None,
) -> None:
    """Run ffmpeg; stream -progress into on_progress, mirror stderr into the job log, raise on failure."""
    full = [config.FFMPEG, "-hide_banner", "-y", "-nostdin"]
    if on_progress:
        full += ["-nostats", "-progress", "pipe:1"]
    full += cmd
    logger("ffmpeg " + _q(full[1:]))
    proc = subprocess.Popen(
        full, cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if on_progress else subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    err: list[str] = []
    t = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)  # drain stderr, no deadlock
    t.start()
    if on_progress and proc.stdout:
        for line in proc.stdout:
            if line.startswith("out_time_us=") and duration > 0:
                v = line.split("=", 1)[1].strip()
                v.lstrip("-").isdigit() and on_progress(max(0.0, min(1.0, int(v) / 1e6 / duration)))
    proc.wait()
    t.join(timeout=5)
    stderr = "".join(err)
    if stderr_file:
        stderr_file.write_text(stderr, encoding="utf-8")
    lines = [ln.rstrip() for ln in stderr.splitlines() if ln.strip()]
    for ln in lines[:80]:
        logger("  " + ln)
    len(lines) > 80 and logger(f"  ... {len(lines) - 80} more lines in {stderr_file.name if stderr_file else 'stderr'}")
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited {proc.returncode}: {lines[-1] if lines else 'no stderr'}")


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: Path) -> MediaInfo:
    res = subprocess.run(
        [config.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {res.stderr.strip()[-300:]}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise FFmpegError("no video stream found")
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    dur = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    return MediaInfo(dur, int(v.get("width", 0)), int(v.get("height", 0)), fps,
                     any(s.get("codec_type") == "audio" for s in streams))


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 1. audio
def extract_audio(video: Path, out_wav: Path, logger: Logger) -> Path:
    """16 kHz mono pcm - what whisper wants."""
    run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)], logger)
    return out_wav


# ---------------------------------------------------------------- 2. whisper
_model: Any = None
_model_lock = threading.Lock()
WHISPER_FILES = {"config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json"}
Status = Callable[[str, float], None]  # (human message, 0..1)


def _fmt_mb(b: float) -> str:
    return f"{b / 1e6:,.0f} MB"


def _fmt_eta(sec: float) -> str:
    return f"{int(sec // 60)}m {int(sec % 60):02d}s" if sec >= 60 else f"{int(sec)}s"


def ensure_whisper_model(logger: Logger, on_status: Status | None = None) -> None:
    """First run only: download the weights with LIVE progress. faster-whisper/huggingface_hub only draw a tqdm bar
    in the terminal, so a watcher thread polls the HF cache folder once a second and reports
    'downloaded / total, speed, time left' to the job (step text + progress bar)."""
    name = config.WHISPER_MODEL
    if os.path.isdir(name):
        return
    from faster_whisper import download_model

    try:
        download_model(name, local_files_only=True)  # cached -> nothing to do
        return
    except Exception:  # noqa: BLE001 - LocalEntryNotFoundError & friends = not cached yet
        pass
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(name, name)
    except Exception:  # noqa: BLE001
        repo = name
    total = 0
    try:
        from huggingface_hub import HfApi
        total = sum((s.size or 0) for s in HfApi().model_info(repo, files_metadata=True).siblings if s.rfilename in WHISPER_FILES)
    except Exception as e:  # noqa: BLE001 - size is cosmetic
        logger(f"could not read model size from the hub: {e}")
    blobs: Path | None = None
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        from huggingface_hub.file_download import repo_folder_name
        blobs = Path(HF_HUB_CACHE) / repo_folder_name(repo_id=repo, repo_type="model") / "blobs"
    except Exception:  # noqa: BLE001
        pass
    logger(f"downloading whisper '{name}' ({repo}, {_fmt_mb(total) if total else 'size unknown'}) - one time only")
    t_start = time.time()
    # an interrupted earlier attempt leaves <etag>.<rand>.incomplete behind (never resumed) - don't count it
    stale = [f for f in blobs.iterdir() if f.is_file() and f.name.endswith(".incomplete") and f.stat().st_mtime < t_start - 5] if blobs and blobs.exists() else []
    stale and logger(f"note: {len(stale)} stale partial download(s) ({_fmt_mb(sum(f.stat().st_size for f in stale))}) in {blobs} - safe to delete")
    stop = threading.Event()

    def watch() -> None:
        samples: list[tuple[float, int]] = []
        logged = -1
        while not stop.wait(1.0):
            done = 0
            if blobs and blobs.exists():  # only files THIS download is writing (mtime after start)
                for f in blobs.iterdir():
                    st = f.stat()
                    f.is_file() and st.st_mtime >= t_start - 2 and (done := done + st.st_size)
            done = min(done, total) if total else done
            samples.append((time.time(), done))
            samples[:] = samples[-15:]  # ~15 s speed window
            speed = (done - samples[0][1]) / max(0.001, samples[-1][0] - samples[0][0]) if len(samples) > 1 else 0.0
            frac = done / total if total else 0.0
            left = max(0, total - done)
            eta = f" · ~{_fmt_eta(left / speed)} left" if total and left and speed > 1e5 else ""
            msg = (f"downloading whisper {name}: {_fmt_mb(done)} / {_fmt_mb(total)} ({frac:.0%}) · {speed / 1e6:.1f} MB/s{eta}"
                   if total else f"downloading whisper {name}: {_fmt_mb(done)} · {speed / 1e6:.1f} MB/s")
            on_status and on_status(msg, frac)
            if total and int(frac * 10) > logged:  # one log line per 10 %
                logged = int(frac * 10)
                logger(msg)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        download_model(name)
    finally:
        stop.set()
        t.join(timeout=3)
    logger(f"whisper {name} downloaded")


def _whisper() -> Any:
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # lazy: heavy import, not needed for smoke test
            _model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE, compute_type=config.WHISPER_COMPUTE,
                                  cpu_threads=config.WHISPER_THREADS)
    return _model


def transcribe(wav: Path, logger: Logger, on_progress: Progress | None = None) -> tuple[list[Word], str]:
    """Word-level timestamps via faster-whisper. Returns (words, language)."""
    logger(f"whisper {config.WHISPER_MODEL} on {config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE}, "
           f"{config.WHISPER_THREADS} cpu threads, beam {config.WHISPER_BEAM}")
    model = _whisper()
    segments, info = model.transcribe(str(wav), word_timestamps=True, vad_filter=True, language=None, beam_size=config.WHISPER_BEAM)
    logger(f"language {info.language} ({info.language_probability:.0%}), audio {info.duration:.1f}s")
    words: list[Word] = []
    for seg in segments:  # generator - the actual decode happens here
        for wd in seg.words or []:
            txt = wd.word.strip()
            txt and words.append({"w": txt, "s": round(float(wd.start), 3), "e": round(float(wd.end), 3)})
        on_progress and info.duration and on_progress(min(1.0, float(seg.end) / info.duration))
    logger(f"{len(words)} words transcribed")
    return words, info.language or ""


# ---------------------------------------------------------------- 3. llm plan
SYSTEM_PROMPT = """You are a world-class short-form video editor (TikTok / Reels / Shorts) with a retention-first mindset.
You get a word-level transcript as lines "word|start|end" (seconds), the video duration and an editorial BRIEF written a moment ago.
Turn them into an EditPlan. Think like an editor: every 3-5 seconds SOMETHING must change on screen (b-roll, punch zoom, sticker, badge, \
headline) or the viewer scrolls. Every effect serves the words - never decorate for its own sake.

RULES
1. CAPTIONS: group the words IN ORDER into chunks of 2-4 words. Every spoken word appears exactly once, in the original language and script. \
Chunk start = start of its first word, end = end of its last word, copied EXACTLY from the transcript - never invent, round or shift a timestamp. \
UPPERCASE for Latin script. emphasis=true on roughly 1 in 5 chunks - the punch words (numbers, pain, promises, contrasts, reveals).
2. HOOK: one scroll-stopping line (max 6 words, same language as the speech) shown for the first 3 seconds. Rewrite the opening idea, don't quote it.
3. SFX: at most ONE per 4 seconds, placed exactly on the start of a word that is a beat. whoosh / swoosh_out = transition (great on a b-roll cut), \
pop = reveal / list item, ding = positive / key point, boom = shock / big claim, riser = build-up (~1.5s before a reveal), click = UI tap / small change, \
cash = money / price, notification = message / alert, drum = punchline hit, bass_drop = the big reveal, glitch = error / mistake, typing = code / writing.
4. STICKERS: pick the badge that matches the words - question_marks / user_question (question, doubt), money, fire (hype), warning, \
arrow_down ("check below"), check (yes / correct), cross (no / wrong), wow, omg, laugh (funny), subscribe, like, new, tip, hundred (100%), free, \
clock (time / deadline), rocket (fast / growth), idea, code (programming), star (best / top), save, stop, wait. 1.5-3s each, position "top", never two in a row.
5. HEADLINES: when a product, brand, tool, place or topic is NAMED: main = the name (1-3 words, UPPERCASE), optional sub = what it is (max 5 words), \
2-4s from the moment it is spoken.
6. NEON BADGES: warnings, numbers, key takeaways - 1-3 UPPERCASE words, 1.5-3s. red = warning/danger, green = win/benefit, blue = info/fact.
7. TOP ZONE RULE: hook, headlines, badges, stickers and pip b-roll all share the space above the face. Only ONE may be visible at any moment - \
never overlap their time ranges; the hook already owns 0-3s.
8. Every timestamp lies in [0, duration], start < end. A calm moment is fine, but never more than 5s without any visual change.
9. music_mood: one short phrase.
10. VOICE: 3-8 moments where the speaker's delivery should hit harder, covering whole caption chunks, 0.5-4s, never overlapping. \
boost = louder punch lines / numbers, excited = louder + brighter hype / reveals, deep = warmer + bassier serious warnings, soft = quieter intimate lines. \
Prefer emphasis=true chunks.
11. B-ROLL (the cutaways a real editor drops in): for every concrete thing the speaker mentions - product, app, place, object, activity, feeling - \
add a cutaway. query = 2-5 ENGLISH stock-footage keywords describing a visual SCENE ("person overwhelmed by sticky notes desk", \
"notion app dashboard laptop screen", "counting stack of cash"), never a person's name, never text or logos. kind = video for actions and atmosphere, \
image for products, screens, objects, places. mode = full for a 1.5-3s full-screen cutaway (hides the face: never in the first 2s, keep >= 1.5s of \
face between cutaways, at most 30% of the runtime), pip for a 2-4s card above the face (obeys the top-zone rule). motion = zoom_in by default, \
pan_left / pan_right for wide scenes, zoom_out for reveals. 3-6 cutaways per 30-60s, scaled to the length of the video.
12. ZOOMS - decide WHERE the camera moves on the speaker (4-10 total, >= 1s apart, never during a full b-roll):
   punch = snap-zoom on a beat (start of an emphasised chunk, a number, a punchline): scale 1.12-1.25, end = at + 0.4.
   in = slow push-in over 2-5s while tension builds (a secret, a warning, "here's the thing", the seconds before a reveal): scale 1.15-1.4.
   out = start tight and pull back over 1-3s at the release (the reveal, the punchline, the call to action, a laugh): scale 1.15-1.4.
   Pair them: push IN during the build-up, cut/pull OUT on the payoff. This is the cheapest retention tool - use it wherever the voice hits.
13. CODE: when the speaker explains code, a command, a query or an API, show a code window: title = file name or "terminal", language, \
code = 3-8 REAL correct lines (max 44 characters each) that illustrate exactly what is being said, 3-6s from the moment the explanation starts. \
It lives in the top zone (same rule as headlines). Pair it with the typing sfx.
14. CUTS (only when cuts are enabled): list silences longer than 1s between two words and obvious false starts as start..end ranges to remove, \
leaving 0.15s of breathing room on each side. Never cut inside a word. Empty list when cuts are disabled.
"""

BRIEF_PROMPT = """You are the creative director for a short-form video edit. Read the transcript and think hard before answering.
Write a compact editorial brief (max 250 words, plain text, no JSON):
- TOPIC & LANGUAGE: what it is about, which language/script is spoken, who the audience is.
- TONE: the emotional arc (e.g. warning -> fix -> call to action).
- BEATS: the 6-10 strongest moments with timestamps - the claim, the number, the reveal, the warning, the CTA - and for each what the viewer should SEE \
(a b-roll scene in English, a punch zoom, a sticker, a badge or a headline).
- RETENTION RISKS: where a viewer may drop off (long explanations, nothing changing on screen) and what to put there.
- HOOK OPTIONS: 3 hook lines in the speaker's language, then pick one.
Be specific and decisive - this brief drives the edit plan."""


def compact_transcript(words: list[Word]) -> str:
    return "\n".join(f"{w['w']}|{w['s']:.2f}|{w['e']:.2f}" for w in words)


def _chat_kwargs() -> dict[str, Any]:
    """Reasoning models take an effort level, everything else a low temperature."""
    if re.match(r"^(gpt-5|o\d)", config.LLM_MODEL):
        return {"reasoning_effort": config.LLM_REASONING} if config.LLM_REASONING else {}
    return {"temperature": 0.4}


def plan_edit(words: list[Word], duration: float, opts: RenderOptions, logger: Logger,
              brief_path: Path | None = None) -> tuple[EditPlan, Usage]:
    """Two passes: (1) free-text editorial BRIEF = the model thinks about beats, visuals and retention,
    (2) structured EditPlan conditioned on that brief. Returns the raw plan (validate_plan cleans it) + usage."""
    if not words:
        logger("no speech found - empty plan, no LLM call")
        return empty_plan(), Usage(model=config.LLM_MODEL)
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set (add it to .env)")
    from openai import OpenAI  # lazy so the smoke test never needs the SDK configured

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    beta = getattr(client, "beta", None)
    parse = getattr(getattr(getattr(beta, "chat", None), "completions", None), "parse", None) or client.chat.completions.parse
    usage, kw = Usage(model=config.LLM_MODEL), _chat_kwargs()
    transcript = f"Video duration: {duration:.2f}s\nTranscript (word|start|end):\n{compact_transcript(words)}"

    def account(u: Any) -> None:
        usage.prompt_tokens += int(getattr(u, "prompt_tokens", 0) or 0)
        usage.completion_tokens += int(getattr(u, "completion_tokens", 0) or 0)
        usage.calls += 1

    brief = ""
    if config.LLM_TWO_PASS:  # pass 1 - think
        logger(f"asking {config.LLM_MODEL} for an editorial brief ({len(words)} words)")
        r1 = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": BRIEF_PROMPT}, {"role": "user", "content": transcript}], **kw)
        brief = (r1.choices[0].message.content or "").strip()
        account(r1.usage)
        brief_path and brief_path.write_text(brief, encoding="utf-8")
        logger("brief: " + brief[:240].replace("\n", " ") + (" ..." if len(brief) > 240 else ""))

    flags = "\n".join(f"{k}: {'enabled' if v else 'DISABLED - return an empty list'}" for k, v in (
        ("Stickers", opts.enable_stickers), ("SFX", opts.enable_sfx), ("Voice effects", opts.enable_voice),
        ("B-roll", opts.enable_broll), ("Camera zooms", opts.enable_zooms), ("Code windows", opts.enable_code),
        ("Cuts (remove silences)", opts.enable_jumpcuts)))
    user_msg = f"{flags}\n\nEDITORIAL BRIEF:\n{brief or '(none - decide yourself)'}\n\n{transcript}"
    logger(f"asking {config.LLM_MODEL} for the edit plan")
    completion = parse(  # pass 2 - decide
        model=config.LLM_MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
        response_format=EditPlanLLM, **kw)
    msg = completion.choices[0].message
    if getattr(msg, "refusal", None):
        raise RuntimeError(f"LLM refused: {msg.refusal}")
    llm_plan: EditPlanLLM | None = msg.parsed
    if llm_plan is None:
        raise RuntimeError("LLM returned no parsable plan")
    account(completion.usage)
    pin, pout = config.llm_prices(config.LLM_MODEL)
    usage.cost_usd = round((usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1e6, 5)
    logger(f"llm usage {usage.calls} call(s), {usage.prompt_tokens}+{usage.completion_tokens} tokens ~ ${usage.cost_usd:.4f}")
    return EditPlan.model_validate(llm_plan.model_dump()), usage


# ---------------------------------------------------------------- 3b. validate
_MIN = {"caption": 0.05, "headline": 0.8, "badge": 0.8, "sticker": 0.5, "voice": 0.4}
VOICE_MAX_SECS, VOICE_MAX_ITEMS = 6.0, 12
BROLL_MAX = 8
CODE_MAX, CODE_MIN_SECS, CODE_MAX_SECS = 4, 2.5, 8.0
JUMPCUT_MIN_GAP, CUT_MIN = 1.0, 0.25
ZOOM_SECS, ZOOM_RELEASE, ZOOM_GAP, ZOOM_MAX = 0.45, 0.3, 0.8, 12
SFX_GAP = 4.0
HOOK_SECS = 3.0


def _clean(text: str | None, limit: int = 80) -> str:
    """Collapse whitespace, strip ASS control chars ({ } \\)."""
    return re.sub(r"[{}\\]", "", re.sub(r"\s+", " ", str(text or ""))).strip()[:limit]


def _nearest(sorted_vals: list[float], t: float, tol: float) -> float:
    if not sorted_vals:
        return t
    i = bisect.bisect_left(sorted_vals, t)
    cands = [v for v in (sorted_vals[i - 1] if i else None, sorted_vals[i] if i < len(sorted_vals) else None) if v is not None]
    best = min(cands, key=lambda v: abs(v - t))
    return best if abs(best - t) <= tol else t


def validate_plan(plan: EditPlan, duration: float, opts: RenderOptions,
                  words: list[Word] | None = None, logger: Logger | None = None) -> EditPlan:
    """Clamp to [0,duration], start<end, snap captions to real word times, one top element at a time."""
    say = logger or (lambda _m: None)
    D = max(0.0, float(duration))
    clamp = lambda t: round(min(max(0.0, float(t)), D), 3)  # noqa: E731
    dropped: list[str] = []

    # captions - snap to transcript, then de-overlap by trimming the previous chunk
    starts = sorted(w["s"] for w in words) if words else []
    ends = sorted(w["e"] for w in words) if words else []
    caps: list[Caption] = []
    for c in plan.captions:
        text, s, e = _clean(c.text, 60), clamp(c.start), clamp(c.end)
        if words:
            s, e = clamp(_nearest(starts, s, 0.35)), clamp(_nearest(ends, e, 0.35))
        if not text or e - s < _MIN["caption"]:
            dropped.append(f"caption '{text}'")
            continue
        caps.append(Caption(text=text, start=s, end=e, emphasis=bool(c.emphasis)))
    caps.sort(key=lambda c: c.start)
    out_caps: list[Caption] = []
    for c in caps:
        if out_caps and c.start < out_caps[-1].end:
            out_caps[-1].end = c.start
            out_caps[-1].end - out_caps[-1].start < _MIN["caption"] and out_caps.pop()
        out_caps.append(c)

    # hook owns 0-3s of the top zone
    hook = _clean(plan.hook, 60) if D > 0.5 else ""
    taken: list[tuple[float, float]] = [(0.0, min(HOOK_SECS, D))] if hook else []
    free = lambda s, e: all(e <= a or s >= b for a, b in taken)  # noqa: E731

    def claim(items: list, kind: str, build: Callable[[Any, float, float], Any | None]) -> list:
        kept = []
        for it in sorted(items, key=lambda x: x.start):
            s, e = clamp(it.start), clamp(it.end)
            obj = build(it, s, e) if e - s >= _MIN[kind] and free(s, e) else None
            if obj is None:
                dropped.append(f"{kind} @{s:.2f}")
                continue
            taken.append((s, e))
            kept.append(obj)
        return kept

    heads = claim(plan.headlines, "headline",
                  lambda h, s, e: Headline(main=_clean(h.main, 30), sub=_clean(h.sub, 40) or None, start=s, end=e) if _clean(h.main) else None)

    # code windows - top zone, right after headlines
    code_blocks: list[CodeBlock] = []
    for cb in sorted(plan.code if opts.enable_code else [], key=lambda c: c.start):
        s, e = clamp(cb.start), min(clamp(cb.end), clamp(cb.start) + CODE_MAX_SECS)
        lines = [ln.rstrip() for ln in cb.code.strip("\n").splitlines() if ln.strip()][:10]
        if not lines or e - s < CODE_MIN_SECS or len(code_blocks) >= CODE_MAX or not free(s, e):
            dropped.append(f"code '{_clean(cb.title, 20)}' @{s:.2f}")
            continue
        taken.append((s, e))
        code_blocks.append(CodeBlock(title=_clean(cb.title, 40) or cb.language, language=cb.language, code="\n".join(lines), start=s, end=e))

    # b-roll: full = 1-4s cutaway (never in the first 1.5s, >=1.5s of face between cutaways, <=35% of runtime), pip = 1.5-5s top-zone card
    brolls: list[BRoll] = []
    cover = 0.0
    for b in sorted(plan.broll if opts.enable_broll else [], key=lambda b: b.start):
        q, s = _clean(b.query, 60), clamp(b.start)
        lo, hi = (1.0, 4.0) if b.mode == "full" else (1.5, 5.0)
        e = min(clamp(b.end), s + hi)
        gap = 1.5 if b.mode == "full" or (brolls and brolls[-1].mode == "full") else 0.5
        bad = (not q or s < 1.5 or e - s < lo or len(brolls) >= BROLL_MAX or (brolls and s < brolls[-1].end + gap)
               or (b.mode == "full" and cover + e - s > 0.35 * D) or (b.mode == "pip" and not free(s, e)))
        if bad:
            dropped.append(f"b-roll '{q[:20]}' @{s:.2f}")
            continue
        b.mode == "pip" and taken.append((s, e))
        cover += e - s if b.mode == "full" else 0.0
        brolls.append(BRoll(query=q, kind=b.kind, mode=b.mode, motion=b.motion, start=s, end=e, asset=b.asset, skip=max(0, int(b.skip))))

    badges = claim(plan.badges, "badge",
                   lambda b, s, e: NeonBadge(text=_clean(b.text, 30), start=s, end=e, color=b.color) if _clean(b.text) else None)
    stickers = claim(plan.stickers if opts.enable_stickers else [], "sticker",
                     lambda st, s, e: Sticker(kind=st.kind, start=s, end=e, position=st.position))

    # sfx - inside the video, max one per SFX_GAP seconds
    sfx: list[SFX] = []
    for x in sorted(plan.sfx if opts.enable_sfx else [], key=lambda x: x.at):
        at = clamp(x.at)
        if at >= D - 0.05 or (sfx and at - sfx[-1].at < SFX_GAP):
            dropped.append(f"sfx {x.kind} @{at:.2f}")
            continue
        sfx.append(SFX(at=at, kind=x.kind))

    # voice - clamp, cap length, no overlaps (earliest wins); empty + enabled -> auto-boost the emphasised chunks
    voice: list[VoiceFx] = []
    src = plan.voice if plan.voice else [VoiceFx(start=c.start, end=c.end, effect="boost") for c in out_caps if c.emphasis]
    plan.voice or (src and say(f"voice: no LLM voice plan - auto-boosting {len(src)} emphasised chunks"))
    for v in sorted(src if opts.enable_voice else [], key=lambda v: v.start):
        s, e = clamp(v.start), min(clamp(v.end), clamp(v.start) + VOICE_MAX_SECS)
        if e - s < _MIN["voice"] or (voice and s < voice[-1].end) or len(voice) >= VOICE_MAX_ITEMS:
            dropped.append(f"voice {v.effect} @{s:.2f}")
            continue
        voice.append(VoiceFx(start=s, end=e, effect=v.effect))

    # zooms - punch / in / out windows, >= ZOOM_GAP apart, never touching a full cutaway
    fulls = [(b.start, b.end) for b in brolls if b.mode == "full"]
    zooms: list[Zoom] = []
    last_end = -9.0
    for z in sorted(plan.zooms if opts.enable_zooms else [], key=lambda z: z.at):
        at, kind = clamp(z.at), z.kind if z.kind in ("punch", "in", "out") else "punch"
        lo, hi = (1.05, 1.35) if kind == "punch" else (1.08, 1.5)
        sc = round(min(hi, max(lo, float(z.scale))), 3)
        if kind == "punch":
            end = round(at + ZOOM_SECS, 3)
        else:
            mn, mx = (1.0, 6.0) if kind == "in" else (0.8, 4.0)
            end = round(max(at + mn, min(clamp(z.end), at + mx)), 3)
        tail = end + (ZOOM_RELEASE if kind == "in" else 0.0)
        if (at < 0.3 or tail > D - 0.2 or at - last_end < ZOOM_GAP or len(zooms) >= ZOOM_MAX
                or any(a - 0.5 <= at <= b + 0.3 or (at < a < tail) for a, b in fulls)):
            dropped.append(f"zoom {kind} @{at:.2f}")
            continue
        zooms.append(Zoom(at=at, end=end, scale=sc, kind=kind))
        last_end = tail

    # cuts - LLM silences (only when jump-cuts are on), manual trims from the editor, or auto gaps from the transcript
    src_cuts = list(plan.cuts)
    if opts.enable_jumpcuts and words and not src_cuts:
        src_cuts = [Cut(start=a["e"] + 0.15, end=b["s"] - 0.15) for a, b in zip(words, words[1:], strict=False) if b["s"] - a["e"] >= JUMPCUT_MIN_GAP]
        src_cuts and say(f"cuts: {len(src_cuts)} silence(s) over {JUMPCUT_MIN_GAP}s found in the transcript")
    cuts: list[Cut] = []
    for c in sorted(src_cuts, key=lambda c: c.start):
        s, e = clamp(c.start), clamp(c.end)
        if e - s < CUT_MIN:
            dropped.append(f"cut @{s:.2f}")
            continue
        if cuts and s <= cuts[-1].end:  # merge overlaps
            cuts[-1].end = max(cuts[-1].end, e)
            continue
        cuts.append(Cut(start=s, end=e))
    if cuts and sum(c.end - c.start for c in cuts) > 0.6 * D:
        say("cuts: would remove over 60% of the video - ignored")
        cuts = []

    dropped and say(f"validate: dropped {len(dropped)} item(s): " + ", ".join(dropped[:8]) + (" ..." if len(dropped) > 8 else ""))
    say(f"plan: {len(out_caps)} captions, {len(heads)} headlines, {len(badges)} badges, {len(stickers)} stickers, {len(sfx)} sfx, "
        f"{len(voice)} voice fx, {len(brolls)} b-roll, {len(zooms)} zooms, {len(code_blocks)} code, {len(cuts)} cuts")
    return EditPlan(hook=hook, captions=out_caps, sfx=sfx, stickers=stickers, headlines=heads, badges=badges, voice=voice,
                    zooms=zooms, code=code_blocks, cuts=cuts, broll=brolls, music_mood=_clean(plan.music_mood, 60))


def resolve_broll(plan: EditPlan, wd: Path, opts: RenderOptions, usage: Usage, logger: Logger,
                  on_status: Status | None = None) -> EditPlan:
    """Find media for every b-roll that has none yet (new plan, edited query, 'swap'); drop the ones nothing was found for."""
    media_dir = wd / "media"
    todo = {i for i, b in enumerate(plan.broll) if opts.enable_broll and (b.asset is None or not (media_dir / b.asset.file).exists())}
    if not todo:
        return plan
    used = {f"{b.asset.provider}:{b.asset.id}" for b in plan.broll if b.asset}
    kept: list[BRoll] = []
    n = len(todo)

    def cost(usd: float) -> None:
        usage.media_calls += 1
        usage.media_cost_usd = round(usage.media_cost_usd + usd, 4)

    for i, b in enumerate(plan.broll):
        if i not in todo:
            kept.append(b)
            continue
        done = len([j for j in todo if j < i])
        on_status and on_status(f"finding b-roll {done + 1}/{n}: '{b.query}'", done / n)
        asset = media.resolve(b, i, media_dir, used, logger,
                              on_status=lambda m, d=done: on_status and on_status(f"b-roll {d + 1}/{n}: {m}", d / n), on_cost=cost)
        if asset is None:
            logger(f"b-roll {i}: dropped - no media for '{b.query}'")
            continue
        kept.append(b.model_copy(update={"asset": asset}))
    return plan.model_copy(update={"broll": kept})


# ---------------------------------------------------------------- 4. ass
FONT_NAMES = {"montserrat": "Montserrat ExtraBold", "anton": "Anton", "impact": "Impact"}
NEON = {"red": "#FF2D55", "green": "#39FF14", "blue": "#2D9BFF"}
WHITE, BLACK, SHADOW = "&H00FFFFFF", "&H00000000", "&H80000000"


def _rgb(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"{h[4:6]}{h[2:4]}{h[0:2]}".upper()  # BBGGRR


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """Style-table colour: &HAABBGGRR."""
    return f"&H{alpha:02X}{_rgb(hex_rgb)}"


def ass_ovr(hex_rgb: str) -> str:
    """Override-tag colour: &HBBGGRR&."""
    return f"&H{_rgb(hex_rgb)}&"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(text: str) -> str:
    return _clean(text, 200).replace("\n", r"\N")


def _karaoke(c: Caption, words: list[Word], hi: str, base: str) -> list[tuple[float, float, str]]:
    """Word-highlight lines for one chunk: (start, end, text) with the current word in `hi`, the rest in `base`.
    Colour only (no scaling) so the line never jitters. [] when the chunk can't be mapped 1:1 onto transcript words."""
    toks = c.text.split()
    ws = [w for w in words if w["s"] >= c.start - 0.06 and w["e"] <= c.end + 0.06]
    if len(toks) < 2 or len(ws) != len(toks):
        return []
    out: list[tuple[float, float, str]] = []
    for i, w in enumerate(ws):
        s = c.start if i == 0 else max(c.start, w["s"])
        e = c.end if i == len(ws) - 1 else max(s + 0.01, ws[i + 1]["s"])
        out.append((s, e, " ".join(rf"{{\1c{hi}}}{_esc(t)}{{\1c{base}}}" if j == i else _esc(t) for j, t in enumerate(toks))))
    return out


CODE_X, CODE_Y, CODE_W, CODE_PAD, CODE_BAR, CODE_LH, CODE_FS = 70, 150, 940, 24, 56, 44, 34
CODE_COLORS = {"kw": "&HC679FF&", "str": "&H8CFAF1&", "num": "&HF993BD&", "cm": "&HA47262&", "fn": "&HFDE98B&", "txt": "&HF2F8F8&"}
CODE_KW: dict[str, set[str]] = {
    "python": set("def return if else elif for while in import from class try except finally with as lambda not and or None True False yield pass raise async await global is del".split()),
    "javascript": set("const let var function return if else for while do switch case break continue import from export default class new this async await try catch finally throw true false null undefined typeof instanceof of in".split()),
    "php": set("function return if else elseif foreach for while echo print class new public private protected static use namespace require include try catch throw true false null array isset empty".split()),
    "sql": set("select from where and or not in join left right inner on group by order having limit insert into values update set delete create table as distinct count sum avg max min null is like between".split()),
    "bash": set("if then else fi for do done while in echo export cd ls cat grep sudo apt pip npm git docker curl function return exit".split()),
    "html": set("html head body div span script style link meta title p a img ul li h1 h2 h3 button input form".split()),
    "css": set("color background margin padding display flex grid border font width height position".split()),
    "json": set("true false null".split()),
}
CODE_KW["typescript"] = CODE_KW["javascript"] | set("interface type enum implements readonly".split())
CODE_KW["generic"] = CODE_KW["javascript"] | CODE_KW["python"]
_CODE_TOKEN = re.compile(r"""(#.*$|//.*$|--.*$|/\*.*?\*/|<!--.*?-->)|("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)""")


def _code_text(line: str, lang: str) -> str:
    """One code line -> ASS text with Dracula-style colours. Braces/backslashes become their full-width twins
    (libass has no escape for them) and leading spaces become hard spaces so indentation survives."""
    kws = CODE_KW.get(lang, CODE_KW["generic"])
    out, cur = [], None
    for m in _CODE_TOKEN.finditer(line):
        tok = m.group(0)
        kind = ("cm" if m.group(1) else "str" if m.group(2) else "num" if m.group(3) else "fn" if m.group(4)
                else ("kw" if m.group(5) in kws else "txt") if m.group(5) else "txt")
        safe = tok.replace("{", "｛").replace("}", "｝").replace("\\", "＼")
        if kind != cur:
            out.append(f"{{\\1c{CODE_COLORS[kind]}}}")
            cur = kind
        out.append(safe)
    text = "".join(out)
    lead = len(line) - len(line.lstrip(" "))
    return text.replace(" " * lead, "\\h" * lead, 1) if lead else text


def _round_rect(w: int, h: int, r: int) -> str:
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _code_events(cb: CodeBlock) -> list[str]:
    """Editor-style window: dark rounded card, traffic-light title bar, code typed out char by char with a cursor."""
    lines = [ln.rstrip()[:46].replace("\t", "  ") for ln in cb.code.strip("\n").splitlines()][:10] or [""]
    n, dur = len(lines), cb.end - cb.start
    h = CODE_BAR + CODE_PAD + n * CODE_LH + CODE_PAD
    fade = r"\fad(150,120)"
    ev = [
        f"Dialogue: 0,{ass_time(cb.start)},{ass_time(cb.end)},Code,,0,0,0,,{{\\an7\\pos({CODE_X},{CODE_Y}){fade}\\1c&H2A1E1E&\\alpha&H14&\\bord2\\3c&H4A3A3A&\\shad0\\p1}}{_round_rect(CODE_W, h, 22)}{{\\p0}}",
        f"Dialogue: 1,{ass_time(cb.start)},{ass_time(cb.end)},Code,,0,0,0,,{{\\an7\\pos({CODE_X},{CODE_Y}){fade}\\1c&H3A2B2B&\\alpha&H14&\\bord0\\shad0\\p1}}m 22 0 l {CODE_W - 22} 0 b {CODE_W} 0 {CODE_W} 0 {CODE_W} 22 l {CODE_W} {CODE_BAR} l 0 {CODE_BAR} l 0 22 b 0 0 0 0 22 0{{\\p0}}",
    ]
    for i, col in enumerate(("&H565FFF&", "&H2EBDFF&", "&H3FC927&")):  # red, yellow, green dots
        x = CODE_X + 24 + i * 26
        ev.append(f"Dialogue: 2,{ass_time(cb.start)},{ass_time(cb.end)},Code,,0,0,0,,{{\\an7\\pos({x},{CODE_Y + 20}){fade}\\1c{col}\\bord0\\shad0\\p1}}{_round_rect(16, 16, 8)}{{\\p0}}")
    ev.append(f"Dialogue: 2,{ass_time(cb.start)},{ass_time(cb.end)},Code,,0,0,0,,{{\\an7\\pos({CODE_X + 108},{CODE_Y + 13}){fade}\\fs26\\1c&HB0A8A8&}}{_esc(cb.title)}")

    total = sum(len(ln) + 1 for ln in lines)
    type_secs = min(3.0, max(1.0, dur * 0.55))
    cps = max(12.0, total / type_secs)
    step = 1 if total <= 220 else 2
    t0, done = cb.start, 0
    cursor = "{\\1c" + CODE_COLORS["fn"] + "}|"
    for i, ln in enumerate(lines):
        y = CODE_Y + CODE_BAR + CODE_PAD + i * CODE_LH
        pos = f"{{\\an7\\pos({CODE_X + CODE_PAD},{y})}}"
        for j in range(step, len(ln) + step, step):
            j = min(j, len(ln))
            s, e = t0 + (done + j - step) / cps, t0 + (done + j) / cps
            e < cb.end and ev.append(f"Dialogue: 3,{ass_time(s)},{ass_time(min(e, cb.end))},Code,,0,0,0,,{pos}{_code_text(ln[:j], cb.language)}{cursor}")
        done += len(ln) + 1
        s = t0 + done / cps
        s < cb.end and ev.append(f"Dialogue: 3,{ass_time(s)},{ass_time(cb.end)},Code,,0,0,0,,{pos}{_code_text(ln, cb.language)}"
                                  + (cursor if i == n - 1 else ""))
    return ev


def to_ass(plan: EditPlan, out_path: Path, opts: RenderOptions, duration: float = 0.0, words: list[Word] | None = None) -> Path:
    """ASS file, PlayRes 1080x1920. Cap/Emph captions (karaoke word highlight when the transcript words are known),
    Hook, Head + Sub, Neon badges as halo + crisp layers, white flash frames on every full-screen cutaway."""
    font, accent = FONT_NAMES[opts.caption_font], ass_color(opts.accent_color)
    acc, white = ass_ovr(opts.accent_color), "&HFFFFFF&"

    def style(name: str, fn: str, size: int, primary: str, outline: str, bstyle: int, bord: int, shad: int, align: int, mv: int) -> str:
        return f"Style: {name},{fn},{size},{primary},{primary},{outline},{SHADOW},-1,0,0,0,100,100,0,0,{bstyle},{bord},{shad},{align},60,60,{mv},1"

    styles = "\n".join([
        style("Cap", font, 78, WHITE, BLACK, 1, 6, 2, 2, 520),          # bottom-ish, under the face
        style("Emph", font, 92, accent, BLACK, 1, 7, 2, 2, 520),        # punch words
        style("Hook", font, 80, WHITE, BLACK, 1, 6, 2, 8, 180),         # top, first 3s
        style("Head", "Anton", 120, accent, BLACK, 1, 10, 4, 8, 160),   # impact-style headline
        style("Sub", font, 72, WHITE, BLACK, 1, 6, 2, 8, 310),          # under Head
        style("Neon", font, 84, WHITE, ass_color(NEON["red"]), 3, 18, 0, 8, 200),  # opaque box, colour overridden inline
        style("Code", "JetBrains Mono", CODE_FS, "&H00F2F8F8", BLACK, 1, 0, 0, 7, 0),  # code window text, no border
    ])
    ev: list[str] = []

    def d(layer: int, s: float, e: float, st: str, text: str) -> None:
        e > s and ev.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{st},,0,0,0,,{text}")

    pop = r"{\fad(60,60)\fscx112\fscy112\t(0,80,\fscx100\fscy100)}"
    pop_in = r"{\fad(60,0)\fscx112\fscy112\t(0,80,\fscx100\fscy100)}"
    for c in plan.captions:
        st = "Emph" if c.emphasis else "Cap"
        lines = _karaoke(c, words, white if c.emphasis else acc, acc if c.emphasis else white) if words and opts.caption_style == "karaoke" else []
        if not lines:
            d(2, c.start, c.end, st, pop + _esc(c.text))
            continue
        for i, (s, e, text) in enumerate(lines):
            d(2, s, e, st, (pop_in if i == 0 else r"{\fad(0,60)}" if i == len(lines) - 1 else "") + text)
    plan.hook and d(3, 0.0, min(HOOK_SECS, duration) if duration else HOOK_SECS, "Hook", r"{\fad(100,200)}" + _esc(plan.hook))
    for h in plan.headlines:
        d(3, h.start, h.end, "Head", r"{\fad(0,150)\fscx60\fscy60\t(0,120,\fscx100\fscy100)}" + _esc(h.main))
        h.sub and d(3, h.start, h.end, "Sub", r"{\fad(120,150)}" + _esc(h.sub))
    for b in plan.badges:
        col = ass_ovr(NEON[b.color])
        d(3, b.start, b.end, "Neon", rf"{{\fad(80,80)\bord34\blur22\3c{col}\1c{col}\alpha&H60&}}" + _esc(b.text))  # blurred halo
        d(4, b.start, b.end, "Neon", rf"{{\fad(80,80)\bord18\3c{col}\1c&HFFFFFF&\fscx90\fscy90\t(0,90,\fscx100\fscy100)}}" + _esc(b.text))  # crisp box
    for cb in plan.code if opts.enable_code else []:
        ev.extend(_code_events(cb))
    flash = r"{\an7\pos(0,0)\bord0\shad0\1c&HFFFFFF&\alpha&H50&\fad(0,110)\p1}m 0 0 l 1080 0 1080 1920 0 1920{\p0}"
    for b in plan.broll if opts.enable_broll else []:  # flash frame on the cut in and the cut out of a full cutaway
        if b.mode == "full" and b.asset:
            d(1, b.start, b.start + 0.11, "Cap", flash)
            d(1, b.end, b.end + 0.11, "Cap", flash)
    for st in plan.stickers:  # stickers are ffmpeg overlays; a comment keeps the ASS self-describing
        ev.append(f"Comment: 0,{ass_time(st.start)},{ass_time(st.end)},Cap,,0,0,0,,sticker {st.kind} @{st.position}")

    out_path.write_text(
        "[Script Info]\nTitle: AI Shorts Editor\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{styles}\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(ev) + "\n",
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------- 5. render
STICKER_W = 420
STICKER_POS = {"top": "(W-w)/2:180", "top_left": "80:180", "top_right": "W-w-80:180"}
PIP_W, PIP_H, PIP_Y = 820, 500, 180  # pip card inside the top zone
SFX_GAIN = {"whoosh": 0.9, "swoosh_out": 0.9, "pop": 0.9, "ding": 0.8, "boom": 1.0, "riser": 0.7, "click": 0.8, "cash": 0.8, "notification": 0.8,
            "drum": 0.9, "bass_drop": 1.0, "glitch": 0.7, "typing": 0.5}
AFMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
VOICE_FX: dict[str, tuple[float, str | None]] = {  # gain multiplier + optional timeline-gated EQ
    "boost": (1.8, None),
    "excited": (1.6, "treble=g=4"),
    "deep": (1.25, "bass=g=6"),
    "soft": (0.55, None),
}


def voice_chain(voice: list[VoiceFx]) -> str:
    """Speaker-track filters: ONE volume expression (60 ms ramps in/out so gain steps never click) + gated EQ.
    Returns '' or ',volume=...,treble=...' ready to append after aformat."""
    if not voice:
        return ""
    ramp = lambda s, e: f"clip(min((t-{s:.3f})/0.06,({e:.3f}-t)/0.06),0,1)"  # noqa: E731 - 0 outside, 1 inside
    expr = "1" + "".join(f"{VOICE_FX[v.effect][0] - 1:+.2f}*{ramp(v.start, v.end)}" for v in voice)
    eq = [f"{VOICE_FX[v.effect][1]}:enable='between(t,{v.start:.3f},{v.end:.3f})'" for v in voice if VOICE_FX[v.effect][1]]
    return f",volume=volume='{expr}':eval=frame" + "".join("," + e for e in eq)


def zoom_expr(zooms: list[Zoom]) -> str:
    """zoompan `z` over the speaker's own time `it`: 1 + one term per move.
    punch: jump to scale, ease back over ZOOM_SECS · in: smoothstep 1->scale across at..end, release over ZOOM_RELEASE ·
    out: smoothstep scale->1 across at..end."""
    terms = []
    for z in zooms:
        a, e, k = f"{z.at:.3f}", f"{z.end:.3f}", f"{z.scale - 1:.3f}"
        if z.kind == "punch":
            terms.append(f"{k}*pow(clip(1-(it-{a})/{ZOOM_SECS},0,1),2)*gte(it,{a})")
            continue
        p = f"clip((it-{a})/{max(z.end - z.at, 0.1):.3f},0,1)"
        sm = f"({p}*{p}*(3-2*{p}))"
        terms.append(f"{k}*{sm}*between(it,{a},{e})+{k}*pow(clip(1-(it-{e})/{ZOOM_RELEASE},0,1),2)*gt(it,{e})" if z.kind == "in"
                     else f"{k}*(1-{sm})*between(it,{a},{e})")
    return "1+" + "+".join(terms) if terms else "1"


def _motion(motion: str, dur: float) -> tuple[str, str, str]:
    """Ken Burns for a b-roll clip: (zoom, x, y) zoompan expressions over the clip's own time `it`."""
    p = f"min(it/{max(dur, 0.1):.3f},1)"
    cx, cy = "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
    return {
        "zoom_in": (f"1+0.12*{p}", cx, cy),
        "zoom_out": (f"1.12-0.12*{p}", cx, cy),
        "pan_left": ("1.15", f"(iw-iw/zoom)*(1-{p})", cy),
        "pan_right": ("1.15", f"(iw-iw/zoom)*{p}", cy),
    }.get(motion, ("", "", ""))


def cleanup_chain(wd: Path) -> str:
    """ENC on the speaker track: high-pass -> RNNoise neural denoise (or FFT denoise without the model) -> FFT residual
    -> de-esser -> voice compressor. Returns ',...' ready to append after aformat."""
    model = config.AUDIO_DIR / "rnnoise.rnnn"
    denoise = (f"arnndn=m={Path(os.path.relpath(model, wd)).as_posix()}:mix=0.9" if model.exists() else "afftdn=nf=-30:nr=12:tn=1")
    return (f",highpass=f=80,{denoise},afftdn=nf=-38:nr=6:tn=1,deesser=i=0.12:m=0.5:f=0.5,"
            f"acompressor=threshold=-20dB:ratio=3:attack=6:release=150:makeup=3dB")


def render(video: Path, plan: EditPlan, ass_path: Path, out_path: Path, opts: RenderOptions, info: MediaInfo,
           logger: Logger, on_progress: Progress | None = None) -> Path:
    """ONE ffmpeg pass. Video: scale/crop 9:16 -> punch zooms (zoompan) -> b-roll overlays (full cutaway / pip card)
    -> sticker overlays -> subtitles LAST. Audio: speaker track (+ voice fx) + delayed SFX -> amix -> limiter.
    Runs with cwd = job work dir so every filtergraph path is a plain relative name (no escaping games)."""
    caps = ffmpeg_caps()
    if not caps["subtitles"]:
        raise RuntimeError("this ffmpeg build has no 'subtitles' filter (needs libass)")
    wd = ass_path.parent
    fpsv = f"{(info.fps if 1 < info.fps < 121 else 30):.3f}".rstrip("0").rstrip(".")
    inputs = ["-i", str(video)]
    base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=yuv420p"
    zooms = plan.zooms if opts.enable_zooms else []
    cuts = plan.cuts
    if cuts and not zooms:
        base += f",fps={fpsv}"  # constant frame rate so setpts=N/FRAME_RATE/TB is exact after select
    if zooms:  # fps first so VFR sources keep their timing, then one zoompan with every move in its expression
        base += f",fps={fpsv},zoompan=z='{zoom_expr(zooms)}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={fpsv}"
        logger("zooms: " + ", ".join(f"{z.kind}@{z.at:.1f}" for z in zooms))
    fc = [base + "[v0]"]
    idx, vlab = 1, "v0"

    for i, b in enumerate(plan.broll if opts.enable_broll else []):
        f = wd / "media" / b.asset.file if b.asset else None
        if not f or not f.exists():
            logger(f"b-roll {i}: no media file - skipped")
            continue
        dur, is_vid = b.end - b.start, b.asset.kind == "video"
        inputs += (["-stream_loop", "-1", "-t", f"{dur + 0.5:.3f}", "-i", str(f)] if is_vid
                   else ["-loop", "1", "-framerate", fpsv, "-t", f"{dur:.3f}", "-i", str(f)])
        w, h = (W, H) if b.mode == "full" else (PIP_W, PIP_H)
        z, x, y = _motion(b.motion, dur)
        zp = f",zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fpsv}" if z else ""
        fades = f"format=yuva420p,fade=t=in:st=0:d=0.2:alpha=1,fade=t=out:st={max(0.0, dur - 0.2):.3f}:d=0.2:alpha=1"
        fit = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fpsv},format=yuv420p{zp}"
        if b.mode == "full":
            fc.append(f"[{idx}:v]{fit},{fades},setpts=PTS-STARTPTS+{b.start:.3f}/TB[br{idx}]")
            fc.append(f"[{vlab}][br{idx}]overlay=0:0:enable='between(t,{b.start:.3f},{b.end:.3f})':eof_action=pass[v{idx}]")
        else:  # card with a white border that slides down into the top zone
            fc.append(f"[{idx}:v]{fit},pad=iw+16:ih+16:8:8:color=white,{fades},setpts=PTS-STARTPTS+{b.start:.3f}/TB[br{idx}]")
            fc.append(f"[{vlab}][br{idx}]overlay=x='(W-w)/2':y='{PIP_Y}-60*pow(clip(1-(t-{b.start:.3f})/0.3,0,1),2)'"
                      f":enable='between(t,{b.start:.3f},{b.end:.3f})':eof_action=pass[v{idx}]")
        logger(f"b-roll {i}: {b.mode} {b.asset.kind} '{b.query}' {b.start:.2f}-{b.end:.2f}s ({b.asset.provider})")
        vlab, idx = f"v{idx}", idx + 1

    for st in plan.stickers if opts.enable_stickers else []:
        f = config.STICKERS_DIR / f"{st.kind}.webm"
        if not f.exists():
            logger(f"sticker {st.kind}.webm missing - skipped (run scripts/setup_assets.py)")
            continue
        dec = ["-c:v", "libvpx-vp9"] if caps["vp9_alpha_dec"] else []  # libvpx decoder = alpha channel
        inputs += [*dec, "-stream_loop", "-1", "-t", f"{st.end - st.start + 0.5:.3f}", "-i", str(f)]
        fc.append(f"[{idx}:v]scale={STICKER_W}:-2,format=yuva420p,setpts=PTS-STARTPTS+{st.start:.3f}/TB[st{idx}]")
        fc.append(f"[{vlab}][st{idx}]overlay={STICKER_POS[st.position]}:enable='between(t,{st.start:.3f},{st.end:.3f})':eof_action=pass[v{idx}]")
        vlab, idx = f"v{idx}", idx + 1

    fonts_rel = Path(os.path.relpath(config.FONTS_DIR, wd)).as_posix()
    fc.append(f"[{vlab}]subtitles=filename={ass_path.name}:fontsdir={fonts_rel}[{'vsub' if cuts else 'vout'}]")  # text on top of everything
    sel = "not(" + "+".join(f"between(t,{c.start:.3f},{c.end:.3f})" for c in cuts) + ")" if cuts else ""
    if cuts:  # drop the cut ranges LAST so every effect stays in source time
        fc.append(f"[vsub]select='{sel}',setpts=N/FRAME_RATE/TB[vout]")
        logger(f"cuts: removing {sum(c.end - c.start for c in cuts):.2f}s in {len(cuts)} range(s)")

    sfx = [x for x in (plan.sfx if opts.enable_sfx else []) if x.at < info.duration]
    alabs: list[str] = []
    if info.has_audio:
        vc = voice_chain(plan.voice if opts.enable_voice else [])
        vc and logger(f"voice fx: {len(plan.voice)} segment(s) on the speaker track")
        cl = cleanup_chain(wd) if opts.enable_cleanup else ""
        cl and logger("voice cleanup: " + ("rnnoise" if "arnndn" in cl else "fft") + " denoise + de-esser + compressor")
        fc.append(f"[0:a]{AFMT}{cl}{vc}[a0]")
        alabs.append("a0")
    elif sfx:  # silent base so SFX still land on a timeline
        inputs += ["-f", "lavfi", "-t", f"{info.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        fc.append(f"[{idx}:a]{AFMT}[a0]")
        alabs.append("a0")
        idx += 1
    for x in sfx:
        f = config.SFX_DIR / f"{x.kind}.wav"
        if not f.exists():
            logger(f"sfx {x.kind}.wav missing - skipped")
            continue
        ms = int(round(x.at * 1000))
        inputs += ["-i", str(f)]
        fc.append(f"[{idx}:a]{AFMT},adelay={ms}|{ms},volume={SFX_GAIN[x.kind]}[sfx{idx}]")
        alabs.append(f"sfx{idx}")
        idx += 1
    for cb in plan.code if (opts.enable_code and opts.enable_sfx and (config.SFX_DIR / "typing.wav").exists()) else []:
        tsec, ms = min(3.0, max(1.0, (cb.end - cb.start) * 0.55)), int(round(cb.start * 1000))
        inputs += ["-stream_loop", "-1", "-t", f"{tsec:.2f}", "-i", str(config.SFX_DIR / "typing.wav")]
        fc.append(f"[{idx}:a]{AFMT},adelay={ms}|{ms},volume=0.45[typ{idx}]")
        alabs.append(f"typ{idx}")
        idx += 1
    if alabs:
        fc.append("".join(f"[{a}]" for a in alabs)
                  + f"amix=inputs={len(alabs)}:normalize=0:duration=first:dropout_transition=0,alimiter=limit=0.95:level=false[{'amix' if cuts else 'aout'}]")
        cuts and fc.append(f"[amix]aselect='{sel}',asetpts=N/SR/TB[aout]")

    cmd = [*inputs, "-filter_complex", ";".join(fc), "-map", "[vout]"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"] if alabs else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", config.X264_CRF, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out_path)]
    (wd / "filter_complex.txt").write_text(";\n".join(fc), encoding="utf-8")
    run_ffmpeg(cmd, logger, cwd=wd, duration=info.duration, on_progress=on_progress, stderr_file=wd / "ffmpeg_render.log")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise FFmpegError("render produced no output")
    return out_path


# ---------------------------------------------------------------- 6. job drivers
def _load_words(job: Any) -> list[Word]:
    try:
        return json.loads(job.transcript_path.read_text(encoding="utf-8")).get("words", [])
    except (OSError, ValueError):
        return []


def process_job(job_id: str) -> None:
    """Full run. Progress: probe 2 -> audio 5 -> model 8-25 -> whisper 25-44 -> llm 45 -> validate 55 -> b-roll 56-66 -> ass 68 -> render 70-98."""
    job = store.get(job_id)
    if not job:
        return
    job.start("probing input")
    try:
        wd = job.work_dir
        wd.mkdir(parents=True, exist_ok=True)
        info = probe(job.input_path)
        job.duration = info.duration
        job.log(f"input {info.width}x{info.height} @{info.fps:.2f}fps, {info.duration:.2f}s, audio={'yes' if info.has_audio else 'no'}")
        if info.duration <= 0:
            raise RuntimeError("could not read the video duration")

        words: list[Word] = []
        if info.has_audio:
            job.set("extracting audio", 5)
            wav = extract_audio(job.input_path, wd / "audio.wav", job.log)
            job.set("checking whisper model", 8)
            ensure_whisper_model(job.log, lambda msg, f: job.set(msg, 8 + int(17 * f)))  # live download progress
            job.set("loading whisper model into memory", 25)
            words, job.language = transcribe(wav, job.log, lambda f: job.set("transcribing", 25 + int(19 * f)))
        else:
            job.log("no audio stream - skipping transcription")
        _write_json(job.transcript_path, {"language": job.language, "duration": info.duration, "words": words})

        job.set("planning edit (LLM)", 45)
        plan, job.usage = plan_edit(words, info.duration, job.options, job.log, wd / "brief.txt")
        job.set("validating plan", 55)
        plan = validate_plan(plan, info.duration, job.options, words, job.log)
        job.set("finding b-roll", 56)
        plan = resolve_broll(plan, wd, job.options, job.usage, job.log, lambda m, f: job.set(m, 56 + int(10 * f)))
        _write_json(job.plan_path, plan.model_dump())
        _write_json(wd / "plan_ai.json", plan.model_dump())  # pristine copy for "reset to AI plan"

        job.set("building subtitles", 68)
        ass = to_ass(plan, wd / "subs.ass", job.options, info.duration, words)
        job.set("rendering", 70)
        render(job.input_path, plan, ass, job.output_path, job.options, info, job.log,
               lambda f: job.set("rendering", 70 + int(28 * f)))
        job.finish()
    except Exception as e:
        log.error("job %s failed\n%s", job_id, traceback.format_exc())
        job.fail(f"{type(e).__name__}: {e}")


def rerender_job(job_id: str, plan: EditPlan) -> None:
    """Edited plan -> validate -> media for NEW/changed b-roll only -> ASS -> render. No whisper, no LLM."""
    job = store.get(job_id)
    if not job:
        return
    job.start("re-rendering")
    try:
        info = probe(job.input_path)
        job.duration = info.duration
        words = _load_words(job)
        job.set("validating plan", 55)
        plan = validate_plan(plan, info.duration, job.options, None, job.log)
        job.set("finding b-roll", 56)
        plan = resolve_broll(plan, job.work_dir, job.options, job.usage, job.log, lambda m, f: job.set(m, 56 + int(10 * f)))
        _write_json(job.plan_path, plan.model_dump())
        job.set("building subtitles", 68)
        ass = to_ass(plan, job.work_dir / "subs.ass", job.options, info.duration, words)
        job.set("rendering", 70)
        render(job.input_path, plan, ass, job.output_path, job.options, info, job.log,
               lambda f: job.set("rendering", 70 + int(28 * f)))
        job.finish()
    except Exception as e:
        log.error("rerender %s failed\n%s", job_id, traceback.format_exc())
        job.fail(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 7. ask AI to refine
REFINE_PROMPT = """You are revising an EXISTING short-form edit plan. You get the transcript, the CURRENT plan as JSON and the editor's INSTRUCTION.
Return the FULL updated plan. Apply the instruction precisely and keep everything else exactly as it is - same items, same timestamps - \
unless the instruction requires a change. If a time RANGE is given, only touch items inside that range. Every rule of the editor still applies: \
caption timestamps come from the transcript, one top-zone element at a time, b-roll queries are English scene descriptions, zooms never sit inside a full cutaway."""


def refine_plan(plan: EditPlan, words: list[Word], duration: float, instruction: str, rng: tuple[float, float] | None,
                opts: RenderOptions, logger: Logger) -> tuple[EditPlan, Usage]:
    """One structured call: current plan + instruction -> revised plan. Resolved b-roll media is carried over
    for items whose query and kind did not change, so a refine never re-downloads what it keeps."""
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set (add it to .env)")
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    beta = getattr(client, "beta", None)
    parse = getattr(getattr(getattr(beta, "chat", None), "completions", None), "parse", None) or client.chat.completions.parse
    current = EditPlanLLM.model_validate(plan.model_dump()).model_dump()  # strips assets / skip counters
    user_msg = (f"INSTRUCTION: {instruction}\nRANGE: {f'{rng[0]:.2f}s - {rng[1]:.2f}s' if rng else 'whole video'}\n\n"
                f"CURRENT PLAN (JSON):\n{json.dumps(current, ensure_ascii=False)}\n\n"
                f"Video duration: {duration:.2f}s\nTranscript (word|start|end):\n{compact_transcript(words)}")
    logger(f"asking {config.LLM_MODEL} to refine: '{instruction[:80]}'" + (f" in {rng[0]:.1f}-{rng[1]:.1f}s" if rng else ""))
    completion = parse(model=config.LLM_MODEL,
                       messages=[{"role": "system", "content": SYSTEM_PROMPT + "\n" + REFINE_PROMPT}, {"role": "user", "content": user_msg}],
                       response_format=EditPlanLLM, **_chat_kwargs())
    msg = completion.choices[0].message
    if getattr(msg, "refusal", None):
        raise RuntimeError(f"LLM refused: {msg.refusal}")
    llm_plan: EditPlanLLM | None = msg.parsed
    if llm_plan is None:
        raise RuntimeError("LLM returned no parsable plan")
    new = EditPlan.model_validate(llm_plan.model_dump())
    keep = {(b.query.strip().lower(), b.kind): b for b in plan.broll if b.asset}
    new.broll = [b.model_copy(update={"asset": keep[k].asset, "skip": keep[k].skip}) if (k := (b.query.strip().lower(), b.kind)) in keep else b
                 for b in new.broll]
    u = completion.usage
    pin, pout = config.llm_prices(config.LLM_MODEL)
    usage = Usage(model=config.LLM_MODEL, prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
                  completion_tokens=int(getattr(u, "completion_tokens", 0) or 0), calls=1)
    usage.cost_usd = round((usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1e6, 5)
    logger(f"refine usage {usage.prompt_tokens}+{usage.completion_tokens} tokens ~ ${usage.cost_usd:.4f}")
    return new, usage


def refine_job(job_id: str, instruction: str, start: float | None = None, end: float | None = None) -> None:
    """Load plan + transcript -> LLM revision -> validate -> media for new b-roll -> ASS -> render."""
    job = store.get(job_id)
    if not job:
        return
    job.start("refining with AI")
    try:
        info = probe(job.input_path)
        job.duration = info.duration
        words = _load_words(job)
        plan = EditPlan.model_validate_json(job.plan_path.read_text(encoding="utf-8"))
        rng = (max(0.0, float(start)), min(info.duration, float(end))) if start is not None and end is not None and end > start else None
        job.set("asking AI to refine the plan", 45)
        new, u = refine_plan(plan, words, info.duration, instruction, rng, job.options, job.log)
        job.usage.prompt_tokens += u.prompt_tokens
        job.usage.completion_tokens += u.completion_tokens
        job.usage.calls += u.calls
        job.usage.cost_usd = round(job.usage.cost_usd + u.cost_usd, 5)
        job.set("validating plan", 55)
        plan = validate_plan(new, info.duration, job.options, words, job.log)
        job.set("finding b-roll", 56)
        plan = resolve_broll(plan, job.work_dir, job.options, job.usage, job.log, lambda m, f: job.set(m, 56 + int(10 * f)))
        _write_json(job.plan_path, plan.model_dump())
        job.set("building subtitles", 68)
        ass = to_ass(plan, job.work_dir / "subs.ass", job.options, info.duration, words)
        job.set("rendering", 70)
        render(job.input_path, plan, ass, job.output_path, job.options, info, job.log,
               lambda f: job.set("rendering", 70 + int(28 * f)))
        job.finish()
    except Exception as e:
        log.error("refine %s failed\n%s", job_id, traceback.format_exc())
        job.fail(f"{type(e).__name__}: {e}")
