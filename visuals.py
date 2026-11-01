"""Drawing code for the iPhone Charge Tray details window.

Everything here is Pillow only (no Tk), so the window can be rendered and tested anywhere.
The whole window is drawn as one anti-aliased image; app.py just shows it on a canvas.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from device_models import ModelInfo

# ---------------------------------------------------------------- layout (px at 100%)
W, H = 340, 672
MARGIN = 28

HEADER_Y = 36
REFRESH_R = 15

RING_CX, RING_CY = W // 2, 176
RING_D, RING_STROKE = 188, 16

STATUS_Y = 294

ROWS_Y = 322
ROW_H = 38

NOTE_Y = 484
NOTE_LINE = 15.5

CARD_X = 20
CARD_Y = 542
CARD_H = 112

SS = 4  # supersampling for shapes

# ---------------------------------------------------------------- palette
BG = (14, 17, 23)
BG_HEX = "#0e1117"
SURFACE = (22, 27, 36)
HAIR = (36, 43, 56)
TEXT = (240, 243, 248)
MUTED = (140, 150, 167)
FAINT = (92, 102, 119)
POWER = (96, 165, 255)

GREEN = ((38, 190, 128), (126, 236, 176))
AMBER = ((232, 146, 36), (255, 204, 96))
RED = ((222, 62, 62), (255, 128, 112))
GRAY = ((78, 86, 100), (112, 121, 136))


def accent_for(percent: int | None, connected: bool):
    if not connected or percent is None:
        return GRAY
    if percent <= 15:
        return RED
    if percent <= 35:
        return AMBER
    return GREEN


# ---------------------------------------------------------------- fonts
_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_FONT_FILES = {
    "light": ["segoeuil.ttf", "segoeui.ttf", "DejaVuSans.ttf"],
    "regular": ["segoeui.ttf", "DejaVuSans.ttf"],
    "semibold": ["seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"],
}
_FONT_DIRS = (_WIN_FONTS, "/usr/share/fonts/truetype/dejavu", "")


@lru_cache(maxsize=96)
def font(weight: str, size_px: float) -> ImageFont.ImageFont:
    size = max(6, int(round(size_px)))
    for name in _FONT_FILES[weight]:
        for base in _FONT_DIRS:
            path = os.path.join(base, name) if base else name
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, weight: str, size: float, max_w: float, min_size: float):
    """Shrink the font, then truncate with an ellipsis, so text fits max_w."""
    s = size
    while s > min_size and draw.textlength(text, font=font(weight, s)) > max_w:
        s -= 0.5
    f = font(weight, s)
    if draw.textlength(text, font=f) > max_w:
        while len(text) > 1 and draw.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def wrap_text(draw: ImageDraw.ImageDraw, text: str, f, max_w: float, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=f) <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while len(last) > 1 and draw.textlength(last + "…", font=f) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# ---------------------------------------------------------------- small helpers
def _mix(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _aa(img: Image.Image, x0: float, y0: float, x1: float, y1: float, painter) -> None:
    """Run painter(draw, tx, ty, ss) on a supersampled layer and composite it onto img.

    painter draws in layer pixels; tx(x)/ty(y) convert image coordinates to layer coordinates.
    """
    x0, y0 = int(math.floor(x0)), int(math.floor(y0))
    x1, y1 = int(math.ceil(x1)), int(math.ceil(y1))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    layer = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    painter(ImageDraw.Draw(layer), lambda x: (x - x0) * SS, lambda y: (y - y0) * SS, SS)
    img.alpha_composite(layer.resize((w, h), Image.LANCZOS), (x0, y0))


def aa_rounded_rect(img, box, radius, fill=None, outline=None, width=1.0) -> None:
    x0, y0, x1, y1 = box

    def paint(d, tx, ty, ss):
        d.rounded_rectangle((tx(x0), ty(y0), tx(x1) - 1, ty(y1) - 1), radius=radius * ss,
                            fill=fill, outline=outline, width=max(1, int(round(width * ss))))
    _aa(img, x0 - 1, y0 - 1, x1 + 1, y1 + 1, paint)


def aa_circle(img, cx, cy, r, fill=None, outline=None, width=1.0) -> None:
    def paint(d, tx, ty, ss):
        d.ellipse((tx(cx - r), ty(cy - r), tx(cx + r) - 1, ty(cy + r) - 1), fill=fill, outline=outline,
                  width=max(1, int(round(width * ss))))
    _aa(img, cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1, paint)


def aa_polygon(img, points, fill) -> None:
    xs, ys = [p[0] for p in points], [p[1] for p in points]

    def paint(d, tx, ty, ss):
        d.polygon([(tx(x), ty(y)) for x, y in points], fill=fill)
    _aa(img, min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1, paint)


def draw_bolt(img, cx, cy, height, fill) -> None:
    pts = [(0.58, 0.0), (0.12, 0.56), (0.46, 0.56), (0.36, 1.0), (0.88, 0.40), (0.54, 0.40)]
    width = height * 0.78
    x0, y0 = cx - width / 2, cy - height / 2
    aa_polygon(img, [(x0 + px * width, y0 + py * height) for px, py in pts], fill)


def draw_refresh_icon(img, cx, cy, r, color) -> None:
    """A circular arrow, drawn by hand so it never depends on a font glyph."""
    start, end = 40, 330

    def paint(d, tx, ty, ss):
        box = (tx(cx - r), ty(cy - r), tx(cx + r), ty(cy + r))
        d.arc(box, start=start, end=end, fill=color, width=max(1, int(round(r * 0.28 * ss))))
        a = math.radians(end)
        px, py = cx + r * math.cos(a), cy + r * math.sin(a)
        # arrow head pointing along the clockwise tangent at the end of the arc
        tx_, ty_ = -math.sin(a), math.cos(a)
        nx, ny = math.cos(a), math.sin(a)
        L = r * 0.95
        tip = (px + tx_ * L * 0.55, py + ty_ * L * 0.55)
        b1 = (px + nx * L * 0.55, py + ny * L * 0.55)
        b2 = (px - nx * L * 0.55, py - ny * L * 0.55)
        d.polygon([(tx(tip[0]), ty(tip[1])), (tx(b1[0]), ty(b1[1])), (tx(b2[0]), ty(b2[1]))], fill=color)
    _aa(img, cx - r * 2, cy - r * 2, cx + r * 2, cy + r * 2, paint)


# ---------------------------------------------------------------- battery ring
def render_ring(percent: int | None, colors, scale: float, diameter: float = RING_D,
                stroke: float = RING_STROKE, pad: float = 22, glow: bool = True) -> Image.Image:
    """Circular battery gauge with rounded ends, a colour sweep and a soft glow."""
    ss = 4 if scale <= 1.25 else 3
    box = diameter + 2 * pad
    final = max(8, int(round(box * scale)))
    big = final * ss
    c = big / 2
    r_outer = diameter / 2 * scale * ss
    sw = stroke * scale * ss
    bbox = (c - r_outer, c - r_outer, c + r_outer, c + r_outer)
    r_mid = r_outer - sw / 2

    track = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(track).ellipse(bbox, outline=HAIR + (255,), width=int(round(sw)))

    result = Image.new("RGBA", (final, final), (0, 0, 0, 0))
    result.alpha_composite(track.resize((final, final), Image.LANCZOS))

    sweep = 0.0 if percent is None else max(0.0, min(100.0, float(percent))) * 3.6
    if sweep > 0:
        mask = Image.new("L", (big, big), 0)
        md = ImageDraw.Draw(mask)
        if sweep >= 359.5:
            md.ellipse(bbox, outline=255, width=int(round(sw)))
        else:
            md.arc(bbox, start=270, end=(270 + sweep) % 360, fill=255, width=int(round(sw)))
            for ang in (270.0, 270.0 + sweep):
                a = math.radians(ang)
                x, y = c + r_mid * math.cos(a), c + r_mid * math.sin(a)
                md.ellipse((x - sw / 2, y - sw / 2, x + sw / 2, y + sw / 2), fill=255)

        # colour sweep: draw thin pie slices from the start colour to the end colour
        sweep_img = Image.new("RGB", (big, big), colors[0])
        sd = ImageDraw.Draw(sweep_img)
        pad_box = (bbox[0] - 4, bbox[1] - 4, bbox[2] + 4, bbox[3] + 4)
        steps = int(math.ceil(sweep)) + 2
        for i in range(steps):
            t = min(1.0, i / max(sweep, 1.0))
            if sweep >= 359.5:  # full ring: fade out and back so there is no seam at the top
                t = 1.0 - abs(2.0 * t - 1.0)
            sd.pieslice(pad_box, start=(270 + i) % 360, end=(270 + i + 2) % 360, fill=_mix(colors[0], colors[1], t))

        if glow:
            small = mask.resize((final, final), Image.LANCZOS)
            blurred = small.filter(ImageFilter.GaussianBlur(7 * scale)).point(lambda v: int(v * 0.55))
            glow_layer = Image.new("RGBA", (final, final), colors[1] + (0,))
            glow_layer.putalpha(blurred)
            under = Image.new("RGBA", (final, final), (0, 0, 0, 0))
            under.alpha_composite(glow_layer)
            under.alpha_composite(result)
            result = under

        arc = sweep_img.convert("RGBA")
        arc.putalpha(mask)
        result.alpha_composite(arc.resize((final, final), Image.LANCZOS))
    return result


def render_app_icon(size: int = 64) -> Image.Image:
    ring = render_ring(78, GREEN, scale=1.0, diameter=size - 8, stroke=size * 0.17, pad=4, glow=False)
    return ring.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- device icon (rear view)
ICON_W, ICON_H = 64, 116


def _palette(model: ModelInfo):
    """(top colour, bottom colour) of the body."""
    if model.layout == "bar":
        return (214, 218, 225), (168, 175, 186)       # iPhone Air: silver
    if model.pro:
        return (134, 126, 116), (84, 79, 72)          # titanium
    if model.generation and model.generation < 12:
        return (82, 84, 92), (48, 50, 56)             # space gray
    return (92, 124, 178), (56, 82, 132)              # modern colour


def _lens(d, x, y, r):
    """Camera lens; x, y and r are in layer pixels."""
    d.ellipse((x - r, y - r, x + r, y + r), fill=(26, 28, 33, 255))
    d.ellipse((x - r, y - r, x + r, y + r), outline=(112, 120, 134, 255), width=max(1, int(r * 0.11)))
    ri = r * 0.62
    d.ellipse((x - ri, y - ri, x + ri, y + ri), fill=(12, 16, 26, 255))
    g = r * 0.2
    gx, gy = x - r * 0.22, y - r * 0.22
    d.ellipse((gx - g, gy - g, gx + g, gy + g), fill=(86, 118, 172, 255))


def _dot(d, x, y, r, fill):
    d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def render_device_icon(model: ModelInfo | None, scale: float, ghost: bool = False) -> Image.Image:
    """Rear view of the phone, drawn from the model's camera layout (no downloaded artwork)."""
    model = model or ModelInfo()
    ss = 4 if scale <= 1.25 else 3
    k = scale * ss
    cw, ch = int(round(ICON_W * k)), int(round(ICON_H * k))
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    def u(v):
        return v * k

    bx, by, bw, bh = 6, 4, 52, 104
    body_box = (u(bx), u(by), u(bx + bw), u(by + bh))
    radius = u(10.5)

    top, bottom = _palette(model)
    if ghost:
        top, bottom = (40, 47, 60), (30, 36, 47)

    # soft shadow
    shadow = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((body_box[0], body_box[1] + u(2.5), body_box[2], body_box[3] + u(2.5)),
                                             radius=radius, fill=150)
    shadow = shadow.filter(ImageFilter.GaussianBlur(u(2.6)))
    sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
    sh.putalpha(shadow)
    layer.alpha_composite(sh)

    # side buttons
    btn = _mix(bottom, (0, 0, 0), 0.15)
    bd = ImageDraw.Draw(layer)
    for y0, y1 in ((by + 16, by + 21), (by + 25, by + 35), (by + 38, by + 48)):
        bd.rounded_rectangle((u(bx - 1.6), u(y0), u(bx + 1), u(y1)), radius=u(0.8), fill=btn + (255,))
    bd.rounded_rectangle((u(bx + bw - 1), u(by + 32), u(bx + bw + 1.6), u(by + 46)), radius=u(0.8), fill=btn + (255,))

    # body with vertical gradient
    gw, gh = int(round(body_box[2] - body_box[0])), int(round(body_box[3] - body_box[1]))
    grad = Image.linear_gradient("L").resize((gw, gh))
    body = Image.composite(Image.new("RGB", (gw, gh), bottom), Image.new("RGB", (gw, gh), top), grad)
    body_mask = Image.new("L", (gw, gh), 0)
    ImageDraw.Draw(body_mask).rounded_rectangle((0, 0, gw - 1, gh - 1), radius=radius, fill=255)
    layer.paste(body, (int(round(body_box[0])), int(round(body_box[1]))), body_mask)

    d = ImageDraw.Draw(layer)
    rim = (255, 255, 255, 38 if ghost else 70)
    d.rounded_rectangle(body_box, radius=radius, outline=rim, width=max(1, int(round(u(0.9)))))

    if not ghost:
        glass_dark = model.pro or bool(model.generation and model.generation < 12)
        module_fill = _mix(bottom, (0, 0, 0), 0.45) if glass_dark else _mix(top, (255, 255, 255), 0.12)
        module_edge = (255, 255, 255, 46)
        flash = (246, 240, 214, 255)
        if model.layout == "square":
            mx, my, ms = bx + 4, by + 4, 30
            d.rounded_rectangle((u(mx), u(my), u(mx + ms), u(my + ms)), radius=u(8.5), fill=module_fill + (255,),
                                outline=module_edge, width=max(1, int(round(u(0.7)))))
            if model.lenses >= 3:
                for lx, ly in ((9.5, 9.5), (9.5, 21), (21, 15.2)):
                    _lens(d, u(mx + lx), u(my + ly), u(5.2))
                _dot(d, u(mx + 22.5), u(my + 6.2), u(1.5), flash)
                _dot(d, u(mx + 22), u(my + 24.5), u(1.3), (30, 32, 38, 255))
            else:
                for lx, ly in ((9.5, 9.5), (20.5, 20.5)):
                    _lens(d, u(mx + lx), u(my + ly), u(5.4))
                _dot(d, u(mx + 22.5), u(my + 7), u(1.5), flash)
        elif model.layout == "pill":
            mx, my = bx + 4, by + 4
            d.rounded_rectangle((u(mx), u(my), u(mx + 15), u(my + 31)), radius=u(7.5), fill=module_fill + (255,),
                                outline=module_edge, width=max(1, int(round(u(0.7)))))
            _lens(d, u(mx + 7.5), u(my + 7.8), u(4.9))
            _lens(d, u(mx + 7.5), u(my + 23.2), u(4.9))
            _dot(d, u(mx + 21), u(my + 6), u(1.4), flash)
        elif model.layout == "bar":
            d.rounded_rectangle((u(bx + 2), u(by + 9), u(bx + bw - 2), u(by + 28)), radius=u(7), fill=module_fill + (255,),
                                outline=module_edge, width=max(1, int(round(u(0.7)))))
            _lens(d, u(bx + 12.5), u(by + 18.5), u(5.2))
            _dot(d, u(bx + 33), u(by + 14.5), u(1.4), flash)
            _dot(d, u(bx + 33), u(by + 22.5), u(1.2), (60, 66, 76, 255))
        else:  # corner: a single lens
            _lens(d, u(bx + 11), u(by + 11), u(5.6))
            d.ellipse((u(bx + 11 - 8), u(by + 11 - 8), u(bx + 11 + 8), u(by + 11 + 8)), outline=module_edge,
                      width=max(1, int(round(u(0.7)))))
            _dot(d, u(bx + 24), u(by + 8), u(1.5), flash)

    return layer.resize((int(round(ICON_W * scale)), int(round(ICON_H * scale))), Image.LANCZOS)


# ---------------------------------------------------------------- the window
@dataclass(frozen=True)
class WindowState:
    connected: bool = False
    percent: int | None = None
    charging: bool = False
    status: str = "Waiting for iPhone"
    watts_text: str = ""
    health_text: str = ""
    cycles_text: str = ""
    temp_text: str = ""
    detail: str = ""
    model: ModelInfo | None = None
    model_name: str = "No iPhone connected"
    owner_name: str = ""
    os_line: str = ""
    updated_text: str = ""
    refreshing: bool = False


def window_size(scale: float) -> tuple[int, int]:
    return int(round(W * scale)), int(round(H * scale))


def render_window(state: WindowState, scale: float = 1.0):
    """Return (RGB image, hit regions). Hit regions are pixel boxes in the returned image."""
    k = scale
    w, h = window_size(k)
    img = Image.new("RGBA", (w, h), BG + (255,))
    d = ImageDraw.Draw(img)

    def p(v):
        return v * k

    colors = accent_for(state.percent, state.connected)
    accent = colors[1] if state.connected else MUTED

    # header
    d.text((p(MARGIN), p(HEADER_Y)), "iPhone Charge Tray", font=font("semibold", p(13)), fill=MUTED, anchor="lm")
    rcx, rcy = p(W - MARGIN - REFRESH_R), p(HEADER_Y)
    if state.refreshing:
        aa_circle(img, rcx, rcy, p(REFRESH_R), fill=POWER + (255,))
        draw_refresh_icon(img, rcx, rcy, p(6.2), (255, 255, 255, 255))
    else:
        aa_circle(img, rcx, rcy, p(REFRESH_R), fill=SURFACE + (255,), outline=HAIR + (255,), width=p(1))
        draw_refresh_icon(img, rcx, rcy, p(6.2), MUTED + (255,))
    hit = {"refresh": (int(rcx - p(REFRESH_R)), int(rcy - p(REFRESH_R)),
                       int(rcx + p(REFRESH_R)), int(rcy + p(REFRESH_R)))}

    # ring
    ring = render_ring(state.percent if state.connected else None, colors, k)
    img.alpha_composite(ring, (int(round(p(RING_CX) - ring.width / 2)), int(round(p(RING_CY) - ring.height / 2))))
    d = ImageDraw.Draw(img)

    if state.connected and state.percent is not None:
        nf, pf = font("light", p(58)), font("semibold", p(22))
        num = str(state.percent)
        nw, pw, gap = d.textlength(num, font=nf), d.textlength("%", font=pf), p(3)
        x0 = p(RING_CX) - (nw + gap + pw) / 2
        base = p(RING_CY) + p(21)
        d.text((x0, base), num, font=nf, fill=TEXT, anchor="ls")
        d.text((x0 + nw + gap, base), "%", font=pf, fill=MUTED, anchor="ls")
    else:
        d.text((p(RING_CX), p(RING_CY)), "—", font=font("light", p(58)), fill=FAINT, anchor="mm")
    if state.connected and state.charging:
        draw_bolt(img, p(RING_CX), p(RING_CY - 44), p(24), accent + (255,))
        d = ImageDraw.Draw(img)

    # status line with a dot
    status, sf = fit_text(d, state.status, "semibold", p(14), p(W - 2 * MARGIN - 20), p(11))
    sw = d.textlength(status, font=sf)
    total = sw + p(16)
    sx = p(RING_CX) - total / 2
    aa_circle(img, sx + p(4), p(STATUS_Y), p(4), fill=accent + (255,))
    d = ImageDraw.Draw(img)
    d.text((sx + p(16), p(STATUS_Y)), status, font=sf, fill=accent if state.connected else MUTED, anchor="lm")

    # key figures
    rows = [
        ("Charging power", state.watts_text, POWER),
        ("Battery health", state.health_text, TEXT),
        ("Charge cycles", state.cycles_text, TEXT),
        ("Temperature", state.temp_text, TEXT),
    ]
    lf, vf = font("regular", p(13)), font("semibold", p(14.5))
    for i, (label, value, color) in enumerate(rows):
        y0 = p(ROWS_Y + i * ROW_H)
        d.line((p(MARGIN), y0, p(W - MARGIN), y0), fill=HAIR, width=max(1, int(round(k))))
        mid = y0 + p(ROW_H) / 2
        d.text((p(MARGIN), mid), label, font=lf, fill=MUTED, anchor="lm")
        if value:
            d.text((p(W - MARGIN), mid), value, font=vf, fill=color, anchor="rm")
        else:
            d.text((p(W - MARGIN), mid), "—", font=vf, fill=FAINT, anchor="rm")
    y_end = p(ROWS_Y + len(rows) * ROW_H)
    d.line((p(MARGIN), y_end, p(W - MARGIN), y_end), fill=HAIR, width=max(1, int(round(k))))

    # one short note: where the wattage came from, or what to do next
    nf2 = font("regular", p(11.5))
    for i, line in enumerate(wrap_text(d, state.detail, nf2, p(W - 2 * MARGIN), 3)):
        d.text((p(MARGIN), p(NOTE_Y) + i * p(NOTE_LINE)), line, font=nf2, fill=FAINT, anchor="lt")

    # device card
    cx0, cy0 = p(CARD_X), p(CARD_Y)
    cx1, cy1 = p(W - CARD_X), p(CARD_Y + CARD_H)
    aa_rounded_rect(img, (cx0, cy0, cx1, cy1), p(20), fill=SURFACE + (255,), outline=HAIR + (255,), width=p(1))
    icon = render_device_icon(state.model, k, ghost=not state.connected)
    ix = int(round(cx0 + p(14)))
    iy = int(round(cy0 + (cy1 - cy0 - icon.height) / 2))
    img.alpha_composite(icon, (ix, iy))
    d = ImageDraw.Draw(img)

    tx = ix + icon.width + p(8)
    max_w = cx1 - p(14) - tx
    name, name_f = fit_text(d, state.model_name, "semibold", p(18), max_w, p(13))
    lines = [(name, name_f, TEXT, p(26))]
    for text, color in ((state.owner_name, MUTED), (state.os_line, MUTED)):
        if text:
            t, f = fit_text(d, text, "regular", p(12), max_w, p(10))
            lines.append((t, f, color, p(19)))
    if state.updated_text:
        lines.append((state.updated_text, font("regular", p(11)), FAINT, p(18)))
    block = sum(line[3] for line in lines)
    y = (cy0 + cy1) / 2 - block / 2
    for text, f, color, height in lines:
        d.text((tx, y + height / 2), text, font=f, fill=color, anchor="lm")
        y += height

    return img.convert("RGB"), hit
