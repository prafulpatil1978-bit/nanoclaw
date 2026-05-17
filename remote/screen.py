import io

import mss
from PIL import Image

MAX_WIDTH = 1440  # caps output and handles Retina 2× scaling


def capture_frame(quality: int = 55) -> tuple[bytes, int, int]:
    """Capture primary screen. Returns (jpeg_bytes, width, height)."""
    with mss.mss() as sct:
        mon = sct.monitors[1]
        grab = sct.grab(mon)
        img = Image.frombytes("RGB", grab.size, grab.bgra, "raw", "BGRX")

        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), img.width, img.height
