# Study guide — defending this codebase

*Not part of the deliverable. This is yours, for the 30-minute presentation.*

The brief says the evaluation includes whether the solution *"demonstrates a
clear understanding of the code."* So the goal is not to have memorised it —
it is to be able to say, for any file they point at, **what it does, why it is
built that way, and what you would do differently at scale.**

Every section below has the same four parts:

- **In plain words** — the version you'd say to the CMO.
- **Technically** — the version you'd say to the IT/Engineering skeptic.
- **Why it's built this way** — the decision, and the alternative you rejected.
- **If they ask** — the question they will actually ask, and your answer.

---

## Part 0 — The 90 seconds that frame everything

Open with this. It sets the terms of the whole conversation, and it is the
difference between "he built a script" and "he understands the problem."

> "The exercise says generate variants for campaign assets. The trap in that
> sentence is that the obvious implementation generates one image per
> deliverable. Two products, three markets, three aspect ratios is eighteen
> deliverables — so the obvious pipeline makes eighteen generative calls.
>
> Mine makes two.
>
> One product already has an approved photograph. That bottle is never
> re-invented — it goes to the model as a reference image and only the surface
> around it is rebuilt, because marketing needs it on volcanic rock this month
> and marble next month, and re-shooting is the cost the whole exercise exists
> to remove. The other product has no asset, so it is generated once at master
> resolution. Every market and every aspect ratio is then composed from those
> two masters locally.
>
> That matters because generation is the only slow, expensive, rate-limited
> step in the whole system. Firefly Services documents four requests a minute
> on a default entitlement. At four a minute, a wasted call is a wasted
> minute. So the interesting engineering isn't calling the API — it's deciding
> what *not* to send it."

Then run `python run.py plan` on screen. It prints `generative 2` next to
`deliverables 18` before anything is spent. That single line does most of your
persuading for you — and note the ratio is the point, not the number: add a
fourth market and it is 24 deliverables for the same 2 calls, because markets
and ratios are free and only products cost anything.

**Numbers to have cold:**

| | |
|---|---|
| Deliverables from the sample brief | 18 (2 products × 3 markets × 3 ratios) |
| Generative calls | 2 (one resurfaced, one generated) |
| Products whose approved photo is preserved | 1 |
| Tests | 38, no pytest required |
| Rules in the gate | 8 (`SPEC-001..003`, `BRAND-001..003`, `LEGAL-001`, `SYS-00x`) |
| Firefly default rate limit | 4 requests/minute |
| Resurface latency (flux-2-klein-9b vs dev) | 2.7s vs 37.5s |
| Share token | 24 bytes → 32 chars, ~190 bits, from `secrets` |
| Objects mirrored per run | 19 (18 creatives + manifest), 0 errors

---

## Part 0.5 — The two newest pieces, and the story to tell about them

These are the freshest code in the repo, so they are the most likely to be
probed. Both are worth telling as *stories about being wrong*, because that is
what a Forward Deployed Engineer actually does all day.

### Resurfacing — "keep the bottle, change the world"

**In plain words.** Marketing has one approved photo of the product. They need
it on volcanic rock this month and marble next month. Re-shooting costs money
and weeks. So: hand the approved photo to the model as a *reference*, and ask
it to change only the surface. The product is never re-invented from a text
description — a model does not get to redesign a bottle legal signed off on.

**Technically.** `Product.regenerate_surface` turns on step 0 of
`AssetResolver.resolve()`. `pipeline/resurface.py` builds the prompt and sizes
the reference (Workers AI caps reference images at 512×512), and
`provider.edit(EditRequest(...))` does it in one call. `EditRequest` is a
separate type from `GenerationRequest` **on purpose** — if the reference were
just an optional field on the generate path, a provider that cannot edit would
silently ignore it and return a freshly invented bottle labelled as the
approved one.

**The story to tell.** Lead with the failure, not the fix:

> "I built the obvious thing first — flood-fill from the corners, cut the
> product out, paste it on a generated scene. Sixty lines, no API. Then I ran
> it on the actual asset and got a rectangular slab of the original background
> pasted over the new scene. I measured why: the corners are (213,204,189) and
> (59,82,90), the border red channel runs 5 to 217. It's a finished
> photograph — wet stones, droplets, a reflection — not a studio shot on a
> clean backdrop. The fill covered 31% and the alpha bbox came back as the
> entire frame.
>
> The premise was wrong, so no tolerance value was going to fix it. But the
> part that actually bothered me is that it failed *quietly* — every function
> returned a plausible value, nothing raised, and the only signal was that the
> picture looked wrong. So the replacement is built so that same class of
> mistake is loud: a provider either declares it can take a reference image, or
> the run stops and names what to switch to. It never falls back to plain
> generation, because handing back an invented product under the words 'your
> approved photograph' is worse than stopping."

**If they ask "why not a segmentation model?"** Because Workers AI does not
host one — checked, 64 models, no segmentation task — and because reference
conditioning makes the cutout *unnecessary* rather than merely better: one call
replaces generate-plus-cut-plus-composite, and the model relights the product
to match the new scene, which no paste operation can do.

**If they ask "how do you know flux-2 keeps the seed?"** Because the published
schema for it is a bare `multipart{}` object that says nothing, so I probed it:
same seed twice → byte-identical; different seed → different image. Same
discipline that caught `flux-1-schnell` rejecting `seed` while documenting it.

**Point at:** `pipeline/resurface.py`, the module docstring — the measurements
are in it.

---

### Automatic mirroring and the shareable URL

**In plain words.** Everything a run makes goes to S3 automatically, and each
file gets a link you can actually open in a browser. The link contains a long
random string, and the bucket cannot be listed — so the link works for anyone
you send it to, and cannot be found by anyone you don't.

**Technically.** Three separate decisions, and it is worth separating them:

1. **Automatic.** `default_storage()` mirrors `default_provider()`: if the
   environment has credentials, use them. A configured backend that sits idle
   because nobody passed `--storage s3` is a footgun, not a safety feature.
2. **Unguessable.** `secrets.token_urlsafe(24)` per run — 32 characters, ~190
   bits. `secrets`, not `random`: `random` is a Mersenne Twister seeded from
   the clock, and a few observed outputs recover its state and therefore every
   other run's token.
3. **Scoped.** The bucket policy grants `s3:GetObject` on `/public/*` and
   nothing else. `ListBucket` stays denied, so the prefix cannot be
   enumerated. The ACL block flags stay ON, so a policy is the *only* route to
   public access — there is no second mechanism a stray upload could use.

**The line that shows judgement.** Say the trade out loud before they ask:

> "A permanent unguessable link is right for something a reviewer opens three
> weeks later. It is also *not revocable* — anyone who has ever had the link
> keeps access until I delete the object. So masters, logs, anything not meant
> for an outside reader stays outside that prefix and gets a signed, expiring
> link instead. And flipping the bucket setting is a separate command that
> dry-runs by default, because a pipeline that quietly relaxes Block Public
> Access the first time it wants a link is one you cannot let near a client's
> account."

**If they ask "why not just presigned URLs?"** AWS caps them at seven days with
long-term IAM credentials. Fine for internal review, wrong for anything handed
over. Both exist; `share_url()` picks per object so no caller has to know the
rules.

**If they ask "isn't this security through obscurity?"** Yes, and that is a
legitimate design when the obscurity is 190 bits and enumeration is denied at
the policy layer. The thing that makes it illegitimate — a guessable or
listable namespace — is exactly what the token and the `ListBucket` denial
remove.

**Point at:** `pipeline/storage/s3.py` (`share_url`, `is_public_key`) and
`tools/make_public.py`, whose docstring is the whole argument.

---

## Part 1 — The five decisions you must be able to defend

If you remember nothing else, remember these. Everything else in the repo is
implementation detail hanging off them.

### Decision 1 — Generate once per product, compose per spec

**In plain words.** Making the picture is the expensive part. Cropping and
resizing it is free. So make the picture once and cut it up locally, instead
of asking the AI for the same product three times at three shapes.

**Technically.** `AssetResolver.resolve()` is called once per *product* in
`runner.run_campaign()`, outside the variant loop. It returns a `MasterAsset`
at 1600×1600. The variant loop then calls `Composer.compose()`, which crops
that master to each target aspect ratio and resizes to exact delivery pixels.
Generation is O(products); composition is O(products × markets × ratios).

**Why it's built this way.** The alternative — ask the model for 1080×1920
directly — costs a generation per spec, and gives you three images of the same
product that don't match each other. Composing from one master also
guarantees visual consistency across the ratio set, which is a brand
requirement, not just a cost one.

**If they ask: "wouldn't a native 9:16 generation look better than a crop?"**
Sometimes, yes — and that is a real trade. My answer: crop from a master for
the long tail, and reach for generative expand (Firefly's Crop and Expand, or
`fill`) for the ratios where the crop loses too much. The architecture already
supports that: it's another call behind the same provider interface. What I
won't do is pay per-spec generation *by default*, because at 4 rpm that is the
difference between a campaign that ships this week and one that ships next
month.

**Point at:** `pipeline/runner.py` — the `for p in brief.products` loop is
above the `for v in brief.variants()` loop. That vertical distance is the
whole optimisation.

---

### Decision 2 — The generator lives behind an adapter

**In plain words.** The pipeline doesn't know or care which AI makes the
image. Swapping from Google to Adobe is one word on the command line.

**Technically.** `pipeline/providers/base.py` defines a `Provider` protocol
with one method: `generate(GenerationRequest) -> GenerationResult`. Three
implementations: `mock` (offline, deterministic), `gemini`, `firefly`. The
registry in `providers/__init__.py` imports each adapter inside a `try/except`
so a missing dependency or missing key can never stop `mock` from loading.

**Why it's built this way.** Three payoffs, in order of how much they matter
in this interview:

1. **Reviewability.** You can clone this repo and run the full pipeline in
   under a minute with no credentials. Nothing is stubbed except the vendor
   call — the composition, the checks, the report all run on real pixels.
2. **Portability.** When a client already pays for Adobe, moving them onto
   Firefly is a flag, not a project.
3. **Honesty.** The interesting work is reuse logic, cost control, composition
   and compliance. Hiding those behind an HTTP call would be hiding the
   product.

**If they ask: "so you didn't actually use Firefly?"**
Straight answer: I don't have Firefly Services enterprise credentials, so the
demo runs on the offline provider. But `providers/firefly.py` is written
against the real v3 async API, not a guess — and I can walk you through the
four things in it that you only know from having been bitten:

- The synchronous generate endpoint was removed; it's async only, and several
  published tutorials still show the old one.
- The 202 response hands you a **per-tenant shard host**, not
  `firefly-api.adobe.io`. You follow the returned `statusUrl` verbatim — if
  you rebuild the URL from the base host, it 404s.
- `x-api-key` is your **client id**, not a separate key.
- `customModelId` is **v3 only**, and needs `x-model-version: image4_custom`.
  If a brand custom model matters more than the newest base model, you stay on
  v3. That's an architectural trade, not a preference.

That answer converts a gap into a demonstration of depth. Do not apologise for
it.

**Point at:** `pipeline/providers/firefly.py`, the module docstring.

---

### Decision 3 — Compliance is measured on the render, not the brief

**In plain words.** We check the finished picture, not the instructions that
made it. Otherwise the tool says "all good" while the ad is wrong.

**Technically.** `Composer.compose()` returns a `Composition` dataclass that
carries measurements taken from the rendered file: the actual cap height of
the drawn type, the pixel area of the text block, the logo's bounding box and
its available clearspace, and the five dominant colours quantised from the
saved image. `checks.evaluate()` consumes only those measurements.

**Why it's built this way.** Legibility is a property of the render. The same
28px headline is fine on a 1080×1080 and marginal on a 1920×1080, because the
floor is a fraction of the *short edge*. A brief-level check can't see that.
This is the same principle as fair balance in regulated advertising — it's a
property of pixels, and checking it against the copy deck is how tools end up
green while the asset is non-compliant.

**If they ask: "why not just validate the input?"**
Because input validation catches typos, and I do that too — that's
`brief.py`. But it can't catch the interesting failures: type that shrank to
fit, a crop that pushed the logo off the safe area, a generated background
that dragged the palette off brand. Those only exist after composition.

**Point at:** the `Composition` dataclass in `pipeline/compose.py`, then
`evaluate()` in `pipeline/checks.py` — note it never touches the brief except
for the prohibited-term list.

---

### Decision 4 — A rule that always fires is worse than no rule

**In plain words.** If the brand-colour check flags every single image,
everyone stops reading the report within a week, and then you have no check at
all.

**Technically.** `BRAND-001` computes **redmean** distance from each dominant
colour to the nearest approved swatch, and flags only if coverage drops below
`min_palette_coverage`. Redmean weights the RGB channels by the average red
level — a cheap perceptual approximation that's close enough to CIE76 for a
brand tolerance and needs no colour-science dependency. Exact hex matching
fails because a gradient, a resize or a JPEG round-trip moves every pixel off
swatch.

**Why it's built this way.** A compliance tool's real failure mode isn't
missing a violation — it's crying wolf until the team routes around it. The
tolerance is a config value in `brandkit/brand.yaml` because **Brand Standards
owns that number, engineering owns the fact that it's applied identically to
every asset.** That sentence is worth saying out loud in the room; it's the
line that makes brand people relax.

**If they ask: "how did you pick 120?"**
I didn't, defensibly — it's tuned so an approved swatch that's been through a
JPEG encode still passes and an obviously off-brand accent fails. There's a
test that pins exactly that (`test_palette_tolerance_survives_a_jpeg_round_trip`).
In a real engagement the brand team sets it by running it against a corpus of
assets they've already approved and one they've already rejected, and moving
the number until the tool agrees with them.

**Point at:** `redmean_distance()` in `pipeline/checks.py`, and the two paired
tests — one proving it doesn't fire on a JPEG round-trip, one proving it does
fire on magenta.

---

### Decision 5 — Nothing is auto-approved, and nothing fails open

**In plain words.** The tool never says "this ad is approved." It says "these
twelve are clean, these two need a human, this one can't ship." A person still
signs off.

**Technically.** Three verdicts — `PASS` / `REVIEW` / `BLOCK` — derived from
the highest severity present. Every rule runs inside `guard()`, which catches
exceptions and converts them to a `SYS-001` **major** finding. So a rule that
crashes downgrades the asset to review; it can never upgrade it to pass.
Findings carry a `routes_to` desk (creative / brand / legal / engineering) so
the output is a sorted queue, not a wall of text.

**Why it's built this way.** Two reasons, one technical and one commercial.
Technically, fail-open is the single unacceptable failure mode in a compliance
system — a crash must not become a green light, and there's a test that asserts
it (`test_a_raising_rule_never_silently_passes`). Commercially, *"AI approves
your ads"* loses a room in thirty seconds; *"your reviewers stop seeing the
half that were never going to pass"* wins it.

**If they ask: "can't this replace the review step?"**
Agree immediately and completely — no. It reduces what reaches the queue. The
machine-checkable subset is real but it is a subset: prominence, tone,
cultural fit and whether the creative is actually *good* remain human
judgements. What it buys you is that every asset a reviewer opens is already
clean on the mechanical rules, and every rejection cites a rule id instead of
an opinion.

**Point at:** the `guard()` closure inside `evaluate()`, and the severity →
verdict mapping at the bottom of the function.

---

## Part 2 — Module by module

Read this section the night before. For each file: what it is, what to say,
and the one question that file attracts.

### `run.py` — the command line

**In plain words.** Three commands: `plan` tells you what a run would cost
without spending anything, `run` does it, `providers` lists which AI backends
are wired up.

**Technically.** `argparse` with subcommands; each sets `args.fn`. `plan`
loads and validates the brief, prints the variant/generation counts and the
pre-flight findings. `run` calls `run_campaign()`, writes the report, and
**exits non-zero if anything was blocked** — so it drops straight into CI
without a wrapper.

**Why it's built this way.** `plan` exists because at 4 requests/minute you
want to know the bill before you pay it. The non-zero exit is a small thing
that signals you've thought about this running unattended.

**If they ask: "why a CLI and not a web app?"**
Because the deliverable is a pipeline, and a pipeline's natural interface is a
command that a scheduler, a CI job or a Workflow Builder node can call. A UI
on top is a thin layer; a UI *instead* would have meant less time on the part
being evaluated. If they want to see it in a browser, the HTML report is the
UI — it's the artifact a marketer would actually be sent.

---

### `pipeline/brief.py` — parse, validate, expand

**In plain words.** Reads the campaign file, refuses it if anything important
is missing, and works out the full list of files that need to exist.

**Technically.** YAML → frozen dataclasses (`Product`, `Market`, `Ratio`).
`Brief.variants()` yields the cross product in a stable order.
`Variant.seed` is derived from the variant id, so it's deterministic.
`Product.has_asset()` hits the filesystem — a path in a brief is a *claim*,
not a fact.

**Why it's built this way.** Validation is deliberately strict and errors name
the field (`markets[1]: missing required field 'audience'`). A brief that's
90% right produces creatives that are 90% right, and nobody notices — that's
worse than a hard failure.

Two rules encode requirements from the exercise rather than taste: at least
two products, at least three aspect ratios. Fail fast, with the reason.

**If they ask: "why frozen dataclasses?"**
Immutability. These objects get passed through composition and checking; if
any stage could mutate the brief, a finding might not correspond to what was
actually rendered. Frozen makes that impossible rather than merely discouraged.

**If they ask: "why derive the seed from the id?"**
Reproducibility is a compliance property, not a convenience. Six months from
now somebody asks why an asset looks the way it does. With a random seed that
question is unanswerable. With a derived seed, the same brief regenerates the
same pixels, and that's the first thing an auditor asks for.

---

### `pipeline/assets.py` — the cost decision

**In plain words.** For each product, in order: use the team's own photo if
it's there; use one we made earlier if nothing changed; only then pay for a
new one.

**Technically.** `AssetResolver.resolve()` implements the three-tier
resolution. The cache key is a SHA-256 over `{prompt, seed, size, provider,
model}` — everything that could change the pixels and nothing that couldn't.
Generated files are written to `*.part` and then `os.replace()`d.

**Why it's built this way.**

- Including **provider and model** in the key means switching vendors
  correctly *misses* the cache, instead of silently serving the old vendor's
  image.
- Excluding the campaign name means renaming a campaign doesn't throw away
  work.
- The atomic write matters more than it looks: a crash mid-write leaves a
  truncated PNG that every future run happily "reuses". Write-then-rename is
  the same discipline as an atomic drop into a watch folder.

**If they ask: "what invalidates the cache?"**
Any change to the prompt, seed, size, provider or model. Not the campaign
name, not the market, not the aspect ratio — because none of those change the
master image. And if a human wants a fresh take, deleting `.cache/masters` is
the escape hatch.

**If they ask: "why is the prompt built in code rather than in the brief?"**
`build_prompt()` is deliberately one function so the prompt is a reviewable
artifact, not an f-string buried at a call site. In a real engagement the
brand's creative director wants to read and edit that text — and if you're
using a custom model, the vocabulary here has to match the vocabulary used in
the training captions, or you've trained a model in one language and are
prompting it in another.

---

### `pipeline/providers/` — the adapter layer

Covered as Decision 2. Extra detail worth knowing:

**`base.py` also holds the rate limiter.** A token bucket, thread-safe, with
the interval derived from requests-per-minute. It's in `base` rather than in
each adapter because the *policy* (queue, don't spray) is the same everywhere
even though the *number* differs — Firefly's documented default is 4/min.

**`mock.py` is not a stub.** It renders a plausible product scene: a seeded
gradient background, an off-centre light source, a cast shadow composited
*before* the subject so the object sits on the surface, a rounded body with an
edge highlight, and a cap. Colours are derived by hashing the prompt, so
different products reliably look different and each looks identical across
runs.

**If they ask: "why go to that trouble for a mock?"**
Because a grey rectangle doesn't exercise the pipeline. The crop stage needs
real tonal range to find a subject; the palette check needs a real colour
distribution; the scrim needs something to sit on. A mock that returns a flat
fill would let all four of those stages pass while being broken.

---

### `pipeline/localize.py` — fonts, not translation

**In plain words.** The Japanese copy is already in the brief, written by a
person. The thing that actually breaks is the font: if it can't draw Japanese
characters, you get rows of empty boxes and nobody notices until the Tokyo
team opens the file.

**Technically.** Builds an index of every font file across Linux/Windows/macOS
font directories, once per process. `_can_render()` renders the string with
`getmask()` and compares against a known-unsupported control string — if the
mask is empty, or identical in size to pure tofu, the face is rejected.
`font_for()` picks the CJK or Latin preference list by language, and falls
back to any CJK-capable face on the machine before giving up. If nothing can
render it, it **raises** — with an actionable message.

**Why it's built this way.** Pillow does not warn you. It renders tofu, saves
successfully, and the pipeline reports success. Raising is correct: a creative
full of empty boxes is worse than a failed run, because a failed run gets
fixed.

**If they ask: "why not inspect the font's cmap table?"**
Because `.ttc` collections and the differences between OTF and TTF make that a
rabbit hole, and the render-and-compare approach is format-agnostic and
directly tests the thing we actually care about — will this draw. It costs
about a millisecond per variant.

**If they ask: "what about right-to-left, Arabic, Hebrew?"**
Not handled, and I'd say so rather than pretend. Pillow doesn't do complex
text shaping without `libraqm`; Arabic needs contextual letter forms and RTL
bidi ordering. The honest answer is that a market needing RTL is a different
composition path, and I'd reach for a proper text engine — or Adobe's own,
via InDesign server or the Photoshop API — rather than bolt it onto Pillow.

---

### `pipeline/compose.py` — one master, many specs

**In plain words.** Cuts the big image down to each shape, darkens the bottom
so text stays readable, writes the headline in the right language and size,
puts the logo on, and measures what it just did.

**Technically.** Four stages, in order:

1. **`_subject_box()`** — downscale to 128×128, measure per-row and per-column
   deviation from the image's own median tone, and take the narrowest band
   containing 70% of that energy. That's the subject, roughly.
2. **`crop_to_ratio()`** — crop to the target aspect around that subject, then
   `LANCZOS` resize to exact delivery pixels. Vertical crops bias **upward**
   (0.45 rather than 0.5) because product shots put the subject above centre
   and a straight centre crop beheads the bottle.
3. **Scrim** — a gradient from transparent to ~75% black across the bottom
   42%, drawn with a 1.6 exponent so it ramps late and doesn't look like a
   grey band.
4. **Message and logo** — `_fit_message()` shrinks the type until the wrapped
   block fits the reserved area; `_wrap()` breaks per character for CJK
   because Japanese has no spaces.

Returns measurements, not just a path.

**Why it's built this way.** The scrim is the one that surprises people:
text over an arbitrary photograph is a contrast lottery, and a generated
background is *especially* unpredictable. Putting a scrim in the template
makes legibility a property of the design system rather than of whatever the
model happened to produce that day.

**If they ask: "why fit the type instead of truncating?"**
A cut-off campaign message is a defect. Slightly smaller type is a design
decision. And `SPEC-003` catches it if the fit went too far — so the system
degrades visibly rather than silently.

**If they ask: "that subject detection is pretty crude."**
Agreed, and I'd say it before they do. It's a tonal-energy heuristic, not
saliency. I chose it because it's explainable in one sentence, has no model
dependency, and fails towards centre — which is safe. The upgrade path is a
saliency or segmentation model, and it slots in behind `_subject_box()`
without touching anything else.

---

### `pipeline/checks.py` — the gate

Covered as Decisions 3, 4 and 5. Two details to have ready:

**Blockers vs majors is the design.** Wrong pixel dimensions block — the file
is unusable. A headline under the legibility floor routes to Creative as a
major. Getting that distinction wrong is how a compliance tool becomes a blunt
instrument the brand team works around.

**Pre-flight runs before generation.** `preflight_brief()` checks the copy in
the brief against the prohibited list and reports products whose asset path
points at nothing. If a blocker fires, `run_campaign()` aborts having spent
**zero** credits and says so in the log.

**If they ask: "your prohibited-term check is just substring matching."**
Yes — and I'd flag it as a limitation before they do. It has no notion of
context, so "not clinically proven" would flag. Real systems need phrase-level
rules with negation handling, and lists per market ratified by that market's
legal lead — machine-translating an English list is the standard failure mode,
and it produces a gate that can't read your own ads. What the substring check
*does* buy is that it runs pre-flight, for free, and catches the obvious cases
before you spend anything.

---

### `pipeline/runner.py` — orchestration

**In plain words.** Runs the stages in order, writes a log of everything that
happened, and drops a manifest so a machine can read the results later.

**Technically.** `run_campaign()` is the whole flow. `JsonLogger` writes one
JSON object per line to `run.log.jsonl` and echoes a human-readable form to
stdout. `output_path_for()` produces `<product>/<ratio>/<product>_<locale>_<ratio>.jpg`.
The `RunSummary` dataclass is serialised whole into `manifest.json`.

**Why it's built this way.** JSONL rather than free text because it's
greppable, appendable and machine-readable — and because it *is* the audit
trail. Locale in the filename rather than another directory level because a
reviewer wants all three languages of one spec side by side.

**If they ask: "why not parallelise?"**
Because the expensive step is rate-limited — at 4 rpm, concurrency buys you
nothing but a queue. Composition is local and fast enough that it hasn't been
worth it on this scale. The path is a worker pool behind the existing
`RateLimiter`, and I'd reach for it when a brief crosses a few hundred
variants.

---

### `app.py` + `webui/` — the local app

**In plain words.** A small web page that runs on your own machine. Pick a
brief, see what it would cost, run it, watch the log, look at the results.
One folder, one double-click, no install beyond Python.

**Technically.** Python's standard-library `ThreadingHTTPServer`. The
endpoints: `/api/init`, `/api/brief`, `/api/save`, `/api/plan`, `/api/run`,
`/api/progress`, plus `/api/assets` and `/api/upload` for the asset library,
`/api/insights` and `/api/sample` for the analytics loop, `/api/signed` for a
time-boxed S3 link, and `/api/whoami` + `/api/shutdown` for the
one-instance-at-a-time handshake. A run executes on a worker thread and the page polls
`/api/progress` every 400ms for log lines, so the console feels live without
websockets. Static output is served through `/out/` with a path-traversal
guard.

**Why it's built this way.** Two decisions worth stating out loud:

1. **No framework.** Flask or FastAPI would have been fewer lines, but every
   dependency is another thing that fails on the reviewer's machine. The
   deliverable includes "help the interviewers set up and run the app
   locally" — so the install story *is* part of the product. Three packages,
   all of which the pipeline already needed.
2. **The app contains no pipeline logic.** It calls `load_brief`,
   `preflight_brief` and `run_campaign` — the exact functions the CLI calls.
   That is the property that matters: a demo UI that reimplements the flow
   drifts from the tool within a week and then demos something that isn't
   real.

**The flow canvas is the part to demo.** A node graph in n8n's idiom: source
nodes (asset on disk / cache / generate) feeding one master per product, then
the per-deliverable chain — crop → scrim → message → logo → measure → gate →
sorted queue → report. Nodes go amber while working and carry a live count.
Pipes animate while data flows. Anything that never fires stays dashed.

**Why that matters, and the sentence to say:**

> "Everything on that canvas is driven by events the pipeline actually emits —
> the same records that get written to `run.log.jsonl`. The server folds them
> into graph state and the browser polls it. Nothing is on a timer, so the
> picture can't show a stage that didn't happen. I instrumented the composer
> with a real stage callback rather than faking the animation, because an
> animation that isn't driven by the work it depicts is a lie, and it's the
> first thing I'd want to check if someone showed me one."

Two things to point out while it runs:

1. **The source group.** Three ways an image can arrive, and only one of them
   costs money. On a cold run you can watch *Brand asset* and *Generate hero*
   fire while *Cache hit* stays dashed — then run it again and watch the
   opposite happen. That is the cost argument, animated.
2. **The counts diverge.** The source nodes stop at 1. Everything after the
   master ticks to 18. That visual — narrow at the expensive end, wide at the
   cheap end — is Decision 1 in one picture, and it saves you two minutes of
   explaining.

**If they ask: "why both a CLI and a UI?"
Different jobs. The CLI is what a scheduler, a CI job or a Workflow Builder
node calls — it exits non-zero when something blocks. The UI is what you hand
a marketing manager, and it's what makes the cost argument visible: you press
Plan, and it says 18 deliverables, 1 generative call, before anything is
spent. Same code underneath.

**If they ask: "is this production-ready?"**
No, and I would not pretend otherwise. It binds to localhost, runs one job at
a time, has no auth and no queue. It is a demo surface and an operator tool.
Production is the CLI in a scheduler, or the pipeline behind a proper job
runner — which is why the logic lives in `pipeline/` and not in `app.py`.

---

### `pipeline/insights.py` + the Analytics tab — the loop back

**In plain words.** The brief tells the pipeline what to make. This tab is the
other direction: it looks at how previous posts did and what the market is
talking about, and turns that into a prompt you can actually run. You look at
one sample render, and if you like it you press Adopt and it goes into the
brief.

**Why it exists at all.** Read the exercise's business goals again. Four of
the five are about pushing creative *out* — speed, volume, consistency, cost.
The fifth is "**learn what content, creative and localization drives the best
business outcomes**", and that one points the other way. A pipeline with no
feedback path answers four of five. This is the fifth.

**The one sentence to say:**

> "A dashboard that produces a feeling is decoration. One that produces a
> diff is a tool. Every number on that tab exists to justify one specific
> change to the brief — the surface prompt and the order the placements get
> produced in — and there is a button that makes the change."

#### The two sources, and why they are drawn differently

| | Internal | External |
|---|---|---|
| What | our own posts, per channel | what the market is doing regardless of us |
| Shape | a **calendar** | trend rows + a virality dial + where/who |
| Real source would be | GA4 Data API `runReport`, Meta Graph `insights`, TikTok Business API, YouTube Analytics API | search-trend APIs and scraping |
| Answers | "what were we doing, and did it work" | "what should we be doing next" |

The calendar is a deliberate choice and worth defending. A sortable table
answers *which post won*. A calendar answers *what were we doing* — the run of
Stories in week three, the empty weekends, the two-post days. Posting is
periodic, so the questions you ask about it are periodic, and a table throws
that axis away. The grid is padded to start on a Monday for the same reason:
without that, weekday-vs-weekend is invisible and you have drawn a table with
extra steps.

The grid shows a thumbnail and **one** number per post. Twenty-eight cells
with six numbers each is unreadable; the other five are one click away in the
modal. That is the whole rule.

#### The synthetic-data problem, and how it is handled

Every figure is generated from `SEED = 20260823`. Nothing touches a real API.
It says **SYNTHETIC** in four places: the orange banner, a chip in the toolbar,
a chip in every post modal, and `"synthetic": true` in the JSON payload.

**Say this before they ask:**

> "This is fabricated data and I have made it hard to mistake for anything
> else. A made-up metric that escapes a demo as a real one is the worst thing
> a tool like this could do, and an unlabelled dashboard is exactly how that
> happens. What is *not* fabricated is the shape: each channel names the API a
> real integration would call, and the reason there is an adapter layer for
> providers and storage is the same reason there would need to be one here —
> four vendors returning four shapes for the same idea, all per-market, all
> rate-limited."

Two details that show the numbers were thought about rather than randomised:

* **Impression-weighted averages.** `calendar()` computes engagement and CTR
  as `sum(rate × impressions) / sum(impressions)`, not a plain mean. A 6% rate
  on 900 views must not outrank 2% on 900,000. Un-weighted averaging is how
  dashboards end up celebrating the smallest sample they have.
* **Virality is normalised velocity, not volume.** The score is
  `(velocity − VELOCITY_MIN) / (VELOCITY_MAX − VELOCITY_MIN)` mapped onto
  8–92 with jitter. The first version was `38 + velocity × 26`, which
  overflowed and clamped nearly every term to 98–100 — a meter that always
  reads maximum is a meter nobody looks at twice. A term everyone already
  uses is not a trend; a term that doubled last week is.

#### How the suggestion is made, and what it does when the sources disagree

`suggest()` takes the best treatment from our own history (ranked by
impression-weighted engagement) and the treatment implied by the
fastest-moving external term, and compares them.

* **They agree** → confidence `high`, and the `why` list says so.
* **They disagree** → confidence `medium`, and *the external signal wins*.

That second rule is the interesting one and you should volunteer the reason:

> "Our own history can only rank treatments we have already tried. If it
> disagrees with a term that is moving in the market, the disagreement is
> usually a gap in our sample, not a finding. So the external signal takes it
> — but the tab says `CONFLICT` in the evidence list rather than hiding the
> disagreement behind a single number. A recommendation you can't audit is a
> recommendation you shouldn't take."

The placement order is a **reorder, never a drop**. Four weeks of data
disliking 16:9 is not grounds for cutting a placement the campaign committed
to — that is the over-fitting a media team would rightly refuse. Every spec is
still produced; the best-performing one is just produced first.

#### `/api/sample` — one render, not eighteen

"Act on a suggestion" has to mean *see it* first, and a full run is eighteen
deliverables and two paid model calls. `_render_sample()` builds exactly one:
one product × one market × one ratio.

Three things about it are worth pointing at:

1. **It calls the same code the real run does** — the same `AssetResolver`,
   the same `Composer`, the same `evaluate`. It is not a preview renderer. A
   sample that goes through a different path is a sample that can lie.
2. **It writes to `.cache/samples/`, never `output/`.** A sample is not a
   deliverable and must never turn up in the folder a reviewer has been told
   holds the campaign.
3. **`Product` is frozen**, so it uses `dataclasses.replace(product,
   surface=…)` rather than mutating. Immutability was a deliberate choice in
   `brief.py`; this is the place it stops being an inconvenience and starts
   being the reason a sample can't corrupt the brief it came from.

#### Adopt, and the bug it would have shipped with

Adopting writes the surface prompt into every product, reorders the ratios,
and — this is the part worth mentioning — **sets `regenerate_surface: true`**.

Without that line the feature is a silent no-op for any product being reused
as-shot: the prompt lands in the brief, the brief never sends it, and the next
run produces the identical creative. A suggestion that quietly does nothing is
worse than not offering one, because now the tool has lied about cause and
effect. Nothing is written to disk until Save, so an adoption you dislike
costs one brief reload.

---

### The theme — Spectrum dark, macOS type

**In plain words.** It looks like an Adobe application because it is a tool
you look at images in.

**Technically.** One token set: Spectrum's dark grey ramp (`--g50`…`--g900`),
Spectrum blue 500 (`#2680eb`) as the accent, and semantic colours for the
three verdicts. Type is the system stack — SF on macOS, Segoe on Windows —
with Inter named after them for machines that have it.

**Three decisions to defend:**

1. **Dark, and that is not a preference.** A light chrome around a photograph
   biases how the photograph reads. Photoshop, Premiere and Lightroom are all
   dark for this reason, and this app's entire job is showing you creatives
   and asking whether they pass.
2. **The stylesheet was rewritten, not patched.** The old one carried 189
   hard-coded colour literals across 90 distinct values. Converting those one
   at a time gives you a half-themed app where one panel is warm paper and the
   next is dark — which looks worse than either. It was cheaper and safer to
   restate the whole sheet against one token set, and the splice was done by a
   script so a 500-line edit could not half-apply.
3. **No webfont is fetched.** Adobe Clean is proprietary and cannot ship here,
   and a lookalike would be worse than the platform's own face. Loading Inter
   from a CDN would also mean a tool meant to run on a laptop with no network
   flashes a fallback on every load — for a font nobody is grading.

**If they ask about the tabs:** the page used to be one long scroll. That works
until a run produces five markets, at which point the stage strip you want to
read and the deliverables you want to check it against are two screens apart.
Run and the status dot stay in the chrome rather than in a pane, because
starting a run is the one thing you want to do from wherever you are — and
the tabs follow the work: to Pipeline while it runs, to Results when it
finishes.

---

### `pipeline/report.py` — the thing you screen-share

**In plain words.** A single HTML file with every creative, its verdict and
why. Emailable, no server.

**Technically.** Thumbnails are downscaled and inlined as base64 data URIs, so
the file is self-contained. Grouped by product. Findings are shown per card
with the rule id and the desk they route to.

**Why it's built this way.** Because "scroll through my terminal" has never
won a room, and because a self-contained file still opens correctly six months
later from a USB stick, with no broken relative paths.

**If they ask: "isn't the manifest enough?"**
Different audiences. The manifest is for machines and for audit; the report is
for the marketing director. Same data, and they're generated from the same
`RunSummary`, so they can't drift.

---

### `tests/test_pipeline.py`

23 tests, runnable with plain `python`. The four to mention by name:

| Test | What it pins |
|---|---|
| `test_generation_count_is_per_product_not_per_variant` | the cost claim |
| `test_existing_asset_is_never_generated` | reuse actually short-circuits the provider |
| `test_palette_tolerance_survives_a_jpeg_round_trip` | the anti-cry-wolf rule |
| `test_a_raising_rule_never_silently_passes` | fail-open is impossible |

**If they ask: "why tests on a two-hour exercise?"**
Because a compliance rule with no test is a liability, not a control. Each one
pins a claim the README makes — if someone changes the code, the claim either
stays true or the test goes red. And the reuse test uses a spy provider that
*raises* if called, which is a stronger assertion than counting calls.

---

## Part 2b — The bug worth telling them about

Interviewers love a real debugging story more than they love clean code. You
have one, and it is genuinely instructive.

**The symptom.** After a clean run I checked `.cache/masters` and found
*three* cached files for the same product. The cache was never hitting. Worse,
the pass/review counts changed between runs — 13/5, then 14/4 — on an
identical brief.

**The cause.** Seeds were derived with Python's builtin `hash()`. Python
**salts string hashing per process** (`PYTHONHASHSEED`), so `hash("velvet-matte-lip")`
returns a different value every time the interpreter starts. Every run got a
different seed, so every run generated a new master, so the cache key never
matched and the "identical inputs produce identical pixels" claim was quietly
false.

**Why it survived the first test.** My determinism test compared two seeds
*inside one process*. Within a single interpreter, `hash()` is perfectly
stable — so the test passed while the property it was supposed to protect was
broken.

**The fix.** `stable_seed()` in `brief.py`, a SHA-256 digest truncated to 31
bits. And a new test, `test_seeds_are_stable_ACROSS_processes`, that shells
out to three subprocesses and asserts they agree — because that is the only
place the bug is visible.

**The proof.** Two clean runs with the cache deleted in between now produce
byte-identical creatives (same SHA-256 across all 18 files).

**Say it like this:**

> "Reproducibility was one of my design claims, so I tried to break it. Two
> clean runs, diff the outputs. They differed — and the cache was missing
> every time. It was `hash()`: Python salts string hashing per process, so my
> seeds were different on every run. What's instructive is that my
> determinism test passed the whole time, because it compared two values
> inside one process. The property only fails across process boundaries, so
> the test had to as well. That's the general lesson — a test has to fail in
> the same dimension the bug lives in."

That answer demonstrates three things at once: you verify your own claims, you
understand a genuinely subtle language behaviour, and you know that a passing
test isn't evidence of anything unless it's testing the right axis.

---

## Part 3 — The questions that will actually come

**"Walk me through what happens when I type `python run.py run`."**
Load and validate the brief → pre-flight against the copy, spending nothing →
for each product, resolve one master (reuse from disk, reuse from cache, or
generate) → for each of the 18 variants, crop that master to spec, draw the
localized message and logo, measure it → run 8 rules against those
measurements → write the files, the manifest, the JSONL log and the HTML
report → exit non-zero if anything blocked.

**"What would break first at real scale?"**
The rate limit, and it's not close. Everything else is local compute. At 4
requests/minute, a brand with 500 products that all need generation is a
two-day queue — which is exactly why the reuse tiers exist, and why the next
thing I'd build is a persistent job queue with resumability rather than a
faster compositor.

**"What would you do differently with a month instead of an afternoon?"**
In order: (1) replace Pillow composition with the Photoshop API against the
brand's approved PSD templates, because brand teams don't accept "the tool
laid out the ad"; (2) DAM integration behind the same resolver interface, so
reuse checks the customer's actual asset library instead of a folder; (3)
saliency-based cropping; (4) per-market legal lexicons ratified locally; (5)
C2PA content credentials on output, verified after every transform, because
re-encodes strip the manifest.

**"How does this connect to Adobe's stack?"**
The provider adapter is already Firefly Services. Composition would move to
the Photoshop API. The reuse tier becomes AEM Assets or the customer's DAM.
And the whole pipeline is the shape of a Workflow Builder node — brief in,
checked variants out — which is how it stops being my script and starts being
something the customer's team can recombine.

**"What are you least happy with?"**
Subject detection, and I'd rather say it than have it found. It's a tonal
heuristic that fails towards centre. It's safe, it's explainable, and it's the
first thing I'd replace.

**"Did AI write this?"**
I used it, the way I use it every day — and the honest answer is that the
value wasn't in the typing. It was in deciding that generation belongs outside
the variant loop, that the checks read the render and not the brief, and that
an exact-hex brand rule would have been worse than none. Those are the
decisions this stands or falls on, and I can defend every one of them —
which is what this conversation is testing.

---

## Part 4 — Vocabulary, so nothing lands as a gap

| Term | What it means here |
|---|---|
| **Variant** | one deliverable file: product × market × aspect ratio |
| **Master** | the single high-res image every variant of a product is cut from |
| **Pre-flight** | checks that run before any generative call, so failures cost nothing |
| **Scrim** | the gradient overlay that keeps text legible over any photo |
| **Tofu** | the empty box a font renders when it has no glyph for a character |
| **Redmean** | a cheap perceptual colour distance, weighted by average red level |
| **CJK** | Chinese, Japanese, Korean — scripts needing a specific font family |
| **Blocker / major / minor** | severity: don't ship / a human must look / note it |
| **Fail-open** | a broken check letting bad work through; the one unacceptable bug |
| **Token bucket** | the rate limiter: N calls per minute, queued not dropped |
| **Content-addressed** | cached by a hash of its inputs, so identical inputs reuse |
| **Idempotent** | running it twice produces the same result and no extra cost |
