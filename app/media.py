"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

B-roll media. Stock search (Pexels / Pixabay / Wikimedia Commons) and AI generation (OpenAI images, Sora video).
resolve() turns one BRoll request into a downloaded + probed asset inside the job's media/ folder.
Every network failure is logged and skipped - a missing clip never fails the job.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import config
from .schemas import BRoll, MediaAsset

log = logging.getLogger("shorts.media")
Logger = Callable[[str], None]
UA = "AIShortsEditor/1.0 (+https://rameezscripts.com)"
TIMEOUT = httpx.Timeout(30.0, read=180.0)
IMG_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


@dataclass
class Candidate:
    provider: str
    id: str
    kind: str  # image | video
    url: str
    page_url: str = ""
    credit: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    ext: str = "jpg"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"


def _get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA, **(headers or {})}, follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- stock providers
def pexels(query: str, kind: str, n: int = 6) -> list[Candidate]:
    h = {"Authorization": config.PEXELS_API_KEY}
    if kind == "video":
        d = _get("https://api.pexels.com/videos/search", {"query": query, "orientation": "portrait", "per_page": n}, h)
        out: list[Candidate] = []
        for v in d.get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("height")]
            if not files:
                continue
            files.sort(key=lambda f: abs(int(f["height"]) - 1280))  # ~720x1280 = sharp enough, small download
            f = next((x for x in files if int(x["height"]) <= 1920), files[0])
            out.append(Candidate("pexels", str(v["id"]), "video", f["link"], v.get("url", ""),
                                 f"Video by {v.get('user', {}).get('name', '')} on Pexels", int(f.get("width") or 0), int(f["height"]),
                                 float(v.get("duration") or 0), "mp4"))
        return out
    d = _get("https://api.pexels.com/v1/search", {"query": query, "orientation": "portrait", "per_page": n}, h)
    return [Candidate("pexels", str(p["id"]), "image", p["src"].get("large2x") or p["src"].get("large") or p["src"]["original"],
                      p.get("url", ""), f"Photo by {p.get('photographer', '')} on Pexels", int(p.get("width") or 0), int(p.get("height") or 0))
            for p in d.get("photos", []) if p.get("src")]


def pixabay(query: str, kind: str, n: int = 6) -> list[Candidate]:
    key, n = config.PIXABAY_API_KEY, max(3, n)  # pixabay: per_page >= 3
    if kind == "video":
        d = _get("https://pixabay.com/api/videos/", {"key": key, "q": query, "per_page": n, "safesearch": "true"})
        out: list[Candidate] = []
        for hit in d.get("hits", []):
            vids = hit.get("videos") or {}
            f = vids.get("medium") or vids.get("small") or vids.get("large") or {}
            f.get("url") and out.append(Candidate("pixabay", str(hit["id"]), "video", f["url"], hit.get("pageURL", ""),
                                                  f"Video by {hit.get('user', '')} on Pixabay", int(f.get("width") or 0),
                                                  int(f.get("height") or 0), float(hit.get("duration") or 0), "mp4"))
        return out
    d = _get("https://pixabay.com/api/", {"key": key, "q": query, "orientation": "vertical", "image_type": "photo", "per_page": n, "safesearch": "true"})
    return [Candidate("pixabay", str(h["id"]), "image", h.get("largeImageURL") or h["webformatURL"], h.get("pageURL", ""),
                      f"Image by {h.get('user', '')} on Pixabay", int(h.get("imageWidth") or 0), int(h.get("imageHeight") or 0))
            for h in d.get("hits", []) if h.get("largeImageURL") or h.get("webformatURL")]


def wikimedia(query: str, kind: str, n: int = 6) -> list[Candidate]:
    """Keyless fallback. Free-licensed media, quality varies; credits carry the licence."""
    d = _get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "generator": "search", "gsrsearch": f"filetype:{'video' if kind == 'video' else 'bitmap'} {query}",
        "gsrnamespace": 6, "gsrlimit": n, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1280, "format": "json"})
    out: list[Candidate] = []
    for p in sorted(d.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        mime, em = ii.get("mime", ""), ii.get("extmetadata", {})
        if kind == "video":
            url = ii.get("url") or ""
            ext = url.rsplit(".", 1)[-1].lower()
            if ext not in ("webm", "ogv", "mp4"):
                continue
            w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
        else:
            if mime not in IMG_EXT:  # skip svg / gif / tiff
                continue
            url, ext = ii.get("thumburl") or ii.get("url") or "", IMG_EXT[mime]
            w, h = int(ii.get("thumbwidth") or ii.get("width") or 0), int(ii.get("thumbheight") or ii.get("height") or 0)
        artist = re.sub(r"<[^>]+>", "", em.get("Artist", {}).get("value", "")).strip()[:50]
        credit = " · ".join(x for x in (artist, em.get("LicenseShortName", {}).get("value", ""), "Wikimedia Commons") if x)
        url and out.append(Candidate("wikimedia", str(p.get("pageid")), kind, url, ii.get("descriptionurl", ""), credit, w, h,
                                     float(ii.get("duration") or 0), ext))
    return out


STOCK = {"pexels": pexels, "pixabay": pixabay, "wikimedia": wikimedia}


def stock_provider() -> str | None:
    return "pexels" if config.PEXELS_API_KEY else "pixabay" if config.PIXABAY_API_KEY else None


def sources(kind: str) -> list[str]:
    """Ordered source names for one b-roll kind, from the .env policy."""
    stock = stock_provider()
    if kind == "image":
        mode = config.BROLL_IMAGE_SOURCE
        mode = ("search" if stock or not config.OPENAI_API_KEY else "generate") if mode == "auto" else mode
        return [stock or "wikimedia"] if mode == "search" else ["openai_image"]
    mode = config.BROLL_VIDEO_SOURCE
    if mode == "off":
        return []
    if mode == "generate":
        return ["sora"]
    if mode == "search":
        return [stock or "wikimedia"]
    return [stock] if stock else (["openai_image"] if config.OPENAI_API_KEY else ["wikimedia"])  # auto: generated still + motion stands in for video


# ---------------------------------------------------------------- ai generation
def generate_image(query: str, dest: Path, variant: int, logger: Logger) -> Candidate:
    from openai import OpenAI

    prompt = (f"Vertical 9:16 cinematic stock photo for a social media short: {query}. "
              f"Photorealistic, natural lighting, no text, no watermark, no logos." + (f" Variation {variant}." if variant else ""))
    t0 = time.time()
    r = OpenAI(api_key=config.OPENAI_API_KEY).images.generate(model=config.IMAGE_MODEL, prompt=prompt, size="1024x1536",
                                                                quality=config.IMAGE_QUALITY, n=1)
    d = r.data[0]
    data = base64.b64decode(d.b64_json) if getattr(d, "b64_json", None) else httpx.get(d.url, timeout=TIMEOUT).content
    dest.write_bytes(data)
    logger(f"generated image with {config.IMAGE_MODEL} in {time.time() - t0:.0f}s (~${config.image_price():.3f})")
    return Candidate("openai", f"img-{int(t0)}-{variant}", "image", "", "", f"AI image · {config.IMAGE_MODEL}", 1024, 1536, 0.0, "png")


def generate_video(query: str, dest: Path, variant: int, logger: Logger, on_wait: Callable[[str], None] | None = None) -> Candidate:
    from openai import OpenAI

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    prompt = f"{query}. Cinematic vertical short-form b-roll, natural motion, no text, no logos." + (f" Variation {variant}." if variant else "")
    t0 = time.time()
    v = client.videos.create(model=config.VIDEO_MODEL, prompt=prompt, size="720x1280", seconds=str(config.VIDEO_SECONDS))
    while getattr(v, "status", "") in ("queued", "in_progress"):
        on_wait and on_wait(f"{config.VIDEO_MODEL} rendering '{query[:40]}' ({getattr(v, 'progress', 0) or 0}%)")
        time.sleep(5)
        v = client.videos.retrieve(v.id)
    if getattr(v, "status", "") != "completed":
        raise RuntimeError(f"video generation {getattr(v, 'status', '?')}: {getattr(getattr(v, 'error', None), 'message', '')}")
    content = client.videos.download_content(v.id)
    dest.write_bytes(content.read() if hasattr(content, "read") else content.content)
    logger(f"generated {config.VIDEO_SECONDS}s clip with {config.VIDEO_MODEL} in {time.time() - t0:.0f}s (~${config.video_price():.2f})")
    return Candidate("openai", v.id, "video", "", "", f"AI video · {config.VIDEO_MODEL}", 720, 1280, float(config.VIDEO_SECONDS), "mp4")


# ---------------------------------------------------------------- download + probe
def download(url: str, dest: Path, max_mb: int) -> None:
    limit = max_mb * 1024 * 1024
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True) as c, c.stream("GET", url) as r:
        r.raise_for_status()
        if int(r.headers.get("content-length") or 0) > limit:
            raise ValueError(f"file over {max_mb} MB")
        n = 0
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                n += len(chunk)
                if n > limit:
                    raise ValueError(f"file over {max_mb} MB")
                fh.write(chunk)


def _probe(path: Path) -> tuple[int, int, float]:
    """(width, height, duration) via ffprobe; raises on anything ffmpeg can't read."""
    r = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"ffprobe: {r.stderr.strip()[-160:]}")
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    if not st.get("width"):
        raise ValueError("no video/image stream")
    return int(st["width"]), int(st["height"]), float((d.get("format") or {}).get("duration") or 0)


def _thumb(src: Path, dest: Path, is_video: bool) -> None:
    subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *(["-ss", "0.5"] if is_video else []), "-i", str(src),
                    "-frames:v", "1", "-vf", "scale=320:-2", str(dest)], capture_output=True, timeout=60)


# ---------------------------------------------------------------- resolve
def resolve(item: BRoll, index: int, media_dir: Path, used: set[str], logger: Logger,
            on_status: Callable[[str], None] | None = None, on_cost: Callable[[float], None] | None = None) -> MediaAsset | None:
    """Search or generate, download, probe, thumbnail. item.skip = how many earlier candidates to pass over ("swap").
    on_cost gets the estimated USD of every AI generation."""
    media_dir.mkdir(parents=True, exist_ok=True)
    chain = sources(item.kind)
    if item.kind == "video" and chain and chain[0] in STOCK:  # stock video empty -> same provider's images -> generated still
        chain = chain + [c for c in sources("image") if c not in chain]
    for src in chain:
        try:
            if src in ("openai_image", "sora"):
                if not config.OPENAI_API_KEY:
                    continue
                ext = "png" if src == "openai_image" else "mp4"
                dest = media_dir / f"b{index}_ai_{item.skip}.{ext}"
                cand = (generate_image(item.query, dest, item.skip, logger) if src == "openai_image"
                        else generate_video(item.query, dest, item.skip, logger, on_wait=on_status))
                on_cost and on_cost(config.image_price() if src == "openai_image" else config.video_price())
            else:
                cands = [c for c in STOCK[src](item.query, item.kind) if c.key not in used]
                if not cands and item.kind == "video":  # try still images from the same provider
                    cands = [c for c in STOCK[src](item.query, "image") if c.key not in used]
                    cands and logger(f"b-roll {index}: no stock video for '{item.query}' on {src} - using a still with motion")
                if not cands:
                    logger(f"b-roll {index}: nothing on {src} for '{item.query}'")
                    continue
                cand = cands[min(item.skip, len(cands) - 1)]
                dest = media_dir / f"b{index}_{cand.provider}_{re.sub(r'[^A-Za-z0-9]', '', cand.id)[:24]}.{cand.ext}"
                on_status and on_status(f"downloading {cand.kind} from {cand.provider}")
                download(cand.url, dest, config.MEDIA_MAX_MB)
            w, h, dur = _probe(dest)
            is_video = cand.kind == "video" and dur > 0.2
            thumb = dest.with_suffix(".thumb.jpg")
            _thumb(dest, thumb, is_video)
            used.add(cand.key)
            logger(f"b-roll {index}: {cand.kind} {w}x{h} from {cand.provider} for '{item.query}'")
            return MediaAsset(provider=cand.provider, id=cand.id, kind="video" if is_video else "image", file=dest.name,
                              thumb=thumb.name if thumb.exists() else "", page_url=cand.page_url, credit=cand.credit,
                              width=w, height=h, duration=dur if is_video else 0.0)
        except Exception as e:  # noqa: BLE001 - provider down, 4xx, bad file: log + next source
            logger(f"b-roll {index}: {src} failed for '{item.query}': {type(e).__name__}: {str(e)[:120]}")
    return None
