#!/usr/bin/env python3
"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

Smoke test - no API key, no whisper download:
  1. ffmpeg synthesizes a 10s 720x1280 clip (testsrc + quiet sine)
  2. transcribe() and plan_edit() are swapped for fakes
  3. process_job() runs the real validate -> ASS -> render path
  4. asserts final.mp4 exists, is 1080x1920, ~10s, has audio
  5. rerender_job() with an edited plan re-renders without whisper/LLM

Usage:  python scripts/smoke_test.py [--keep]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, pipeline  # noqa: E402
from app.jobs import store  # noqa: E402
from app.schemas import (SFX, BRoll, Caption, CodeBlock, Cut, EditPlan, Headline, MediaAsset, NeonBadge,  # noqa: E402
                         RenderOptions, Sticker, Usage, VoiceFx, Zoom)
from scripts.setup_assets import ensure_assets  # noqa: E402

DUR = 12.0
TEXT = "this is a smoke test of the ai shorts editor with captions stickers and sound effects rendering fine".split()
WORDS: list[pipeline.Word] = [{"w": w, "s": round(0.3 + i * 0.45, 3), "e": round(0.3 + i * 0.45 + 0.4, 3)} for i, w in enumerate(TEXT)]


def fake_plan() -> EditPlan:
    """Deliberately dirty: overlaps + out-of-range items so validate_plan has work to do."""
    chunks = [WORDS[i:i + 3] for i in range(0, len(WORDS), 3)]
    caps = [Caption(text=" ".join(w["w"] for w in c).upper(), start=c[0]["s"], end=c[-1]["e"], emphasis=i % 4 == 1)
            for i, c in enumerate(chunks)]
    caps.append(Caption(text="GHOST", start=13.0, end=14.0, emphasis=False))  # past the end -> dropped
    return EditPlan(
        hook="SMOKE TEST PASSING?",
        captions=caps,
        sfx=[SFX(at=0.5, kind="whoosh"), SFX(at=4.6, kind="pop"), SFX(at=8.7, kind="ding"),
             SFX(at=9.0, kind="boom"),   # < 4s after ding -> dropped
             SFX(at=12.0, kind="riser")],  # past the end -> dropped
        stickers=[Sticker(kind="question_marks", start=7.6, end=9.5, position="top"),
                  Sticker(kind="fire", start=4.0, end=6.0, position="top_right")],  # overlaps headline -> dropped
        headlines=[Headline(main="SHORTS EDITOR", sub="AI CAPTION TOOL", start=3.2, end=5.5)],
        badges=[NeonBadge(text="WARNING", start=5.6, end=7.5, color="red"),
                NeonBadge(text="CLASH", start=1.0, end=2.0, color="blue")],  # inside the 0-3s hook -> dropped
        voice=[VoiceFx(start=1.65, end=2.95, effect="excited"), VoiceFx(start=7.0, end=8.35, effect="boost"),
               VoiceFx(start=7.5, end=8.0, effect="soft")],  # overlaps the boost -> dropped
        broll=[BRoll(query="sticky notes desk", kind="image", mode="full", motion="zoom_in", start=3.2, end=5.0),
               BRoll(query="fire hype", kind="video", mode="pip", motion="pan_left", start=9.8, end=11.6),
               BRoll(query="too early", kind="image", mode="full", motion="none", start=0.5, end=2.0)],  # in the first 1.5s -> dropped
        zooms=[Zoom(at=1.7, end=2.1, scale=1.2, kind="punch"), Zoom(at=3.5, end=3.9, scale=1.2, kind="punch"),  # inside the cutaway -> dropped
               Zoom(at=6.0, end=8.0, scale=1.3, kind="in"), Zoom(at=8.1, end=8.5, scale=1.2, kind="punch"),  # < gap after the push-in -> dropped
               Zoom(at=9.2, end=10.7, scale=1.25, kind="out")],
        code=[CodeBlock(title="main.py", language="python", start=9.5, end=12.0,  # claims the top zone before the pip card -> pip dropped
                        code='def hello(name):\n    # greet\n    return f"hi {name}"\nprint(hello("world"))')],
        cuts=[Cut(start=5.7, end=6.4), Cut(start=6.2, end=6.6), Cut(start=11.0, end=11.1)],  # merged -> 5.7-6.6; 0.1s -> dropped
        music_mood="upbeat lo-fi",
    )


def fake_resolve(item: BRoll, index: int, media_dir: Path, used: set, logger, on_status=None, on_cost=None) -> MediaAsset:
    """Stand-in for media.resolve: a synthetic still / clip written straight into the job's media folder."""
    media_dir.mkdir(parents=True, exist_ok=True)
    if item.kind == "video":
        f = media_dir / f"b{index}_test.mp4"
        subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(f)], check=True)
        return MediaAsset(provider="test", id=f"v{index}", kind="video", file=f.name, credit="test clip", width=640, height=360, duration=3.0)
    f = media_dir / f"b{index}_test.png"
    subprocess.run([config.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "smptebars=size=720x1280", "-frames:v", "1", str(f)], check=True)
    return MediaAsset(provider="test", id=f"i{index}", kind="image", file=f.name, credit="test still", width=720, height=1280)


def make_test_video(path: Path) -> None:
    subprocess.run([
        config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(DUR), "-af", "volume=0.15", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def probe_out(path: Path) -> dict:
    out = subprocess.run([config.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    cond or sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the job files afterwards")
    a = ap.parse_args()
    t0 = time.time()

    print("assets")
    ensure_assets(force=False)
    caps = pipeline.ffmpeg_caps()
    check(caps["ffmpeg"] and caps["ffprobe"], f"ffmpeg + ffprobe on PATH ({caps['version'][:40]})")
    check(caps["subtitles"], "ffmpeg has the subtitles filter (libass)")
    check(caps["libx264"], "ffmpeg has libx264")

    print("fakes")
    pipeline.ensure_whisper_model = lambda logger, on_status=None: None  # type: ignore[assignment]  # never download here
    pipeline.transcribe = lambda wav, logger, on_progress=None: (WORDS, "en")  # type: ignore[assignment]
    pipeline.plan_edit = lambda words, duration, opts, logger, brief_path=None: (fake_plan(), Usage(model="mock", calls=0))  # type: ignore[assignment]
    pipeline.media.resolve = fake_resolve  # type: ignore[assignment]  # no network, no API keys

    print("job")
    opts = RenderOptions(caption_font="montserrat", accent_color="#FFD400", enable_stickers=True, enable_sfx=True)
    job = store.create("smoke.mp4", opts)
    make_test_video(job.input_path)
    job.duration = DUR
    t1 = time.time()
    pipeline.process_job(job.id)
    if job.status != "done":
        print("\n".join(job.log_lines[-40:]))
    check(job.status == "done", f"process_job finished ({time.time() - t1:.1f}s): {job.error or 'ok'}")

    plan = EditPlan.model_validate_json(job.plan_path.read_text(encoding="utf-8"))
    check(len(plan.captions) == len([WORDS[i:i + 3] for i in range(0, len(WORDS), 3)]), f"captions kept ({len(plan.captions)}), GHOST dropped")
    check(all(c.end <= DUR for c in plan.captions), "captions clamped to duration")
    check([s.kind for s in plan.sfx] == ["whoosh", "pop", "ding"], f"sfx spacing/range enforced -> {[s.kind for s in plan.sfx]}")
    check([s.kind for s in plan.stickers] == ["question_marks"], f"overlapping sticker dropped -> {[s.kind for s in plan.stickers]}")
    check([b.text for b in plan.badges] == ["WARNING"], f"badge inside hook window dropped -> {[b.text for b in plan.badges]}")
    check([v.effect for v in plan.voice] == ["excited", "boost"], f"overlapping voice fx dropped -> {[v.effect for v in plan.voice]}")
    check([(b.mode, b.asset.kind if b.asset else None) for b in plan.broll] == [("full", "image")],
          f"b-roll validated + resolved, pip lost the top zone to the code window -> {[(b.mode, b.query) for b in plan.broll]}")
    check([(c.title, c.start) for c in plan.code] == [("main.py", 9.5)] and plan.code[0].code.count("\n") == 3, "code window kept, 4 lines")
    check([(c.start, c.end) for c in plan.cuts] == [(5.7, 6.6)], f"cuts merged + tiny one dropped -> {[(c.start, c.end) for c in plan.cuts]}")
    check([(z.kind, z.at) for z in plan.zooms] == [("punch", 1.7), ("in", 6.0), ("out", 9.2)], f"zoom rules -> {[(z.kind, z.at) for z in plan.zooms]}")
    fc = (job.work_dir / "filter_complex.txt").read_text(encoding="utf-8")
    check("volume=volume='1+0.60*clip(" in fc and "treble=g=4:enable=" in fc, "voice gain ramp + gated EQ in the filtergraph")
    check(fc.count("zoompan=") == 2 and "overlay=0:0:enable=" in fc, "zoompan on speaker + full b-roll overlay in the filtergraph")
    check("arnndn=m=" in fc and "deesser=" in fc and "acompressor=" in fc, "voice cleanup chain (rnnoise + de-esser + compressor)")
    check("select='not(between(t,5.700,6.600))',setpts=N/FRAME_RATE/TB[vout]" in fc and "aselect='not(between(t,5.700,6.600))',asetpts=N/SR/TB[aout]" in fc, "cut applied to video and audio")
    check("typing.wav" in " ".join(job.log_lines) and "[typ" in fc, "typing sfx mixed under the code window")
    check(all(x in fc for x in ("between(it,6.000,8.000)", "(1-", "pow(clip(1-(it-1.700)")), "punch / in / out zoom terms present")
    ass_txt = (job.work_dir / "subs.ass").read_text(encoding="utf-8")
    check(ass_txt.count("\\p1}m 0 0 l 1080 0") == 2 and "{\\1c&H00D4FF&}THIS{\\1c&HFFFFFF&}" in ass_txt, "flash frames on the cutaway + karaoke word highlight in the ASS")
    check(ass_txt.count(",Code,") > 40 and "{\\1c&HC679FF&}def" in ass_txt and "\\h\\h\\h\\h" in ass_txt, "code window typed out with syntax colours + indentation")
    ass = (job.work_dir / "subs.ass").read_text(encoding="utf-8")
    check("PlayResX: 1080" in ass and "PlayResY: 1920" in ass, "ASS PlayRes 1080x1920")
    check(all(f",{s}," in ass for s in ("Cap", "Emph", "Hook", "Head", "Sub", "Neon")), "ASS uses all six styles")
    check(job.transcript_path.exists() and (job.work_dir / "plan_ai.json").exists(), "transcript.json + plan_ai.json written")

    info = probe_out(job.output_path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    dur = float(info["format"]["duration"])
    check((v["width"], v["height"]) == (1080, 1920), f"output is {v['width']}x{v['height']}")
    check(abs(dur - (DUR - 0.9)) < 0.6, f"output duration {dur:.2f}s (12s source minus the 0.9s cut)")
    check(any(s["codec_type"] == "audio" for s in info["streams"]), "output has an audio track")
    check(v.get("codec_name") == "h264", f"video codec {v.get('codec_name')}")

    print("re-render")
    edited = plan.model_copy(update={"hook": "EDITED HOOK", "sfx": plan.sfx[:1]})  # sticker stays -> visible in final.mp4
    mtime = job.output_path.stat().st_mtime
    t2 = time.time()
    pipeline.rerender_job(job.id, edited)
    job.status == "done" or print("\n".join(job.log_lines[-40:]))
    check(job.status == "done", f"rerender_job finished ({time.time() - t2:.1f}s)")
    check(job.output_path.stat().st_mtime >= mtime, "output re-written")
    check("EDITED HOOK" in (job.work_dir / "subs.ass").read_text(encoding="utf-8"), "edited hook reached the ASS")
    check(job.usage.model == "mock" and job.usage.calls == 0, "no LLM call on re-render")
    check(len(plan.broll) == 1 and all(b.asset and (job.work_dir / "media" / b.asset.file).exists() for b in plan.broll), "b-roll media kept across re-render")

    print(f"\nALL PASS in {time.time() - t0:.1f}s  ->  {job.output_path}")
    if a.keep:
        print(f"kept job {job.id}: {job.work_dir}")
    else:
        store.remove(job.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
