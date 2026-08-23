# Creative Automation Pipeline

A campaign brief goes in. An organised folder of on-spec, checked social
creatives comes out — every product, every market, every aspect ratio — from
as few generative calls as the brief actually requires.

```
brief.yaml
   │
   ├─ plan ─────────  cost the run before spending anything
   ├─ pre-flight ───  refuse copy that can never clear legal   (0 credits)
   ├─ resolve ──────  reuse the team's asset ▸ reuse cache ▸ generate
   ├─ compose ──────  one master ▸ N aspect ratios, localized message, logo
   ├─ check ────────  brand + legal + spec, measured on the rendered pixels
   └─ report ───────  organised output, JSON manifest, screen-shareable HTML
```

---

## What this covers

Every requirement, and the code and test that carries it.

| Requirement | Where | Proved by |
|---|---|---|
| Brief in YAML, **two or more products** | `campaigns/aurora-spring.yaml` · `pipeline/brief.py` | `test_brief_loads_and_expands`, `test_brief_rejects_single_product` |
| Target region / market | `Market.region` — a required field, not optional | `test_brief_loads_and_expands` |
| Target audience | `Market.audience` — likewise required | `test_brief_loads_and_expands` |
| Campaign message | `Market.message`, falling back to `default_message` | `test_brief_loads_and_expands` |
| **Reuse** input assets when present | `pipeline/assets.py` · `AssetResolver` | `test_existing_asset_is_never_generated`, `test_cache_prevents_a_second_generation` |
| **Generate** when missing, via a GenAI model | `AssetResolver` · `pipeline/providers/{cloudflare,gemini,firefly,mock}.py` | `test_generation_count_is_per_product_not_per_variant` |
| Three or more aspect ratios | `aspect_ratios:` in the brief · `compose.crop_to_ratio` | `test_crop_hits_exact_delivery_dimensions` |
| Campaign message on the final creative | `compose.Composer` · rule `SPEC-002` | `test_clean_creative_passes_with_no_findings` |
| Localized message *(the "plus")* | `pipeline/localize.py` · `font_for` — ja-JP ships in Japanese | `test_japanese_resolves_a_font_that_can_draw_japanese` |
| Runs locally | `run.py` (CLI) · `app.py` (local app) — no credentials needed | `test_mock_provider_needs_no_credentials` |
| Output organised by product and ratio | `runner.output_path_for` | `test_full_run_produces_every_deliverable` |
| Documentation | this file | — |
| *Bonus* — brand compliance | `checks.py` · `BRAND-001` palette, `-002` logo, `-003` clearspace | `test_missing_logo_blocks_and_routes_to_brand`, `test_palette_tolerance_survives_a_jpeg_round_trip` |
| *Bonus* — legal content checks | `checks.py` · `LEGAL-001`, run pre-flight **and** per creative | `test_prohibited_term_blocks_and_routes_to_legal` |
| *Bonus* — logging / reporting | `runner.JsonLogger` → `run.log.jsonl` · `report.write_report` → `report.html` · `manifest.json` | — |

---

## Run it — the app

Double-click **`start.bat`** (Windows) or **`start.sh`** (macOS/Linux). It
checks the three dependencies, starts a local server and opens
<http://127.0.0.1:8765>.

From there: pick a brief, press **Plan** to see what a run would cost before
spending anything, edit the brief, then **Run pipeline** and watch it move
through the flow canvas.

The brief has two tabs. **Form** gives you fields — products, markets with a
locale picker that fills in the region, aspect ratios with presets for the
specs people actually order, and the prohibited-term list. **YAML** is the
file itself. The form is a *view*: every edit regenerates the YAML, and the
Save button still writes that text, so you can always read exactly what the
file is about to become before you commit to it. That matters here, because
serialising a brief drops the comments the shipped sample is full of.

The parsing happens server-side with the same PyYAML the pipeline imports —
the browser only ever emits. Two parsers would eventually disagree about what
a brief says, and the one being edited would be the wrong one. Emitting is
hand-rolled and quotes every string: a message containing a colon, a `#` or a
leading `%` is perfectly good ad copy and completely invalid bare YAML.

The canvas is a node graph in the style of n8n: it shows where the image comes
from (the creative team's asset on disk, the cache, or a generative call), the
single master each product resolves to, and then every transformation applied
per deliverable — crop, scrim, localized message, logo, measure, gate. Nodes
light amber while working and carry a live count; pipes animate while data is
flowing down them; a path that never fires stays dashed and grey, so you can
see at a glance that (for example) the cache did not hit on a cold run.

**It is driven by the pipeline's own events** — the same records written to
`run.log.jsonl`, fed to the browser through `/api/progress`. Nothing on the
canvas is on a timer, so it cannot show a stage that did not happen. Finished creatives appear as a gallery with their verdicts,
and the full report is one click away.

The app is a thin layer — it calls the same `load_brief`, `preflight_brief`
and `run_campaign` functions the CLI calls, so the demo and the tool cannot
drift apart. It is built on Python's standard-library HTTP server: no Flask,
no npm, nothing to install beyond the three packages the pipeline already
needs.

> Sharing it: zip the folder. Anyone with Python 3.10+ can run it, offline,
> with no credentials.

## Run it — the command line

No API key required. The default provider renders real pixels offline and
deterministically, so a reviewer can clone and run this in under a minute.

```bash
pip install -r requirements.txt

python run.py plan campaigns/aurora-spring.yaml    # what would this cost?
python run.py run  campaigns/aurora-spring.yaml    # produce the creatives
open output/aurora-spring-2026/<run-id>/report.html
```

Expected result on the sample brief:

```
18 creatives from 1 generative call(s)
pass 15   review 3   block 0
```

*(Both images in `campaigns/assets/` were generated for this demo — there is
no client photography in this repo. In a real engagement that folder holds the
creative team's approved shots, which is precisely why the pipeline reuses
whatever it finds there rather than regenerating it.)*

*(That is the offline default. The same brief against a real image model is
below, and lands `pass 17  review 1` — the numbers differ because the images
differ, which is the honest reason.)*

**18 deliverables, 1 generative call.** 2 products × 3 markets × 3 ratios = 18
files. One product already has an approved image on disk, so it is reused; the
other is generated once at master resolution and every ratio is composed from
it. This is the core cost decision in the repo and it is enforced by a test.

**The three reviews are the point, not noise.** They are one finding, on one
product, in one ratio:

```
velvet-matte-lip__en-US__9x16   BRAND-001  minor
velvet-matte-lip__ja-JP__9x16   BRAND-001  minor
velvet-matte-lip__de-DE__9x16   BRAND-001  minor
    20% of dominant colours are on palette; off-palette:
    #b6aebe (Δ133), #b3aabb (Δ143), #b0a6b8 (Δ147)
```

The product with a real photograph on disk passes in every ratio. The
*generated* one drifts off the brand palette — and only at 9:16, the tallest
crop, the one showing the least product and the most background. Nothing is
blocked and nothing is auto-approved; a person is asked. This is the argument
for a gate that measures the rendered pixels rather than trusting the
generator, and it is the difference between an asset a brand team approved and
one a model produced.

Run the tests:

```bash
python tests/test_pipeline.py        # 23 tests, no pytest needed
```

### Using a real image model

```bash
export CLOUDFLARE_ACCOUNT_ID=...  CLOUDFLARE_API_TOKEN=...
python run.py run campaigns/aurora-spring.yaml --provider cloudflare

export GEMINI_API_KEY=...
python run.py run campaigns/aurora-spring.yaml --provider gemini

export FIREFLY_CLIENT_ID=...  FIREFLY_CLIENT_SECRET=...
python run.py run campaigns/aurora-spring.yaml --provider firefly
```

Nothing else in the pipeline changes. That symmetry is the point of the
adapter — see *Key design decisions* below.

A real run, committed to [`examples/cloudflare-run/`](examples/cloudflare-run)
so you can see the output without holding a key:

```
provider  cloudflare        model  @cf/leonardo/phoenix-1.0
18 creatives from 1 generative call(s)      4.1s
pass 17   review 1   block 0
```

**On the default model, and a trap worth knowing about.** The Cloudflare
adapter used to default to `@cf/black-forest-labs/flux-1-schnell`, which
refuses this brief: every call for the lipstick product returns
`400 Input prompt contains NSFW content` on an ordinary cosmetics prompt.
That is 8 refusals out of 8 — and 8 out of 8 again after rewording the subject
to remove anything a classifier could reasonably object to, so it is not a
prompt you can write your way around.

The trap is that the same error text also appears when you send that model a
*parameter it does not accept*. A schema problem and a moderation problem are
reported identically, which is a good way to spend an afternoon fixing the
wrong thing. The default is now `@cf/leonardo/phoenix-1.0`: 8/8 on the same
prompt, and it honours `seed` — two runs at a fixed seed returned
byte-identical images, which is what lets this repo claim the same brief
regenerates the same pixels. Both facts were measured against the live
endpoint rather than read off a documentation page.

---

## Example input

`campaigns/aurora-spring.yaml` (abridged):

```yaml
campaign:  {id: aurora-spring-2026, name: Aurora Spring Refresh, brand: Aurora}
default_message: "Your skin, wide awake."

products:
  - id: hydra-glow-serum      # asset exists on disk  -> reused, no credit spent
    asset: campaigns/assets/hydra-glow-serum.png
    subject: "a frosted glass serum dropper bottle with a matte white cap"
  - id: velvet-matte-lip      # asset missing          -> generated once
    subject: "a slim matte lipstick bullet, cap off, angled upright"

markets:
  - {locale: en-US, region: North America, audience: "...", message: "Your skin, wide awake."}
  - {locale: ja-JP, region: Japan,        audience: "...", message: "肌が、目を覚ます。"}
  - {locale: de-DE, region: DACH,         audience: "...", message: "Wach für deine Haut."}

aspect_ratios:
  - {id: "1:1",  width: 1080, height: 1080, channel: "Instagram feed"}
  - {id: "9:16", width: 1080, height: 1920, channel: "Stories / Reels"}
  - {id: "16:9", width: 1920, height: 1080, channel: "YouTube / display"}

prohibited_terms: [clinically proven, guaranteed, miracle, cures, anti-aging]
```

## Example output

```
output/aurora-spring-2026/20260821-170138/
├── report.html                 # screen-shareable run report, thumbnails inlined
├── manifest.json               # every variant, verdict, finding, colour, font
├── run.log.jsonl               # one structured line per event
├── hydra-glow-serum/
│   ├── 1x1/   hydra-glow-serum_en-US_1x1.jpg   (+ ja-JP, de-DE)
│   ├── 9x16/  hydra-glow-serum_en-US_9x16.jpg  (+ ja-JP, de-DE)
│   └── 16x9/  hydra-glow-serum_en-US_16x9.jpg  (+ ja-JP, de-DE)
└── velvet-matte-lip/
    └── … same structure
```

Organised by product, then aspect ratio, with the locale in the filename — so
a reviewer can see all three languages of one spec side by side rather than
opening three folders.

---

## Key design decisions

**1. Generate once per product, compose per spec.**
The naive pipeline generates product × market × ratio images. This one
generates one master per product that lacks an asset, then crops, resizes and
composes every deliverable locally. On the sample brief that is 1 call instead
of 18. At Firefly Services' documented 4 requests/minute, a wasted call is a
wasted minute — so the pipeline decides what *not* to generate before it
decides what to generate. (`pipeline/assets.py`, `pipeline/compose.py`)

**2. The generator sits behind an adapter.**
The pipeline never calls an image API. It calls `Provider.generate()` and gets
PNG bytes. Three adapters ship: a deterministic offline `mock`, `gemini`, and
`firefly` written against Firefly Services' real v3 async API. Consequences: a
reviewer runs the whole thing with no credentials; swapping vendors is a flag,
not a refactor; and the interesting engineering stops being hidden behind an
HTTP call. (`pipeline/providers/`)

**3. Compliance is measured on the render, not the brief.**
Whether the logo is present, whether the headline is legible at this spec,
whether the palette is on brand — these are properties of pixels. A checker
that reads the YAML and pronounces the creative compliant will go green while
the artwork is wrong. Every check reads measurements taken from the composed
file. (`pipeline/compose.py` returns measurements; `pipeline/checks.py` judges
them)

**4. A rule that always fires is worse than no rule.**
`BRAND-001` does not test exact hex equality. Any gradient, resize or JPEG
round-trip moves every pixel off-swatch, so exact matching flags 100% of
assets and the report gets ignored within a week. It measures perceptual
(redmean) distance to the nearest approved swatch, with a tolerance the brand
team owns — not engineering. There is a test that pins this.

**5. Nothing is auto-approved, and nothing fails open.**
Output is a sorted queue for humans plus a remediation list. Blockers and
majors are different things: wrong delivery dimensions block; a headline
slightly under the legibility floor routes to Creative. And if a rule *raises*,
the asset is flagged for review — never passed. Fail-open is the one
unacceptable failure mode in a compliance tool.

**6. Localization is a font problem before it is a language problem.**
The brief already carries each market's copy, written by someone who speaks
the language. The failure mode that actually ships is Pillow silently
rendering tofu when the chosen face has no CJK glyphs. `pipeline/localize.py`
resolves a font family across platforms, verifies it can draw *every*
character in the string, and falls back until one can — and raises rather than
producing a creative full of empty boxes.

**7. Reproducibility is a compliance property.**
Seeds are derived from the variant id, not random. Six months from now
somebody will ask why an asset looks the way it does; the same brief must
regenerate the same pixels.

---

## What it checks

| Rule | Checks | Severity | Routes to |
|---|---|---|---|
| `SPEC-001` | Delivered pixels match the requested spec | blocker | engineering |
| `SPEC-002` | Campaign message present | blocker | creative |
| `SPEC-003` | Message type size at or above the legibility floor | major | creative |
| `BRAND-001` | Dominant colours within the approved palette (redmean tolerance) | minor | brand |
| `BRAND-002` | Logo present | blocker | brand |
| `BRAND-003` | Logo clearspace meets the brand minimum | minor | brand |
| `LEGAL-001` | No prohibited terms in campaign copy | blocker | legal |
| `SYS-00x` | A check could not complete / composer warning | major | engineering |

`LEGAL-001` also runs **pre-flight**, against the brief, before a single
generative credit is spent.

---

## Assumptions and limitations

Stated rather than hidden, because most of these are where the real work would
go next.

- **The prohibited-term list is a placeholder.** A real programme needs a list
  per market, ratified by that market's legal lead — not machine-translated
  from English. Substring matching also has no notion of context; "not
  clinically proven" would flag. Real systems need phrase-level rules.
- **Logo detection is presence-by-construction, not recognition.** The check
  knows the logo is there because the composer placed it. It does not verify a
  logo in a supplied asset. That needs template matching or a small detector.
- **Subject-aware cropping is a heuristic**, not a saliency model. It measures
  tonal deviation to find the busy band of the frame and biases upward because
  product shots sit above centre. It fails towards centre, which is safe but
  not clever. A saliency or segmentation model is the obvious upgrade.
- **Composition is Pillow.** In production a regulated or brand-governed
  client composes into their own approved template — Photoshop API against a
  PSD master, or InDesign server — because brand teams do not accept "the tool
  laid out the ad." The layout code here is deliberately isolated so that
  swapping it is one module.
- **Thresholds are illustrative.** `palette_tolerance` and
  `min_message_height` are proxies for judgements Brand Standards owns.
  Engineering owns the fact that they are enforced identically on every asset.
- **Mock output is synthetic.** It renders plausible product scenes so every
  downstream stage gets real pixels, but it is not a generative model. Use
  `--provider gemini` or `--provider firefly` to see real generations.
- **No DAM integration.** Assets come from a local folder. Connecting to a
  customer DAM (or Azure/S3/Dropbox) is a resolver implementation behind the
  same interface as `AssetResolver`.
- **Single-threaded.** Generation is rate-limited so concurrency buys little;
  composition is fast enough that it has not been worth it. A worker pool
  behind the existing `RateLimiter` is the path if a brief grows to thousands
  of variants.

---

## Repo map

```
start.bat / start.sh         one-click launcher for the local app
app.py                       local web app (stdlib http.server, no framework)
webui/index.html             the app's front end, incl. the live flow canvas
run.py                       CLI: plan / run / providers
pipeline/
  brief.py                   parse + validate a brief, expand to variants
  assets.py                  reuse ▸ cache ▸ generate; the cost decision
  providers/
    base.py                  Provider protocol + token-bucket rate limiter
    mock.py                  deterministic offline renderer (default)
    gemini.py                live Google image API
    firefly.py               Adobe Firefly Services v3 async
  localize.py                cross-platform font resolution + glyph coverage
  compose.py                 crop, scrim, message, logo — returns measurements
  checks.py                  brand / legal / spec rules + pre-flight
  report.py                  self-contained HTML run report
  runner.py                  orchestration, structured logging, manifest
tests/test_pipeline.py       23 tests, runnable without pytest
tools/make_placeholders.py   regenerates the committed logo and input asset
```

---

## Requirements

Python 3.10+, `pillow`, `pyyaml`, `requests`. No API key needed for the
default run.
