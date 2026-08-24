"""A real .psd, written by hand, so the copy stays editable.

WHY THIS EXISTS. The pipeline burns type onto a photograph. That is correct
for a deliverable and useless to the person who has to change it: a flattened
JPEG means a new headline is a new render, and a market that wants its own
line of copy has to come back through the tool. A layered file makes the last
mile somebody else's to drive -- hide the message layer, set your own, keep
everything underneath exactly as it was approved.

WHY IT IS WRITTEN BY HAND. Same rule as the rest of this repo: three
dependencies, all of which the pipeline already needed. `psd-tools` is a read
library, and the write-capable options are heavy. The PSD layer format is
public, stable and about two hundred lines to emit -- cheaper than a
dependency the reviewer has to install, and it cannot break on a version bump.

WHAT IT IS NOT. The message lands as a rasterised layer, not a live Photoshop
type layer. Live type means emitting the TySh descriptor -- an engine-data
blob carrying the font, the leading, the tracking and a transform -- and a
half-correct one renders as garbage or drops the layer. A designer can hide
this layer and set type over it in seconds, and that is honest; a broken type
layer would not be. The SVG path, if it is ever wanted, is where live text
actually belongs.

Layers, bottom to top, matching the order the composer builds them in:

    product   the cropped master, before anything was drawn on it
    scrim     the gradient that makes type legible over any photograph
    message   the localized copy, alone, on transparency
    logo      the brand mark, knocked out white

Compression is PackBits, because the alternative is not. Raw channel data for
four RGBA layers at 1080x1920 is about 33 MB per creative, and eighteen
creatives would be six hundred megabytes of "deliverable". Three of those four
layers are almost entirely transparent, which is precisely the case run-length
encoding was invented for.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from PIL import Image

# Channel ids, as the format numbers them. -1 is alpha, and it comes FIRST in
# a layer's channel list, which is the opposite of every other convention here
# and the single easiest thing to get wrong.
ALPHA, RED, GREEN, BLUE = -1, 0, 1, 2

SIG = b"8BPS"
BIM = b"8BIM"
BLEND_NORMAL = b"norm"


@dataclass
class Layer:
    name: str
    image: Image.Image          # RGBA, already positioned on the full canvas


def _packbits(data: bytes) -> bytes:
    """PackBits RLE, the variant PSD uses.

    Literal runs are encoded as (n-1, bytes...) for n in 1..128; repeats as
    (257-n, byte) for n in 2..128. The awkward part is that a run of two is
    not worth encoding as a run in the middle of a literal, which is why the
    lookahead below only breaks a literal on a run of three or more.
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        # a repeat?
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 128:
            run += 1
        if run >= 3:
            out.append(257 - run)
            out.append(data[i])
            i += run
            continue
        # otherwise a literal, ended by a run of 3+ or by 128 bytes
        start = i
        i += 1
        while i < n and i - start < 128:
            if (i + 2 < n and data[i] == data[i + 1] == data[i + 2]):
                break
            i += 1
        chunk = data[start:i]
        out.append(len(chunk) - 1)
        out += chunk
    return bytes(out)


def _rle_rows(plane: bytes, width: int, height: int) -> tuple[list[int], bytes]:
    """PackBits each row of one channel. Returns (row lengths, packed rows)."""
    rows, lengths = [], []
    for y in range(height):
        enc = _packbits(plane[y * width:(y + 1) * width])
        rows.append(enc)
        lengths.append(len(enc))
    return lengths, b"".join(rows)


def _channel_rle(plane: bytes, width: int, height: int) -> bytes:
    """One LAYER channel: its own row-length table, then its own rows.

    This layout is NOT the one the flattened composite uses -- see
    `_composite_rle`. Using this one for the composite is exactly the bug this
    file shipped with for ten minutes: it wrote, it read back with four
    correctly-named layers, and then Pillow refused the composite with "image
    file is truncated". Everything looked right except the part that every
    viewer which is not Photoshop actually reads.
    """
    lengths, rows = _rle_rows(plane, width, height)
    return b"".join(struct.pack(">H", n) for n in lengths) + rows


def _composite_rle(planes: list[bytes], width: int, height: int) -> bytes:
    """The flattened image: row counts for EVERY channel first, then all rows.

    The spec is explicit and it is the opposite of the per-layer layout: "the
    image data starts with the byte counts for all the scan lines (rows *
    channels), followed by the RLE compressed data".
    """
    all_lengths: list[int] = []
    all_rows: list[bytes] = []
    for plane in planes:
        lengths, rows = _rle_rows(plane, width, height)
        all_lengths += lengths
        all_rows.append(rows)
    return (b"".join(struct.pack(">H", n) for n in all_lengths)
            + b"".join(all_rows))


def _pascal4(s: str) -> bytes:
    """Pascal string padded so the whole field is a multiple of 4."""
    b = s.encode("ascii", "replace")[:255]
    out = bytes([len(b)]) + b
    while len(out) % 4:
        out += b"\x00"
    return out


def write_psd(path: str, layers: list[Layer], composite: Image.Image) -> str:
    """Write a layered PSD. `composite` is what a flat viewer shows."""
    if not layers:
        raise ValueError("a layered file with no layers is just a picture")
    w, h = composite.size
    composite = composite.convert("RGB")

    # ---- header ---------------------------------------------------------
    out = bytearray()
    out += SIG + struct.pack(">H", 1) + b"\x00" * 6
    out += struct.pack(">HIIHH", 3, h, w, 8, 3)     # channels, h, w, depth, RGB
    out += struct.pack(">I", 0)                      # colour mode data
    out += struct.pack(">I", 0)                      # image resources

    # ---- layer records --------------------------------------------------
    records = bytearray()
    channel_blobs: list[bytes] = []

    for lyr in layers:
        im = lyr.image.convert("RGBA")
        if im.size != (w, h):
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(im, (0, 0))
            im = canvas
        r, g, b, a = im.split()

        records += struct.pack(">iiii", 0, 0, h, w)  # top, left, bottom, right
        records += struct.pack(">H", 4)              # channel count

        blobs = []
        for cid, plane in ((ALPHA, a), (RED, r), (GREEN, g), (BLUE, b)):
            body = struct.pack(">H", 1) + _channel_rle(plane.tobytes(), w, h)
            blobs.append(body)
            records += struct.pack(">hI", cid, len(body))
        channel_blobs.append(b"".join(blobs))

        records += BIM + BLEND_NORMAL
        records += bytes([255, 0, 0, 0])             # opacity, clip, flags, pad

        extra = bytearray()
        extra += struct.pack(">I", 0)                # layer mask
        extra += struct.pack(">I", 0)                # blending ranges
        extra += _pascal4(lyr.name)
        records += struct.pack(">I", len(extra)) + bytes(extra)

    layer_info = bytearray()
    layer_info += struct.pack(">h", len(layers))
    layer_info += records
    for blob in channel_blobs:
        layer_info += blob
    if len(layer_info) % 2:                          # must be even-padded
        layer_info += b"\x00"

    layer_and_mask = struct.pack(">I", len(layer_info)) + bytes(layer_info)
    layer_and_mask += struct.pack(">I", 0)           # global layer mask
    out += struct.pack(">I", len(layer_and_mask)) + layer_and_mask

    # ---- flattened composite -------------------------------------------
    # Not optional, and not decoration: anything that is not Photoshop --
    # Finder previews, Bridge, a browser, most CMSes -- reads only this. A PSD
    # with a blank composite looks empty everywhere except the one app that
    # can rebuild it.
    out += struct.pack(">H", 1)
    out += _composite_rle([p.tobytes() for p in composite.split()], w, h)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def verify(path: str) -> dict:
    """Read the file back and report what it actually contains.

    Written because "it wrote 4 MB without raising" says nothing about whether
    Photoshop will open it. Pillow parses the real header and the real layer
    records, so a wrong offset or a bad row table shows up here rather than in
    front of an audience.
    """
    with Image.open(path) as im:
        names = []
        try:
            names = [l[0] for l in getattr(im, "layers", [])]
        except Exception:                                    # noqa: BLE001
            pass
        return {"format": im.format, "mode": im.mode, "size": im.size,
                "layers": names, "n_layers": len(names),
                "bytes": os.path.getsize(path)}
