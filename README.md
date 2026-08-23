# FDE Social Content Agentic Automation & Analytics

**Douglas McKay** · <doug@dougmckay.info> · take-home exercise for Adobe
Firefly Services, Forward Deployed Engineer (Creative AI)

A campaign brief goes in. An organized folder of on-spec, checked social
creatives comes out — every product, every market, every aspect ratio — from
as few generative calls as the brief actually requires. Then performance goes
back in: the Analytics tab turns channel history and market trends into the
next brief's prompt.

```
brief.yaml
   │
   ├─ plan ─────────  cost the run before spending anything
   ├─ pre-flight ───  refuse copy that can never clear legal   (0 credits)
   ├─ resolve ──────  reuse the team's asset ▸ reuse cache ▸ generate
   ├─ compose ──────  one master ▸ N aspect ratios, localized message, logo
   ├─ check ────────  brand + legal + spec, measured on the rendered pixels
   └─ report ───────  organized output, JSON manifest, screen-shareable HTML
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
| **Storage** for generated / transient assets | `pipeline/storage/` · `local.py` (always) + `s3.py` (mirror) | `test_local_storage_round_trips_and_refuses_to_escape`, `test_sigv4_matches_the_published_aws_vector`, `test_a_storage_failure_does_not_lose_the_run` |
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

The app is four tabs — **Brief**, **Analytics**, **Pipeline**, **Results** —
with Run and the status light in the top bar rather than in any one of them,
because starting a run is the one thing you want to do from wherever you are.
The tabs follow the work: pressing Run switches to Pipeline, and finishing
switches to Results, which carries a live count of deliverables while you are
looking at something else. `1`–`4` switch panes from the keyboard. This was
one long scroll until a run produced five markets, at which point the stage
strip you want to read and the deliverables you want to check it against were
two screens apart.

It is themed on Adobe's Spectrum dark palette with the platform's own type —
SF on macOS, Segoe on Windows, no webfont fetched, since this is meant to run
on a laptop with no network. Dark is not a preference here: a light chrome
around a photograph biases how the photograph reads, which is why Photoshop,
Premiere and Lightroom all look like this, and this app's whole job is showing
you creatives and asking whether they pass.

Inside the Brief tab are two views. **Form** gives you fields — products, markets with a
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
canvas is on a timer, so it cannot show a stage that did not happen.

Finished creatives land in the Results tab
— one row per market, in the order the markets appear in the brief, each row
carrying that market's own line and its own count of what needs review. A flat
grid of eighteen files reads as eighteen unrelated files; the unit a regional
lead actually cares about is "everything going out in my market", so that is
the unit on screen. Every thumbnail is letterboxed into the same band, which
lines the captions up and lets you compare the three *crops* side by side.

The rows fill in **while the run is going**, newest on the left. That is fed
by the pipeline's own `variant` event rather than by a timer, so a tile cannot
appear for a creative that does not exist yet — and only unseen ids are drawn
on each poll, because re-rendering the gallery every tick restarts every image
load and makes the page flicker. The full report is one click away.

### Providers and keys

The dropdown holds two kinds of entry, and they do different things. A
provider that **can** run is a choice. One that cannot is not a choice at all
— a greyed-out row is a dead end that makes you wonder what you did wrong — so
it appears under *Connect a provider* as **+ Add Adobe Firefly API…**, and
picking it opens the key panel with that provider's first empty field focused
instead of selecting something that would fail several seconds into a run.

It defaults to the best provider that works: `cloudflare` when it is
configured, `mock` when nothing is — so a reviewer who cloned this two minutes
ago still gets a working default, and somebody with a key gets the real model
without remembering a flag.

**API keys** opens a panel that writes credentials into `.env`. Three things
make that defensible rather than reckless: the server binds `127.0.0.1` only;
the key names are checked against a registry, so it writes the variables the
adapters read and refuses anything else; and a value goes in and never comes
back out — no route returns a credential, and the panel is only ever told
whether one is set.

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
    20% of dominant colors are on palette; off-palette:
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
python tests/test_pipeline.py        # 38 tests, no pytest needed
```

### Storage

The brief lists Storage as a data source alongside user inputs and GenAI:
somewhere to keep generated or transient assets. In a real engagement that is
never a question of *which* vendor — it is whichever one the client already
pays for — so the pipeline talks to a `Storage` and the class is chosen by a
flag, exactly like the image providers.

**It happens by itself.** If the environment has S3 credentials, every run
mirrors — no flag. A configured backend that sits idle because nobody
remembered `--storage s3` is a footgun, not a safety feature. `--storage local`
still opts out, and with no credentials the fallback is local, so a reviewer
who cloned this two minutes ago still gets a complete run.

```bash
python run.py run campaigns/aurora-spring.yaml                  # mirrors if it can
python run.py run campaigns/aurora-spring.yaml --storage local  # deliberately not
```

```
s3://creative-automation-doug/public/pMiWtaE3oz-H29KJ2apJgIy51hH1dnUE/
    hydra-glow-serum/1x1/hydra-glow-serum_en-US_1x1.jpg
    ...
    manifest.json
```

Verified against real AWS: **19 objects, 18 creatives plus the manifest, 0
errors**, and each result carries both its `stored_uri` and a `share_url`.

#### The link you can actually send someone

`s3://bucket/key` is an identifier, not a URL — paste it in a browser and
nothing happens. There are only two honest ways to fix that, and this repo does
both, choosing per object:

| Where the object is | Link it gets | Lifetime |
|---|---|---|
| `public/<token>/…` | plain `https://…` | permanent |
| anywhere else | SigV4 query-signed | expires (7 days max) |

The public prefix is protected by **obscurity that is actually strong enough to
carry the weight**: 24 bytes from `secrets.token_urlsafe` — 32 characters,
about 190 bits — generated per run, and the bucket policy denies `ListBucket`,
so the prefix cannot be enumerated. `secrets`, not `random`: `random` is a
Mersenne Twister seeded from the clock, and observing a few outputs recovers
its state and therefore every other run's token.

The bucket configuration is deliberately *mixed*, not "public":

```
BlockPublicAcls        true    no object can be exposed by ACL, ever
IgnorePublicAcls       true    and any existing public ACL is ignored
BlockPublicPolicy      false   one policy may grant public read
RestrictPublicBuckets  false   and it is honoured

policy:  s3:GetObject  on  arn:aws:s3:::<bucket>/public/*   — and nothing else
```

Only the two policy flags move, so the single policy above is the *only* route
to public access; there is no second mechanism by which a stray upload can
expose itself. Applying it is a separate, explicit command that dry-runs by
default — a pipeline that quietly relaxes Block Public Access the first time it
wants a link is one you cannot let near a client's account:

```bash
python tools/make_public.py              # print exactly what would change
python tools/make_public.py --yes        # apply it
python tools/make_public.py --revoke --yes
```

**Say the trade out loud.** A permanent unguessable link is right for work a
reviewer opens three weeks later, and it is *not revocable* — anyone who has
ever seen the link keeps access until the objects are deleted. That is why
masters, logs and anything not meant for an outside reader stay outside the
prefix and get signed, expiring links instead.

**It mirrors, it does not replace.** The task also requires outputs saved to a
folder organised by product and aspect ratio, so the local tree is written
either way and the backend receives a copy. Which means a failed upload is
worth reporting and is *not* worth discarding eighteen finished, already
checked files — there is a test that breaks the backend deliberately and
asserts the run still completes, the folder is still full, and every failure is
recorded in the manifest.

**The manifest goes up too.** A bucket full of creatives with no record of
which brief, model and seed produced them is an archive nobody can audit,
which is the opposite of the reason to put them there.

**No boto3.** This repo has three dependencies and the point of that is that
you can read all of it; pulling in an SDK larger than the rest of the codebase
to make four HTTP calls is the wrong trade. SigV4 is about seventy lines of
hashing, and writing it out shows the protocol instead of hiding it behind
`client.put_object`. Because a hand-rolled signature that is subtly wrong is
indistinguishable from a bad key — a 403 that says nothing, in front of
whoever you are demoing to — it is tested against **AWS's own published worked
example**, canonical request and final signature, byte for byte.

The same class speaks to **Cloudflare R2, MinIO, Backblaze B2 and DigitalOcean
Spaces** by setting `S3_ENDPOINT`, because they are all SigV4 over path-style
URLs. That is the payoff of the adapter: "which object store" stays a
procurement decision instead of becoming a code decision.

### Handing it in

```bash
python tools/make_submission.py
```

Writes `creative-automation-submission.zip` next to the repo: the whole tree
plus `.env`, minus `output/`, `.cache/`, `.git/` and scratch. A reviewer
unzips it and runs against the real model with no setup — verified by
unpacking into an empty directory and running only the commands this README
prints: `25/25 passed`, then `18 creatives from 1 generative call(s)`.

**Why a zip and not a commit.** The credential is a Cloudflare *user API
token*, which is why it starts with `cfut_` — Cloudflare publishes that prefix
so scanners can recognise it, and they are a GitHub secret-scanning partner:
a token pushed to a public repository is **revoked automatically**. Committing
it would not just be careless, it would arrive broken. So the repository stays
keyless and the key travels out of band. Scope it to `Account · Workers AI ·
Read`, and roll it once the review is done.

Without a key the zip still runs — the offline provider needs no credentials —
and `examples/cloudflare-run/` is a committed real run either way.

### Seeing what the pipeline did

The results view is a panel per market: a tab per aspect ratio labelled with
the **channel** from the brief, a strip showing what every compose stage did to
the selected creative, and the market's deliverables with verdict and score.
Clicking a deliverable re-points the strip at it.

```
cut to spec  →  legibility  →  market copy  →  brand mark  →  measure
[thumbnail]     [thumbnail]    [thumbnail]     [thumbnail]    [thumbnail]
bias   0.45     height  0.42   type    0.085   width   0.16   colours …
margin 0.07     strength 0.745 leading 1.28    clear   0.5
                falloff  1.6   lines       2   achieved 1.6
69ms            20ms           20ms            26ms           45ms
```

**Every number there is the number the code ran on.** Those constants used to
be literals scattered through `compose()` — `0.42` here, `190` there, `1.6`
inside a loop. That was fine until a panel started claiming "scrim strength
0.745", because a panel reporting a *copy* of a value is decoration pretending
to be instrumentation. They are named constants now, and the stage reports read
from the same ones the compositor uses.

`compose()` also writes a 420px JPEG after each stage into `<run>/_stages/`. A
progress graph tells you a stage ran; a thumbnail tells you what it **did** —
which is the only way to see that the scrim landed in the wrong place or the
crop ate the cap off a bottle.

The score beside each verdict is `compliance_score()`: 100 less 35 per blocker,
12 per major, 4 per minor. It exists because eighteen thumbnails need a way to
say "this one first" and a verdict only has three values. It measures
conformance to the rules in `checks.py` and **is not a quality judgement** — a
perfectly on-brand, on-spec, entirely boring creative scores 100 — so it never
appears without the verdict next to it.

### Analytics — and the loop back into the brief

The exercise lists five business goals. Four are about producing creative
faster and more consistently; the fifth is *"learn what content, creative and
localization drives the best business outcomes"*. That one is a loop, not a
report: performance should decide what you make **next**.

> **Every figure on the Analytics tab is synthetic.** Nothing connects to
> Google, Meta, TikTok or YouTube. It is generated from a fixed seed so it is
> identical on every machine, and it is labelled as sample data in four
> places — the module docstring, the API payload, a standing banner, and a
> chip in the toolbar and in every post detail.

The tab is organised by **social channel**, because the four do not behave
alike and averaging them together throws away the only thing worth knowing.
Each channel names the API a real integration would call:

| Channel | Headline metric | Real endpoint |
|---|---|---|
| Google Analytics | Sessions | GA4 Data API · `runReport` |
| Facebook / IG | Reach | Graph API · `insights` edge |
| TikTok | Views | Business API · integrated report |
| YouTube | Views | YouTube Analytics API |

Four vendors returning four shapes for the same idea, all per-market, all
rate-limited — which is exactly the argument for an adapter layer like
`providers/` and `storage/`. `pipeline/insights.py` is deliberately shaped
like one.

**Two sources feed each channel.**

*Internal — what we posted.* A 28-day **calendar**, one thumbnail and one
number per post, click any post for the full detail. A calendar rather than a
table because posting is periodic and so are the questions: a sortable table
answers *which post won*, a calendar answers *what were we doing*. It is
padded to start on a Monday, or weekday-versus-weekend is invisible and you
have built a table with extra steps. Engagement and CTR are
impression-weighted — a 6% rate on 900 views must not outrank 2% on 900,000,
and un-weighted averages are how dashboards end up celebrating the smallest
sample they have.

*External — what the market is doing.* Trending terms per market with a
week-over-week velocity, a **virality meter**, where the audience is and who
they are. The virality score is normalised velocity, not volume: a term
everyone already uses is not a trend.

**They meet in one suggested surface prompt**, with the evidence listed and
labelled by source. When the two agree, confidence is `high`. When they
disagree the tab says `CONFLICT` out loud and the external signal wins —
our own history can only rank treatments we have already tried, so a
disagreement is usually a gap in our sample rather than a finding.

From there the loop closes in two clicks:

| Button | What it does |
|---|---|
| **Render one sample** | One creative — one product × one market × one ratio — through the *same* resolver, composer and checks a real run uses. Written to `.cache/samples/`, never `output/`. |
| **Adopt into brief** | Writes the surface prompt into every product, turns on `regenerate_surface` so the prompt actually reaches the model, and reorders the placements. |

One sample, not eighteen: "act on a suggestion" has to mean *see it* first,
and a full run is eighteen deliverables and two paid calls. Trying a
suggestion should cost about what looking at it is worth.

Placements **reorder, never drop** — removing a placement because four weeks
of data disliked it is the kind of over-fitting a media team would rightly
refuse. Nothing is written to disk until you press Save, so an adoption you
dislike costs one brief reload.

### Uploading and reusing product images

The brief's first data source is *"campaign briefs and assets uploaded
manually"*, and its first requirement is to **reuse those assets when
available**. Both halves need a way in that is not "type a relative path from
memory and hope you spelled it right", so each product card has three:

- **Upload image…** — puts a new photograph on disk and points the product at it
- **Choose existing…** — a grid of everything already uploaded, with thumbnails
  and dimensions. This is the *reuse* half, and reuse only happens if you can
  see what you already have
- **Drag and drop** onto the product thumbnail, because that is what people try
  first

All three land in the same place: `products[i].asset` gets a path, the card
repaints, and the mode switch unlocks the two positions that need a photograph.

Uploads go to `campaigns/assets/` — the same folder the sample brief already
points at, because a file someone dropped in by hand and a file uploaded
through the app are the same kind of thing, and giving uploads their own
directory would create two places to look for one concept.

**It is base64 in JSON, not multipart.** Multipart would mean hand-parsing
boundaries in a stdlib HTTP handler (`cgi` is deprecated and gone in 3.13), and
a subtly wrong parser corrupts binary in ways that surface much later as "the
image looks funny". Base64 costs 33% on the wire, for a couple of megabytes,
over localhost. That is a good trade here and a bad one at scale — worth
stating rather than leaving implied.

Four checks, in this order, because the cheap ones should not be paid for by
the expensive one:

| Check | Why |
|---|---|
| Extension against an allow-list | Cheapest possible rejection |
| Size, before decoding | Decoding first would allocate the thing you are about to refuse |
| **Pillow can actually open it** | The extension is a claim; only decoding is evidence |
| Name is a sanitised basename | `../../../.env.png` becomes `env.png` and stays in the folder |

A filename that already exists is **never overwritten** — it becomes `-2`, `-3`.
Two people photographing two products both produce `product.png`, and losing
the first to the second is data loss wearing a convenience costume.

Uploaded inputs are mirrored to object storage too, under a **private**
`assets/` prefix rather than the shareable one — a bucket holding the outputs
but not the source they descend from is only half an archive, and someone
else's product photography is not a deliverable to hand out.

### Keep the approved product, change the world around it

Reuse used to be all-or-nothing: a product either had a photograph on disk and
was used exactly as shot, or it had none and was invented whole. That is a
false choice for the thing this pipeline is *for*. Marketing has one approved
shot and needs it on volcanic rock this month and marble the next — and
re-shooting is the cost the whole exercise exists to remove.

```yaml
products:
  - id: "hydra-glow-serum"
    asset: "campaigns/assets/hydra-glow-serum.png"   # approved, never regenerated
    subject: "a frosted glass serum bottle with a white dropper cap"
    surface: "volcanic black rock with soft water droplets and warm light"
    regenerate_surface: true
```

or pick **Photo + new surface** on that product's card in the app. The approved
photograph goes to the model as a *reference image*; only the scene changes.

Each product card carries a three-position switch, and it has three positions
because the resolver has exactly three behaviours — no state the UI can express
that the pipeline cannot do, and none it can do that the UI cannot reach:

| Switch | Brief | `master_origin` | Model calls |
|---|---|---|---|
| Photo as shot | *(nothing)* | `brief` | 0 |
| Photo + new surface | `regenerate_surface: true` | `resurfaced` | 1 |
| Generate product | `regenerate_product: true` | `generated` | 1 |

`regenerate_product` is the per-product form of `--regen`, and it is expressed
by *ignoring* the asset path rather than blanking it — switching back to the
photograph must not mean typing the path in again. A mode that cannot work
(no file on disk; a provider that cannot take a reference image) is disabled
with the reason on the control, rather than offered and then failing several
seconds into a run.
The bottle is never described to a model and rebuilt from words, which is the
point — a model does not get to reinvent a bottle legal signed off on.
`master_origin` records it as `resurfaced`, distinct from `brief` and
`generated`, so the manifest never blurs the three.

**The first implementation was wrong, and how it was wrong is the interesting
part.** It was classical: flood-fill inward from the four corners, call
whatever the fill reaches background, paste the remainder onto a generated
scene. Sixty lines, no API. It rested on an assumption that sounds reasonable
and is false — that an approved product asset is a studio shot on a clean
backdrop. Measured against the real asset in this repo:

```
corner (0,0)        (213, 204, 189)     warm grey, lit
corner (1023,1023)  ( 59,  82,  90)     near-black wet stone
border sample, R    5 .. 217            the full range, not a flat field
```

It is a finished photograph — wet stones, droplets, a reflection. There is no
flat region to fill. The fill stopped at 31% of the frame, `getbbox()` on the
resulting alpha returned `(0, 0, 1024, 1024)` — the whole image — and the
"cutout" pasted a rectangular slab of the original background over the new
scene. No tolerance value fixes that, because the premise was never true.

What matters is not that it failed but that it failed **silently**: every
function returned a plausible value, nothing raised, and the only signal was
the picture looking wrong. So the replacement is built to make that class of
mistake loud. A provider either declares `supports_edit` and is asked to do the
whole job, or the run stops with a message naming what to switch to. It never
falls back to plain generation — that would hand back an invented product
under a banner reading *your approved photograph, new surface*, and wrong-and-
confident is worse than stopped.

| Provider | Reference-image editing |
|---|---|
| `cloudflare` | ✅ FLUX.2 (`flux-2-klein-9b` default, `-4b`, `-dev`) |
| `gemini` | ✅ `gemini-2.5-flash-image` ("nano banana") |
| `mock` | ✅ offline stand-in, so tests cover the path |
| `firefly` | ❌ this adapter is text-to-image only |

**Why klein-9b and not dev.** Same reference, same prompt: `flux-2-dev` 37.5s,
`flux-2-klein-9b` 2.7s. Fourteen times faster, and klein held the product
identity at least as well. A pipeline whose selling point is turning one hero
into many deliverables cannot afford 37s per product when 2.7s buys the same
thing.

Everything here was established by probing the live endpoint, because the
published schema for these models is a bare `multipart{}` object that says
nothing:

```
prompt + input_image_0 + steps + width + height  -> 200
+ seed=12345, twice                              -> byte-identical
+ seed=99999                                     -> a different image
1024 / 1280 / 1440 / 1600 square                 -> 200
2048 square                                      -> 500, not 400
```

That last line is why the size clamp exists in code rather than in the
degrade-on-400 retry: an oversize request fails with a **500**, which the retry
path never sees, so the run would die on a number that could have been rounded
down before it was ever sent.

The good news in the second line: unlike `flux-1-schnell`, the edit path keeps
seeded reproducibility, so the repo's central claim — same brief, same seed,
same pixels — does not have to be qualified here.

**A resurface is a paid call, and the report says so.** It counts toward
`generative_calls` exactly like a from-scratch generation. Counting only
`generated` under-reported spend by one per resurfaced product, and the entire
argument this pipeline makes is about how few model calls it takes — which is
worth nothing if the number is flattering.

### Regenerating on purpose

Reuse is the default and it is the cost argument this repo is built on. While
you are *iterating on a prompt*, though, it is exactly the wrong behaviour:
edit a product's `subject` and press Run, and nothing changes — because an
asset on disk short-circuits the resolver before the prompt is ever built, and
a cache hit answers the next attempt with the previous picture.

```bash
python run.py run campaigns/aurora-spring.yaml --provider cloudflare --regen
```

or tick **Regenerate every run** in the app. It ignores both the asset on disk
and the cache, so every product is generated fresh from its current `subject`
and `surface`.

**It also has to move the seed, and that is the part worth understanding.**
The provider honours the seed — two calls at a fixed seed return byte-identical
images, which is measured, and is the whole basis for "the same brief
regenerates the same pixels". So regenerating *without* changing the seed
spends a real generative call to receive the picture you already had, and looks
from the outside like the flag does nothing. Under `--regen` the seed is salted
with the run id: it still lands in the manifest, so any image can be reproduced
exactly later, but it differs run to run. Verified — two consecutive forced
runs of the same brief produced **24 different deliverables out of 24**.

The cost is honest about itself: `--regen` spends one call per product on every
run, and the summary says so.

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

Organized by product, then aspect ratio, with the locale in the filename — so
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
| `BRAND-001` | Dominant colors within the approved palette (redmean tolerance) | minor | brand |
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

- **The prohibited-term list is a placeholder.** A real program needs a list
  per market, ratified by that market's legal lead — not machine-translated
  from English. Substring matching also has no notion of context; "not
  clinically proven" would flag. Real systems need phrase-level rules.
- **Logo detection is presence-by-construction, not recognition.** The check
  knows the logo is there because the composer placed it. It does not verify a
  logo in a supplied asset. That needs template matching or a small detector.
- **Subject-aware cropping is a heuristic**, not a saliency model. It measures
  tonal deviation to find the busy band of the frame and biases upward because
  product shots sit above center. It fails towards center, which is safe but
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
webui/index.html             the app's front end — four panes, one file
run.py                       CLI: plan / run / providers
pipeline/
  brief.py                   parse + validate a brief, expand to variants
  assets.py                  reuse ▸ cache ▸ generate; the cost decision
  resurface.py               keep the approved photo, rebuild the scene
  providers/
    base.py                  Provider protocol + token-bucket rate limiter
    mock.py                  deterministic offline renderer (default)
    cloudflare.py            Workers AI — FLUX.2 (edit) and Phoenix
    gemini.py                live Google image API
    firefly.py               Adobe Firefly Services v3 async
  storage/
    base.py                  Storage protocol + the public prefix
    local.py                 always on; the run folder itself
    s3.py                    hand-rolled SigV4 mirror + share tokens
  localize.py                cross-platform font resolution + glyph coverage
  compose.py                 crop, scrim, message, logo — returns measurements
  checks.py                  brand / legal / spec rules + pre-flight
  insights.py                synthetic channel history → a suggested prompt
  report.py                  self-contained HTML run report
  runner.py                  orchestration, structured logging, manifest
tests/test_pipeline.py       38 tests, runnable without pytest
tools/make_placeholders.py   regenerates the committed logo and input asset
tools/make_public.py         scopes the S3 bucket policy to public/ (dry-run)
```

---

## Requirements

Python 3.10+, `pillow`, `pyyaml`, `requests`. No API key needed for the
default run.
