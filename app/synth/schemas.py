"""Pydantic schemas for the two synthesis calls.

These are passed straight to `client.messages.parse(output_format=...)`, so the shape
here *is* the contract with the model — the SDK validates the response against it.
"""
from __future__ import annotations

from typing import Literal

from app.categories import CategoryName
from app.regions import RegionName
from pydantic import BaseModel, Field


# ---------------------------------------------------------------- call 1: facts

class ConsensusFact(BaseModel):
    statement: str = Field(description="A single factual claim, stripped of framing.")
    supporting_source_indices: list[int] = Field(
        description="1-based indices of the sources asserting this. Must have >= 2."
    )


class DisputedClaim(BaseModel):
    statement: str
    asserted_by_source_index: int
    note: str = Field(description="Why this is single-sourced or contested.")


class TimelineEvent(BaseModel):
    when: str = Field(description="Date or relative time as stated in the sources.")
    what: str


class KeyNumber(BaseModel):
    value: str = Field(description="The figure exactly as reported, with units.")
    describes: str
    supporting_source_indices: list[int]


class FactSheetOut(BaseModel):
    headline_summary: str = Field(description="One neutral sentence: what happened.")
    consensus_facts: list[ConsensusFact]
    disputed_claims: list[DisputedClaim]
    key_entities: list[str]
    timeline: list[TimelineEvent]
    numbers: list[KeyNumber]
    editorial_framing_removed: list[str] = Field(
        description="Loaded phrasings found in the sources that were deliberately dropped."
    )


# ------------------------------------------------------- call 2: article/script

class ArticleSection(BaseModel):
    heading: str
    body_markdown: str = Field(description="Markdown. Inline citations as [1], [2].")


class ArticleOut(BaseModel):
    title: str
    slug: str = Field(description="lowercase-hyphenated, <= 60 chars")
    meta_description: str = Field(description="<= 155 characters, for SEO")
    dek: str = Field(description="One-sentence standfirst under the title.")
    sections: list[ArticleSection]
    key_takeaways: list[str] = Field(description="3-5 bullets.")
    why_it_matters: str = Field(description="The analytical take. 2-3 sentences.")
    word_count: int


class ScriptBeat(BaseModel):
    beat: Literal["hook", "conflict", "resolution", "cta"]
    narration: str = Field(description="Spoken words only. No stage directions.")
    on_screen_text: str = Field(description="Short kinetic caption, <= 6 words.")
    broll_query: str = Field(description="2-4 word stock-footage search for this beat.")
    target_seconds: float


class ScriptOut(BaseModel):
    beats: list[ScriptBeat]
    total_words: int
    estimated_seconds: float


class MetadataOut(BaseModel):
    title_variants: list[str] = Field(description="3 alternative headlines for A/B use.")
    target_keywords: list[str]
    hashtags: list[str] = Field(description="Platform-ready, with the leading #.")
    thumbnail_text: str = Field(description="<= 5 words for the video thumbnail.")
    music_mood: str
    story_label: str = Field(
        description="A short NAME for the ongoing story this article belongs to, 2-6 words, "
                    "noun-phrase style, not a sentence: 'OpenAI real-time voice model', "
                    "'Quebec election', 'Nvidia agent safety platform'. Later articles about the "
                    "same subject will share it, so name the subject, not today's development.")
    region: RegionName = Field(
        description="Exactly one edition: Global, Asia, Europe, Americas, Middle East or Africa. "
                    "Global unless the story is clearly about, or mainly affects, one region.")
    category: CategoryName = Field(
        description="Exactly one category: Technology, Politics, Finance, Entertainment, "
                    "Launches or Healthcare. A product launch is Launches even if it is a gadget; "
                    "sports belong to Entertainment; medicine, drugs, outbreaks and health policy "
                    "belong to Healthcare.")


class DraftOut(BaseModel):
    article: ArticleOut
    script: ScriptOut
    metadata: MetadataOut
