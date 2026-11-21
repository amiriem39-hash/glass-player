# -*- coding: utf-8 -*-
"""
GLASS PLAYER - Kivy (Android)
تبدیل نسخه وب به اپ کیوی. همه‌ی قابلیت‌ها: اسکن آهنگ‌ها، تگ و کاور، جستجو،
مینی‌پلیر، پلیر تمام‌صفحه، دیسک چرخان، ویژوالایزر، شافل/تکرار، رنگ از روی کاور.
"""
import colorsys
import base64
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
from kivy import kivy_data_dir
from kivy.core.image import Image as CoreImage
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
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
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
    # "Display" = the bold techno font used for the app name / welcome screen.
    disp = os.path.join(here, "display.ttf")
    if not os.path.isfile(disp):
        disp = os.path.join(kivy_data_dir, "fonts", "Roboto-Bold.ttf")
    LabelBase.register("Display", fn_regular=disp, fn_bold=disp)
    for p in (os.path.join(here, "font.ttf"),
              "/system/fonts/NotoNaskhArabic-Regular.ttf",
              "/system/fonts/NotoSansArabic-Regular.ttf",
              "/system/fonts/DroidSansArabic.ttf"):
        if os.path.isfile(p):
            LabelBase.register(DEFAULT_FONT, fn_regular=p, fn_bold=p)
            return


def grad_tex(a, b, vertical=False):
    """2-pixel RGBA texture; GPU interpolation turns it into a smooth gradient."""
    t = Texture.create(size=(1, 2) if vertical else (2, 1), colorfmt="rgba")
    t.blit_buffer(bytes(int(max(0, min(1, v)) * 255) for v in list(a) + list(b)),
                  colorfmt="rgba", bufferfmt="ubyte")
    t.mag_filter = t.min_filter = "linear"
    return t


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
    # Query Android's public audio library using the runtime media permission.
    # This avoids sending the user to the special "All files access" Settings page.
    if platform == "android":
        cursor = None
        try:
            from jnius import autoclass
            MediaAudio = autoclass("android.provider.MediaStore$Audio$Media")
            resolver = get_activity().getContentResolver()
            cursor = resolver.query(MediaAudio.EXTERNAL_CONTENT_URI, None,
                                    "is_music != 0", None, "title COLLATE NOCASE ASC")
            if cursor:
                data_col = cursor.getColumnIndex("_data")
                while cursor.moveToNext():
                    if data_col >= 0:
                        path = cursor.getString(data_col)
                        if path and os.path.isfile(path):
                            real = os.path.realpath(path)
                            if real not in seen:
                                seen.add(real)
                                found.append(path)
        except Exception:
            found = []
        finally:
            try:
                if cursor:
                    cursor.close()
            except Exception:
                pass
    else:
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
        self.bytes = 0
        self.ex = ThreadPoolExecutor(2)

    def clear(self):
        self.cache.clear()
        self.bytes = 0

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
        if val[0] is not None:
            self.bytes += key[1] * key[1] * 4
        while self.cache and (len(self.cache) > 300 or self.bytes > 48 * 1024 * 1024):
            k, v = self.cache.popitem(last=False)
            if v[0] is not None:
                self.bytes -= k[1] * k[1] * 4
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
                d = _Done(lambda: self.on_end(g))
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
    fallback = ObjectProperty(None, allownone=True)
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


class Shimmer(Widget):
    p = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.draw, size=self.draw, p=self.draw)

    def draw(self, *a):
        app = App.get_running_app()
        self.canvas.clear()
        with self.canvas:
            Color(1, 1, 1, .14)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[self.height / 2])
            w = self.width * .34
            x = self.x + (self.width + w) * self.p - w
            x0, x1 = max(self.x, x), min(self.right, x + w)
            if x1 > x0:
                Color(app.c2[0], app.c2[1], app.c2[2], .95)
                RoundedRectangle(pos=(x0, self.y), size=(x1 - x0, self.height),
                                 radius=[self.height / 2])


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
        self.opacity = 0
        self.bind(height=self._h)

    def _h(self, *a):
        if not self.opened:
            self.y = -self.height * 1.05

    def show(self):
        self.opened = True
        self.opacity = 1
        Animation.cancel_all(self, "y")
        Animation(y=0, d=.45, t="out_cubic").start(self)

    def hide(self):
        self.opened = False
        Animation.cancel_all(self, "y")
        a = Animation(y=-self.height * 1.05, d=.35, t="in_cubic")
        a.bind(on_complete=lambda *x: None if self.opened else setattr(self, "opacity", 0))
        a.start(self)

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
            rgba: 1, 1, 1, (1 if (self.texture or self.fallback) else 0)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.rad]
            texture: self.texture or self.fallback
    Label:
        text: root.letter
        font_size: root.height * .42
        opacity: 0 if (root.texture or root.fallback) else 1

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
    CoverBox:
        id: cov
        size_hint: .94, .94
        pos_hint: {'center_x': .5, 'center_y': .5}
        rad: dp(30)
        hue: app.cur_hue
        letter: app.cur_letter
    Widget:
        size_hint: .94, .94
        pos_hint: {'center_x': .5, 'center_y': .5}
        canvas:
            Color:
                rgba: 1, 1, 1, .28
            Line:
                rounded_rectangle: (self.x, self.y, self.width, self.height, dp(30))
                width: dp(1.2)

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
            Image:
                source: app.logo_path
                size_hint: None, None
                size: dp(46), dp(46)
                allow_stretch: True
                keep_ratio: True
            BoxLayout:
                orientation: 'vertical'
                Label:
                    text: 'DCED Plyer'
                    font_name: 'Display'
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
        text: 'No songs found.\nAllow music access when asked,\nthen tap refresh.'
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
    title = "DCED Plyer"
    player_art_path = StringProperty("")
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
        self.logo_path, self.player_art_path = self._prepare_artwork()
        self.icon = self.logo_path
        self._notification_ready = False
        self._media_session = None
        self._bg_song = None
        self._logo_bmp = None
        self.fg = True
        self.raw_title = self.raw_artist = ""
        self.cur_path = None
        self.hue, self.t, self.ang, self.errs = 260, 0.0, 0.0, 0
        self.bars = [0.0] * 44
        self.drag = False
        self._last_notification_update = 0.0
        self.covers = Covers()
        self.engine = Engine(self.on_end_raw, self.on_err)
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        return Root()

    def on_start(self):
        ids = self.root.ids
        self.sheet, self.rv, self.mini = ids.sheet, ids.rv, ids.mini
        self.sk = self.sheet.ids.sk
        self.viz = self.sheet.ids.viz
        self.stage_cov = self.sheet.ids.stage.ids.cov
        try:   # app logo is the fallback cover when a song has no artwork
            tex = CoreImage(self.logo_path).texture if self.logo_path else None
            self.stage_cov.fallback = tex
            self.mini.ids.cov.fallback = tex
        except Exception:
            pass
        Clock.schedule_interval(self.tick, 1 / 24.)
        self.show_welcome()
        if platform == "android":
            self.init_media_notification()
            Clock.schedule_once(lambda dt: self.request_audio_permissions(), 1.4)
        else:
            self.rescan()

    def _prepare_artwork(self):
        """The app icon doubles as logo (header, notification, cover fallback)."""
        here = os.path.dirname(os.path.abspath(__file__))
        for n in ("icon.png", "icon.jpg"):
            p = os.path.join(here, n)
            if os.path.isfile(p):
                return p, p
        return "", ""

    # ---- welcome: glass panel, no image, techno font
    def show_welcome(self):
        overlay = FloatLayout()
        with overlay.canvas.before:
            Color(0.015, 0.025, 0.045, .97)
            overlay._shade = Rectangle(pos=overlay.pos, size=overlay.size)
        overlay.bind(pos=lambda w, v: setattr(w._shade, "pos", v),
                     size=lambda w, v: setattr(w._shade, "size", v))

        panel = BoxLayout(orientation="vertical", size_hint=(.86, None), height=dp(210),
                          pos_hint={"center_x": .5, "center_y": .5},
                          padding=(dp(26), dp(26)), spacing=dp(10))
        R = dp(34)
        with panel.canvas.before:
            Color(1, 1, 1, 1)                             # colour tint, left -> right
            panel._g1 = RoundedRectangle(
                pos=panel.pos, size=panel.size, radius=[R],
                texture=grad_tex(self.c1[:3] + [.34], self.c2[:3] + [.28]))
            panel._glass = RoundedRectangle(              # frosted body, light from the top
                pos=panel.pos, size=panel.size, radius=[R],
                texture=grad_tex((1, 1, 1, .20), (1, 1, 1, .03), True))
            Color(1, 1, 1, .34)                           # glass edge
            panel._edge = Line(rounded_rectangle=(panel.x, panel.y, panel.width,
                                                  panel.height, R), width=1.2)

        def shape(*_):
            x, y, w, h = panel.x, panel.y, panel.width, panel.height
            panel._g1.pos, panel._g1.size = (x, y), (w, h)
            panel._glass.pos, panel._glass.size = (x, y), (w, h)
            panel._edge.rounded_rectangle = (x, y, w, h, R)
        panel.bind(pos=shape, size=shape)

        top = Label(text="Welcome To", font_name="Display", font_size="19sp",
                    color=(1, 1, 1, .78), halign="center", valign="bottom",
                    size_hint=(1, .30))
        name = Label(text="Music Plyer DECD", font_name="Display", font_size="29sp",
                     color=(1, 1, 1, 1), halign="center", valign="middle",
                     size_hint=(1, .42))
        for lb in (top, name):
            lb.bind(size=lambda w, v: setattr(w, "text_size", v))
        shim = Shimmer(size_hint=(.7, None), height=dp(5), pos_hint={"center_x": .5})
        for w in (top, name, shim):
            panel.add_widget(w)
        overlay.add_widget(panel)
        self.root.add_widget(overlay)

        loop = Animation(p=1, d=1.0, t="in_out_quad") + Animation(p=0, d=0)
        loop.repeat = True
        loop.start(shim)
        overlay._shim = shim
        panel.opacity = 0
        Animation(opacity=1, d=.45, t="out_quad").start(panel)
        overlay.opacity = 0
        Animation(opacity=1, d=.3, t="out_quad").start(overlay)
        Clock.schedule_once(lambda dt: self._hide_welcome(overlay), 2.7)

    def _hide_welcome(self, overlay):
        Animation.cancel_all(overlay._shim)
        anim = Animation(opacity=0, d=.4, t="in_quad")
        anim.bind(on_complete=lambda *_: self.root.remove_widget(overlay)
                  if overlay.parent else None)
        anim.start(overlay)

    # ---- permissions are asked in-app (no trip to Settings)
    def _audio_perm(self):
        from jnius import autoclass
        sdk = autoclass("android.os.Build$VERSION").SDK_INT
        return ("android.permission.READ_MEDIA_AUDIO" if sdk >= 33
                else "android.permission.READ_EXTERNAL_STORAGE"), sdk

    def request_audio_permissions(self):
        try:
            from android.permissions import request_permissions
            audio, sdk = self._audio_perm()
            permissions = [audio]
            if sdk >= 33:
                permissions.append("android.permission.POST_NOTIFICATIONS")

            def done(perms, grants):
                Clock.schedule_once(lambda dt: self._perm_done(audio, perms, grants))
            request_permissions(permissions, done)
        except Exception:
            self.toast("Permission request unavailable")
            self.rescan(True)

    def _perm_done(self, audio, perms, grants):
        try:
            ok = dict(zip(perms, grants)).get(audio, True)
        except Exception:
            ok = True
        if not ok:
            self.toast("Allow music access to see your songs")
        self.rescan(True)
        self.update_media_notification()

    def have_audio_perm(self):
        try:
            from android.permissions import check_permission
            return bool(check_permission(self._audio_perm()[0]))
        except Exception:
            return True

    # ---- media notification (works with its own buttons, no app launch)
    def init_media_notification(self):
        if platform != "android":
            return
        try:
            from jnius import autoclass
            from android.broadcast import BroadcastReceiver
            sdk = autoclass("android.os.Build$VERSION").SDK_INT
            act = get_activity()
            self._ctx = act.getApplicationContext()
            self._pkg = self._ctx.getPackageName()
            self._manager = self._ctx.getSystemService("notification")
            if sdk >= 26:
                Channel = autoclass("android.app.NotificationChannel")
                channel = Channel("dced_music", "DCED Plyer playback", 2)
                channel.setDescription("Music playback controls")
                self._manager.createNotificationChannel(channel)
            MediaSession = autoclass("android.media.session.MediaSession")
            self._media_session = MediaSession(self._ctx, "DCED Plyer")
            self._media_session.setFlags(3)
            self._media_session.setActive(True)
            self._acts = {self._pkg + ".PLAYPAUSE": "toggle",
                          self._pkg + ".NEXT": "next",
                          self._pkg + ".PREV": "previous"}
            self._br = BroadcastReceiver(self._on_broadcast, actions=list(self._acts))
            self._br.start()
            if self.logo_path:
                try:
                    self._logo_bmp = autoclass("android.graphics.BitmapFactory").decodeFile(
                        self.logo_path)
                except Exception:
                    self._logo_bmp = None
            self._notification_ready = True
            self.update_media_notification()
        except Exception:
            self._notification_ready = False

    def _on_broadcast(self, context, intent):
        """Runs on an Android thread."""
        a = self._acts.get(intent.getAction())
        if not a:
            return
        if self.fg:
            Clock.schedule_once(lambda dt: self.ui_action(a), 0)
        else:
            threading.Thread(target=self.bg_action, args=(a,), daemon=True).start()

    def ui_action(self, a):
        if a == "toggle":
            self.toggle()
        elif a == "next":
            self.go_next()
        elif a == "previous":
            self.go_prev()

    def update_media_notification(self, playing=None):
        if (platform != "android" or not getattr(self, "_notification_ready", False)
                or not self.cur_path):
            return
        if playing is None:
            playing = self.playing
        try:
            from jnius import autoclass
            MetadataBuilder = autoclass("android.media.MediaMetadata$Builder")
            PlaybackStateClass = autoclass("android.media.session.PlaybackState")
            PlaybackBuilder = autoclass("android.media.session.PlaybackState$Builder")
            NotificationBuilder = autoclass("android.app.Notification$Builder")
            MediaStyle = autoclass("android.app.Notification$MediaStyle")
            PendingIntent = autoclass("android.app.PendingIntent")
            Intent = autoclass("android.content.Intent")
            RDrawable = autoclass("android.R$drawable")
            title = self.raw_title or "DCED Plyer"       # raw text: Android shapes Persian itself
            artist = self.raw_artist or ""
            metadata = (MetadataBuilder()
                        .putString("android.media.metadata.TITLE", title)
                        .putString("android.media.metadata.ARTIST", artist).build())
            self._media_session.setMetadata(metadata)
            position = int(self.engine.pos() * 1000)
            actions = (PlaybackStateClass.ACTION_PLAY | PlaybackStateClass.ACTION_PAUSE |
                       PlaybackStateClass.ACTION_PLAY_PAUSE |
                       PlaybackStateClass.ACTION_SKIP_TO_NEXT |
                       PlaybackStateClass.ACTION_SKIP_TO_PREVIOUS |
                       PlaybackStateClass.ACTION_SEEK_TO)
            playback = (PlaybackBuilder().setActions(actions)
                        .setState(3 if playing else 2, position, 1.0 if playing else 0.0).build())
            self._media_session.setPlaybackState(playback)

            flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE

            def pending(name, code):
                i = Intent(self._pkg + "." + name)
                i.setPackage(self._pkg)
                return PendingIntent.getBroadcast(self._ctx, code, i, flags)

            launch = self._ctx.getPackageManager().getLaunchIntentForPackage(self._pkg)
            sdk = autoclass("android.os.Build$VERSION").SDK_INT
            builder = (NotificationBuilder(self._ctx, "dced_music")
                       if sdk >= 26 else NotificationBuilder(self._ctx))
            builder.setContentTitle(title).setContentText(artist)
            builder.setSmallIcon(RDrawable.ic_media_play)
            builder.setOngoing(bool(playing)).setOnlyAlertOnce(True)
            builder.setVisibility(1)
            if launch is not None:
                builder.setContentIntent(
                    PendingIntent.getActivity(self._ctx, 10, launch, flags))
            if getattr(self, "_logo_bmp", None):
                builder.setLargeIcon(self._logo_bmp)
            builder.addAction(RDrawable.ic_media_previous, "Previous", pending("PREV", 11))
            builder.addAction(RDrawable.ic_media_pause if playing else RDrawable.ic_media_play,
                              "Play/Pause", pending("PLAYPAUSE", 12))
            builder.addAction(RDrawable.ic_media_next, "Next", pending("NEXT", 13))
            style = MediaStyle()
            style.setMediaSession(self._media_session.getSessionToken())
            style.setShowActionsInCompactView([0, 1, 2])
            builder.setStyle(style)
            self._manager.notify(7401, builder.build())
        except Exception:
            pass

    # ---- background playback: no Kivy properties are touched off the UI thread
    def bg_action(self, a):
        if a == "toggle":
            if self.engine.playing():
                self.engine.pause()
            else:
                self.engine.resume()
            Clock.schedule_once(lambda dt: self.sync_ui(), 0)
            self.update_media_notification(self.engine.playing())
        elif a == "next":
            self.bg_step(1)
        elif a == "previous":
            if self.engine.pos() > 3:
                self.engine.seek(0)
            else:
                self.bg_step(-1)

    def bg_step(self, step):
        view = list(self.view)
        if not view:
            return
        i = next((k for k, s in enumerate(view) if s["path"] == self.cur_path), -1)
        if step > 0 and self.shuffle:
            j = random.randrange(len(view))
        else:
            j = (max(i, 0) + step) % len(view)
        s = view[j]
        self.cur_path = s["path"]
        self.raw_title, self.raw_artist = s["title"], s["artist"]
        ok = self.engine.play(s["path"])
        self._bg_song = s
        self.update_media_notification(ok)
        Clock.schedule_once(lambda dt: self.sync_ui(), 0)

    def bg_on_end(self, gen):
        if gen != self.engine.gen:
            return
        if self.repeat == 2:
            self.engine.seek(0)
            self.engine.resume()
            return
        i = next((k for k, s in enumerate(self.view) if s["path"] == self.cur_path), -1)
        if self.repeat == 1 or self.shuffle or i < len(self.view) - 1:
            self.bg_step(1)
        else:
            self.update_media_notification(False)
            Clock.schedule_once(lambda dt: self.sync_ui(), 0)

    def sync_ui(self):
        s = getattr(self, "_bg_song", None)
        if s is not None:
            self._bg_song = None
            self.apply_song_ui(s)
        self.playing = self.engine.playing()

    def handle_media_intent(self):
        pass

    def on_pause(self):
        self.fg = False
        return True

    def on_resume(self):
        self.fg = True
        self.sync_ui()
        if not self.songs:
            self.rescan()

    def on_stop(self):
        self.engine.release()
        try:
            if getattr(self, "_manager", None):
                self._manager.cancel(7401)
            if getattr(self, "_br", None):
                self._br.stop()
            if self._media_session:
                self._media_session.setActive(False)
                self._media_session.release()
        except Exception:
            pass

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
        if platform == "android" and not first and not self.have_audio_perm():
            self.request_audio_permissions()
            return
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

    def apply_song_ui(self, s):
        self.cur_path = s["path"]
        self.raw_title, self.raw_artist = s["title"], s["artist"]
        self.cur_title, self.cur_artist = s.get("td", s["title"]), s.get("ad", s["artist"])
        self.cur_letter = s.get("letter", "\u266a")
        self.cur_hue = s.get("hue", hue_of(s["title"]))
        self.has_song = True
        self.set_hue(self.cur_hue)
        self.mini.ids.cov.set_cover(s["path"], 160)
        self.stage_cov.set_cover(s["path"], 400)
        self.frac, self.t0 = 0, "0:00"
        self.mark_current()

    def start(self, s):
        self.apply_song_ui(s)
        path = s["path"]
        if self.engine.play(path):
            self.playing = True
            self.update_media_notification()
        else:
            self.playing = False
            self.update_media_notification()
            self.fail()

    def fail(self):
        self.errs += 1
        self.toast("Can't play this file, skipping")
        if self.errs <= max(1, len(self.view)):
            Clock.schedule_once(lambda dt: self.go_next(), .6)

    def on_err(self, gen):
        if gen == self.engine.gen:
            self.fail()

    def on_end_raw(self, gen):
        """Called when a track finishes (possibly from an Android thread)."""
        if self.fg:
            Clock.schedule_once(lambda dt: self.on_end(gen), 0)
        else:
            threading.Thread(target=self.bg_on_end, args=(gen,), daemon=True).start()

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
        self.update_media_notification()

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
        self.update_media_notification()

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
        if self.t - self._last_notification_update >= 10:
            self._last_notification_update = self.t
            self.update_media_notification()
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
            self.bass = sum(self.bars[19:25]) / 6.0
            if self.playing or max(self.bars) > .01:
                self.viz.draw()


if __name__ == "__main__":
    GlassApp().run()
