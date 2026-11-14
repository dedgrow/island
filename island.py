"""Островок для Windows: часы, погода и плеер в «жидкой» плашке сверху экрана.
- Играет музыка: маленькая плашка с обложкой и прыгающими столбиками.
- Мышь у верхнего края по центру (или наведена на плашку): раскрывается целиком.
- Ничего не играет: остров спрятан.
- Колёсико над островом — громкость; клик по часам — таймер +5 мин; скопировал — короткое «Скопировано».
Запуск: pythonw island.py   Меню (настройки, выход): правый клик по острову.
"""
import base64
import colorsys
import ctypes
import ctypes.wintypes as wt
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps  # noqa: E402

LAT, LON = 55.75, 37.62  # координаты для погоды (Москва) — поменяй на свой город
BG = (13, 13, 15)
BG_ALPHA = 215  # прозрачность фона: 255 — непрозрачный, 0 — невидимый
FG, DIM, ACCENT = (242, 242, 242), (150, 150, 156), (74, 222, 128)
FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
SS = 3  # контент рисуем в 3 раза крупнее и уменьшаем — так края гладкие
BIG = (470, 124)  # размер раскрытого острова (до масштаба экрана)
PILL = (240, 36)  # маленькая плашка, пока играет музыка
TOAST = (200, 36)  # плашка уведомления («Скопировано», таймер)
TOP = 8  # отступ от верха экрана
SQUARE = (0.5, 0.24, 0.31)  # квадрат с обложкой на рабочем столе: центр x, центр y, сторона — доли экрана
SETTINGS = os.path.join(HERE, "settings.json")  # то, что поменяли через меню
THEMES = {"Зелёный": (74, 222, 128), "Голубой": (96, 186, 255), "Фиолетовый": (176, 120, 255),
          "Розовый": (255, 110, 180), "Красный": (255, 85, 85), "Оранжевый": (255, 160, 60), "Белый": (235, 235, 240)}
APPS = {"yandex": "Яндекс Музыка", "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge",
        "firefox": "Firefox", "opera": "Opera", "telegram": "Telegram", "vlc": "VLC", "zunemusic": "Медиаплеер"}

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BIH), wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLEND), wt.DWORD]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
gdi32.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]

# «жидкий» край: после размытия оставляем узкую полосу перехода вокруг 50% — гладко и без мыла
GOO_LUT = [0 if v < 96 else 255 if v > 160 else (v - 96) * 255 // 64 for v in range(256)]


def wmo_icon(code):
    """Код погоды Open-Meteo -> значок."""
    if code is None:
        return ""
    if code == 0:
        return "☀"
    if code in (1, 2, 3):
        return "⛅" if code < 3 else "☁"
    if code in (45, 48):
        return "≋"
    if 71 <= code <= 77 or code in (85, 86):
        return "❄"
    if code >= 95:
        return "⚡"
    return "☂"  # морось и дождь


def fit(text, font, maxw):
    """Обрезает строку с «…», чтобы влезла в maxw пикселей."""
    if font.getlength(text) <= maxw:
        return text
    while text and font.getlength(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


def rounded(img, size, radius):
    """Квадратная картинка size×size со скруглёнными углами (обрезает по центру)."""
    img = ImageOps.fit(img.convert("RGBA"), (size, size), Image.LANCZOS)
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), m))
    return img


def fade(img, f):
    if f >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * f)))
    return out


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def accent_from(img):
    """Яркий цвет обложки: среднее по насыщенным пикселям, приподнятое по яркости. Серая обложка -> ACCENT."""
    small = img.convert("RGB").resize((24, 24))
    rgb, hsv = small.tobytes(), small.convert("HSV").tobytes()
    picks = [rgb[i:i + 3] for i in range(0, len(rgb), 3) if hsv[i + 1] > 90 and hsv[i + 2] > 90]
    if len(picks) < 20:
        return ACCENT
    r, g, b = (sum(p[c] for p in picks) / len(picks) / 255 for c in range(3))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, max(s, 0.5), max(v, 0.9))
    return int(r * 255), int(g * 255), int(b * 255)


def app_name(aumid):
    """Id плеера из Windows -> короткое имя: 'Spotify.exe' -> 'Spotify'."""
    low = aumid.lower()
    for key, name in APPS.items():
        if key in low:
            return name
    name = re.split(r"[!\\/]", aumid)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.split(".")[-1]


def load_settings():
    cfg = {"lat": LAT, "lon": LON, "alpha": BG_ALPHA}
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def same_art(a, b):
    """Это одна и та же обложка? Сравниваем уменьшенные до 16×16 копии по цвету."""
    x, y = (im.convert("RGB").resize((16, 16), Image.BILINEAR).tobytes() for im in (a, b))
    return sum(abs(p - q) for p, q in zip(x, y)) / len(x) < 22


LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text):
    """Текст LRC -> [(секунда, строка), ...] по времени. У строки может быть несколько меток (припев)."""
    out = []
    for line in text.splitlines():
        words = LRC_TAG.sub("", line).strip()
        out += [(int(m) * 60 + float(s), words) for m, s in LRC_TAG.findall(line)]
    return sorted(out)


def current_lyric(lines, pos):
    """-> (текущая строка, следующая, сколько текущей «спето» 0..1)."""
    i = sum(1 for t, _ in lines if t <= pos) - 1
    nxt = lines[i + 1][1] if i + 1 < len(lines) else ""
    if i < 0:
        return "", nxt, 0.0
    t0, cur = lines[i]
    t1 = lines[i + 1][0] if i + 1 < len(lines) else t0 + 4
    # заливка кончается чуть раньше следующей строки; длинный проигрыш не растягивает её на полминуты
    return cur, nxt, max(0.0, min(1.0, (pos - t0) / max(0.3, min(0.9 * (t1 - t0), 6))))


def closeness(w, h, target):
    """1 — форма почти в целевом размере, 0 — далеко (тогда контент не показываем)."""
    tw, th = target
    c = 1 - (abs(w - tw) / (0.12 * tw) + abs(h - th) / (0.25 * th))
    return max(0.0, min(1.0, c))


def push(hwnd, img, x, y):
    """Показывает RGBA-картинку как окно с настоящей прозрачностью (UpdateLayeredWindow)."""
    w, h = img.size
    r, g, b, a = img.split()
    data = Image.merge("RGBA", [ImageChops.multiply(c, a) for c in (r, g, b)] + [a]).tobytes("raw", "BGRA")
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bih), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mem, bmp)
    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)), mem,
                               ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(BLEND(0, 0, 255, 1)), 2)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)


class Spring:
    """Пружина с перелётом — даёт «желейное» покачивание."""

    def __init__(self, x):
        self.x, self.v = x, 0.0

    def step(self, target, k=0.12, damp=0.66, f=1.0):  # ~8% перелёта: желе, но без «взрыва»
        # f — длина кадра в шагах по 1/60 с: на 144 Гц анимация идёт с той же скоростью, что на 60.
        # Длинный кадр (подтормозило) считаем мелкими шагами — одним большим шагом пружина «выстреливает» на +45%
        n = max(1, math.ceil(f / 0.5))
        for _ in range(n):
            self.v = self.v * damp ** (f / n) + (target - self.x) * k * (f / n)
            self.x += self.v * (f / n)

    def settled(self, target):
        return abs(self.v) < 0.05 and abs(self.x - target) < 0.4


class Card:
    """Рисует содержимое острова (без фона — фон «жидкий» и рисуется отдельно)."""

    def __init__(self, k):
        self.k = k
        self.w, self.h = int(BIG[0] * k), int(BIG[1] * k)
        self.pw, self.ph = int(PILL[0] * k), int(PILL[1] * k)
        self.tw, self.th = int(TOAST[0] * k), int(TOAST[1] * k)
        self.info_box = (0, 0, 0, 0)  # где часы (клик — таймер), координаты карточки
        f = lambda name, px: ImageFont.truetype(os.path.join(FONTS, name), max(1, int(px * k * SS)))
        self.f_title, self.f_artist = f("segoeuib.ttf", 16), f("segoeui.ttf", 13.5)
        self.f_info, self.f_sym = f("segoeui.ttf", 13), f("seguisym.ttf", 13)
        self.f_note, self.f_time = f("seguisym.ttf", 30), f("segoeui.ttf", 11.5)
        self.buttons = {}  # имя -> (x0, y0, x1, y1) в координатах карточки
        # полоска прогресса справа от кнопок: (x начала, x конца, y) в координатах карточки
        self.bar = (252 * k, self.w - 20 * k, 92 * k)
        self.theme = None  # свой цвет темы или None — брать из обложки
        self.set_cover(None)

    def set_cover(self, cover):
        k = self.k
        self.cover = cover
        size = int(24 * k)
        self.small_cover = rounded(cover, size, int(7 * k)) if cover else None
        self.accent = self.theme or (accent_from(cover) if cover else ACCENT)
        self.bg = tuple(int(b * 0.88 + a * 0.12) for b, a in zip(BG, self.accent))  # фон чуть в цвет обложки

    def render_big(self, clock, weather, media):
        s = self.k * SS
        W, H = self.w * SS, self.h * SS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pad, cs = 18 * s, 88 * s  # отступ и размер обложки
        cy0 = (H - cs) / 2
        if self.cover:
            img.alpha_composite(rounded(self.cover, int(cs), int(16 * s)), (int(pad), int(cy0)))
        else:  # заглушка вместо обложки
            d.rounded_rectangle((pad, cy0, pad + cs, cy0 + cs), radius=16 * s, fill=(40, 40, 46, 255))
            d.text((pad + cs / 2, cy0 + cs / 2), "♪", font=self.f_note, fill=DIM, anchor="mm")

        x = pad + cs + 16 * s
        right = W - 20 * s
        info = f"{clock}   {weather}".strip()
        d.text((right, 32 * s), info, font=self.f_sym, fill=DIM, anchor="rm")
        info_w = self.f_sym.getlength(info)
        self.info_box = ((right - info_w - 6 * s) / SS, 16 * s / SS, (right + 6 * s) / SS, 48 * s / SS)
        title = media.get("title") or "Ничего не играет"
        d.text((x, 32 * s), fit(title, self.f_title, right - x - info_w - 14 * s), font=self.f_title, fill=FG, anchor="lm")
        sub = "  ·  ".join(p for p in (media.get("artist", ""), app_name(media.get("app", ""))) if p)
        d.text((x, 56 * s), fit(sub, self.f_artist, right - x), font=self.f_artist, fill=DIM, anchor="lm")

        # кнопки — фигурами, а не символами шрифта: так одинаково ровно везде
        cy, step, r = 92 * s, 44 * s, 7 * s
        playing = media.get("playing")
        self.buttons = {}
        for i, name in enumerate(("prev", "play", "next")):
            cx = x + 12 * s + i * step
            if name == "play" and playing:
                for dx in (-5 * s, 2 * s):
                    d.rounded_rectangle((cx + dx, cy - r, cx + dx + 3.5 * s, cy + r), radius=1.5 * s, fill=FG)
            elif name == "play":
                d.polygon([(cx - 5 * s, cy - r - s), (cx - 5 * s, cy + r + s), (cx + 8 * s, cy)], fill=FG)
            else:
                sgn = -1 if name == "prev" else 1
                d.polygon([(cx - sgn * 6 * s, cy - r), (cx - sgn * 6 * s, cy + r), (cx + sgn * 4 * s, cy)], fill=FG)
                bx = cx + sgn * 5 * s
                d.rectangle((min(bx, bx + sgn * 2.5 * s), cy - r, max(bx, bx + sgn * 2.5 * s), cy + r), fill=FG)
            self.buttons[name] = ((cx - 20 * s) / SS, (cy - 18 * s) / SS, (cx + 20 * s) / SS, (cy + 18 * s) / SS)
        return img.resize((self.w, self.h), Image.LANCZOS)

    def render_pill(self, t, playing, look=(0.0, 0.0), shut=0.0):
        """Маленькая плашка: обложка слева, глазки посередине, прыгающие столбики справа.
        look — куда смотреть (-1..1 по x и y), shut — насколько закрыты глаза (моргание)."""
        k, s = self.k, self.k * SS
        W, H = self.pw * SS, self.ph * SS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for side in (-1, 1):
            self.eye(img, W / 2 + side * 12 * s, H / 2, look, shut, s)
        for i in range(4):
            if playing:
                amp = 0.35 + 0.65 * abs(math.sin(t * (5.2 + i * 1.3) + i * 1.9))
            else:
                amp = 0.25
            bh = 16 * s * amp
            bx = W - 18 * s - (3 - i) * 6 * s
            d.rounded_rectangle((bx, H / 2 - bh / 2, bx + 3.5 * s, H / 2 + bh / 2), radius=1.7 * s, fill=self.accent)
        img = img.resize((self.pw, self.ph), Image.LANCZOS)
        if self.small_cover:
            img.alpha_composite(self.small_cover, (int(7 * k), (self.ph - self.small_cover.height) // 2))
        else:
            ImageDraw.Draw(img).ellipse((10 * k, self.ph / 2 - 4 * k, 18 * k, self.ph / 2 + 4 * k), fill=self.accent)
        return img

    def eye(self, img, x, y, look, shut, s):
        """Глаз Гона: сплошной белый круг без зрачка, край колышется каждый кадр, по контуру мягкая кайма."""
        rx = ry = 6 * s
        glow = 4 * s  # запас под неровный край, кайму и синяки
        x, y = x + look[0] * 1.2 * s, y + look[1] * 1.2 * s  # чуть сдвигается в сторону курсора
        box = (int(x - rx - glow), int(y - ry - glow), int(x + rx + glow) + 1, int(y + ry + glow) + 1)
        ew, eh = box[2] - box[0], box[3] - box[1]
        cx, cy = ew / 2, eh / 2
        core = Image.new("L", (ew, eh), 0)
        # край — плавные «волны» со случайной фазой каждый кадр + мелкая дрожь: круг живой, но не рваный
        p1, p2 = random.uniform(0, 6.3), random.uniform(0, 6.3)
        pts = []
        for i in range(40):
            a = 2 * math.pi * i / 40
            f = 1 + 0.03 * math.sin(3 * a + p1) + 0.02 * math.sin(5 * a + p2) + random.uniform(-0.01, 0.01)
            pts.append((cx + rx * f * math.cos(a), cy + ry * f * math.sin(a)))
        ImageDraw.Draw(core).polygon(pts, fill=255)
        core = core.filter(ImageFilter.GaussianBlur(0.9 * s))  # мягкая кайма по контуру
        # моргание: видимая часть — полоса, сплющенная к середине
        open_h = (ry + glow) * (1 - shut)
        m = Image.new("L", (ew, eh), 0)
        if open_h > 0.5 * s:
            ImageDraw.Draw(m).ellipse((0, cy - open_h, ew - 1, cy + open_h), fill=255)
        # как на рисунке: серые синяки под глазом и тонкие «штрихи»-очертания вокруг всего глаза
        bruise = Image.new("RGBA", (ew, eh), (0, 0, 0, 0))
        db = ImageDraw.Draw(bruise)
        for grow, a0, a1, alpha in ((1.4, 10, 170, 190), (2.6, 30, 150, 120), (3.6, 50, 130, 70)):  # синяки
            g = grow * s
            db.arc((cx - rx - g, cy - ry - g * 0.6, cx + rx + g, cy + ry + g), a0, a1,
                   fill=(150, 150, 158, alpha), width=max(1, int(0.8 * s)))
        for grow, a0, a1 in ((1.2, 160, 380), (1.9, 200, 330), (1.6, 300, 470), (2.4, 120, 250), (2.2, 340, 420)):
            g = grow * s  # очертания: незамкнутые дуги разного радиуса — выглядят как карандашный набросок
            db.arc((cx - rx - g, cy - ry - g, cx + rx + g, cy + ry + g), a0, a1,
                   fill=(175, 175, 182, 170), width=max(1, int(0.45 * s)))
        img.alpha_composite(bruise.filter(ImageFilter.GaussianBlur(0.35 * s)), box[:2])
        layer = Image.new("RGBA", (ew, eh), (255, 255, 255, 0))
        layer.putalpha(ImageChops.multiply(core, m))
        if shut > 0.6:  # закрытый глаз — дужка
            ImageDraw.Draw(layer).arc((cx - rx, cy - 3 * s, cx + rx, cy + 2 * s), 20, 160, fill=FG, width=int(1.4 * s))
        img.alpha_composite(layer, box[:2])

    def render_toast(self, icon, text):
        """Плашка уведомления: значок в цвет обложки и текст по центру."""
        s = self.k * SS
        W, H = self.tw * SS, self.th * SS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        iw, gap = self.f_sym.getlength(icon), 8 * s
        text = fit(text, self.f_info, W - iw - gap - 28 * s)
        x = (W - iw - gap - self.f_info.getlength(text)) / 2
        d.text((x, H / 2), icon, font=self.f_sym, fill=self.accent, anchor="lm")
        d.text((x + iw + gap, H / 2), text, font=self.f_info, fill=FG, anchor="lm")
        return img.resize((self.tw, self.th), Image.LANCZOS)

    def render_progress(self, pos, dur):
        """Время и полоска прогресса -> (картинка, x, y) в координатах карточки."""
        k, s = self.k, self.k * SS
        bx0, bx1, by = self.bar
        x0, y0 = int(bx0 - 6 * k), int(by - 26 * k)
        W, H = int(bx1 + 6 * k) - x0, int(36 * k)
        img = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        L, R, Y = (bx0 - x0) * SS, (bx1 - x0) * SS, (by - y0) * SS
        d.text((R, Y - 13 * s), f"{fmt_time(pos)} / {fmt_time(dur)}", font=self.f_time, fill=DIM, anchor="rm")
        d.rounded_rectangle((L, Y - 2 * s, R, Y + 2 * s), radius=2 * s, fill=(70, 70, 78))
        fx = L + (R - L) * max(0.0, min(1.0, pos / dur))
        if fx > L + 4 * s:
            d.rounded_rectangle((L, Y - 2 * s, fx, Y + 2 * s), radius=2 * s, fill=self.accent)
        d.ellipse((fx - 5 * s, Y - 5 * s, fx + 5 * s, Y + 5 * s), fill=FG)
        return img.resize((W, H), Image.LANCZOS), x0, y0


class Square:
    """Квадрат с обложкой посреди рабочего стола: «жидкий», как остров, края обложки размазаны, снизу капает."""

    def __init__(self, side):
        self.S = int(side)
        self.W, self.H, self.top = self.S + 80, self.S + 280, 40  # запас по бокам и снизу — под капли
        self.size = Spring(0.0)
        self.drops, self.next_drop = [], 0.0  # капля: [время, длительность, сдвиг x, радиус]
        self.bg = self.sharp = None
        bold = os.path.join(FONTS, "segoeuib.ttf")
        self.f_lyric = ImageFont.truetype(bold, max(8, int(self.S * 0.075)))
        self.f_next = ImageFont.truetype(bold, max(8, int(self.S * 0.055)))
        self.lyr_cache = {}
        # затемнение под текстом внизу обложки: снизу темнее, бока мягко сходят на нет
        w, h = int(self.S * 0.92), int(self.S * 0.36)
        sides = Image.new("L", (w, h), 0)
        ImageDraw.Draw(sides).rectangle((w * 0.06, -h, w * 0.94, h * 0.88), fill=255)
        alpha = ImageChops.multiply(Image.linear_gradient("L").resize((w, h)), sides.filter(ImageFilter.GaussianBlur(w * 0.04)))
        self.shade = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self.shade.putalpha(alpha.point(lambda v: v * 170 // 255))
        self.shade_top = self.shade.transpose(Image.FLIP_TOP_BOTTOM)  # то же затемнение под названием сверху

    def lyric_imgs(self, text, small):
        """Строка -> (тусклая, яркая) картинки. Длинную ужимаем до 75% размера, дальше — «…»."""
        key = (text, small)
        if key not in self.lyr_cache:
            if len(self.lyr_cache) > 8:
                self.lyr_cache.clear()
            font, maxw = (self.f_next if small else self.f_lyric), self.S * 0.84
            if font.getlength(text) > maxw:
                font = font.font_variant(size=max(int(font.size * 0.75), int(font.size * maxw / font.getlength(text))))
                text = fit(text, font, maxw)
            size = (int(font.getlength(text)) + 4, int(font.size * 1.45))
            pair = []
            for a in (110 if not small else 90, 255):
                im = Image.new("RGBA", size, (0, 0, 0, 0))
                ImageDraw.Draw(im).text((2, size[1] / 2), text, font=font, fill=(255, 255, 255, a), anchor="lm")
                pair.append(im)
            self.lyr_cache[key] = pair
        return self.lyr_cache[key]

    def draw_title(self, out, cx, cy, title, artist):
        S = self.S
        top = cy - S / 2 + S * 0.04
        out.alpha_composite(self.shade_top, (int(cx - self.shade_top.width / 2), int(top)))
        _, name = self.lyric_imgs(title, False)
        out.alpha_composite(name, (int(cx - name.width / 2), int(top + S * 0.03)))
        if artist:
            dim, _ = self.lyric_imgs(artist, True)
            out.alpha_composite(dim, (int(cx - dim.width / 2), int(top + S * 0.03 + name.height * 0.9)))

    def draw_lyric(self, out, cx, cy, cur, nxt, fill):
        S = self.S
        bottom = cy + S / 2 - S * 0.04
        out.alpha_composite(self.shade, (int(cx - self.shade.width / 2), int(bottom - self.shade.height)))
        if nxt:
            dim, _ = self.lyric_imgs(nxt, True)
            out.alpha_composite(dim, (int(cx - dim.width / 2), int(bottom - S * 0.04 - dim.height)))
        if cur:
            dim, bright = self.lyric_imgs(cur, False)
            x, y = int(cx - dim.width / 2), int(bottom - S * 0.12 - dim.height)
            out.alpha_composite(dim, (x, y))
            fw = int(dim.width * fill)  # караоке: яркая часть растёт слева направо
            if fw > 0:
                out.alpha_composite(bright.crop((0, 0, fw, bright.height)), (x, y))

    def set_cover(self, cover):
        W, H, S = self.W, self.H, self.S
        # фон — сильно размытая обложка на всё окно: им залиты размазанные края и капли
        self.bg = cover.convert("RGB").resize((W // 10, H // 10)).filter(ImageFilter.GaussianBlur(2))
        self.bg = self.bg.resize((W, H), Image.BILINEAR).point(lambda v: v * 7 // 10).convert("RGBA")  # чуть темнее
        sharp = ImageOps.fit(cover.convert("RGB"), (S, S), Image.LANCZOS)
        if min(cover.size) < S:  # плееры отдают маленькие обложки (Яндекс — 150×150): после растяжения подтягиваем резкость
            sharp = sharp.filter(ImageFilter.UnsharpMask(radius=2, percent=90, threshold=2))
        # затемнение к краю: середина как есть, у самого края ~70% яркости
        edge = Image.new("L", (S, S), 178)
        ImageDraw.Draw(edge).rectangle((S * 0.08, S * 0.08, S * 0.92, S * 0.92), fill=255)
        edge = edge.filter(ImageFilter.GaussianBlur(S * 0.05)).convert("RGB")
        sharp = ImageChops.multiply(sharp, edge).convert("RGBA")
        feather = Image.new("L", (S, S), 0)  # чёткая почти вся, размывается только узкая полоса по краю
        ImageDraw.Draw(feather).rectangle((S * 0.04, S * 0.04, S * 0.96, S * 0.96), fill=255)
        sharp.putalpha(feather.filter(ImageFilter.GaussianBlur(S * 0.02)))
        self.sharp = sharp
        self.shifted = {}

    def render(self, now, show, f=1.0, lyric=None, title=None):
        S, W, H, top = self.S, self.W, self.H, self.top
        self.size.step(S if show else 0.0, damp=0.55, f=f)  # спокойнее острова: перелёт ~1%, квадрат не «дышит»
        s = max(0.0, self.size.x)
        if show and s > S * 0.9 and now > self.next_drop:
            self.drops.append([now, random.uniform(1.0, 1.5), random.uniform(-0.38, 0.38) * S,
                               random.uniform(0.05, 0.08) * S])
            self.next_drop = now + random.uniform(0.5, 1.4)
        self.drops = [d for d in self.drops if now - d[0] < d[1]]
        # медленно плавает по маленькому кругу: ~6 px, полный круг ≈ 9 с
        cx, cy = W / 2 + 6 * math.sin(now * 0.7), top + S / 2 + 4 * math.cos(now * 0.7)
        # маску рисуем и размываем в половинном размере, порог — после увеличения: край такой же гладкий, а в 3 раза быстрее
        mask = Image.new("L", (W // 2, H // 2), 0)
        d = ImageDraw.Draw(mask)
        # рисуем по целым пикселям, а дробный остаток сдвига добавляем ниже — иначе край прыгает по 2 px
        hx, hy = cx / 2, cy / 2
        ox, oy = hx - math.floor(hx), hy - math.floor(hy)
        if s > 2:
            h = s / 4
            d.rounded_rectangle((hx - ox - h, hy - oy - h, hx - ox + h, hy - oy + h), radius=s * 0.08, fill=255)
            for t0, dur, dx, r in self.drops:  # капля сначала тянется от края, потом отрывается и падает
                t = (now - t0) / dur
                y = (cy + s / 2 - r * 0.6 + 230 * t * t) / 2 - oy
                rr = r * (1 - 0.45 * t) / 2
                x = hx - ox + dx / 2
                d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=255)
        mask = mask.transform(mask.size, Image.AFFINE, (1, 0, -ox, 0, 1, -oy), Image.BILINEAR)
        mask = mask.filter(ImageFilter.GaussianBlur(3.5)).resize((W, H), Image.BILINEAR).point(GOO_LUT)
        out = self.bg.copy()
        out.putalpha(mask)
        if s > 4:
            settled = abs(s - S) < 1
            sharp = self.sharp if settled else self.sharp.resize((int(s), int(s)), Image.LANCZOS)
            x0, y0 = cx - sharp.width / 2, cy - sharp.height / 2
            px, py = math.floor(x0), math.floor(y0)
            if settled:  # сдвиг на доли пикселя (по 1/4) — иначе медленное плавание идёт рывками по 1 px
                # округляем сразу всю координату до 1/4, иначе 0.9 → «0 четвертей» и картинка прыгает на пиксель назад
                qx, qy = round(x0 * 4), round(y0 * 4)
                px, py = qx // 4, qy // 4
                key = (qx % 4, qy % 4)
                if key not in self.shifted:  # 16 заготовок на обложку, дальше — бесплатно
                    fx, fy = key[0] / 4, key[1] / 4
                    self.shifted[key] = sharp.transform(sharp.size, Image.AFFINE, (1, 0, -fx, 0, 1, -fy), Image.BICUBIC)
                sharp = self.shifted[key].copy()
            # обложку тоже обрезаем «жидкой» маской (края прозрачные, так что за маску ничего не вылезает)
            sharp.putalpha(ImageChops.multiply(sharp.getchannel("A"), mask.crop((px, py, px + sharp.width, py + sharp.height))))
            out.alpha_composite(sharp, (px, py))
            if lyric and s > S * 0.95:  # текст только на раскрытом квадрате
                self.draw_lyric(out, cx, cy, *lyric)
            if title and title[0] and s > S * 0.95:
                self.draw_title(out, cx, cy, *title)
        return out

    def settled_hidden(self):
        return self.size.settled(0.0) and not self.drops


class Island:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        # без этого таймер Windows шагает по ~15.6 мс и after(16) превращается в 31 мс (30 fps, дёргается)
        ctypes.windll.winmm.timeBeginPeriod(1)
        dc = user32.GetDC(None)
        self.fps = max(30, min(240, gdi32.GetDeviceCaps(dc, 116) or 60))  # VREFRESH: частота монитора (144 Гц и т.п.)
        user32.ReleaseDC(None, dc)
        self.last_tick = time.time()
        r = self.root = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        k = self.k = r.winfo_fpixels("1i") / 96
        self.card = Card(k)
        self.card.set_cover(None)
        self.top = int(TOP * k)
        self.sw = user32.GetSystemMetrics(0)
        # окно фиксированного размера с запасом под перелёт и капли; прозрачные пиксели пропускают клики
        self.ww = int(self.card.w + 60 * k)
        self.wh = int(self.top + self.card.h * 1.15 + 50 * k)
        self.wx = (self.sw - self.ww) // 2
        r.geometry(f"{self.ww}x{self.wh}+{self.wx}+0")
        r.update_idletasks()
        self.hwnd = int(r.wm_frame(), 16)
        ex = user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(self.hwnd, -20, ex | 0x80000 | 0x80 | 0x08000000)  # LAYERED|TOOLWINDOW|NOACTIVATE

        # квадрат с обложкой: окно под всеми окнами, клики насквозь — виден только на открытом рабочем столе
        sh = user32.GetSystemMetrics(1)
        self.sq = Square(SQUARE[2] * sh)
        self.sq_x = int(SQUARE[0] * self.sw - self.sq.W / 2)
        self.sq_y = int(SQUARE[1] * sh - self.sq.top - self.sq.S / 2)
        sw = tk.Toplevel(r)
        sw.overrideredirect(True)
        sw.geometry(f"{self.sq.W}x{self.sq.H}+{self.sq_x}+{self.sq_y}")
        sw.update_idletasks()
        self.sq_hwnd = int(sw.wm_frame(), 16)
        ex = user32.GetWindowLongW(self.sq_hwnd, -20)
        user32.SetWindowLongW(self.sq_hwnd, -20, ex | 0x80000 | 0x80 | 0x08000000 | 0x20)  # + TRANSPARENT
        # сразу пустая картинка: иначе, пока квадрат выключен/не играет, Windows показывает серое окно Tk
        push(self.sq_hwnd, Image.new("RGBA", (self.sq.W, self.sq.H), (0, 0, 0, 0)), self.sq_x, self.sq_y)

        self.media = {"title": "", "artist": "", "playing": False, "cover": ""}
        self.cover_b64 = ""
        self.hq_ready = None  # (cover_b64, крупная обложка) из fetch_hq
        self.lyrics_key = self.lyrics = None  # (название, исполнитель) и (ключ, [(сек, строка)]) из fetch_lyrics
        self.weather = ""
        self.mode = "hidden"  # hidden / pill / big
        self.sw_, self.sh = Spring(PILL[0] * k * 0.5), Spring(0.0)
        self.drops = []  # [время рождения, длительность, сдвиг по x, радиус, глубина]
        self.next_bubble = 0.0
        self.peek_until = 0.0
        self.edge_since = None
        self.last_title = None
        # своя плавная прозрачность контента: не повторяет покачивание формы, иначе текст мигает
        self.fade = {"big": 0.0, "pill": 0.0, "toast": 0.0}
        self.cfg = load_settings()
        self.set_theme(self.cfg.get("theme", ""))
        self.weather_wake = threading.Event()  # сменили город — обновить погоду сразу
        self.toast, self.toast_until, self.toast_key, self.toast_img = ("", ""), 0.0, None, None
        self.timer_end = 0.0
        self.next_blink = 0.0
        self.clip_seq = user32.GetClipboardSequenceNumber()
        self.media_t = time.time()
        self.prog_key = self.prog = None
        self.content_key = None
        self.big_img = None
        self.was_drawn_empty = False
        self.through = False  # сейчас клики проходят сквозь остров

        r.bind("<Button-1>", self.click)
        r.bind("<Button-3>", self.menu)
        r.bind("<MouseWheel>", self.wheel)
        self.ps = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(HERE, "media.ps1"), str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        threading.Thread(target=self.read_media, daemon=True).start()
        threading.Thread(target=self.read_weather, daemon=True).start()
        threading.Thread(target=self.square_loop, daemon=True).start()
        self.tick()

    # --- data ---
    def read_media(self):
        for line in self.ps.stdout:
            try:
                self.media, self.media_t = json.loads(line), time.time()
            except ValueError:
                pass

    def position(self):
        """Позиция трека сейчас: между сообщениями плеера досчитываем сами, чтобы полоска ехала плавно."""
        m = self.media
        dur = m.get("dur", 0)
        pos = m.get("pos", 0) + (time.time() - self.media_t if m.get("playing") else 0)
        return min(pos, dur), dur

    def read_weather(self):
        while True:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={self.cfg['lat']}&longitude={self.cfg['lon']}"
                   "&current=temperature_2m,weather_code")
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    cur = json.load(resp)["current"]
                self.weather = f"{wmo_icon(cur['weather_code'])} {round(cur['temperature_2m'])}°"
            except Exception:
                pass  # нет интернета — просто не показываем погоду
            self.weather_wake.wait(900)
            self.weather_wake.clear()

    def fetch_hq(self, key, small, title, artist):
        """Обложка 600×600 из поиска Apple (бесплатно, без ключа); берём, только если похожа на обложку от плеера."""
        if not title:
            return
        for country in ("ru", "us"):
            q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 5, "country": country})
            try:
                with urllib.request.urlopen(f"https://itunes.apple.com/search?{q}", timeout=10) as resp:
                    results = json.load(resp).get("results", [])
                for r in results:
                    url = r.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
                    if not url:
                        continue
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        img = Image.open(io.BytesIO(resp.read()))
                        img.load()
                    if same_art(small, img):
                        self.hq_ready = (key, img)
                        return
            except Exception:
                pass  # нет сети или не нашлось — остаётся обложка от плеера

    def fetch_lyrics(self, key, dur):
        """Текст с таймингом строк из LRCLIB (бесплатно, без ключа). Не нашлось — пустой список, караоке молчит."""
        lines = []
        # точный поиск часто находит только текст без тайминга, а версия с таймингом лежит под «Название (…)» —
        # её находит свободный поиск; оттуда берём только того же исполнителя
        for params in ({"track_name": key[0], "artist_name": key[1]}, {"q": f"{key[1]} {key[0]}"}):
            try:
                url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(url, timeout=10) as resp:
                    found = [r for r in json.load(resp) if r.get("syncedLyrics")
                             and (r.get("artistName") or "").lower() == key[1].lower()]
            except Exception:
                continue
            if found:  # из нескольких версий берём ближайшую по длине трека
                best = min(found, key=lambda r: abs((r.get("duration") or 0) - dur)) if dur else found[0]
                lines = parse_lrc(best["syncedLyrics"])
                break
        self.lyrics = (key, lines)

    def set_city(self, name):
        q = urllib.parse.urlencode({"name": name, "count": 1, "language": "ru"})
        try:
            with urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{q}", timeout=15) as resp:
                res = (json.load(resp).get("results") or [None])[0]
        except Exception:
            res = None
        if not res:
            self.notify("✕", "Город не найден")
            return
        self.set_cfg(lat=res["latitude"], lon=res["longitude"], city=res["name"])
        self.weather_wake.set()
        self.notify("✓", f"Погода: {res['name']}")

    def set_theme(self, name, save=False):
        """Цвет столбиков, полоски и оттенок фона: из THEMES или (пустое имя) — из обложки."""
        self.card.theme = THEMES.get(name)
        self.card.set_cover(self.card.cover)
        self.content_key = self.toast_key = self.prog_key = None  # перерисовать всё в новом цвете
        if save:
            self.set_cfg(theme=name)

    def set_cfg(self, **kw):
        self.cfg.update(kw)
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False)
        except OSError:
            pass

    def notify(self, icon, text, secs=1.6):
        self.toast, self.toast_until = (icon, text), time.time() + secs

    def send(self, cmd):
        try:
            self.ps.stdin.write(cmd + "\n")
            self.ps.stdin.flush()
        except OSError:
            pass

    # --- interaction ---
    def cursor(self):
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def shape_rect(self):
        w, h = self.sw_.x, max(0.0, self.sh.x)
        cx = self.wx + self.ww / 2
        return cx - w / 2, self.top, cx + w / 2, self.top + h

    def decide(self, now):
        x, y = self.cursor()
        x0, y0, x1, y1 = self.shape_rect()
        m = 10 * self.k
        over = self.mode != "hidden" and x0 - m <= x <= x1 + m and y <= y1 + m
        if over:
            return "big"
        # у самого верхнего края по центру — короткая задержка, чтобы не выскакивать при каждом касании вкладок
        if y <= 1 and abs(x - self.sw / 2) < self.card.w / 2:
            self.edge_since = self.edge_since or now
            if now - self.edge_since > 0.12:
                return "big"
        else:
            self.edge_since = None
        if now < self.peek_until:
            return "big"
        if now < self.toast_until:
            return "toast"
        if self.media.get("playing"):
            return "pill"
        return "toast" if self.timer_end else "hidden"  # без музыки таймер тикает в плашке

    def click(self, _e):
        if self.mode != "big" or self.fade["big"] < 0.8:
            return
        x, y = self.cursor()
        x -= self.wx + (self.ww - self.card.w) / 2
        y -= self.top
        for name, (x0, y0, x1, y1) in self.card.buttons.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.send(name)
        x0, y0, x1, y1 = self.card.info_box
        if x0 <= x <= x1 and y0 <= y <= y1:
            self.add_timer()
        pos, dur = self.position()
        bx0, bx1, by = self.card.bar
        if dur > 0 and bx0 - 6 * self.k <= x <= bx1 + 6 * self.k and abs(y - by) <= 10 * self.k:
            self.send(f"seek {dur * min(1.0, max(0.0, (x - bx0) / (bx1 - bx0))):.1f}")

    def hit_zone(self):
        """Курсор над кнопкой, полоской, часами или обложкой раскрытого острова? Остальное пропускает клики насквозь."""
        x, y = self.cursor()
        x -= self.wx + (self.ww - self.card.w) / 2
        y -= self.top
        k = self.k
        bx0, bx1, by = self.card.bar
        zones = list(self.card.buttons.values()) + [
            self.card.info_box, (bx0 - 6 * k, by - 10 * k, bx1 + 6 * k, by + 10 * k),
            (18 * k, 18 * k, 106 * k, 106 * k)]  # обложка: на ней правый клик (меню) и колёсико
        return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in zones)

    def wheel(self, e):
        vk = 0xAF if e.delta > 0 else 0xAE  # VK_VOLUME_UP / VK_VOLUME_DOWN: Windows сам покажет громкость
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    def add_timer(self):
        self.timer_end = max(time.time(), self.timer_end) + 300
        self.notify("◷", f"Таймер {fmt_time(self.timer_end - time.time() + 0.5)}")

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Таймер +5 мин", command=self.add_timer)
        if self.timer_end:
            m.add_command(label="Сбросить таймер", command=lambda: setattr(self, "timer_end", 0.0))
        sub = tk.Menu(m, tearoff=0)
        self.alpha_var = tk.IntVar(value=self.cfg["alpha"])
        for label, a in (("100%", 255), ("85%", 215), ("70%", 180), ("55%", 140)):
            sub.add_radiobutton(label=label, value=a, variable=self.alpha_var,
                                command=lambda: self.set_cfg(alpha=self.alpha_var.get()))
        m.add_cascade(label="Непрозрачность", menu=sub)
        themes = tk.Menu(m, tearoff=0)
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", ""))
        for name in ("",) + tuple(THEMES):
            themes.add_radiobutton(label=name or "Под обложку", value=name, variable=self.theme_var,
                                   command=lambda: self.set_theme(self.theme_var.get(), save=True))
        m.add_cascade(label="Цвет темы", menu=themes)
        self.square_var = tk.BooleanVar(value=self.cfg.get("square", True))
        m.add_checkbutton(label="Квадрат с обложкой", variable=self.square_var,
                          command=lambda: self.set_cfg(square=self.square_var.get()))
        self.karaoke_var = tk.BooleanVar(value=self.cfg.get("karaoke", False))
        m.add_checkbutton(label="Караоке на квадрате", variable=self.karaoke_var,
                          command=lambda: self.set_cfg(karaoke=self.karaoke_var.get()))
        self.sq_title_var = tk.BooleanVar(value=self.cfg.get("sq_title", False))
        m.add_checkbutton(label="Название на квадрате", variable=self.sq_title_var,
                          command=lambda: self.set_cfg(sq_title=self.sq_title_var.get()))
        m.add_command(label="Город для погоды…", command=self.ask_city)
        m.add_separator()
        m.add_command(label="Выход", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def ask_city(self):
        name = simpledialog.askstring("Погода", "Город:", initialvalue=self.cfg.get("city", ""), parent=self.root)
        if name and name.strip():
            threading.Thread(target=self.set_city, args=(name.strip(),), daemon=True).start()

    def quit(self):
        ctypes.windll.winmm.timeEndPeriod(1)
        self.ps.kill()
        self.root.destroy()

    def drip(self, now, small=False):
        k = self.k
        r = (5 if small else 9) * k
        depth = (12 if small else 22) * k
        self.drops.append([now, 0.9 if small else 0.7, random.uniform(-0.25, 0.25) * self.sw_.x, r, depth])

    # --- animation ---
    def tick(self):
        now = time.time()
        fr, self.last_tick = min(3.0, (now - self.last_tick) * 60), now  # длина кадра в шагах по 1/60 с
        m = self.media
        playing = bool(m.get("playing"))
        if playing and m.get("title") and m["title"] != self.last_title:
            if self.last_title is not None:  # не при запуске
                self.peek_until = now + 4  # новый трек — ненадолго раскрываемся
            self.last_title = m["title"]
        elif self.last_title is None and m.get("title"):
            self.last_title = m["title"]
        if m.get("cover", "") != self.cover_b64:
            self.cover_b64 = m.get("cover", "")
            try:
                cover = Image.open(io.BytesIO(base64.b64decode(self.cover_b64))) if self.cover_b64 else None
                if cover:
                    cover.load()  # недокачанная картинка падает здесь, а не рисуется наполовину
            except Exception:
                cover = None
            self.card.set_cover(cover)
            self.content_key = None
            if cover is not None:  # плеер даёт 150×150 — ищем ту же обложку покрупнее в фоне
                args = (self.cover_b64, cover.copy(), m.get("title", ""), m.get("artist", ""))
                threading.Thread(target=self.fetch_hq, args=args, daemon=True).start()
        hq = self.hq_ready
        if hq and hq[0] == self.cover_b64:  # нашлась крупная — подменяем, пока трек тот же
            self.hq_ready = None
            self.card.set_cover(hq[1])
            self.content_key = None

        lkey = (m.get("title", ""), m.get("artist", "")) if self.cfg.get("karaoke") else None
        if lkey != self.lyrics_key:  # новый трек или караоке включили
            self.lyrics_key = lkey
            if lkey and lkey[0]:
                threading.Thread(target=self.fetch_lyrics, args=(lkey, m.get("dur", 0)), daemon=True).start()

        seq = user32.GetClipboardSequenceNumber()
        if seq != self.clip_seq:
            self.clip_seq = seq
            self.notify("✓", "Скопировано")
        if self.timer_end and now >= self.timer_end:
            self.timer_end = 0.0
            self.notify("◷", "Время вышло!", 6)
            user32.MessageBeep(0x40)
        elif self.timer_end and now >= self.toast_until:
            self.toast = ("◷", fmt_time(self.timer_end - now + 0.999))

        mode = self.decide(now)
        if mode != self.mode:
            if mode != "hidden" and self.mode != "hidden":
                self.drip(now)  # «бульк» при смене формы
            self.mode = mode
        # раскрытый остров: клики мимо кнопок проходят в окно под ним (WS_EX_TRANSPARENT включаем/выключаем на лету)
        through = mode == "big" and not self.hit_zone()
        if through != self.through:
            self.through = through
            ex = user32.GetWindowLongW(self.hwnd, -20)
            user32.SetWindowLongW(self.hwnd, -20, (ex | 0x20) if through else (ex & ~0x20))
        if mode != "hidden" and playing and now > self.next_bubble:
            self.drip(now, small=True)  # пока играет музыка — иногда булькает
            self.next_bubble = now + random.uniform(2.0, 3.5)
        self.drops = [d for d in self.drops if now - d[0] < d[1]]

        k = self.k
        sizes = {"big": (self.card.w, self.card.h), "pill": (self.card.pw, self.card.ph),
                 "toast": (self.card.tw, self.card.th)}
        tw, th = sizes.get(mode, (PILL[0] * k * 0.5, 0.0))
        self.sw_.step(tw, f=fr)
        self.sh.step(th, f=fr)
        for name, f in self.fade.items():
            # начали проявляться в этом режиме — держим до смены режима, даже пока форма покачивается
            on = mode == name and (f > 0 or closeness(self.sw_.x, self.sh.x, sizes[name]) > 0.6)
            f += ((1.0 if on else 0.0) - f) * (1 - (1 - (0.22 if on else 0.4)) ** fr)
            self.fade[name] = 1.0 if f > 0.99 else 0.0 if f < 0.01 else f

        clock = time.strftime("%H:%M")
        if self.timer_end:
            clock = f"◷ {fmt_time(self.timer_end - now + 0.999)}   {clock}"
        content = (clock, self.weather, m.get("title"), m.get("artist"), m.get("app"), playing)
        if content != self.content_key:
            self.content_key = content
            self.big_img = self.card.render_big(content[0], content[1], m)
        if self.toast != self.toast_key:
            self.toast_key, self.toast_img = self.toast, self.card.render_toast(*self.toast)

        visible = self.sh.x > 1 or not self.sh.settled(th)
        if visible:
            self.draw(now, playing)
            self.was_drawn_empty = False
        elif not self.was_drawn_empty:
            push(self.hwnd, Image.new("RGBA", (self.ww, self.wh), (0, 0, 0, 0)), self.wx, 0)
            self.was_drawn_empty = True
        spent = int((time.time() - now) * 1000)
        self.root.after(max(1, int(1000 / self.fps) - spent), self.tick)  # кадров/с как у монитора

    def desktop_active(self):
        """Место квадрата не закрыто ни одним окном? Смотрим именно туда, а не на активное окно:
        с двумя мониторами активным бывает окно на соседнем экране, пока ты смотришь на рабочий стол."""
        name = ctypes.create_unicode_buffer(64)
        x0, y0, S = self.sq_x + (self.sq.W - self.sq.S) // 2, self.sq_y + self.sq.top, self.sq.S
        for fx, fy in ((0.5, 0.5), (0.15, 0.5), (0.85, 0.5), (0.15, 0.85), (0.85, 0.85), (0.5, 0.85)):
            root = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(int(x0 + S * fx), int(y0 + S * fy))), 2)  # GA_ROOT
            if root == self.hwnd:
                continue  # раскрытый остров сверху квадрат не прячет
            user32.GetClassNameW(root, name, 64)
            if name.value not in ("Progman", "WorkerW"):
                return False
        return True

    def square_loop(self):
        """Квадрат рисуется в своём потоке с частотой монитора, чтобы не отнимать кадры у острова."""
        seen, shown, check, active, last, off_since = None, False, 0.0, False, time.time(), None
        # ровный темп важнее частоты: каждый 2-й кадр монитора по точному расписанию (рваные 5–20 мс видны как рывки)
        period = 2 / self.fps if self.fps > 90 else 1 / self.fps
        due = time.perf_counter()
        while True:
            now = time.time()
            f, last = min(3.0, (now - last) * 60), now
            try:
                if now > check:  # 10 раз в секунду: держим окно в самом низу и смотрим, не открыто ли окно
                    check = now + 0.1
                    user32.SetWindowPos(self.sq_hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM, NOSIZE|NOMOVE|NOACTIVATE
                    active = self.desktop_active()
                cover = self.card.cover
                if cover is not None and cover is not seen:
                    self.sq.set_cover(cover)
                    seen = cover
                # окно легло на место квадрата — он втягивается; место снова свободно — вытекает
                want = (self.cfg.get("square", True) and bool(self.media.get("playing")) and cover is not None
                        and active)
                # прячемся, только если причина держится 0.5 с: мелькнувшая подсказка или секундная «пауза»
                # плеера между треками иначе заставляют весь квадрат сжаться и тут же вырасти снова
                off_since = None if want else (off_since or now)
                show = want or (self.sq.size.x > 1 and now - off_since < 0.5)
                if self.sq.bg is not None and (show or not self.sq.settled_hidden()):
                    lyr, lyric = self.lyrics, None
                    if lyr and lyr[1] and lyr[0] == self.lyrics_key:
                        lyric = current_lyric(lyr[1], self.position()[0])
                    m = self.media
                    title = (m.get("title", ""), m.get("artist", "")) if self.cfg.get("sq_title") else None
                    push(self.sq_hwnd, self.sq.render(now, show, f, lyric, title), self.sq_x, self.sq_y)
                    shown = True
                elif shown and not show and self.sq.settled_hidden():
                    push(self.sq_hwnd, Image.new("RGBA", (self.sq.W, self.sq.H), (0, 0, 0, 0)), self.sq_x, self.sq_y)
                    shown = False
            except Exception:
                pass
            due += period
            wait = due - time.perf_counter()
            if wait < -period:  # сильно отстали (например, машина была занята) — не догоняем пачкой кадров
                due, wait = time.perf_counter(), 0
            time.sleep(max(0.0, wait))

    def draw(self, now, playing):
        k = self.k
        # форма никогда не вылезает за окно — иначе края обрезаются и видно прямоугольник
        w = min(self.sw_.x, self.ww - 24 * k)
        h = min(max(0.0, self.sh.x), self.wh - self.top - 34 * k)
        cx = self.ww / 2
        mask = Image.new("L", (self.ww, self.wh), 0)
        d = ImageDraw.Draw(mask)
        if h > 1 and w > 1:
            d.rounded_rectangle((cx - w / 2, self.top, cx + w / 2, self.top + h), radius=min(h / 2, 30 * k), fill=255)
            if h > 6 * k:
                for t0, dur, dx, r, depth in self.drops:
                    t = (now - t0) / dur
                    y = self.top + h - r * 0.4 + depth * math.sin(math.pi * t)
                    rr = r * (1 - 0.35 * t)
                    d.ellipse((cx + dx - rr, y - rr, cx + dx + rr, y + rr), fill=255)
        # размытие + порог = капли «слипаются» с островом, как жидкость
        mask = mask.filter(ImageFilter.GaussianBlur(6 * k)).point(GOO_LUT)

        frame = Image.new("RGBA", (self.ww, self.wh), self.card.bg + (0,))
        alpha = self.cfg["alpha"]
        frame.putalpha(mask.point(lambda v: v * alpha // 255))

        layer = Image.new("RGBA", (self.ww, self.wh), (0, 0, 0, 0))
        f = self.fade["big"]
        if f > 0.01:
            layer.alpha_composite(fade(self.big_img, f), (int(cx - self.card.w / 2), self.top))
            pos, dur = self.position()
            if dur > 0:
                key = (round(pos * 4), dur, self.card.accent)  # перерисовываем полоску 4 раза в секунду
                if key != self.prog_key:
                    self.prog_key, self.prog = key, self.card.render_progress(pos, dur)
                img, px, py = self.prog
                layer.alpha_composite(fade(img, f), (int(cx - self.card.w / 2 + px), self.top + py))
        f = self.fade["pill"]
        if f > 0.01:
            mx, my = self.cursor()
            dx, dy = mx - (self.wx + cx), my - (self.top + self.card.ph / 2)
            look = (dx / (abs(dx) + 150 * k), dy / (abs(dy) + 150 * k))
            if now > self.next_blink + 0.16:  # моргает раз в 2.5–6 с
                self.next_blink = now + random.uniform(2.5, 6.0)
            shut = math.sin(math.pi * (now - self.next_blink) / 0.16) if now > self.next_blink else 0.0
            pill = self.card.render_pill(now, playing, look, shut)
            layer.alpha_composite(fade(pill, f), (int(cx - self.card.pw / 2), self.top))
        f = self.fade["toast"]
        if f > 0.01:
            layer.alpha_composite(fade(self.toast_img, f), (int(cx - self.card.tw / 2), self.top))
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
        push(self.hwnd, Image.alpha_composite(frame, layer), self.wx, 0)


def selftest():
    assert wmo_icon(0) == "☀" and wmo_icon(3) == "☁" and wmo_icon(61) == "☂"
    assert wmo_icon(73) == "❄" and wmo_icon(95) == "⚡" and wmo_icon(None) == ""
    assert closeness(100, 30, (100, 30)) == 1 and closeness(50, 30, (100, 30)) == 0
    sp = Spring(0)
    for _ in range(300):
        sp.step(100)
    assert sp.settled(100)
    for f in (0.36, 0.6, 1.0, 3.0):  # перелёт не зависит от частоты кадров и подтормаживаний
        sp, top = Spring(0), 0.0
        for _ in range(int(300 / f)):
            sp.step(100, f=f)
            top = max(top, sp.x)
        assert top < 116, (f, top)
    card = Card(1.5)
    assert fit("abc", card.f_title, 1000) == "abc" and fit("a" * 200, card.f_title, 300).endswith("…")
    cover = Image.new("RGB", (300, 200), (200, 80, 160))
    card.set_cover(cover)
    img = card.render_big("12:34", "☀ 5°", {"title": "Очень длинное название трека " * 3, "artist": "Кто-то", "playing": True})
    assert img.size == (card.w, card.h) and set(card.buttons) == {"prev", "play", "next"}
    assert card.render_pill(1.0, True).size == (card.pw, card.ph)
    assert card.render_pill(1.0, False, (1.0, -1.0), 1.0).size == (card.pw, card.ph)
    assert fmt_time(83) == "1:23" and fmt_time(0) == "0:00" and fmt_time(600.9) == "10:00"
    assert accent_from(Image.new("RGB", (50, 50), (120, 120, 120))) == ACCENT  # серая обложка
    r, g, b = card.accent  # розовая обложка -> розовый акцент
    assert r > 200 and b > 120 and g < r and card.bg != BG
    bar, px, py = card.render_progress(83, 185)
    assert app_name("Spotify.exe") == "Spotify" and app_name("ru.yandex.desktop.music") == "Яндекс Музыка"
    assert app_name("Foo.Player_abc!App") == "App" and app_name("") == ""
    assert card.render_toast("✓", "Скопировано").size == (card.tw, card.th)
    assert card.info_box[2] > card.info_box[0] > 0
    big = cover.resize((600, 400), Image.LANCZOS)
    assert same_art(cover, big) and not same_art(cover, Image.new("RGB", (50, 50), (20, 200, 40)))
    sq = Square(300)
    sq.set_cover(cover)
    for n in range(60):
        frame = sq.render(n / 30, True)
    assert frame.size == (sq.W, sq.H) and abs(sq.size.x - sq.S) < 2 and sq.drops
    for n in range(60, 160):
        sq.render(n / 30, False)
    assert sq.settled_hidden()
    lines = parse_lrc("[ar:x]\n[00:01.50]Раз\n[00:05.00][01:00.00]Два три\n[00:09.00]")
    assert lines == [(1.5, "Раз"), (5.0, "Два три"), (9.0, ""), (60.0, "Два три")]
    assert current_lyric(lines, 0.5) == ("", "Раз", 0.0)
    cur, nxt, fill = current_lyric(lines, 3.0)
    assert cur == "Раз" and nxt == "Два три" and 0.4 < fill < 0.6
    assert current_lyric(lines, 61)[2] > 0 and current_lyric(lines, 100) == ("Два три", "", 1.0)
    for n in range(75):  # с текстом не падает, длинная строка влезает в квадрат
        frame = sq.render(n / 30, True, lyric=("Очень длинная строка песни " * 4, "Следующая", 0.5))
    assert sq.lyric_imgs("Очень длинная строка песни " * 4, False)[0].width <= sq.S * 0.85
    if len(sys.argv) > 3:
        sq2 = Square(300)
        sq2.set_cover(cover)
        for n in range(75):
            frame = sq2.render(n / 30, True, lyric=("Строка, которую поют", "А это следующая", 0.45),
                               title=("Название трека", "Исполнитель"))
        Image.alpha_composite(Image.new("RGBA", frame.size, (10, 10, 14, 255)), frame).save(sys.argv[3])
    card.theme = THEMES["Красный"]
    card.set_cover(cover)
    assert card.accent == THEMES["Красный"]
    card.theme = None
    card.set_cover(cover)
    if len(sys.argv) > 2:
        bg = Image.new("RGBA", img.size, card.bg + (BG_ALPHA,))
        out = Image.alpha_composite(bg, img)
        out.alpha_composite(bar, (px, py))
        out.save(sys.argv[2])
    print("ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        Island().root.mainloop()
