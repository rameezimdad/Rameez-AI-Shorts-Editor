"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

Paths + env settings. Everything else imports from here so there's ONE place
that knows where the project lives and what the .env said.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # silently no-ops if missing
# classic HTTP download for whisper weights: streams into the cache file so the UI can show live progress
# (the xet transport lands the file in bursts). Set HF_HUB_DISABLE_XET=0 in .env to opt back in.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# dirs
ASSETS = ROOT / "assets"
FONTS_DIR = ASSETS / "fonts"
SFX_DIR = ASSETS / "sfx"
STICKERS_DIR = ASSETS / "stickers"
AUDIO_DIR = ASSETS / "audio"  # rnnoise model for voice cleanup
FRONTEND_DIR = ROOT / "frontend"
STORAGE = ROOT / "storage"
UPLOADS_DIR = STORAGE / "uploads"
OUTPUTS_DIR = STORAGE / "outputs"
WORK_DIR = STORAGE / "work"

for _d in (FONTS_DIR, SFX_DIR, STICKERS_DIR, AUDIO_DIR, UPLOADS_DIR, OUTPUTS_DIR, WORK_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _env(key: str, default: str) -> str:
    v = os.getenv(key)
    return v.strip() if v and v.strip() else default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


# llm / whisper
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-2024-08-06")
LLM_TWO_PASS = _env_bool("LLM_TWO_PASS", True)  # editorial brief first, then the structured plan
LLM_REASONING = _env("LLM_REASONING", "medium")  # gpt-5 / o-series only: minimal | low | medium | high
MODELS_DIR = ROOT / "models"


def _whisper_model() -> str:
    """WHISPER_MODEL = size name (auto-download), an absolute folder, or a folder under <project>/models.
    Hand-downloaded weights in models/<name>/model.bin win over the hub download."""
    name = _env("WHISPER_MODEL", "large-v3")
    for d in (Path(name), ROOT / name, MODELS_DIR / name, MODELS_DIR / f"faster-whisper-{name}"):
        if (d / "model.bin").exists():
            return str(d.resolve())
    return name


WHISPER_MODEL = _whisper_model()
WHISPER_DEVICE = _env("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = _env("WHISPER_COMPUTE", "int8")
# measured on an i9-14900K: 4-6 threads is the sweet spot, 16+ is SLOWER (hyper-threads / E-cores fight over cache)
WHISPER_THREADS = int(_env("WHISPER_THREADS", "6" if (os.cpu_count() or 4) >= 12 else "4"))
WHISPER_BEAM = max(1, int(_env("WHISPER_BEAM", "5")))  # 1 = greedy, ~2x faster, slightly less accurate

# ffmpeg
def _tool(env_key: str, name: str) -> str:
    """Env override > PATH > common Windows drop-in folders (C:\\ffmpeg\\bin, <project>\\ffmpeg\\bin)."""
    if (v := _env(env_key, "")) or shutil.which(name):
        return v or name
    for d in (Path("C:/ffmpeg/bin"), ROOT / "ffmpeg" / "bin", ROOT / "ffmpeg"):
        if (exe := d / f"{name}.exe").exists():
            return str(exe)
    return name


FFMPEG = _tool("FFMPEG_BIN", "ffmpeg")
FFPROBE = _tool("FFPROBE_BIN", "ffprobe")
# b-roll media
PEXELS_API_KEY = _env("PEXELS_API_KEY", "")
PIXABAY_API_KEY = _env("PIXABAY_API_KEY", "")
BROLL_IMAGE_SOURCE = _env("BROLL_IMAGE_SOURCE", "auto")  # auto | search | generate
BROLL_VIDEO_SOURCE = _env("BROLL_VIDEO_SOURCE", "auto")  # auto | search | generate (Sora, paid) | off
IMAGE_MODEL = _env("IMAGE_MODEL", "gpt-image-1-mini")
IMAGE_QUALITY = _env("IMAGE_QUALITY", "low")  # low | medium | high
VIDEO_MODEL = _env("VIDEO_MODEL", "sora-2")
VIDEO_SECONDS = max(4, min(12, int(_env("VIDEO_SECONDS", "4"))))
MEDIA_MAX_MB = int(_env("MEDIA_MAX_MB", "80"))
_IMG_PRICES = {  # usd per 1024x1536 image (rough, for the estimate only)
    "gpt-image-1-mini": {"low": 0.006, "medium": 0.015, "high": 0.05},
    "gpt-image-1": {"low": 0.02, "medium": 0.07, "high": 0.27},
}


def image_price() -> float:
    return float(_env("IMAGE_PRICE", str(_IMG_PRICES.get(IMAGE_MODEL, _IMG_PRICES["gpt-image-1-mini"]).get(IMAGE_QUALITY, 0.02))))


def video_price() -> float:
    return VIDEO_SECONDS * float(_env("VIDEO_PRICE_PER_SEC", "0.10" if VIDEO_MODEL == "sora-2" else "0.50"))


X264_PRESET = _env("X264_PRESET", "medium")
X264_CRF = _env("X264_CRF", "18")

# server limits
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_EXT = {".mp4", ".mov", ".webm"}
MAX_CONCURRENT_JOBS = max(1, int(_env("MAX_CONCURRENT_JOBS", "1")))
LOG_KEEP_LINES = 600  # per job, in memory (full ffmpeg stderr also lands in work/<job>/ffmpeg_render.log)

# output canvas (9:16)
OUT_W, OUT_H = 1080, 1920

# usd per 1M tokens (prompt, completion) - override via LLM_PRICE_IN / LLM_PRICE_OUT
_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
}


def llm_prices(model: str = LLM_MODEL) -> tuple[float, float]:
    """(usd_in, usd_out) per 1M tokens; env override wins, unknown model -> gpt-4o rates."""
    # longest prefix wins so gpt-4o-mini never falls into gpt-4o
    base = next((p for k, p in sorted(_PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), (2.50, 10.00))
    return float(_env("LLM_PRICE_IN", str(base[0]))), float(_env("LLM_PRICE_OUT", str(base[1])))
