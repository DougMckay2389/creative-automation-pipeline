"""Keep the approved product. Replace the world around it.

The reuse story was all-or-nothing: a product either had a photograph on disk
and was used exactly as shot, or it had none and was generated whole. That is
a false choice for the thing this pipeline is actually for. Marketing has ONE
approved shot of the product and needs it on volcanic rock this month, marble
the next, wet sand for the summer campaign -- and re-shooting is precisely the
cost the whole exercise exists to remove.

So: hand the approved photograph to an image model as a REFERENCE, and ask it
to change only the surface and background. The product is never regenerated
from a text description, which is the point -- a model does not get to
reinvent a bottle that legal signed off on.


Why this is not a local cutout
------------------------------
The obvious implementation is classical: flood fill inward from the four
corners, call whatever the fill reaches "background", make it transparent,
paste the remainder onto a generated scene. That was built first. It is about
sixty lines, needs no API, and is wrong.

It was wrong because of an assumption that reads as reasonable and is not:
that an approved product asset is a studio shot on a clean, continuous
backdrop. Measured against the real asset in this repo
(`campaigns/assets/hydra-glow-serum.png`):

    corner (0,0)       (213, 204, 189)      warm grey, lit
    corner (1023,1023) ( 59,  82,  90)      near-black wet stone
    border sample R    5 .. 217             the full range, not a flat field

It is a finished photograph -- wet stones, scattered water droplets, a
reflection under the bottle, a lit gradient across the backdrop. There is no
flat region to fill. The fill stopped after 31% of the frame, `getbbox()` on
the resulting alpha returned `(0, 0, 1024, 1024)` -- the entire image -- and
the "cutout" composited a rectangular slab of the original photograph's own
background over the generated scene. No tolerance value fixes that, because
the premise was never true.

The important part is not that the first attempt failed. It is that it failed
*quietly*: every function returned a plausible value, no exception was raised,
and the only signal was the picture looking wrong. That is the failure mode
this codebase treats as the primary enemy, so the replacement below is
structured to make the same class of mistake loud -- the provider either
supports reference-image editing and is asked to do the whole job, or it says
so and the run stops with a message naming what to switch to.

The honest upgrade to a local cutout would be a segmentation model. Workers AI
does not currently host one (checked: 64 models, no segmentation task). But
reference-image conditioning makes the cutout unnecessary rather than merely
better -- one call replaces generate-plus-cut-plus-composite, and the model
relights the product to match the new scene, which no paste operation can do.
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image

# Reference images are capped by the provider. Workers AI documents 512x512
# for FLUX.2 input images; sending more is rejected, and sending a huge PNG
# through a multipart form for the model to immediately downscale is waste.
MAX_REFERENCE_EDGE = 512


def build_resurface_prompt(subject: str, surface: str) -> str:
    """Ask for a new surface and nothing else.

    Every clause here is load-bearing, and each one is in response to a way
    the instruction gets misread:

    * "keep it exactly as it is", enumerated -- shape, label, cap, colour.
      A general "keep the product" gets treated as a style hint and the model
      redesigns the bottle into something tastefully similar. Listing the
      attributes is what makes it an identity constraint.
    * "Replace only ..." -- names the one thing that IS allowed to change, so
      the rest is implicitly fixed.
    * "Do not redesign the product." -- a plain negative at the end, because
      the constraint is the whole point of the feature and it is worth saying
      twice.

    `subject` is included so the model knows what the object IS. Without it a
    reference photo of a frosted bottle on a dark surface can be read as an
    instruction about mood rather than about an object to preserve.
    """
    return (
        f"Take the {subject} from the reference image and keep it exactly as it is: "
        f"identical shape, identical label, identical cap, identical colour, "
        f"unchanged in every detail. "
        f"Replace only the background and the surface it stands on with: {surface}. "
        f"Studio product photography, soft directional daylight from the upper left, "
        f"shallow depth of field, generous negative space, no text, no logos, no people. "
        f"Do not redesign the product."
    )


def prepare_reference(path: str, max_edge: int = MAX_REFERENCE_EDGE) -> bytes:
    """Load the approved asset and return PNG bytes sized for a reference slot.

    `thumbnail` preserves aspect ratio and never upscales, so a small asset is
    passed through at its own size rather than being blown up into softness.
    """
    with Image.open(path) as im:
        ref = im.convert("RGB")
        ref.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        ref.save(buf, "PNG")
        return buf.getvalue()


def reference_fingerprint(png_bytes: bytes) -> str:
    """Identify the reference by its CONTENT, for the cache key.

    Keying on the file path would be a bug with a long fuse: replace
    `hydra-glow-serum.png` with a new approved shot at the same path and every
    future run would serve the cached image built from the OLD photograph, for
    as long as the prompt stayed the same. Hashing the bytes means a new
    photograph misses the cache, which is the entire job of a cache key.
    """
    return hashlib.sha256(png_bytes).hexdigest()[:20]
