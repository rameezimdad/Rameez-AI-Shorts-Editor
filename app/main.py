"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

FastAPI app: upload -> background job -> poll -> plan edit -> re-render -> download.
Run: uvicorn app.main:app --reload   (or simply: python app/main.py)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator

if __name__ == "__main__" and not __package__:  # run as a script (python app/main.py / IDE play button)
    _root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_root))
    __package__ = "app"
    import importlib.util
    import subprocess

    # first run: pull anything missing from requirements.txt into THIS interpreter's env
    _missing = [m for m in ("fastapi", "uvicorn", "python_multipart", "dotenv", "openai", "faster_whisper") if importlib.util.find_spec(m) is None]
    if _missing:
        print(f"first run - installing requirements ({', '.join(_missing)} missing)...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(_root / "requirements.txt")], check=True)
    if not (_root / ".env").exists() and (_root / ".env.example").exists():
        (_root / ".env").write_bytes((_root / ".env.example").read_bytes())
        print("created .env from .env.example - put your OPENAI_API_KEY in it", flush=True)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, media, pipeline
from .jobs import Job, store
from .schemas import EditPlan, RefineRequest, RenderOptions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shorts.api")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_JOB_ID = re.compile(r"^[0-9a-f]{12}$")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    store.attach_loop()
    caps = pipeline.ffmpeg_caps()
    caps["ffmpeg"] or log.warning("ffmpeg not found on PATH (set FFMPEG_BIN in .env)")
    caps["subtitles"] or log.warning("ffmpeg has no 'subtitles' filter - captions will fail to render")
    config.OPENAI_API_KEY or log.warning("OPENAI_API_KEY missing - uploads will be rejected until .env is set")
    any(config.STICKERS_DIR.glob("*.webm")) or log.warning("no stickers in assets/stickers - run: python scripts/setup_assets.py")
    log.info("ready: %s | whisper=%s/%s | llm=%s", caps["version"], config.WHISPER_MODEL, config.WHISPER_DEVICE, config.LLM_MODEL)
    yield


app = FastAPI(title="AI Shorts Editor", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


def _job(jid: str) -> Job:
    job = store.get(jid) if _JOB_ID.match(jid) else None
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _read_json(path: Path, what: str) -> Any:
    if not path.exists():
        raise HTTPException(404, f"{what} not available yet")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html", media_type="text/html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ffmpeg": pipeline.ffmpeg_caps(),
        "whisper": {"model": config.WHISPER_MODEL, "device": config.WHISPER_DEVICE, "compute": config.WHISPER_COMPUTE},
        "llm": {"model": config.LLM_MODEL, "key_set": bool(config.OPENAI_API_KEY), "usd_per_1m": config.llm_prices()},
        "limits": {"max_upload_mb": config.MAX_UPLOAD_MB, "allowed": sorted(config.ALLOWED_EXT), "workers": config.MAX_CONCURRENT_JOBS},
        "media": {"stock": media.stock_provider(), "image_source": media.sources("image"), "video_source": media.sources("video"),
                  "image_model": config.IMAGE_MODEL, "video_model": config.VIDEO_MODEL, "two_pass": config.LLM_TWO_PASS},
        "assets": {
            "fonts": sorted(p.name for p in config.FONTS_DIR.glob("*.ttf")),
            "sfx": sorted(p.stem for p in config.SFX_DIR.glob("*.wav")),
            "stickers": sorted(p.stem for p in config.STICKERS_DIR.glob("*.webm")),
        },
    }


# ---------------------------------------------------------------- jobs
@app.post("/api/jobs", status_code=202)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    caption_font: str = Form("montserrat"),
    accent_color: str = Form("#FFD400"),
    enable_stickers: bool = Form(True),
    enable_sfx: bool = Form(True),
    enable_voice: bool = Form(True),
    enable_broll: bool = Form(True),
    enable_zooms: bool = Form(True),
    enable_code: bool = Form(True),
    enable_cleanup: bool = Form(True),
    enable_jumpcuts: bool = Form(False),
    caption_style: str = Form("karaoke"),
) -> dict[str, Any]:
    if not config.OPENAI_API_KEY:
        raise HTTPException(400, "OPENAI_API_KEY is not set - add it to .env and restart the server")
    if int(request.headers.get("content-length") or 0) > config.MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(413, f"file exceeds {config.MAX_UPLOAD_MB} MB")
    name = Path(file.filename or "video").name
    ext = Path(name).suffix.lower()
    if ext not in config.ALLOWED_EXT:
        raise HTTPException(400, f"only {', '.join(sorted(config.ALLOWED_EXT))} files are accepted")
    try:
        opts = RenderOptions(caption_font=caption_font, accent_color=accent_color,  # type: ignore[arg-type]
                             enable_stickers=enable_stickers, enable_sfx=enable_sfx, enable_voice=enable_voice,
                             enable_broll=enable_broll, enable_zooms=enable_zooms, enable_code=enable_code,
                             enable_cleanup=enable_cleanup, enable_jumpcuts=enable_jumpcuts, caption_style=caption_style)  # type: ignore[arg-type]
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e

    safe = (_SAFE.sub("_", Path(name).stem).strip("._")[:60] or "video") + ext
    job = store.create(safe, opts)
    size = 0
    try:
        with job.input_path.open("wb") as fh:  # stream to disk, never buffer 200 MB in RAM
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"file exceeds {config.MAX_UPLOAD_MB} MB")
                fh.write(chunk)
        if size == 0:
            raise HTTPException(400, "empty upload")
        info = await asyncio.to_thread(pipeline.probe, job.input_path)  # reject non-video before queueing
    except HTTPException:
        store.remove(job.id)
        raise
    except pipeline.FFmpegError as e:
        store.remove(job.id)
        raise HTTPException(400, f"not a readable video: {e}") from e
    job.duration = info.duration
    job.log(f"uploaded {job.filename} ({size / 1e6:.1f} MB, {info.width}x{info.height}, {info.duration:.1f}s)")
    store.submit(job, pipeline.process_job, job.id)
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [j.to_dict(log_tail=0) for j in store.all()]


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict[str, Any]:
    return _job(jid).to_dict()


@app.get("/api/jobs/{jid}/plan")
def get_plan(jid: str, original: bool = False) -> Any:
    job = _job(jid)
    data = _read_json(job.work_dir / "plan_ai.json" if original else job.plan_path, "plan")
    if isinstance(data, dict):  # plans written before voice fx / b-roll / zooms existed
        for k in ("voice", "zooms", "broll", "code", "cuts"):
            data.setdefault(k, [])
    return data


@app.post("/api/jobs/{jid}/refine", status_code=202)
async def refine(jid: str, req: RefineRequest) -> dict[str, Any]:
    """'Ask AI to refine': one LLM call revises the current plan (optionally only inside start..end), then media + render."""
    job = _job(jid)
    if job.busy:
        raise HTTPException(409, "job is still running - wait for it to finish")
    if not job.plan_path.exists():
        raise HTTPException(409, "no plan yet - generate first")
    if not config.OPENAI_API_KEY:
        raise HTTPException(400, "OPENAI_API_KEY is not set")
    job.log(f"refine requested: '{req.instruction[:120]}'")
    store.submit(job, pipeline.refine_job, job.id, req.instruction.strip(), req.start, req.end)
    return job.to_dict()


@app.get("/api/jobs/{jid}/media/{name}")
def get_media(jid: str, name: str) -> FileResponse:
    """Thumbnails + picked b-roll files for the editor panel."""
    job = _job(jid)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise HTTPException(404, "not found")
    p = job.work_dir / "media" / name
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})


@app.put("/api/jobs/{jid}/plan", status_code=202)
async def put_plan(jid: str, plan: EditPlan) -> dict[str, Any]:
    """Edited plan -> ASS + render only. No whisper, no LLM call."""
    job = _job(jid)
    if job.busy:
        raise HTTPException(409, "job is still running - wait for it to finish")
    if not job.input_path.exists():
        raise HTTPException(410, "source video is gone - upload again")
    job.log("plan edited in the UI - re-rendering")
    store.submit(job, pipeline.rerender_job, job.id, plan)
    return job.to_dict()


@app.get("/api/jobs/{jid}/output")
def get_output(jid: str, download: bool = False) -> FileResponse:
    job = _job(jid)
    if job.status != "done" or not job.output_path.exists():
        raise HTTPException(404, "output not ready")
    stem = Path(job.filename).stem
    return FileResponse(job.output_path, media_type="video/mp4",
                        filename=f"{stem}_shorts.mp4" if download else None,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{jid}/transcript")
def get_transcript(jid: str) -> Any:
    return _read_json(_job(jid).transcript_path, "transcript")


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str) -> dict[str, Any]:
    job = _job(jid)
    if job.busy:
        raise HTTPException(409, "job is still running")
    store.remove(jid)
    return {"deleted": jid}


if __name__ == "__main__":  # python app/main.py -> assets if missing -> serve + open the browser
    import threading
    import webbrowser

    import uvicorn

    if not (any(config.STICKERS_DIR.glob("*.webm")) and any(config.SFX_DIR.glob("*.wav")) and any(config.FONTS_DIR.glob("*.ttf"))):
        from scripts.setup_assets import ensure_assets

        ensure_assets()
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
