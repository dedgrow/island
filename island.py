"""Островок для Windows: часы, погода и плеер в «жидкой» плашке сверху экрана.
- Играет музыка: маленькая плашка с обложкой и прыгающими столбиками.
- Мышь у верхнего края по центру (или наведена на плашку): раскрывается целиком.
- Ничего не играет: остров спрятан.
Запуск: pythonw island.py   Закрыть: правый клик по острову.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request

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
PILL = (150, 36)  # маленькая плашка, пока играет музыка
TOP = 8  # отступ от верха экрана

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

    def step(self, target, k=0.12, damp=0.66):  # ~8% перелёта: желе, но без «взрыва»
        self.v = self.v * damp + (target - self.x) * k
        self.x += self.v

    def settled(self, target):
        return abs(self.v) < 0.05 and abs(self.x - target) < 0.4


class Card:
    """Рисует содержимое острова (без фона — фон «жидкий» и рисуется отдельно)."""

    def __init__(self, k):
        self.k = k
        self.w, self.h = int(BIG[0] * k), int(BIG[1] * k)
        self.pw, self.ph = int(PILL[0] * k), int(PILL[1] * k)
        f = lambda name, px: ImageFont.truetype(os.path.join(FONTS, name), max(1, int(px * k * SS)))
        self.f_title, self.f_artist = f("segoeuib.ttf", 16), f("segoeui.ttf", 13.5)
        self.f_info, self.f_sym = f("segoeui.ttf", 13), f("seguisym.ttf", 13)
        self.f_note = f("seguisym.ttf", 30)
        self.buttons = {}  # имя -> (x0, y0, x1, y1) в координатах карточки
        self.small_cover = None

    def set_cover(self, cover):
        k = self.k
        self.cover = cover
        size = int(24 * k)
        self.small_cover = rounded(cover, size, int(7 * k)) if cover else None

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
        title = media.get("title") or "Ничего не играет"
        d.text((x, 32 * s), fit(title, self.f_title, right - x - info_w - 14 * s), font=self.f_title, fill=FG, anchor="lm")
        d.text((x, 56 * s), fit(media.get("artist", ""), self.f_artist, right - x), font=self.f_artist, fill=DIM, anchor="lm")

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

    def render_pill(self, t, playing):
        """Маленькая плашка: обложка слева, прыгающие столбики справа."""
        k, s = self.k, self.k * 2
        W, H = self.pw * 2, self.ph * 2
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for i in range(4):
            if playing:
                amp = 0.35 + 0.65 * abs(math.sin(t * (5.2 + i * 1.3) + i * 1.9))
            else:
                amp = 0.25
            bh = 16 * s * amp
            bx = W - 18 * s - (3 - i) * 6 * s
            d.rounded_rectangle((bx, H / 2 - bh / 2, bx + 3.5 * s, H / 2 + bh / 2), radius=1.7 * s, fill=ACCENT)
        img = img.resize((self.pw, self.ph), Image.LANCZOS)
        if self.small_cover:
            img.alpha_composite(self.small_cover, (int(7 * k), (self.ph - self.small_cover.height) // 2))
        else:
            ImageDraw.Draw(img).ellipse((10 * k, self.ph / 2 - 4 * k, 18 * k, self.ph / 2 + 4 * k), fill=ACCENT)
        return img


class Island:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        # без этого таймер Windows шагает по ~15.6 мс и after(16) превращается в 31 мс (30 fps, дёргается)
        ctypes.windll.winmm.timeBeginPeriod(1)
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

        self.media = {"title": "", "artist": "", "playing": False, "cover": ""}
        self.cover_b64 = ""
        self.weather = ""
        self.mode = "hidden"  # hidden / pill / big
        self.sw_, self.sh = Spring(PILL[0] * k * 0.5), Spring(0.0)
        self.drops = []  # [время рождения, длительность, сдвиг по x, радиус, глубина]
        self.next_bubble = 0.0
        self.peek_until = 0.0
        self.edge_since = None
        self.last_title = None
        # своя плавная прозрачность контента: не повторяет покачивание формы, иначе текст мигает
        self.fade = {"big": 0.0, "pill": 0.0}
        self.content_key = None
        self.big_img = None
        self.was_drawn_empty = False

        r.bind("<Button-1>", self.click)
        r.bind("<Button-3>", lambda e: self.quit())
        self.ps = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(HERE, "media.ps1"), str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        threading.Thread(target=self.read_media, daemon=True).start()
        threading.Thread(target=self.read_weather, daemon=True).start()
        self.tick()

    # --- data ---
    def read_media(self):
        for line in self.ps.stdout:
            try:
                self.media = json.loads(line)
            except ValueError:
                pass

    def read_weather(self):
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
               "&current=temperature_2m,weather_code")
        while True:
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    cur = json.load(resp)["current"]
                self.weather = f"{wmo_icon(cur['weather_code'])} {round(cur['temperature_2m'])}°"
            except Exception:
                pass  # нет интернета — просто не показываем погоду
            time.sleep(900)

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
        if self.mode == "big" and over:
            return "big"
        if self.mode == "pill" and over:
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
        return "pill" if self.media.get("playing") else "hidden"

    def click(self, _e):
        if self.mode != "big" or self.fade["big"] < 0.8:
            return
        x, y = self.cursor()
        x -= self.wx + (self.ww - self.card.w) / 2
        y -= self.top
        for name, (x0, y0, x1, y1) in self.card.buttons.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.send(name)

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
            except Exception:
                cover = None
            self.card.set_cover(cover)
            self.content_key = None

        mode = self.decide(now)
        if mode != self.mode:
            if mode != "hidden" and self.mode != "hidden":
                self.drip(now)  # «бульк» при смене формы
            self.mode = mode
        if mode != "hidden" and playing and now > self.next_bubble:
            self.drip(now, small=True)  # пока играет музыка — иногда булькает
            self.next_bubble = now + random.uniform(2.0, 3.5)
        self.drops = [d for d in self.drops if now - d[0] < d[1]]

        k = self.k
        tw, th = {"big": (self.card.w, self.card.h), "pill": (self.card.pw, self.card.ph),
                  "hidden": (PILL[0] * k * 0.5, 0.0)}[mode]
        self.sw_.step(tw)
        self.sh.step(th)
        sizes = {"big": (self.card.w, self.card.h), "pill": (self.card.pw, self.card.ph)}
        for name, f in self.fade.items():
            # начали проявляться в этом режиме — держим до смены режима, даже пока форма покачивается
            on = mode == name and (f > 0 or closeness(self.sw_.x, self.sh.x, sizes[name]) > 0.6)
            f += ((1.0 if on else 0.0) - f) * (0.22 if on else 0.4)
            self.fade[name] = 1.0 if f > 0.99 else 0.0 if f < 0.01 else f

        content = (time.strftime("%H:%M"), self.weather, m.get("title"), m.get("artist"), playing)
        if content != self.content_key:
            self.content_key = content
            self.big_img = self.card.render_big(content[0], content[1], m)

        visible = self.sh.x > 1 or not self.sh.settled(th)
        if visible:
            self.draw(now, playing)
            self.was_drawn_empty = False
        elif not self.was_drawn_empty:
            push(self.hwnd, Image.new("RGBA", (self.ww, self.wh), (0, 0, 0, 0)), self.wx, 0)
            self.was_drawn_empty = True
        spent = int((time.time() - now) * 1000)
        self.root.after(max(1, 16 - spent), self.tick)  # ~60 кадров/с с учётом времени отрисовки

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

        frame = Image.new("RGBA", (self.ww, self.wh), BG + (0,))
        frame.putalpha(mask.point(lambda v: v * BG_ALPHA // 255))

        layer = Image.new("RGBA", (self.ww, self.wh), (0, 0, 0, 0))
        f = self.fade["big"]
        if f > 0.01:
            layer.alpha_composite(fade(self.big_img, f), (int(cx - self.card.w / 2), self.top))
        f = self.fade["pill"]
        if f > 0.01:
            layer.alpha_composite(fade(self.card.render_pill(now, playing), f), (int(cx - self.card.pw / 2), self.top))
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
    card = Card(1.5)
    assert fit("abc", card.f_title, 1000) == "abc" and fit("a" * 200, card.f_title, 300).endswith("…")
    cover = Image.new("RGB", (300, 200), (200, 80, 160))
    card.set_cover(cover)
    img = card.render_big("12:34", "☀ 5°", {"title": "Очень длинное название трека " * 3, "artist": "Кто-то", "playing": True})
    assert img.size == (card.w, card.h) and set(card.buttons) == {"prev", "play", "next"}
    assert card.render_pill(1.0, True).size == (card.pw, card.ph)
    if len(sys.argv) > 2:
        bg = Image.new("RGBA", img.size, BG + (BG_ALPHA,))
        Image.alpha_composite(bg, img).save(sys.argv[2])
    print("ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        Island().root.mainloop()
