"""Label rendering and GB/MX cat-printer protocol encoding.

Packet format: 51 78 CMD 00 LEN_LO LEN_HI DATA.. CRC8 FF
Byte sequence matches rbaron/catprinter (verified), with feed done via blank
rows because MX05/06/08/10 ignore the feed command (NaitLee/Cat-Printer).
"""
from __future__ import annotations

import qrcode
from PIL import Image, ImageDraw, ImageFont

PRINT_WIDTH = 384  # dots; 48 mm printable at 203 dpi


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def packet(cmd: int, data: bytes) -> bytes:
    n = len(data)
    return bytes([0x51, 0x78, cmd, 0x00, n & 0xFF, n >> 8]) + data + bytes([crc8(data), 0xFF])


def row_bytes(img: Image.Image, y: int) -> bytes:
    px = img.load()
    out = bytearray(PRINT_WIDTH // 8)
    for x in range(PRINT_WIDTH):
        if px[x, y] == 0:
            out[x // 8] |= 1 << (x % 8)  # LSB = leftmost dot
    return bytes(out)


def build_job(img: Image.Image, energy: int, feed_px: int) -> bytes:
    img = img.convert("1")
    assert img.width == PRINT_WIDTH
    job = bytearray()
    job += packet(0xA3, b"\x00")                                        # get state
    job += packet(0xA4, b"\x32")                                        # quality
    job += packet(0xAF, bytes([(energy >> 8) & 0xFF, energy & 0xFF]))   # energy
    job += packet(0xBE, b"\x01")                                        # apply energy
    job += packet(0xA6, bytes([0xAA, 0x55, 0x17, 0x38, 0x44, 0x5F, 0x5F, 0x5F, 0x44, 0x38, 0x2C]))
    for y in range(img.height):
        job += packet(0xA2, row_bytes(img, y))
    job += packet(0xA2, bytes(PRINT_WIDTH // 8)) * feed_px             # feed via blank rows
    job += packet(0xA1, b"\x30\x00") * 3                                # set paper
    job += packet(0xA6, bytes([0xAA, 0x55, 0x17, 0, 0, 0, 0, 0, 0, 0, 0x17]))
    job += packet(0xA3, b"\x00")
    return bytes(job)


def _font(size: int, bold: bool = False):
    names = ["DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold else ["DejaVuSans.ttf", "arial.ttf"]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _fit(draw, text, size, max_w, bold=False):
    while size > 10:
        f = _font(size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
        size -= 1
    f = _font(size, bold)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…", f


def qr_payload(qr_mode: str, public_url: str, spool_id: int) -> str:
    if qr_mode == "spoolman":
        return f"web+spoolman:s-{spool_id}"
    return f"{public_url}/spool/show/{spool_id}"


def render_label(spool: dict, *, qr_mode: str, public_url: str, height: int) -> Image.Image:
    img = Image.new("1", (PRINT_WIDTH, height), 1)
    d = ImageDraw.Draw(img)

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(qr_payload(qr_mode, public_url, spool["id"]))
    qr.make(fit=True)
    q = qr.make_image(fill_color="black", back_color="white").get_image().convert("1")
    side = min(height - 8, 200)
    q = q.resize((side, side), Image.NEAREST)
    img.paste(q, (0, (height - side) // 2))

    fil = spool.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name", "")
    e, b = fil.get("settings_extruder_temp"), fil.get("settings_bed_temp")
    temps = " / ".join(p for p in [f"{e}°C" if e else "", f"bed {b}°C" if b else ""] if p)
    weight = spool.get("initial_weight")
    if weight is None:
        weight = fil.get("weight")

    x = side + 8
    w = PRINT_WIDTH - x - 2
    y = 6
    for text, size, bold in [
        (f"#{spool['id']}", 40, True),
        (fil.get("material") or "", 30, True),
        (fil.get("name") or "", 22, False),
        (vendor, 20, False),
        (temps, 18, False),
        (f"{weight:.0f} g" if weight is not None else "", 18, False),
    ]:
        if not text:
            continue
        t, f = _fit(d, text, size, w, bold)
        d.text((x, y), t, font=f, fill=0)
        y = d.textbbox((x, y), t, font=f)[3] + 6
        if y > height - 14:
            break
    return img
