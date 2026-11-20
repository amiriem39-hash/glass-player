# -*- coding: utf-8 -*-
"""
GLASS PLAYER - Kivy (Android)
تبدیل نسخه وب به اپ کیوی. همه‌ی قابلیت‌ها: اسکن آهنگ‌ها، تگ و کاور، جستجو،
مینی‌پلیر، پلیر تمام‌صفحه، دیسک چرخان، ویژوالایزر، شافل/تکرار، رنگ از روی کاور.
"""
import colorsys
import io
import math
import os
import random
import re
import struct
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import DEFAULT_FONT, LabelBase
from kivy.core.window import Window
from kivy.graphics import (Color, Ellipse, Line, PopMatrix, PushMatrix,
                           Rectangle, Rotate, RoundedRectangle, Triangle)
from kivy.graphics.texture import Texture
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty,
                             StringProperty, ObjectProperty)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.slider import Slider
from kivy.uix.widget import Widget
from kivy.utils import platform

try:
    from PIL import Image, ImageOps
except Exception:  # noqa
    Image = ImageOps = None
try:
    from mutagen import File as MFile
except Exception:  # noqa
    MFile = None
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _SHAPE = True
except Exception:  # noqa
    _SHAPE = False

EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus")
SKIP_DIRS = {"android", "lost.dir", "data", "obb"}
MIN_SIZE = 1024 * 1024
SONGS = []
LOCK = threading.Lock()
_AR = re.compile("[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


# ------------------------------------------------------------ text helpers
def fa(t):
    """Reshape Persian/Arabic text so Kivy draws it correctly."""
    if _SHAPE and t and _AR.search(t):
        try:
            return get_display(arabic_reshaper.reshape(t))
        except Exception:
            return t
    return t


def hue_of(s):
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) % 360
    return h


def fmt(t):
    t = max(0, int(t))
    return "%d:%02d" % (t // 60, t % 60)


def setup_font():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "font.ttf"),
              "/system/fonts/NotoNaskhArabic-Regular.ttf",
              "/system/fonts/NotoSansArabic-Regular.ttf",
              "/system/fonts/DroidSansArabic.ttf"):
        if os.path.isfile(p):
            LabelBase.register(DEFAULT_FONT, fn_regular=p, fn_bold=p)
            return


def get_activity():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


# ------------------------------------------------------------ library
def storage_roots():
    roots = [p for p in ("/storage/emulated/0", "/sdcard") if os.path.isdir(p)]
    try:
        for n in os.listdir("/storage"):
            p = os.path.join("/storage", n)
            if n not in ("emulated", "self") and os.path.isdir(p):
                roots.append(p)
    except Exception:
        pass
    return roots or [os.path.expanduser("~")]


def guess_names(path):
    name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    name = re.sub(r"\s*[\(\[]\s*\d{2,3}\s*(k|kbps)?\s*[\)\]]", "", name).strip()
    if " - " in name:
        a, t = name.split(" - ", 1)
        return t.strip() or name, a.strip()
    return name, ""


def _ss(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def id3_iter(path, want):
    with open(path, "rb") as f:
        h = f.read(10)
        if len(h) < 10 or h[:3] != b"ID3":
            return
        ver, fl, size = h[3], h[5], _ss(h[6:10])
        if fl & 0x80:
            src = io.BytesIO(f.read(size).replace(b"\xff\x00", b"\xff"))
            base, end = 0, size
        else:
            src, base, end = f, 10, 10 + size
        pos = base
        if fl & 0x40:
            src.seek(base)
            e = src.read(4)
            if len(e) == 4:
                pos = base + (_ss(e) if ver == 4 else int.from_bytes(e, "big") + 4)
        idlen, hl = (3, 6) if ver == 2 else (4, 10)
        while pos + hl <= end:
            src.seek(pos)
            fh = src.read(hl)
            if len(fh) < hl or fh[0] == 0:
                break
            fid = fh[:idlen].decode("latin1")
            ff = 0
            if ver == 2:
                n = int.from_bytes(fh[3:6], "big")
            elif ver == 4:
                n, ff = _ss(fh[4:8]), fh[9]
            else:
                n = int.from_bytes(fh[4:8], "big")
            if n <= 0 or pos + hl + n > end:
                break
            if fid in want:
                data = src.read(n)
                if ver == 4:
                    if ff & 1:
                        data = data[4:]
                    if ff & 2:
                        data = data.replace(b"\xff\x00", b"\xff")
                yield fid, data, ver
            pos += hl + n


def dec_text(b):
    if not b:
        return ""
    e, t = b[0], b[1:]
    try:
        if e == 1:
            s = t.decode("utf-16")
        elif e == 2:
            s = t.decode("utf-16-be")
        elif e == 3:
            s = t.decode("utf-8")
        else:
            try:
                s = t.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    s = t.decode("cp1256")
                except UnicodeDecodeError:
                    s = t.decode("latin1")
    except Exception:
        s = t.decode("utf-8", "ignore")
    return s.split("\x00")[0].strip()


def sniff_image(data):
    for magic, mime in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"),
                        (b"GIF8", "image/gif"), (b"RIFF", "image/webp")):
        i = data.find(magic)
        if 0 <= i < 400:
            return data[i:], mime
    return None


def parse_apic(data, ver):
    try:
        e = data[0]
        i = 4 if ver == 2 else data.index(b"\x00", 1) + 1
        i += 1
        if e in (1, 2):
            while i + 1 < len(data) and data[i:i + 2] != b"\x00\x00":
                i += 2
            i += 2
        else:
            j = data.find(b"\x00", i)
            i = j + 1 if j >= 0 else len(data)
        r = sniff_image(data[i:])
        if r:
            return r
    except Exception:
        pass
    return sniff_image(data)


def flac_cover(path):
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            return None
        while True:
            h = f.read(4)
            if len(h) < 4:
                return None
            last, typ = h[0] & 0x80, h[0] & 0x7F
            n = int.from_bytes(h[1:4], "big")
            if typ == 6:
                d = f.read(n)
                p = 4
                ml = struct.unpack(">I", d[p:p + 4])[0]
                p += 4 + ml
                dl = struct.unpack(">I", d[p:p + 4])[0]
                p += 4 + dl + 16
                il = struct.unpack(">I", d[p:p + 4])[0]
                return sniff_image(d[p + 4:p + 4 + il])
            f.seek(n, 1)
            if last:
                return None


def read_tags(path):
    title, artist = guess_names(path)
    got_t = got_a = False
    try:
        for fid, data, _v in id3_iter(path, ("TIT2", "TT2", "TPE1", "TP1")):
            txt = dec_text(data)
            if not txt:
                continue
            if fid in ("TIT2", "TT2"):
                title, got_t = txt, True
            else:
                artist, got_a = txt, True
            if got_t and got_a:
                break
    except Exception:
        pass
    if MFile and not (got_t or got_a):
        try:
            a = MFile(path, easy=True)
            if a is not None and a.tags:
                title = (a.tags.get("title") or [title])[0] or title
                artist = (a.tags.get("artist") or [artist])[0] or artist
        except Exception:
            pass
    return title, artist or "Unknown artist"


def scan():
    found, seen = [], set()
    for root in storage_roots():
        for dp_, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            for f in files:
                if not f.lower().endswith(EXTS):
                    continue
                p = os.path.join(dp_, f)
                try:
                    rp = os.path.realpath(p)
                    if rp in seen or os.path.getsize(p) < MIN_SIZE:
                        continue
                    seen.add(rp)
                    found.append(p)
                except Exception:
                    pass
    found.sort(key=lambda p: os.path.basename(p).lower())
    songs = []
    for i, p in enumerate(found):
        t, a = read_tags(p)
        songs.append({"id": i, "path": p, "title": t, "artist": a})
    with LOCK:
        SONGS[:] = songs
    return len(songs)


def read_cover(path):
    try:
        for fid, data, ver in id3_iter(path, ("APIC", "PIC")):
            r = parse_apic(data, ver)
            if r:
                return r
    except Exception:
        pass
    try:
        if path.lower().endswith(".flac"):
            r = flac_cover(path)
            if r:
                return r
    except Exception:
        pass
    if MFile:
        try:
            a = MFile(path)
            pics = getattr(a, "pictures", None)
            if pics:
                return pics[0].data, pics[0].mime or "image/jpeg"
            tags = getattr(a, "tags", None)
            if tags is not None:
                for k in list(tags.keys()):
                    if str(k).startswith("APIC"):
                        fr = tags[k]
                        return fr.data, fr.mime or "image/jpeg"
                if "covr" in tags and tags["covr"]:
                    return bytes(tags["covr"][0]), "image/jpeg"
        except Exception:
            pass
    folder = os.path.dirname(path)
    for n in ("cover.jpg", "folder.jpg", "cover.png", "folder.png"):
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read(), "image/jpeg"
    return None


# ------------------------------------------------------------ covers (texture cache)
class Covers:
    def __init__(self):
        self.cache = OrderedDict()
        self.pending = {}
        self.ex = ThreadPoolExecutor(2)

    def clear(self):
        self.cache.clear()

    def get(self, path, size, cb):
        key = (path, size)
        if key in self.cache:
            self.cache.move_to_end(key)
            cb(*self.cache[key])
            return
        if key in self.pending:
            self.pending[key].append(cb)
            return
        self.pending[key] = [cb]
        self.ex.submit(self._work, key)

    def _work(self, key):
        path, size = key
        res = None
        try:
            r = read_cover(path)
            if r and Image:
                im = Image.open(io.BytesIO(r[0]))
                try:
                    im.draft("RGB", (size * 2, size * 2))
                except Exception:
                    pass
                im = ImageOps.fit(im.convert("RGBA"), (size, size))
                px = list(im.resize((8, 8)).convert("RGB").getdata())
                rr = sum(p[0] for p in px) / 64.0
                gg = sum(p[1] for p in px) / 64.0
                bb = sum(p[2] for p in px) / 64.0
                hue = None
                if max(rr, gg, bb) - min(rr, gg, bb) >= 12:
                    hue = colorsys.rgb_to_hsv(rr / 255, gg / 255, bb / 255)[0] * 360
                res = (size, size, im.tobytes(), hue)
        except Exception:
            res = None
        Clock.schedule_once(lambda dt: self._done(key, res))

    def _done(self, key, res):
        val = (None, None)
        if res:
            w, h, data, hue = res
            tex = Texture.create(size=(w, h), colorfmt="rgba")
            tex.blit_buffer(data, colorfmt="rgba", bufferfmt="ubyte")
            tex.flip_vertical()
            val = (tex, hue)
        self.cache[key] = val
        while len(self.cache) > 260:
            self.cache.popitem(last=False)
        for cb in self.pending.pop(key, []):
            cb(*val)


# ------------------------------------------------------------ audio engine
if platform == "android":
    from jnius import autoclass, PythonJavaClass, java_method
    MediaPlayer = autoclass("android.media.MediaPlayer")

    class _Done(PythonJavaClass):
        __javainterfaces__ = ["android/media/MediaPlayer$OnCompletionListener"]
        __javacontext__ = "app"

        def __init__(self, cb):
            super().__init__()
            self.cb = cb

        @java_method("(Landroid/media/MediaPlayer;)V")
        def onCompletion(self, mp):
            self.cb()

    class _Err(PythonJavaClass):
        __javainterfaces__ = ["android/media/MediaPlayer$OnErrorListener"]
        __javacontext__ = "app"

        def __init__(self, cb):
            super().__init__()
            self.cb = cb

        @java_method("(Landroid/media/MediaPlayer;II)Z")
        def onError(self, mp, what, extra):
            self.cb()
            return True

    class Engine:
        def __init__(self, on_end, on_err):
            self.on_end, self.on_err = on_end, on_err
            self.mp, self.gen, self._refs = None, 0, []

        def release(self):
            if self.mp:
                try:
                    self.mp.release()
                except Exception:
                    pass
            self.mp = None

        def play(self, path):
            self.gen += 1
            g = self.gen
            self.release()
            mp = None
            try:
                mp = MediaPlayer()
                d = _Done(lambda: Clock.schedule_once(lambda dt: self.on_end(g)))
                e = _Err(lambda: Clock.schedule_once(lambda dt: self.on_err(g)))
                self._refs = [d, e]
                mp.setOnCompletionListener(d)
                mp.setOnErrorListener(e)
                try:
                    mp.setWakeMode(get_activity().getApplicationContext(), 1)
                except Exception:
                    pass
                mp.setDataSource(path)
                mp.prepare()
                mp.start()
                self.mp = mp
                return True
            except Exception:
                try:
                    mp.release()
                except Exception:
                    pass
                return False

        def _c(self, fn, default=0):
            try:
                return fn() if self.mp else default
            except Exception:
                return default

        def pos(self):
            return self._c(lambda: self.mp.getCurrentPosition() / 1000.0)

        def dur(self):
            return self._c(lambda: max(0, self.mp.getDuration()) / 1000.0)

        def playing(self):
            return bool(self._c(lambda: self.mp.isPlaying(), False))

        def pause(self):
            self._c(lambda: self.mp.pause())

        def resume(self):
            self._c(lambda: self.mp.start())

        def seek(self, sec):
            self._c(lambda: self.mp.seekTo(int(sec * 1000)))
else:
    from kivy.core.audio import SoundLoader

    class Engine:  # desktop fallback (testing only)
        def __init__(self, on_end, on_err):
            self.on_end, self.on_err = on_end, on_err
            self.s, self.gen, self._m, self._p = None, 0, False, 0

        def release(self):
            if self.s:
                self._m = True
                self.s.stop()
                self._m = False
            self.s = None

        def play(self, path):
            self.gen += 1
            g = self.gen
            self.release()
            s = SoundLoader.load(path)
            if not s:
                return False
            s.bind(on_stop=lambda *a: (not self._m) and g == self.gen and
                   Clock.schedule_once(lambda dt: self.on_end(g)))
            s.play()
            self.s = s
            return True

        def pos(self):
            return self.s.get_pos() if self.s else 0

        def dur(self):
            return (self.s.length or 0) if self.s else 0

        def playing(self):
            return bool(self.s and self.s.state == "play")

        def pause(self):
            if self.s:
                self._p = self.pos()
                self._m = True
                self.s.stop()
                self._m = False

        def resume(self):
            if self.s:
                self.s.play()
                self.s.seek(self._p)

        def seek(self, sec):
            if self.s:
                self.s.seek(sec)


# ------------------------------------------------------------ widgets
def draw_icon(k, cx, cy, sz, col):
    u = sz / 24.0
    X = lambda v: cx + (v - 12) * u
    Y = lambda v: cy - (v - 12) * u
    P = lambda *p: [c for x, y in p for c in (X(x), Y(y))]
    w = max(dp(1.4), 1.9 * u)
    Color(*col)

    def L(*p):
        Line(points=P(*p), width=w, cap="round", joint="round")
    if k == "play":
        Triangle(points=P((8, 5), (8, 19), (20, 12)))
    elif k == "pause":
        Rectangle(pos=(X(6.3), Y(19)), size=(4.2 * u, 14 * u))
        Rectangle(pos=(X(13.5), Y(19)), size=(4.2 * u, 14 * u))
    elif k == "next":
        Triangle(points=P((6, 6), (6, 18), (17, 12)))
        Rectangle(pos=(X(17), Y(18.5)), size=(2.7 * u, 13 * u))
    elif k == "prev":
        Triangle(points=P((18, 6), (18, 18), (7, 12)))
        Rectangle(pos=(X(4.3), Y(18.5)), size=(2.7 * u, 13 * u))
    elif k == "down":
        L((6, 9), (12, 15), (18, 9))
    elif k == "shuffle":
        L((3, 7), (7, 7), (16, 17), (20, 17))
        L((3, 17), (7, 17), (16, 7), (20, 7))
        L((17.5, 4.5), (20.5, 7), (17.5, 9.5))
        L((17.5, 14.5), (20.5, 17), (17.5, 19.5))
    elif k in ("repeat", "repeat1"):
        L((5, 12), (5, 8), (19, 8))
        L((16.5, 5.5), (19.5, 8), (16.5, 10.5))
        L((19, 12), (19, 16), (5, 16))
        L((7.5, 13.5), (4.5, 16), (7.5, 18.5))
        if k == "repeat1":
            L((10.8, 11), (12.5, 10), (12.5, 14))
    elif k == "refresh":
        Line(circle=(cx, cy, 8 * u, 20, 330), width=w, cap="round")
        a = math.radians(330)
        px, py = cx + 8 * u * math.sin(a), cy + 8 * u * math.cos(a)
        tx, ty, nx, ny = math.cos(a), -math.sin(a), math.sin(a), math.cos(a)
        Triangle(points=[px + tx * 5 * u, py + ty * 5 * u,
                         px + nx * 3.5 * u, py + ny * 3.5 * u,
                         px - nx * 3.5 * u, py - ny * 3.5 * u])


class Glass(BoxLayout):
    rad = NumericProperty(dp(20))
    fill = NumericProperty(.10)


class Mini(ButtonBehavior, Glass):
    pass


class GBtn(ButtonBehavior, Widget):
    kind = StringProperty("play")
    style = StringProperty("plain")      # plain | glass | accent
    active = BooleanProperty(False)
    isz = NumericProperty(.5)

    def __init__(self, **kw):
        super().__init__(**kw)
        for p in ("pos", "size", "kind", "style", "active", "state", "isz"):
            self.fbind(p, self.draw)
        app = App.get_running_app()
        app.fbind("c1", self.draw)
        app.fbind("c2", self.draw)
        self.draw()

    def draw(self, *a):
        app = App.get_running_app()
        cx, cy = self.center
        d = min(self.size)
        r = d / 2
        down = self.state == "down"
        self.canvas.clear()
        with self.canvas:
            if self.style == "glass":
                Color(1, 1, 1, .30 if down else .16)
                RoundedRectangle(pos=(cx - r, cy - r), size=(d, d), radius=[d * .36])
                Color(1, 1, 1, .26)
                Line(rounded_rectangle=(cx - r, cy - r, d, d, d * .36), width=1)
            elif self.style == "accent":
                c1, c2 = app.c1, app.c2
                Color(c2[0], c2[1], c2[2], .22)
                Ellipse(pos=(cx - r * 1.18, cy - r * 1.18), size=(d * 1.18, d * 1.18))
                Color(*c1)
                Ellipse(pos=(cx - r, cy - r), size=(d, d))
                Color(c2[0], c2[1], c2[2], .55)
                Ellipse(pos=(cx - r * .45, cy - r * .85), size=(d * .65, d * .65))
                Color(1, 1, 1, .26)
                Ellipse(pos=(cx - r * .55, cy + r * .1), size=(r, r * .65))
            if self.style == "plain" and self.active:
                Color(*app.c2)
                Ellipse(pos=(cx - dp(2.5), cy - r + dp(1)), size=(dp(5), dp(5)))
            on = self.style != "plain" or self.active
            draw_icon(self.kind, cx, cy, d * self.isz * (.92 if down else 1),
                      (1, 1, 1, 1 if on else .5))


class CoverBox(FloatLayout):
    rad = NumericProperty(dp(15))
    hue = NumericProperty(0)
    letter = StringProperty("")
    path = StringProperty("")
    texture = ObjectProperty(None, allownone=True)
    bg = ListProperty([.6, .2, .8, 1])

    def on_hue(self, *a):
        r, g, b = colorsys.hls_to_rgb((self.hue % 360) / 360., .42, .8)
        self.bg = [r, g, b, 1]

    def set_cover(self, path, size, cb=None):
        if path == self.path and self.texture is not None:
            return
        self.path = path
        self.texture = None
        App.get_running_app().covers.get(path, size, lambda t, h: self._got(path, t, h, cb))

    def _got(self, path, tex, h, cb):
        if path == self.path:
            self.texture = tex
            if cb:
                cb(tex, h)


class Backdrop(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.draw, size=self.draw)
        app = App.get_running_app()
        app.bind(c1=self.draw, c2=self.draw)
        self.draw()

    def draw(self, *a):
        app = App.get_running_app()
        self.canvas.clear()
        m = max(self.size)
        with self.canvas:
            Color(.03, .024, .07, 1)
            Rectangle(pos=self.pos, size=self.size)
            for col, cx, cy in ((app.c1, self.x + self.width * .1, self.top - self.height * .05),
                                (app.c2, self.right - self.width * .05, self.y + self.height * .05)):
                for k in range(7):
                    r = m * (.75 - k * .08)
                    Color(col[0], col[1], col[2], .07)
                    Ellipse(pos=(cx - r / 2, cy - r / 2), size=(r, r))


class Halo(Widget):
    scale = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.draw, size=self.draw, scale=self.draw)
        App.get_running_app().bind(c1=self.draw)
        self.draw()

    def draw(self, *a):
        c = App.get_running_app().c1
        k = 1 + self.scale * .3
        self.canvas.clear()
        with self.canvas:
            for i in range(8):
                r = min(self.size) * .5 * (1 - i / 8.0) * k
                Color(c[0], c[1], c[2], .07)
                Ellipse(pos=(self.center_x - r, self.center_y - r), size=(2 * r, 2 * r))


class Disc(Widget):
    ang = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rot = None
        self.bind(pos=self.draw, size=self.draw)
        App.get_running_app().bind(c1=self.draw)
        self.draw()

    def draw(self, *a):
        cx, cy = self.center
        r = min(self.size) / 2
        c = App.get_running_app().c1
        self.canvas.clear()
        with self.canvas:
            PushMatrix()
            self.rot = Rotate(angle=-self.ang, origin=(cx, cy))
            Color(.05, .05, .07, 1)
            Ellipse(pos=(cx - r, cy - r), size=(2 * r, 2 * r))
            Color(.16, .16, .2, 1)
            for k in range(1, 12):
                Line(circle=(cx, cy, r * (1 - k * .055)), width=1)
            Color(*c)
            Ellipse(pos=(cx - r * .28, cy - r * .28), size=(r * .56, r * .56))
            Color(1, 1, 1, .5)
            Ellipse(pos=(cx - r * .04, cy + r * .1), size=(r * .08, r * .08))
            PopMatrix()

    def on_ang(self, *a):
        if self.rot:
            self.rot.angle = -self.ang


class Viz(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.draw, size=self.draw)

    def draw(self, *a):
        app = App.get_running_app()
        n = len(app.bars)
        bw = self.width / n
        mid = self.center_y
        self.canvas.clear()
        with self.canvas:
            for i, v in enumerate(app.bars):
                bh = max(dp(3), v * self.height * .95)
                Color(*colorsys.hsv_to_rgb(((app.hue + i * 4) % 360) / 360., .6, 1), .95)
                RoundedRectangle(pos=(self.x + i * bw + bw * .22, mid - bh / 2),
                                 size=(bw * .56, bh), radius=[bw * .28])


class Eq(Widget):
    active = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.draw, size=self.draw, active=self.draw)
        App.get_running_app().bind(eqt=self.draw)

    def draw(self, *a):
        self.canvas.clear()
        if not self.active:
            return
        app = App.get_running_app()
        w = self.width / 5.0
        with self.canvas:
            Color(*app.c2)
            for j in range(3):
                f = .3 + .7 * (.5 + .5 * math.sin(app.eqt + j * 2.1)) if app.playing else .3
                RoundedRectangle(pos=(self.x + j * w * 2, self.y), size=(w, self.height * f),
                                 radius=[w / 2])


class Orb(FloatLayout):
    frac = NumericProperty(0)


class Seek(Slider):
    def on_touch_down(self, t):
        if self.collide_point(*t.pos):
            App.get_running_app().drag = True
        return super().on_touch_down(t)

    def on_touch_up(self, t):
        r = super().on_touch_up(t)
        app = App.get_running_app()
        if app.drag:
            app.drag = False
            app.engine.seek(self.value / 1000.0 * app.engine.dur())
        return r

    def on_value(self, *a):
        app = App.get_running_app()
        if app.drag:
            app.t0 = fmt(self.value / 1000.0 * app.engine.dur())


class Stage(FloatLayout):
    tx = NumericProperty(0)
    ty = NumericProperty(0)

    def _tilt(self, t):
        rx = (t.x - self.x) / max(1, self.width) - .5
        ry = (t.y - self.y) / max(1, self.height) - .5
        Animation.cancel_all(self, "tx", "ty")
        self.tx, self.ty = rx * 34, -ry * 34

    def on_touch_down(self, t):
        if self.collide_point(*t.pos):
            t.grab(self)
            self._tilt(t)
            return True
        return super().on_touch_down(t)

    def on_touch_move(self, t):
        if t.grab_current is self:
            self._tilt(t)
            return True
        return super().on_touch_move(t)

    def on_touch_up(self, t):
        if t.grab_current is self:
            t.ungrab(self)
            Animation(tx=0, ty=0, d=.35, t="out_cubic").start(self)
            return True
        return super().on_touch_up(t)


class Sheet(FloatLayout):
    opened = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(height=self._h)

    def _h(self, *a):
        if not self.opened:
            self.y = -self.height * 1.05

    def show(self):
        self.opened = True
        Animation.cancel_all(self, "y")
        Animation(y=0, d=.45, t="out_cubic").start(self)

    def hide(self):
        self.opened = False
        Animation.cancel_all(self, "y")
        Animation(y=-self.height * 1.05, d=.35, t="in_cubic").start(self)

    def on_touch_down(self, t):
        return super().on_touch_down(t) or (self.opened and self.collide_point(*t.pos))

    def on_touch_move(self, t):
        return super().on_touch_move(t) or self.opened

    def on_touch_up(self, t):
        return super().on_touch_up(t) or self.opened


class SongRow(RecycleDataViewBehavior, ButtonBehavior, Glass):
    idx = NumericProperty(0)
    path = StringProperty("")
    title = StringProperty("")
    artist = StringProperty("")
    letter = StringProperty("")
    hue = NumericProperty(0)
    ha = StringProperty("left")
    cur = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        super().refresh_view_attrs(rv, index, data)
        self.ids.cov.set_cover(self.path, 160)


class Root(FloatLayout):
    pass


KV = r'''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp

<Toast@Label>:
    canvas.before:
        Color:
            rgba: 0, 0, 0, .7
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

<Glass>:
    canvas.before:
        Color:
            rgba: 1, 1, 1, self.fill
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.rad]
        Color:
            rgba: 1, 1, 1, .22
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, self.rad)
            width: 1

<CoverBox>:
    canvas:
        Color:
            rgba: self.bg
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.rad]
        Color:
            rgba: 1, 1, 1, (1 if self.texture else 0)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.rad]
            texture: self.texture
    Label:
        text: root.letter
        font_size: root.height * .42
        opacity: 0 if root.texture else 1

<Orb>:
    canvas.before:
        Color:
            rgba: 1, 1, 1, .14
        Line:
            circle: self.center_x, self.center_y, self.width * .47
            width: dp(2)
        Color:
            rgba: app.c2
        Line:
            circle: self.center_x, self.center_y, self.width * .47, 0, max(.5, 360 * self.frac)
            width: dp(3)
            cap: 'round'

<SongRow>:
    size_hint_y: None
    height: dp(78)
    padding: dp(10)
    spacing: dp(12)
    rad: dp(22)
    fill: .09
    on_release: app.play_view(self.idx)
    canvas.after:
        Color:
            rgba: app.c2[0], app.c2[1], app.c2[2], (.9 if self.cur else 0)
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, self.rad)
            width: dp(1.2)
    CoverBox:
        id: cov
        size_hint: None, None
        size: dp(58), dp(58)
        pos_hint: {'center_y': .5}
        hue: root.hue
        letter: root.letter
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(3)
        Label:
            text: root.title
            font_size: sp(16)
            halign: root.ha
            valign: 'bottom'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
        Label:
            text: root.artist
            font_size: sp(13)
            color: 1, 1, 1, .65
            halign: root.ha
            valign: 'top'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
    Eq:
        size_hint: None, None
        size: dp(26), dp(18)
        pos_hint: {'center_y': .5}
        active: root.cur

<Mini>:
    rad: dp(26)
    fill: .13
    padding: dp(9), dp(9), dp(12), dp(9)
    spacing: dp(12)
    canvas.after:
        Color:
            rgba: 1, 1, 1, .14
        RoundedRectangle:
            pos: self.x + dp(22), self.y + dp(4)
            size: self.width - dp(44), dp(3)
            radius: [dp(1.5)]
        Color:
            rgba: app.c2
        RoundedRectangle:
            pos: self.x + dp(22), self.y + dp(4)
            size: (self.width - dp(44)) * app.frac, dp(3)
            radius: [dp(1.5)]
    CoverBox:
        id: cov
        size_hint: None, None
        size: dp(48), dp(48)
        rad: dp(13)
        pos_hint: {'center_y': .5}
        hue: app.cur_hue
        letter: app.cur_letter
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: app.cur_title
            font_size: sp(15)
            halign: 'left'
            valign: 'bottom'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
        Label:
            text: app.cur_artist
            font_size: sp(12)
            color: 1, 1, 1, .65
            halign: 'left'
            valign: 'top'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
    GBtn:
        kind: 'pause' if app.playing else 'play'
        style: 'accent'
        isz: .46
        size_hint: None, None
        size: dp(44), dp(44)
        pos_hint: {'center_y': .5}
        on_release: app.toggle()

<Stage>:
    Halo:
        size_hint: 1.32, 1.32
        pos_hint: {'center_x': .5, 'center_y': .5}
        scale: app.bass
    Disc:
        id: disc
        size_hint: .88, .88
        pos_hint: {'x': .19, 'y': .06}
    FloatLayout:
        id: art
        canvas.before:
            PushMatrix
            Rotate:
                angle: root.ty
                axis: 1, 0, 0
                origin: self.center
            Rotate:
                angle: root.tx
                axis: 0, 1, 0
                origin: self.center
        canvas.after:
            PopMatrix
        CoverBox:
            id: cov
            rad: dp(28)
            hue: app.cur_hue
            letter: app.cur_letter

<Sheet>:
    Backdrop:
        pos_hint: {'x': 0, 'y': 0}
    Widget:
        pos_hint: {'x': 0, 'y': 0}
        canvas:
            Color:
                rgba: 0, 0, 0, .4
            Rectangle:
                pos: self.pos
                size: self.size
    BoxLayout:
        pos_hint: {'x': 0, 'y': 0}
        orientation: 'vertical'
        padding: dp(22), dp(10), dp(22), dp(18)
        spacing: dp(8)
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            GBtn:
                kind: 'down'
                style: 'glass'
                size_hint: None, None
                size: dp(44), dp(44)
                on_release: root.hide()
            Widget:
        Stage:
            id: stage
            size_hint: None, None
            size: [min(root.width * .7, root.height * .34, dp(320))] * 2
            pos_hint: {'center_x': .5}
        Label:
            text: app.cur_title
            font_size: sp(24)
            size_hint_y: None
            height: dp(70)
            text_size: self.size
            halign: 'center'
            valign: 'middle'
            max_lines: 2
        Label:
            text: app.cur_artist
            font_size: sp(15)
            color: 1, 1, 1, .7
            size_hint_y: None
            height: dp(26)
            text_size: self.size
            halign: 'center'
            valign: 'middle'
            shorten: True
        Viz:
            id: viz
            size_hint_y: None
            height: dp(64)
        BoxLayout:
            size_hint_y: None
            height: dp(36)
            spacing: dp(8)
            Label:
                text: app.t0
                font_size: sp(12)
                color: 1, 1, 1, .7
                size_hint_x: None
                width: dp(40)
            Seek:
                id: sk
                min: 0
                max: 1000
                value: 0
                cursor_size: dp(20), dp(20)
                value_track: True
                value_track_color: app.c2
                value_track_width: dp(4)
            Label:
                text: app.t1
                font_size: sp(12)
                color: 1, 1, 1, .7
                size_hint_x: None
                width: dp(40)
        Widget:
        BoxLayout:
            size_hint_y: None
            height: dp(108)
            spacing: dp(6)
            Widget:
            GBtn:
                kind: 'shuffle'
                active: app.shuffle
                isz: .56
                size_hint: None, None
                size: dp(42), dp(42)
                pos_hint: {'center_y': .5}
                on_release: app.toggle_shuffle()
            GBtn:
                kind: 'prev'
                style: 'glass'
                isz: .46
                size_hint: None, None
                size: dp(56), dp(56)
                pos_hint: {'center_y': .5}
                on_release: app.go_prev()
            Orb:
                frac: app.frac
                size_hint: None, None
                size: dp(104), dp(104)
                pos_hint: {'center_y': .5}
                GBtn:
                    kind: 'pause' if app.playing else 'play'
                    style: 'accent'
                    isz: .42
                    size_hint: .78, .78
                    pos_hint: {'center_x': .5, 'center_y': .5}
                    on_release: app.toggle()
            GBtn:
                kind: 'next'
                style: 'glass'
                isz: .46
                size_hint: None, None
                size: dp(56), dp(56)
                pos_hint: {'center_y': .5}
                on_release: app.go_next()
            GBtn:
                kind: 'repeat1' if app.repeat == 2 else 'repeat'
                active: app.repeat > 0
                isz: .56
                size_hint: None, None
                size: dp(42), dp(42)
                pos_hint: {'center_y': .5}
                on_release: app.cycle_repeat()
            Widget:

<Root>:
    Backdrop:
        pos_hint: {'x': 0, 'y': 0}
    BoxLayout:
        pos_hint: {'x': 0, 'y': 0}
        orientation: 'vertical'
        padding: 0, dp(8), 0, 0
        BoxLayout:
            size_hint_y: None
            height: dp(62)
            padding: dp(16), 0
            BoxLayout:
                orientation: 'vertical'
                Label:
                    text: 'Glass Player'
                    font_size: sp(28)
                    halign: 'left'
                    valign: 'bottom'
                    text_size: self.size
                Label:
                    text: app.count_text
                    font_size: sp(12)
                    color: 1, 1, 1, .6
                    halign: 'left'
                    valign: 'top'
                    text_size: self.size
            GBtn:
                kind: 'refresh'
                style: 'glass'
                isz: .5
                size_hint: None, None
                size: dp(42), dp(42)
                pos_hint: {'center_y': .5}
                on_release: app.rescan()
        BoxLayout:
            size_hint_y: None
            height: dp(58)
            padding: dp(14), dp(4)
            Glass:
                rad: dp(18)
                padding: dp(16), 0
                TextInput:
                    id: q
                    hint_text: 'Search title or artist'
                    multiline: False
                    background_normal: ''
                    background_active: ''
                    background_color: 0, 0, 0, 0
                    foreground_color: 1, 1, 1, 1
                    hint_text_color: 1, 1, 1, .55
                    cursor_color: app.c2
                    font_size: sp(16)
                    padding: [0, (self.height - self.line_height) / 2]
                    on_text: app.set_query(self.text)
        RecycleView:
            id: rv
            viewclass: 'SongRow'
            bar_width: dp(3)
            RecycleBoxLayout:
                default_size: None, dp(78)
                default_size_hint: 1, None
                size_hint_y: None
                height: self.minimum_height
                orientation: 'vertical'
                spacing: dp(10)
                padding: dp(14), dp(6), dp(14), dp(130)
    Label:
        text: 'No songs found.\nGive the app storage access,\nthen tap refresh.'
        halign: 'center'
        color: 1, 1, 1, .6
        pos_hint: {'center_x': .5, 'center_y': .45}
        opacity: 0 if app.nsongs else 1
    Mini:
        id: mini
        size_hint: None, None
        size: root.width - dp(24), dp(66)
        pos: dp(12), dp(12)
        opacity: 1 if app.has_song else 0
        disabled: not app.has_song
        on_release: sheet.show()
    Sheet:
        id: sheet
    Toast:
        id: toast
        size_hint: None, None
        size: self.texture_size[0] + dp(32), dp(40)
        pos_hint: {'center_x': .5, 'top': .96}
        opacity: 0
'''


# ------------------------------------------------------------ app
class GlassApp(App):
    title = "Glass Player"
    c1 = ListProperty([.486, .302, 1, 1])
    c2 = ListProperty([0, .898, 1, 1])
    has_song = BooleanProperty(False)
    playing = BooleanProperty(False)
    shuffle = BooleanProperty(False)
    repeat = NumericProperty(0)
    cur_title = StringProperty("")
    cur_artist = StringProperty("")
    cur_letter = StringProperty("")
    cur_hue = NumericProperty(0)
    count_text = StringProperty("loading...")
    nsongs = NumericProperty(0)
    t0 = StringProperty("0:00")
    t1 = StringProperty("0:00")
    frac = NumericProperty(0)
    eqt = NumericProperty(0)
    bass = NumericProperty(0)

    def build(self):
        setup_font()
        Window.clearcolor = (.03, .024, .07, 1)
        Window.softinput_mode = "below_target"
        self.songs, self.view, self.q = [], [], ""
        self.cur_path = None
        self.hue, self.t, self.ang, self.errs = 260, 0.0, 0.0, 0
        self.bars = [0.0] * 44
        self.drag = False
        self.covers = Covers()
        self.engine = Engine(self.on_end, self.on_err)
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        return Root()

    def on_start(self):
        ids = self.root.ids
        self.sheet, self.rv, self.mini = ids.sheet, ids.rv, ids.mini
        self.sk = self.sheet.ids.sk
        self.viz = self.sheet.ids.viz
        self.disc = self.sheet.ids.stage.ids.disc
        self.stage_cov = self.sheet.ids.stage.ids.cov
        Clock.schedule_interval(self.tick, 1 / 24.)
        if platform == "android":
            from android.permissions import request_permissions
            request_permissions(["android.permission.READ_EXTERNAL_STORAGE",
                                 "android.permission.READ_MEDIA_AUDIO"],
                                lambda p, g: Clock.schedule_once(lambda dt: self.rescan(True)))
        else:
            self.rescan()

    def on_pause(self):
        return True

    def on_resume(self):
        if not self.songs:
            self.rescan()

    def on_stop(self):
        self.engine.release()

    def on_key(self, win, key, *a):
        if key == 27:
            if self.sheet.opened:
                self.sheet.hide()
                return True
            if platform == "android":
                try:
                    get_activity().moveTaskToBack(True)
                    return True
                except Exception:
                    return False
        return False

    # ---- library
    def rescan(self, first=False):
        self.toast("Scanning...")
        threading.Thread(target=self._scan_job, args=(first,), daemon=True).start()

    def _scan_job(self, first):
        n = scan()
        Clock.schedule_once(lambda dt: self._scanned(n, first))

    def _scanned(self, n, first):
        self.covers.clear()
        with LOCK:
            self.songs = list(SONGS)
        self.refresh()
        self.toast("%d songs found" % n)
        if first and n == 0 and platform == "android":
            self.ask_all_files()

    def ask_all_files(self):
        try:
            from jnius import autoclass
            if autoclass("android.os.Build$VERSION").SDK_INT >= 30:
                Env = autoclass("android.os.Environment")
                if not Env.isExternalStorageManager():
                    Intent = autoclass("android.content.Intent")
                    Settings = autoclass("android.provider.Settings")
                    Uri = autoclass("android.net.Uri")
                    act = get_activity()
                    act.startActivity(Intent(
                        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        Uri.parse("package:" + act.getPackageName())))
        except Exception:
            pass

    def set_query(self, text):
        self.q = text.strip().lower()
        self.refresh()

    def refresh(self):
        q = self.q
        self.view = [s for s in self.songs if q in (s["title"] + " " + s["artist"]).lower()]
        data = []
        for i, s in enumerate(self.view):
            if "td" not in s:
                s["td"], s["ad"] = fa(s["title"]), fa(s["artist"])
                s["hue"] = hue_of(s["title"])
                s["rtl"] = bool(_AR.search(s["title"]))
                s["letter"] = (s["title"] or "\u266a")[0]
            data.append({"idx": i, "path": s["path"], "title": s["td"], "artist": s["ad"],
                         "letter": s["letter"], "hue": s["hue"],
                         "ha": "right" if s["rtl"] else "left",
                         "cur": s["path"] == self.cur_path})
        self.rv.data = data
        self.nsongs = len(self.songs)
        self.count_text = "%d songs" % len(self.songs)

    def mark_current(self):
        for d in self.rv.data:
            d["cur"] = d["path"] == self.cur_path
        self.rv.refresh_from_data()

    def cur_idx(self):
        for i, s in enumerate(self.view):
            if s["path"] == self.cur_path:
                return i
        return -1

    # ---- theme
    def set_hue(self, h):
        self.hue = h
        a = colorsys.hls_to_rgb((h % 360) / 360., .55, .8)
        b = colorsys.hls_to_rgb(((h + 70) % 360) / 360., .55, .9)
        Animation(c1=[a[0], a[1], a[2], 1], c2=[b[0], b[1], b[2], 1], d=.8).start(self)

    def toast(self, msg):
        t = self.root.ids.toast
        t.text = msg
        Animation.cancel_all(t)
        (Animation(opacity=1, d=.2) + Animation(opacity=1, d=1.8) + Animation(opacity=0, d=.3)).start(t)

    # ---- playback
    def play_view(self, i, open_sheet=True):
        if not (0 <= i < len(self.view)):
            return
        if open_sheet:
            self.errs = 0
        self.start(self.view[i])
        if open_sheet:
            self.sheet.show()

    def start(self, s):
        self.cur_path = s["path"]
        self.cur_title, self.cur_artist = s.get("td", s["title"]), s.get("ad", s["artist"])
        self.cur_letter = s.get("letter", "\u266a")
        self.cur_hue = s.get("hue", hue_of(s["title"]))
        self.has_song = True
        self.set_hue(self.cur_hue)
        self.mini.ids.cov.set_cover(s["path"], 160)
        path = s["path"]
        self.stage_cov.set_cover(path, 640, lambda tex, h: h is not None and
                                 self.cur_path == path and self.set_hue(h))
        self.frac, self.t0 = 0, "0:00"
        self.mark_current()
        if self.engine.play(path):
            self.playing = True
        else:
            self.playing = False
            self.fail()

    def fail(self):
        self.errs += 1
        self.toast("Can't play this file, skipping")
        if self.errs <= max(1, len(self.view)):
            Clock.schedule_once(lambda dt: self.go_next(), .6)

    def on_err(self, gen):
        if gen == self.engine.gen:
            self.fail()

    def on_end(self, gen):
        if gen != self.engine.gen:
            return
        if self.repeat == 2:
            self.engine.seek(0)
            self.engine.resume()
        elif self.repeat == 1 or self.shuffle or self.cur_idx() < len(self.view) - 1:
            self.go_next()
        else:
            self.playing = False

    def go_next(self, *a):
        if not self.view:
            return
        i = random.randrange(len(self.view)) if self.shuffle else (self.cur_idx() + 1) % len(self.view)
        self.play_view(i, False)

    def go_prev(self, *a):
        if not self.view:
            return
        if self.engine.pos() > 3:
            self.engine.seek(0)
            return
        self.play_view((max(self.cur_idx(), 0) - 1) % len(self.view), False)

    def toggle(self):
        if not self.cur_path:
            if self.view:
                self.play_view(0, False)
            return
        if self.engine.playing():
            self.engine.pause()
        else:
            self.engine.resume()
        self.playing = self.engine.playing()

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle

    def cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3

    # ---- per-frame
    def tick(self, dt):
        self.t += dt
        if self.cur_path:
            d = self.engine.dur()
            if d > 0:
                pos = self.engine.pos()
                if pos > 2:
                    self.errs = 0
                if not self.drag:
                    self.frac = min(1.0, pos / d)
                    self.sk.value = self.frac * 1000
                    self.t0 = fmt(pos)
                self.t1 = fmt(d)
        opened = self.sheet.opened
        if self.playing:
            if not opened:
                self.eqt += dt * 7
            beat = .55 + .45 * math.sin(self.t * 7.5)
            n = len(self.bars)
            for i in range(n):
                k = abs(i - n / 2.0) / (n / 2.0)
                tgt = (1 - .75 * k) * (.25 + .75 * random.random()) * beat
                self.bars[i] += (tgt - self.bars[i]) * .35
        else:
            self.bars = [b * .85 for b in self.bars]
        if opened or self.sheet.y > -self.sheet.height * .98:
            if self.playing:
                self.ang = (self.ang + dt * 72) % 360
                self.disc.ang = self.ang
            self.bass = sum(self.bars[19:25]) / 6.0
            if self.playing or max(self.bars) > .01:
                self.viz.draw()


if __name__ == "__main__":
    GlassApp().run()
