"""A perceptual hash for near-duplicate images, in the style of Meta's PDQ.

It follows the published design: luma, a blurred 64x64 downsample, a 2-D DCT, the
16x16 low-frequency block, one bit per coefficient against the median. That gives
a 256-bit hash whose Hamming distance is small for the same picture after resizing,
recompression or mild colour changes.

HONEST STATUS. This is NOT validated bit-for-bit against the reference
implementation (the Jarosz filter and the exact coefficient selection were not
checked against Meta's source). Its hashes are named `pdq-like-1` and must not be
compared with PDQ hashes produced by other systems. Within this installation it
is self-consistent, and the test suite measures that it separates re-encodings
from different pictures on generated images. Treat distance as a screening signal
that precedes embedding work, never as proof two images are the same.
"""

from __future__ import annotations

ALGO = "pdq-like-1"
DEFAULT_MAX_DISTANCE = 32              # of 256 bits; PDQ's own guidance is ~31


def _dct_matrix(n: int):
    import numpy as np

    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] = np.sqrt(1.0 / n)
    return m


def hash_image(data: bytes) -> tuple[str, float]:
    """-> (64 hex chars, quality 0..1). Quality is the share of gradient energy
    that survived the blur: a flat or blank image scores near 0 and its hash
    should not be trusted for matching."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a decodable image")
    luma = (0.299 * img[:, :, 2] + 0.587 * img[:, :, 1] + 0.114 * img[:, :, 0]).astype(np.float32)
    big = cv2.resize(luma, (512, 512), interpolation=cv2.INTER_AREA)
    big = cv2.blur(cv2.blur(big, (4, 4)), (4, 4))                   # two box passes ~ a tent filter
    small = cv2.resize(big, (64, 64), interpolation=cv2.INTER_AREA)
    d = _dct_matrix(64)
    coef = d @ small @ d.T
    block = coef[:16, :16].flatten()
    median = float(np.median(block))
    bits = (block > median).astype(np.uint8)
    value = int("".join(map(str, bits.tolist())), 2)
    gx, gy = np.abs(np.diff(small, axis=1)), np.abs(np.diff(small, axis=0))
    quality = float(min(1.0, (gx.sum() + gy.sum()) / (64 * 63 * 2 * 255.0 * 0.05)))
    return f"{value:064x}", round(quality, 3)


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
