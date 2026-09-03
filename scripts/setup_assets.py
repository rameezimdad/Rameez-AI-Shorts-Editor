#!/usr/bin/env python3
"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

One-shot asset bootstrap so the app runs end-to-end with zero manual work:
  fonts/     Montserrat ExtraBold + Anton (Impact-like) + JetBrains Mono (code windows) from GitHub
  audio/     rnnoise.rnnn - RNNoise model for the voice cleanup filter
  sfx/       13 synthesized placeholder sounds (whoosh, pop, ding, boom, riser, click, cash, ...)
  stickers/  25 two-second looping alpha WebM badges with 5 animation styles

Idempotent: existing files are kept.  --force regenerates,  --skip-fonts for offline use,
--sfx-pack DIR imports your own <kind>.wav/mp3 files (normalised to 48 kHz stereo, -16 LUFS).
Replace any placeholder by dropping a real file with the same name into the folder.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

FONTS: dict[str, list[str]] = {
    "Montserrat-ExtraBold.ttf": [
        "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-ExtraBold.ttf",
    ],
    "Anton-Regular.ttf": [
        "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
    ],
    "JetBrainsMono-Bold.ttf": [
        "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/master/fonts/ttf/JetBrainsMono-Bold.ttf",
    ],
}
AUDIO_MODELS: dict[str, list[str]] = {
    "rnnoise.rnnn": [
        "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/somnolent-hogwash-2018-09-01/sh.rnnn",
        "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/beguiling-drafter-2018-08-30/bd.rnnn",
    ],
}

# lavfi graphs - synthesized, 48k stereo. Drop real files with the same names into assets/sfx/ (or --sfx-pack) for the real thing.
SFX: dict[str, str] = {
    "whoosh": "anoisesrc=d=0.7:c=pink:r=48000:a=0.9,highpass=f=300,lowpass=f=2500,afade=t=in:st=0:d=0.2,afade=t=out:st=0.3:d=0.4",
    "swoosh_out": "anoisesrc=d=0.7:c=pink:r=48000:a=0.9,highpass=f=300,lowpass=f=2500,afade=t=in:st=0:d=0.2,afade=t=out:st=0.3:d=0.4,areverse",
    "pop": "aevalsrc='0.9*sin(2*PI*t*(900-4000*t))*exp(-t*35)':d=0.14:s=48000",
    "ding": "aevalsrc='0.5*sin(2*PI*1318*t)*exp(-t*4)+0.25*sin(2*PI*2637*t)*exp(-t*6)+0.12*sin(2*PI*3955*t)*exp(-t*9)':d=1.0:s=48000",
    "boom": "aevalsrc='0.9*sin(2*PI*(55+30*exp(-t*12))*t)*exp(-t*3)+0.3*random(0)*exp(-t*40)':d=1.0:s=48000",
    "riser": "aevalsrc='(0.5*sin(2*PI*t*(180+520*t))+0.25*random(0))*min(t/1.3,1)':d=1.5:s=48000,afade=t=out:st=1.35:d=0.15",
    "click": "aevalsrc='0.8*random(0)*exp(-t*400)+0.6*sin(2*PI*1200*t)*exp(-t*300)':d=0.06:s=48000",
    "cash": "aevalsrc='0.5*(sin(2*PI*2093*t)*exp(-t*8)+sin(2*PI*2637*(t-0.09))*exp(-(t-0.09)*8)*gte(t,0.09)+sin(2*PI*3136*(t-0.18))*exp(-(t-0.18)*8)*gte(t,0.18))':d=0.8:s=48000",
    "notification": "aevalsrc='0.5*sin(2*PI*880*t)*exp(-t*5)*lt(t,0.18)+0.5*sin(2*PI*1320*(t-0.18))*exp(-(t-0.18)*4)*gte(t,0.18)':d=0.7:s=48000",
    "drum": "aevalsrc='0.9*sin(2*PI*(150-100*min(t*8,1))*t)*exp(-t*9)+0.5*random(0)*exp(-t*60)':d=0.4:s=48000",
    "bass_drop": "aevalsrc='0.9*sin(2*PI*(70-40*min(t,1))*t)*(1-exp(-t*30))*exp(-t*1.2)':d=1.6:s=48000",
    "glitch": "aevalsrc='0.7*random(0)*gt(sin(2*PI*23*t),0.6)*gt(sin(2*PI*7*t),0)':d=0.5:s=48000",
    "typing": "aevalsrc='0.6*random(0)*lt(mod(t,0.11),0.012)*exp(-mod(t,0.11)*250)':d=2.0:s=48000",
}

# kind -> (label, box colour, font size, text colour, animation)
STICKERS: dict[str, tuple[str, str, int, str, str]] = {
    "question_marks": ("???", "0xFFD400", 150, "black", "tilt"),
    "user_question": ("USER?", "0x2D9BFF", 96, "white", "tilt"),
    "money": ("$$$", "0x39FF14", 150, "black", "bounce"),
    "fire": ("FIRE", "0xFF5A1F", 116, "white", "pulse"),
    "warning": ("!", "0xFF2D55", 200, "white", "shake"),
    "arrow_down": ("↓", "0xFFD400", 230, "black", "bounce"),
    "check": ("YES!", "0x39FF14", 120, "black", "pop"),
    "cross": ("NO!", "0xFF2D55", 120, "white", "shake"),
    "wow": ("WOW", "0xFF3CAC", 130, "white", "pop"),
    "subscribe": ("SUBSCRIBE", "0xFF0000", 62, "white", "pulse"),
    "new": ("NEW", "0x2D9BFF", 130, "white", "pop"),
    "tip": ("TIP", "0xFFD400", 130, "black", "tilt"),
    "hundred": ("100%", "0x39FF14", 110, "black", "pop"),
    "free": ("FREE", "0xFF5A1F", 120, "white", "bounce"),
    "like": ("LIKE", "0x2D9BFF", 120, "white", "pulse"),
    "clock": ("TIME", "0xFFFFFF", 110, "black", "shake"),
    "rocket": ("FAST", "0xFF5A1F", 120, "white", "bounce"),
    "idea": ("IDEA", "0xFFD400", 120, "black", "pop"),
    "laugh": ("LOL", "0xFF3CAC", 130, "white", "shake"),
    "code": ("</>", "0x1E1E28", 130, "white", "pop"),
    "star": ("TOP", "0xFFD400", 130, "black", "pulse"),
    "omg": ("OMG", "0xFF2D55", 130, "white", "shake"),
    "save": ("SAVE", "0x39FF14", 120, "black", "bounce"),
    "stop": ("STOP", "0xFF2D55", 120, "white", "pop"),
    "wait": ("WAIT", "0xFFD400", 120, "black", "tilt"),
}
STICKER_SIZE, STICKER_SECS, STICKER_FPS = 420, 2.0, 30


def ff(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                          cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def fetch(url: str, dest: Path, min_bytes: int = 10_000) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ai-shorts-editor"}), timeout=60) as r:
            data = r.read()
        if len(data) < min_bytes:  # html error page, not the file
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:  # noqa: BLE001 - any network failure just tries the next mirror
        print(f"    {url} -> {e}")
        return False


def _download_set(files: dict[str, list[str]], folder: Path, what: str, force: bool, min_bytes: int = 10_000) -> None:
    for name, urls in files.items():
        dest = folder / name
        if dest.exists() and not force:
            print(f"  {what} ok      {name}")
            continue
        ok = any(fetch(u, dest, min_bytes) for u in urls)
        print(f"  {what} {'saved' if ok else 'FAILED (optional - a fallback is used)'}   {name}")


def setup_fonts(force: bool) -> None:
    _download_set(FONTS, config.FONTS_DIR, "font", force)


def setup_audio_models(force: bool) -> None:
    _download_set(AUDIO_MODELS, config.AUDIO_DIR, "model", force, min_bytes=20_000)


def setup_sfx(force: bool) -> None:
    for kind, graph in SFX.items():
        dest = config.SFX_DIR / f"{kind}.wav"
        if dest.exists() and not force:
            print(f"  sfx ok       {kind}.wav")
            continue
        r = ff(["-f", "lavfi", "-i", graph, "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(dest)])
        print(f"  sfx {'made' if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()[-200:]}     {kind}.wav")


def import_sfx_pack(folder: Path) -> None:
    """Copy <kind>.(wav|mp3|m4a|ogg|flac) from a folder into assets/sfx, normalised to 48k stereo / -16 LUFS."""
    if not folder.is_dir():
        print(f"  sfx pack: {folder} is not a folder")
        return
    for f in sorted(folder.iterdir()):
        kind = f.stem.lower()
        if kind not in SFX or f.suffix.lower() not in (".wav", ".mp3", ".m4a", ".ogg", ".flac"):
            continue
        r = ff(["-i", str(f), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
                str(config.SFX_DIR / f"{kind}.wav")])
        print(f"  sfx pack {'imported' if r.returncode == 0 else 'FAILED'} {f.name} -> {kind}.wav")


def _bgr(hex6: str) -> str:
    """0xRRGGBB -> ASS &HBBGGRR&"""
    h = hex6[-6:]
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _anim_lines(anim: str, base: str, label: str) -> list[str]:
    """Dialogue lines for one 2 s loop. Each style starts and ends in the same pose so the loop is seamless."""
    c, half, secs = STICKER_SIZE // 2, int(STICKER_SECS * 500), STICKER_SECS
    t = lambda a, b: f"0:00:0{a:.2f},0:00:0{b:.2f}"  # noqa: E731
    if anim == "pulse":
        return [f"Dialogue: 0,{t(0, secs / 2)},S,,0,0,0,,{{{base}\\fscx100\\fscy100\\t(0,{half},\\fscx118\\fscy118)}}{label}",
                f"Dialogue: 0,{t(secs / 2, secs)},S,,0,0,0,,{{{base}\\fscx118\\fscy118\\t(0,{half},\\fscx100\\fscy100)}}{label}"]
    if anim == "bounce":
        b = base.replace(f"\\pos({c},{c})", "")
        return [f"Dialogue: 0,{t(0, secs / 2)},S,,0,0,0,,{{{b}\\move({c},{c - 22},{c},{c + 22})}}{label}",
                f"Dialogue: 0,{t(secs / 2, secs)},S,,0,0,0,,{{{b}\\move({c},{c + 22},{c},{c - 22})}}{label}"]
    if anim == "shake":
        return [f"Dialogue: 0,{t(0, secs / 2)},S,,0,0,0,,{{{base}\\frz-9\\t(0,{half},\\frz9)}}{label}",
                f"Dialogue: 0,{t(secs / 2, secs)},S,,0,0,0,,{{{base}\\frz9\\t(0,{half},\\frz-9)}}{label}"]
    if anim == "pop":  # springs in, holds with a breath, shrinks away so the loop pops again
        return [f"Dialogue: 0,{t(0, 0.25)},S,,0,0,0,,{{{base}\\fscx40\\fscy40\\t(0,250,\\fscx112\\fscy112)}}{label}",
                f"Dialogue: 0,{t(0.25, 0.45)},S,,0,0,0,,{{{base}\\fscx112\\fscy112\\t(0,200,\\fscx100\\fscy100)}}{label}",
                f"Dialogue: 0,{t(0.45, 1.75)},S,,0,0,0,,{{{base}\\fscx100\\fscy100\\t(0,650,\\fscx105\\fscy105)\\t(650,1300,\\fscx100\\fscy100)}}{label}",
                f"Dialogue: 0,{t(1.75, secs)},S,,0,0,0,,{{{base}\\fscx100\\fscy100\\t(0,250,\\fscx40\\fscy40)}}{label}"]
    return [f"Dialogue: 0,{t(0, secs / 2)},S,,0,0,0,,{{{base}\\fscx100\\fscy100\\frz0\\t(0,{half},\\fscx112\\fscy112\\frz-6)}}{label}",  # tilt
            f"Dialogue: 0,{t(secs / 2, secs)},S,,0,0,0,,{{{base}\\fscx112\\fscy112\\frz-6\\t(0,{half},\\fscx100\\fscy100\\frz0)}}{label}"]


def _sticker_ass(kind: str, mask: bool = False) -> str:
    """Text badge rendered by libass (present in every build that can do captions) - no drawtext needed.
    mask=True paints the identical geometry all-white: libass never writes the alpha plane, so that
    render becomes the alpha channel via alphamerge."""
    label, box, size, fg, anim = STICKERS[kind]
    c = STICKER_SIZE // 2
    fill = "&HFFFFFF&" if fg == "white" or mask else "&H000000&"
    boxc = "&HFFFFFF&" if mask else _bgr(box)
    base = rf"\an5\pos({c},{c})\fs{size}\bord28\shad0\3c{boxc}\1c{fill}"
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {STICKER_SIZE}\nPlayResY: {STICKER_SIZE}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: S,Montserrat ExtraBold,{size},{fill},{fill},{boxc},&H00000000,-1,0,0,0,100,100,0,0,3,28,0,5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(_anim_lines(anim, base, label)) + "\n"
    )


def _sticker_cmd(kind: str, dest: Path, ass_name: str | None) -> list[str]:
    """Colour pass + all-white mask pass on opaque black, mask becomes alpha (alphamerge).
    Without an ASS file (no libass): a pulsing box on a transparent canvas - note format=rgba sits INSIDE
    the lavfi source graph, otherwise the colour source negotiates yuv420p and 'transparent' comes out black."""
    src = f"color=c=black:s={STICKER_SIZE}x{STICKER_SIZE}:r={STICKER_FPS}:d={STICKER_SECS}"
    enc = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "30",
           "-deadline", "good", "-cpu-used", "2", "-an", dest.name]
    if not ass_name:
        box = f"drawbox=x=60:y=60+26*sin(2*PI*t/{STICKER_SECS}):w=300:h=300:color={STICKERS[kind][1]}@1:t=fill"
        return ["-f", "lavfi", "-i", f"{src}@0.0,format=rgba", "-vf", f"{box},format=yuva420p", *enc]
    fonts = Path(os.path.relpath(config.FONTS_DIR, config.STICKERS_DIR)).as_posix()
    mask = ass_name.replace(".ass", "_mask.ass")
    fc = (f"[0:v]format=rgb24,split[c][m];[c]subtitles=filename={ass_name}:fontsdir={fonts}[rgb];"
          f"[m]subtitles=filename={mask}:fontsdir={fonts},format=gray[a];[rgb][a]alphamerge,format=yuva420p[out]")
    return ["-f", "lavfi", "-i", src, "-filter_complex", fc, "-map", "[out]", *enc]


def setup_stickers(force: bool) -> None:
    from app.pipeline import ffmpeg_caps  # noqa: E402
    caps = ffmpeg_caps()
    if not caps["vp9_enc"]:
        print("  stickers SKIPPED - ffmpeg has no libvpx-vp9 encoder (alpha WebM). Drop real .webm files into assets/stickers/")
        return
    for kind in STICKERS:
        dest = config.STICKERS_DIR / f"{kind}.webm"
        if dest.exists() and not force:
            print(f"  sticker ok   {kind}.webm")
            continue
        ass = config.STICKERS_DIR / f"_{kind}.ass"  # cwd = stickers dir -> relative names, no path escaping
        ass_mask = config.STICKERS_DIR / f"_{kind}_mask.ass"
        ass.write_text(_sticker_ass(kind), encoding="utf-8")
        ass_mask.write_text(_sticker_ass(kind, mask=True), encoding="utf-8")
        r = ff(_sticker_cmd(kind, dest, ass.name if caps["subtitles"] else None), cwd=config.STICKERS_DIR)
        if r.returncode != 0 and caps["subtitles"]:  # libass trouble -> plain pulsing box, still alpha
            r = ff(_sticker_cmd(kind, dest, None), cwd=config.STICKERS_DIR)
        ass.unlink(missing_ok=True)
        ass_mask.unlink(missing_ok=True)
        print(f"  sticker {'made' if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()[-200:]} {kind}.webm")


def ensure_assets(force: bool = False, skip_fonts: bool = False) -> None:
    print(f"assets -> {config.ASSETS}")
    skip_fonts or setup_fonts(force)
    skip_fonts or setup_audio_models(force)
    setup_sfx(force)
    setup_stickers(force)


def main() -> int:
    ap = argparse.ArgumentParser(description="download fonts + models, generate placeholder sfx/stickers")
    ap.add_argument("--force", action="store_true", help="regenerate even if files exist")
    ap.add_argument("--skip-fonts", action="store_true", help="offline: don't download fonts / models")
    ap.add_argument("--sfx-pack", metavar="DIR", help="import your own <kind>.wav/mp3 sound effects from DIR")
    a = ap.parse_args()
    if subprocess.run([config.FFMPEG, "-version"], capture_output=True).returncode != 0:
        print("ffmpeg not found on PATH - install it (or set FFMPEG_BIN in .env) and re-run", file=sys.stderr)
        return 1
    ensure_assets(a.force, a.skip_fonts)
    a.sfx_pack and import_sfx_pack(Path(a.sfx_pack))
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
