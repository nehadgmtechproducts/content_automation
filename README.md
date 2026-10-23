# Automated Content Engine

Watches tech/AI news, detects when a story is genuinely breaking across multiple
outlets, and produces a publishable package: a ~600-word cited article, a 45-second
vertical video script, a hero image, a voiceover, and a rendered 1080×1920 MP4 with
word-synced kinetic subtitles.

**This is milestone 1 — stages 1–4.** Stage 5 (Telegram approval) and stage 6
(Ghost / X / YouTube / Reels / TikTok publishing) are not built; their interfaces are
laid down in `app/publishers/base.py` so they attach without touching anything upstream.

---

## Articles-only mode (current default)

`GENERATE_VIDEO=false` (the default) makes Stage 4 produce **only the article's images** — the
titled `hero.jpg` and the text-free `cover.jpg`. No voiceover, subtitles, b-roll or MP4 is made, so
those fal footage charges, Piper and FFmpeg steps are skipped. Set `GENERATE_VIDEO=true` to bring
video back; nothing else needs to change (the exporter, the website's `SHOW_VIDEOS` switch and the
database already treat video as optional).

- **Telegram review** — with no video to watch, the card is: the **full article as plain text**
  (several messages if long, sources included) → the metadata block → the hero image carrying the
  **Approve / Discard** buttons, last, so they sit right under what is being judged.
- **Export** — `video`/`poster` are written only when `final.mp4` and `hero_vertical.jpg` exist, so
  new stories export with no `video` key and older stories keep theirs.
- **Cover pictures** — each story gets one AI illustration (FLUX.2-klein on NVIDIA's API, ~3 s, same `NVIDIA_API_KEY`), written from the story's own first shot description and screened by the same safety rules as video footage (Politics: no people/crowds, headline withheld). It becomes the website cover and the background of the titled `hero.jpg` card. NVIDIA's content filter sometimes refuses brand/logo wording, so a refusal retries once with a generic scene for the category; if that also fails the local gradient is used, so image generation can never block a story. The exporter records the source (`cover.source`) and the website shows "Illustration generated with AI" under AI covers. Switch off with `AI_IMAGES=false`.
- The script the model writes is still generated (it comes from the same call as the article), but it is not used.
- After changing this, rebuild so the containers pick it up: `docker compose build worker && docker compose up -d`.

---

## Quick start

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m spacy download en_core_web_sm
cp .env.example .env          # add ANTHROPIC_API_KEY and FIRECRAWL_API_KEY
```

Smoke-test the asset pipeline with no database, no API keys and no network:

```bash
.venv/bin/python -m app.cli run-all --fixture
```

Full stack:

```bash
docker compose up -d postgres redis
.venv/bin/alembic upgrade head
docker compose up -d api worker beat n8n
```

n8n is at http://localhost:5678 — import `n8n/content-engine-workflow.json`.
The API is at http://localhost:8000/docs.

---

## The one thing that matters most

The naive spec rule — "3+ sources on the same entity in 30 minutes" — fires constantly
in this vertical. "OpenAI" clears three sources on a dead news day. So the trigger in
`app/detect/velocity.py` is a **spike against each entity's own rolling baseline**, and
all four conditions must hold:

1. ≥ 3 *distinct domains* — not distinct items, because wire copy republished five times
   is one source;
2. ≥ 1 tier-1 original-reporting source;
3. domain count ≥ `baseline_mean + 2σ` (EWMA per entity), once ≥ 8 observations exist;
4. the story hasn't already fired in the last 24 h.

Clustering is semantic (`all-MiniLM-L6-v2`), not entity-only — otherwise every unrelated
OpenAI story of the day merges into one meaningless blob.

**Tune before you trust it:**

```bash
.venv/bin/python -m app.cli detect --dry-run     # what would fire right now, and why
.venv/bin/python -m app.cli backfill --days 3    # replay history; see the fire rate
```

Thresholds live in `config/detect.yaml`.

---

## Pipeline

| Stage | Module | What it does |
|---|---|---|
| 1 Signal detection | `app/ingest/` | Polls RSS, Google News, HN, Reddit every 10 min |
| 2 Velocity trigger | `app/detect/` | NER → semantic clustering → spike scoring |
| 2b Scraping | `app/scrape/` | Firecrawl → trafilatura fallback, clean Markdown |
| 3 Synthesis | `app/synth/` | Claude: fact sheet, then article + script + metadata |
| 4 Asset production | `app/assets/` | Hero image, voiceover, alignment, ASS subs, MP4 |

`Topic.status` drives everything: `detected → scraped → synthesized → assets_ready`.

### Google News attribution

Google News republishes other outlets. If those links stayed on `news.google.com`, three
separate outlets covering one story would collapse into a single domain and the trigger
would never fire. `app/ingest/normalize.py` resolves each item back to its true
publisher via the feed's `<source>` element, a decodable legacy article id, or the
`" - Publisher"` title suffix — in that order.

### Synthesis is two calls, not one

`extract_facts()` produces an auditable fact sheet where **a claim is only "consensus" if
≥ 2 sources assert it** (enforced in code, not just in the prompt). `generate_draft()`
writes from that sheet alone. Then `validate_grounding()` rejects the draft if it
contains any figure absent from the sheet, and regenerates once. That guards the one
failure mode that actually matters in automated news: a confidently invented number.

Provider is chosen by `SYNTHESIS_PROVIDER` — `fal` (default; one `FAL_KEY` also powers
video, model `FAL_LLM_MODEL`, default `openai/gpt-6.1-sol`), `anthropic`, or `openai`.
All three share the same prompts, schemas and grounding validator, so switching never
changes what "grounded" means. **fal's scripting path is not yet verified live** — its
account balance was exhausted, so no real completion has run (see "Verified state").

---

## Providers: free by default, paid when you want

Every external capability is an adapter with a **free local default**, chosen in
`config/providers.yaml`. Change a value, restart — no code change.

| Capability | Default (no key) | Paid upgrade |
|---|---|---|
| Hero image | `pillow_card` — typeset editorial card | `replicate_flux` |
| Voiceover | `piper` (pip package, voice `en_US-ryan-high`) → macOS `say` | `elevenlabs` |

| Word timestamps | `faster-whisper` small/int8 → proportional estimate | — |
| B-roll | `fal` — footage generated per beat by Gemini Omni Flash 1.1 (~$0.03/output second, ~$0.75–1.35 per video) → `pexels` (free) → hero card + Ken Burns | — |
| Scraping | `firecrawl` → `trafilatura` | — |

A paid adapter selected without its key logs a warning and degrades to the free one
rather than crashing.

**Voiceover length is measured, not assumed.** A 45-second script is only 45 seconds if
the voice cooperates, and rates differ per provider. `_synthesize_to_target()` measures
the result and re-synthesizes once at a corrected pace if it misses by more than 12%.
Without it the fixture script came out at 34.9s against a 45s target.

Piper installs from pip (`piper-tts==1.8.0`, wheels for macOS arm64 and Linux aarch64).
Download a voice once: `python -m piper.download_voices en_US-ryan-high --download-dir models/piper`.
(The old 1.2 package needed `piper-phonemize`, which had no Mac wheel — that no longer applies.)

---

## Verified state

Everything below was run end-to-end on this machine.

| Stage | Status | Evidence |
|---|---|---|
| 1 Ingest | working | 13/15 sources OK, 427 items collected live |
| 2 Detect | working | 338 clusters from 427 items, **1** fired — a real multi-outlet story |
| 2b Scrape | working via trafilatura | 2 articles pulled; Firecrawl path needs your key |
| 3 Synthesis | **untested** | needs `ANTHROPIC_API_KEY` |
| 4 Assets | working | 41.8s 1080×1920 MP4, word-synced captions, verified frame-by-frame |
| Tests | 58 passing | `pytest tests -q`, all offline |

The one live fire was *"Seattle Times and Newsday sue OpenAI and Microsoft"* across
techcrunch.com, theverge.com and winbuzzer.com — while 337 other clusters were correctly
held back. That is the quality bar the trigger was built for.

### Known source issues

- **Reddit** rate-limits unauthenticated RSS hard. With a 7s per-host delay, 2 of 4
  subreddits succeed per poll from a consumer IP; expect better from a VPS on a 10-minute
  cadence. Reddit is tier-3 and can never fire a topic alone, so partial coverage is not
  a correctness problem.
- **VentureBeat** returns 429 to this bot UA from this IP. Left in the config because it
  may behave differently from your server; the source-health table makes it visible.
- **ZDNet** was removed — its RSS 404s on every documented URL.

### fal.ai (scripting + video) — needs a funded balance

One `FAL_KEY` covers Stage 3 and Stage 4. The key authenticates, but fal refuses every
run with `403 User is locked. Reason: Exhausted balance` until credit is added at
**fal.ai/dashboard/billing**. Until then neither scripting nor generated B-roll can run
(B-roll fails soft to the hero card; scripting fails with a message naming the fix).
Unit tests cover request shapes against fake transports — they are not proof fal behaves
that way live. Generated clips cap at 10 s, so a longer beat holds its last frame.

## Rendering requires a containerized FFmpeg

**Your Homebrew FFmpeg cannot burn subtitles.** It's built without libass/freetype —
`ass`, `subtitles` and `drawtext` are all missing:

```bash
ffmpeg -hide_banner -filters | grep -w ass    # no output on the host
docker compose run --rm render ffmpeg -hide_banner -filters | grep -w ass   # present
```

`app/assets/render.py::check_ffmpeg()` fails loudly rather than shipping a
subtitle-less video, and both Dockerfiles assert the filter exists at build time.

So that the acceptance test runs on this machine, set `RENDER_IN_DOCKER=1` and build the
small helper image once — the renderer then delegates to it automatically, mounting the
media tree at the same absolute path so the generated command runs unchanged:

```bash
docker build -t content-engine-ffmpeg -f docker/ffmpeg.Dockerfile .
echo "RENDER_IN_DOCKER=1" >> .env
python -m app.cli run-all --fixture
```

Two details that were only findable by looking at rendered pixels:

- **Captions must not blink.** Chunk timings straight from Whisper leave a gap in every
  inter-phrase pause — 11 gaps totalling ~5s in a 42s video, which reads as broken
  rendering. Each chunk now holds until the next begins (`MAX_HOLD_SECONDS`), while a
  genuine silence still clears the screen.
- **`WrapStyle` must be 0.** With `WrapStyle: 2` libass does no automatic wrapping, so a
  caption wider than the frame runs off both edges instead of breaking onto a second line.

Composition: hard cuts between beats (not xfade — it needs exact offset arithmetic
across a variable input count and breaks noisily on short beats; hard cuts are also the
right editorial choice at this length). Beat visuals are timed from the **aligned word
timestamps**, so the picture cuts on the narration rather than on a script estimate.
Ken Burns via `zoompan` on stills, `loudnorm` to −14 LUFS, x264 CRF 20, `+faststart`.

---

## Host constraints this repo is built around

- **8 GB RAM** → Whisper pinned to `small`/int8, worker concurrency 1, n8n capped at 512 MB.
- **41 GB free disk (91% full)** → `MEDIA_RETENTION_DAYS` (default 14) with a nightly
  prune task. Not optional. Run `python -m app.cli prune` any time.
- **arm64** → every compose service pins `platform: linux/arm64`; an amd64 image would
  run under QEMU and make renders ~5× slower with no warning.
- **Python 3.11**, not 3.14 — spaCy, faster-whisper and sentence-transformers wheels lag
  the newest interpreter on arm64.

---

## Commands

```bash
python -m app.cli ingest                 # stage 1, with per-source health
python -m app.cli detect --dry-run       # stage 2, explains every decision
python -m app.cli backfill --days 3      # replay history to tune thresholds
python -m app.cli scrape  --topic <id>   # stage 2b
python -m app.cli synth   --topic <id>   # stage 3 (needs ANTHROPIC_API_KEY)
python -m app.cli assets  --topic <id>   # stage 4
python -m app.cli produce --topic <id>   # stages 2b→4
python -m app.cli run-all --fixture      # offline end-to-end smoke test
python -m app.cli topics                 # pipeline state
python -m app.cli export-web --topic <id> # approved topic -> news site (needs WEB_PROJECT_DIR)
python -m app.cli check-render           # can this ffmpeg burn subtitles?
python -m app.cli prune                  # enforce media retention
pytest tests -q                          # offline unit tests
```

**Freshness is judged by publish time.** Detection ignores any item whose outlet-published
time (falling back to fetch time only when a feed gives none) is older than
`freshness.max_age_hours`, so a 3-day-old story fetched today can't fire as "breaking".
`export-web` only exports `approved`/`published` topics (override: `--allow-unapproved`).

## Categories (not just tech)

The engine covers six categories, defined once in `app/categories.py`: **Technology,
Politics, Finance, Entertainment** (including sports), **Launches** (cars, bikes, phones,
gadgets) and **Healthcare** (medicine, drugs and vaccines, outbreaks, research, health policy).
Sources live in `config/feeds.yaml` (59 feeds, 10 of them healthcare). The drafting step must pick
exactly one category (enforced by the output schema, so the model can't invent labels), and
the exporter maps legacy labels onto the list or refuses to publish.

- **Per-category editorial rules** are in the drafting prompt (`DRAFT_SYSTEM`): politics
  attributes every claim and never predicts or endorses; finance never gives advice or price
  forecasts; launches attributes specs and prices to the manufacturer; healthcare attributes
  every medical claim to the study, journal or regulator, says what kind of evidence it is,
  never claims a cure or gives medical advice, and does not confuse correlation with causation.
- **Visual safety (`app/assets/safety.py`).** Generated footage is photoreal, so for
  Politics **and Healthcare** any shot showing people, soldiers, crowds, flags, weapons,
  violence, patients, doctors, injections or bodies is replaced with a symbolic people-free one
  (a gavel for politics; a stethoscope or lab bench for healthcare), and the headline is
  withheld from the footage prompt. The same shot description feeds the AI cover picture.
- **Adding a category** means changing `app/categories.py` (name, slug, what it covers, synonyms,
  visual-safety flag), the `CategoryName` literal beside it (a test checks they match), the
  editorial rule in `app/synth/engine.py`, a fallback scene in `app/providers/image_nvidia.py`,
  sources in `config/feeds.yaml`, and the website's `src/core/categories.ts` and colour.
  Prompt rules are a request; this is the code-level backstop.
- **Cost cap.** `trigger.max_topics_per_day` (default 8, rolling 24h, counts manual topics
  too) stops a busy news day from auto-producing dozens of ~$1.50 videos. `detect --dry-run`
  applies it, so dry-run output matches production.
- The web site's `src/core/categories.ts` must mirror `NAMES` here.

**Stories (timelines).** `app/stories.py` groups follow-up reports: after a draft is written, it joins a recent story or starts a new one, recording `story_key` / `story_label` in the draft's metadata (no migration). The label is the AI's short name for the ongoing subject (`story_label` in the draft). Matching is local and free (the detector's MiniLM embeddings) and was calibrated on real headline pairs: follow-ups scored 0.58-0.83, different stories about the same company 0.11-0.31, but two unrelated "AI agent permissions" stories 0.52, so a draft joins a story only if it has the **same entity and similarity >= 0.45**, or **any entity and >= 0.75**, within 7 days. Grouping never blocks production: any failure just makes the draft its own story. The exporter writes `story: {key, label}`; drafts that predate stories form a story of their own.


**Regions (website editions).** `app/regions.py` is a second closed list: Global, Asia, Europe, Americas, Middle East, Africa. The drafting step must pick exactly one per story (enforced by the output schema), and the exporter writes it as `region`. Drafts that predate regions export as `Global`, which shows in every edition, so no old story is hidden; an invalid region blocks the export. The web project's `src/core/regions.ts` must mirror this list.

- Logging never prints the Telegram bot token (httpx URL logging is forced to WARNING).

## Website images and file names

Stage 4 makes three still images per story, and the exporter publishes them to the website:

| File (in `media/<id>/`) | Website name | Used for |
|---|---|---|
| `cover.jpg` (1600x900, **no text**) | `cover-<hash>.jpg` -> `heroImage` | The page cover. The website draws the headline over it as real HTML, so it is selectable and searchable. Made by `generate_cover()` in `app/providers/image_pillow.py`. |
| `hero.jpg` (1200x675, headline printed on it) | `social-<hash>.jpg` -> `socialImage` | Link previews (Open Graph / Twitter) only. |
| `hero_vertical.jpg` + `final.mp4` | `poster-<hash>.jpg`, `video-<hash>.mp4` | The video and its poster. |

- **Hash in the filename.** Next.js caches resized images by URL, and browsers/CDNs cache videos by URL, so overwriting a file at the same URL keeps serving the old one. A new content hash is a new URL, so a regenerated cover or re-rendered video can never go stale. Superseded files are deleted only *after* the new JSON is written, so the live page never points at a missing file.
- **Old stories** rendered before covers existed get one generated at export time from their entity.
- **The Docker image bakes in the code.** After changing the exporter, run `docker compose build worker && docker compose up -d` or the `approvals` container will keep publishing with the old exporter.

---

## Before stage 6

Start these now — they're the long pole and nothing in this milestone unblocks them:

- **TikTok** Content Posting API requires an *audited* app. Until audited, uploads land
  as private drafts only.
- **Instagram Reels** requires a Business account plus Meta app review.
- **YouTube** Data API v3 default quota allows roughly 6 uploads/day.
- **X** free tier caps posting hard; Basic is ~$200/month.
