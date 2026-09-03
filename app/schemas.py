"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

Pydantic models. EditPlan is the LLM structured-output schema AND the editable
plan.json the frontend PUTs back, so every field is required (OpenAI strict mode)
except `sub`, whose None default the SDK strips.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SfxKind = Literal["whoosh", "pop", "ding", "boom", "riser", "click", "cash", "notification", "drum", "bass_drop", "glitch",
                  "typing", "swoosh_out"]
StickerKind = Literal["question_marks", "user_question", "money", "fire", "warning", "arrow_down", "check", "cross", "wow",
                      "subscribe", "new", "tip", "hundred", "free", "like", "clock", "rocket", "idea", "laugh", "code", "star",
                      "omg", "save", "stop", "wait"]
StickerPos = Literal["top", "top_left", "top_right"]
NeonColor = Literal["red", "green", "blue"]


class Caption(BaseModel):
    text: str = Field(description="2-4 spoken words, exactly as transcribed, UPPERCASE")
    start: float = Field(description="start of the first word (seconds, from transcript)")
    end: float = Field(description="end of the last word (seconds, from transcript)")
    emphasis: bool = Field(description="true for punch words - rendered bigger + accent colour")


class SFX(BaseModel):
    at: float = Field(description="seconds - lands on a beat / word start")
    kind: SfxKind


class Sticker(BaseModel):
    kind: StickerKind
    start: float
    end: float
    position: StickerPos = Field(description="top zone only; never over captions")


class Headline(BaseModel):
    main: str = Field(description="product / topic name, 1-3 words, UPPERCASE")
    sub: str | None = Field(default=None, description="optional one-line subtitle under the headline")
    start: float
    end: float


class NeonBadge(BaseModel):
    text: str = Field(description="1-3 word warning / key point, UPPERCASE")
    start: float
    end: float
    color: NeonColor = Field(description="red = warning, green = positive, blue = info")


VoiceEffect = Literal["boost", "excited", "deep", "soft"]


class VoiceFx(BaseModel):
    """Delivery effect on the speaker's own voice for one moment (gain + EQ, applied by ffmpeg)."""
    start: float
    end: float
    effect: VoiceEffect = Field(description="boost = louder punch line · excited = louder + brighter hype · "
                                            "deep = warmer + bassier serious line · soft = quieter intimate line")


BRollKind = Literal["image", "video"]
BRollMode = Literal["full", "pip"]
Motion = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "none"]


class BRollReq(BaseModel):
    """What the LLM asks for: a stock-search query + timing + how to show it."""
    query: str = Field(description="2-5 ENGLISH stock-footage keywords for what the speaker is talking about, no brand logos, no names of people")
    kind: BRollKind = Field(description="video for actions / atmosphere, image for products, objects, screens, places")
    mode: BRollMode = Field(description="full = full-screen cutaway hiding the face 1.5-3s, pip = card above the face 2-4s")
    motion: Motion = Field(description="zoom_in default, pan_left/pan_right for wide scenes, zoom_out for reveals")
    start: float
    end: float


class MediaAsset(BaseModel):
    """The actual file the resolver picked for a BRoll (lives in work/<job>/media/)."""
    provider: str
    id: str
    kind: BRollKind
    file: str
    thumb: str = ""
    page_url: str = ""
    credit: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0


class BRoll(BRollReq):
    """Editable form: request + resolved asset. skip = 'swap' counter (pass over N earlier candidates)."""
    asset: MediaAsset | None = None
    skip: int = 0


ZoomKind = Literal["punch", "in", "out"]


class Zoom(BaseModel):
    """Camera move on the speaker. punch = snap to `scale` at `at`, back in 0.4s (end ignored).
    in = slow push-in from 1.0 to `scale` between at..end, then release. out = start tight at `scale`, pull back to 1.0 by end."""
    at: float
    end: float = Field(description="punch: at+0.4 · in: 2-5s after at · out: 1-3s after at")
    scale: float = Field(description="punch 1.12-1.25 · in/out 1.15-1.4")
    kind: ZoomKind


CodeLang = Literal["python", "javascript", "typescript", "php", "sql", "bash", "html", "css", "json", "generic"]


class CodeBlock(BaseModel):
    """Code window above the face, typed out character by character, while the speaker explains code."""
    title: str = Field(description="file name or short label, e.g. main.py or terminal")
    language: CodeLang
    code: str = Field(description="3-8 REAL, correct lines, max 44 characters per line, newline separated")
    start: float
    end: float


class Cut(BaseModel):
    """Remove start..end from the final video (dead air, false starts, manual trims). Everything else stays in sync."""
    start: float
    end: float


class RefineRequest(BaseModel):
    """'Ask AI to refine' - an instruction, optionally limited to a time range."""
    instruction: str = Field(min_length=3, max_length=1000)
    start: float | None = None
    end: float | None = None


class _PlanBase(BaseModel):
    hook: str = Field(description="punchy hook text shown at the top for the first 3s, max 6 words")
    captions: list[Caption]
    sfx: list[SFX]
    stickers: list[Sticker]
    headlines: list[Headline]
    badges: list[NeonBadge]
    voice: list[VoiceFx] = Field(description="voice delivery effects, aligned to caption chunks, never overlapping")
    zooms: list[Zoom] = Field(description="camera moves on the speaker: punch / in / out")
    code: list[CodeBlock] = Field(description="code windows when the speaker explains code, a command or an API; empty otherwise")
    cuts: list[Cut] = Field(description="segments to remove (silences over 1s between words, false starts); empty when cuts are disabled")
    music_mood: str = Field(description="one short phrase describing the background music mood")


class EditPlanLLM(_PlanBase):
    """Strict structured-output schema - only what the model should decide."""
    broll: list[BRollReq] = Field(description="stock image / video cutaways for the things the speaker mentions")


class EditPlan(_PlanBase):
    """Stored / editable plan.json - same plus resolved media."""
    broll: list[BRoll]


class RenderOptions(BaseModel):
    """Per-job style options from the upload form."""
    caption_font: Literal["montserrat", "anton", "impact"] = "montserrat"
    accent_color: str = Field(default="#FFD400", pattern=r"^#[0-9A-Fa-f]{6}$")
    enable_stickers: bool = True
    enable_sfx: bool = True
    enable_voice: bool = True
    enable_broll: bool = True
    enable_zooms: bool = True
    enable_code: bool = True
    enable_cleanup: bool = True  # background noise reduction + voice compression
    enable_jumpcuts: bool = False  # cut silences over 1s automatically
    caption_style: Literal["karaoke", "chunk"] = "karaoke"  # karaoke = current word lights up


class Usage(BaseModel):
    """LLM token usage + estimated costs, surfaced in the UI."""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    media_calls: int = 0
    media_cost_usd: float = 0.0


def empty_plan() -> EditPlan:
    return EditPlan(hook="", captions=[], sfx=[], stickers=[], headlines=[], badges=[], voice=[], zooms=[], code=[], cuts=[],
                    broll=[], music_mood="")
