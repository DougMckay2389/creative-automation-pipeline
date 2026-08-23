# Example output

`cloudflare-run/` is a real run of the sample brief, committed so you can see
what this produces without holding an API key:

```
provider  cloudflare        model  @cf/leonardo/phoenix-1.0
18 creatives from 1 generative call(s)      4.1s
pass 17   review 1   block 0
```

Open `cloudflare-run/report.html` for the reviewable version — every creative
with its verdict and findings. `manifest.json` is the machine-readable twin,
and `run.log.jsonl` is the event stream the local app renders live.

**Read the folder shape, it is the point.** `<product>/<ratio>/<product>_<locale>_<ratio>.jpg` —
2 products x 3 markets x 3 ratios = 18 files, from **one** generative call.
`hydra-glow-serum` was already on disk so it was reused; `velvet-matte-lip`
was missing, so it was generated once at master resolution and all three
ratios were composed from that single master. Generative cost is per product,
not per deliverable, and there is a test that fails if that stops being true.

**The one `review`** is `velvet-matte-lip` at 16:9 — `BRAND-001 minor`, dominant
colours drifting off the approved palette in the widest crop. It is not
blocked and it is not auto-approved; it is routed to the brand desk. A gate
that measures the rendered pixels will disagree with a generator sometimes,
and that disagreement is the product.

> Both source images in `campaigns/assets/` were generated for this demo —
> there is no real client photography here. In an actual engagement that
> folder is the creative team's approved shots, which is exactly why the
> pipeline reuses whatever it finds there instead of regenerating it.
