"""
Generate geminidictate.ico — standalone, no external assets needed.
Produces a multi-resolution ICO (16, 32, 48, 64, 256 px).
Dark pill background with a white mic glyph, matching the widget aesthetic.
"""
from PIL import Image, ImageDraw, ImageFont
import io, os, struct

SIZES = [16, 32, 48, 64, 256]
BG   = (18,  18,  21, 255)   # #121215 — widget background
PILL = (63,  63,  70, 255)   # #3f3f46 — widget ACTIVE colour
TEXT = (228, 228, 231, 255)  # #e4e4e7 — widget text colour
RED  = (225,  29,  72, 255)  # #e11d48 — stop/record colour

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geminidictate.ico")


def draw_frame(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size

    # Rounded-rectangle background pill
    r = max(2, s // 6)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG)

    # Mic body — centred, proportional
    mw = max(2, s * 14 // 48)   # mic width
    mh = max(3, s * 20 // 48)   # mic height
    mt = max(1, s * 3  // 48)   # mic top margin
    cx = s // 2
    my = mt
    # mic capsule
    d.rounded_rectangle(
        [cx - mw // 2, my, cx + mw // 2, my + mh],
        radius=mw // 2,
        fill=TEXT,
    )

    # Arc stand below mic
    aw = max(3, s * 22 // 48)
    ab = my + mh + max(1, s * 4 // 48)
    at = my + mh + max(1, s * 1 // 48)
    lw = max(1, s // 20)
    d.arc(
        [cx - aw // 2, at, cx + aw // 2, ab + aw // 3],
        start=180, end=0,
        fill=TEXT, width=lw,
    )

    # Vertical stem
    stem_top = ab + aw // 3 - lw // 2
    stem_bot = stem_top + max(1, s * 5 // 48)
    d.line([cx, stem_top, cx, stem_bot], fill=TEXT, width=lw)

    # Horizontal base line
    base_w = max(2, s * 14 // 48)
    d.line([cx - base_w // 2, stem_bot, cx + base_w // 2, stem_bot],
           fill=TEXT, width=lw)

    # Small red recording dot (bottom-right quadrant)
    dot_r = max(1, s * 5 // 48)
    dot_x = s - dot_r - max(1, s // 12)
    dot_y = s - dot_r - max(1, s // 12)
    d.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r], fill=RED)

    return img


import io

def make_ico(frames):
    """Write a proper multi-resolution ICO from a list of RGBA PIL images."""
    n = len(frames)
    png_bufs = []
    for img in frames:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bufs.append(buf.getvalue())

    # ICO header: ICONDIR (6 bytes) + n * ICONDIRENTRY (16 bytes each)
    header_size = 6 + n * 16
    data_offset = header_size
    offsets = []
    for pb in png_bufs:
        offsets.append(data_offset)
        data_offset += len(pb)

    out = io.BytesIO()
    # ICONDIR
    out.write(struct.pack("<HHH", 0, 1, n))
    # ICONDIRENTRY for each image
    for i, (img, pb) in enumerate(zip(frames, png_bufs)):
        w, h = img.size
        # width/height stored as 0 for 256
        bw = 0 if w >= 256 else w
        bh = 0 if h >= 256 else h
        out.write(struct.pack("<BBBBHHII",
            bw, bh,    # width, height (0 = 256)
            0,          # color count (0 = no palette)
            0,          # reserved
            1,          # planes
            32,         # bit count
            len(pb),    # size of image data
            offsets[i], # offset to image data
        ))
    for pb in png_bufs:
        out.write(pb)
    return out.getvalue()


frames = [draw_frame(sz) for sz in SIZES]
ico_bytes = make_ico(frames)
with open(OUT, "wb") as f:
    f.write(ico_bytes)
print(f"Icon saved: {OUT}  ({os.path.getsize(OUT):,} bytes)  frames={len(frames)}")
