"""Video deliverables, rendered from the still that already passed its checks.

WHY THIS AND NOT A VIDEO MODEL. Two reasons, and the second is the real one.

First, availability: Cloudflare Workers AI -- the provider this repo defaults
to when it has credentials -- has no video model at all. That was measured
against the account, not assumed: 64 models, and the task taxonomy has no
video category in it. Real generative video means a new vendor, a new key and
a per-second bill.

Second, and more important: a huge share of real paid social video IS this.
A hero still, a slow push, the line of copy arriving, the logo landing. It is
what a motion designer produces from an approved key visual, and it has a
property generative video does not -- it is the SAME creative that passed
brand and legal checks as a still. Nothing new appears in frame, so nothing
new can go wrong in frame. A generated video would have to be re-checked
frame by frame, and this repo has no rule that can do that.

So the video path deliberately inherits the still's verdict, and says so.

ffmpeg is an OPTIONAL dependency. It is not in `requirements.txt`, the three
-package install is unchanged, and when it is missing this reports that
plainly and the engine records the slot as unrendered rather than pretending.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# Per channel: how long, and how hard to push. Durations are the ones the
# placements actually reward; anything longer is trimmed by the platform or
# skipped by the viewer.
CHANNEL_MOTION = {
    "tiktok":    {"seconds": 8.0,  "zoom": 1.14, "fps": 30},
    "instagram": {"seconds": 7.0,  "zoom": 1.10, "fps": 30},
    "youtube":   {"seconds": 10.0, "zoom": 1.08, "fps": 30},
    "facebook":  {"seconds": 7.0,  "zoom": 1.10, "fps": 30},
}
DEFAULT_MOTION = {"seconds": 8.0, "zoom": 1.12, "fps": 30}

# Long enough to read, short enough not to sit on the product. Fractions of
# the clip, so they hold at any duration.
FADE_IN_FRAC = 0.10
FADE_OUT_FRAC = 0.08


class MotionError(RuntimeError):
    pass


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


def available() -> bool:
    return bool(ffmpeg_path())


def why_unavailable() -> str:
    if available():
        return ""
    return ("ffmpeg is not on PATH. Video slots are planned and captioned but "
            "not rendered. Install it (winget install Gyan.FFmpeg, brew "
            "install ffmpeg, apt install ffmpeg) and re-run to fill them in.")


def render(still_path: str, out_path: str, channel: str = "",
           seconds: float = 0.0, fps: int = 0) -> dict:
    """One still -> one mp4, with a slow push and a gentle fade.

    The zoompan filter runs on an upscaled copy. Zooming the source directly
    is the obvious way to write this and it visibly stair-steps, because
    zoompan quantises its crop to whole source pixels -- upscaling first moves
    that quantisation below what the output can show.
    """
    exe = ffmpeg_path()
    if not exe:
        raise MotionError(why_unavailable())
    if not os.path.isfile(still_path):
        raise MotionError(f"no still at {still_path}")

    cfg = dict(CHANNEL_MOTION.get(channel, DEFAULT_MOTION))
    secs = float(seconds or cfg["seconds"])
    rate = int(fps or cfg["fps"])
    frames = max(2, int(secs * rate))
    zoom = cfg["zoom"]

    from PIL import Image
    with Image.open(still_path) as im:
        w, h = im.size
    # Even dimensions: H.264 chroma subsampling cannot encode an odd one, and
    # the failure is an ffmpeg error two minutes into a batch.
    w -= w % 2
    h -= h % 2

    fade_in = max(0.2, secs * FADE_IN_FRAC)
    fade_out = max(0.2, secs * FADE_OUT_FRAC)

    vf = (
        f"scale={w*4}:{h*4},"
        f"zoompan=z='min(1+({zoom}-1)*on/{frames},{zoom})'"
        f":d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={w}x{h}:fps={rate},"
        f"fade=t=in:st=0:d={fade_in:.2f},"
        f"fade=t=out:st={max(0.0, secs-fade_out):.2f}:d={fade_out:.2f},"
        f"format=yuv420p"
    )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cmd = [exe, "-y", "-loglevel", "error",
           "-loop", "1", "-i", still_path,
           "-t", f"{secs:.2f}", "-r", str(rate),
           "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           # Every one of these platforms re-encodes on upload; faststart and
           # a sane profile mean their encoder starts from something clean.
           "-profile:v", "high", "-movflags", "+faststart",
           out_path]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise MotionError((proc.stderr or "ffmpeg failed").strip()[:400])

    return {
        "path": out_path,
        "seconds": round(secs, 2),
        "fps": rate,
        "width": w,
        "height": h,
        "zoom": zoom,
        "bytes": os.path.getsize(out_path),
        "from_still": still_path,
        # Stated in the record, not just in the docstring. The video contains
        # no pixel the still did not, so it carries the still's verdict --
        # and anyone reading the manifest can see that is the claim.
        "inherits_verdict_from_still": True,
    }
