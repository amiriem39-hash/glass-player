# -*- coding: utf-8 -*-
"""
GLASS PLAYER - Kivy (Android)
تبدیل نسخه وب به اپ کیوی. همه‌ی قابلیت‌ها: اسکن آهنگ‌ها، تگ و کاور، جستجو،
مینی‌پلیر، پلیر تمام‌صفحه، دیسک چرخان، ویژوالایزر، شافل/تکرار، رنگ از روی کاور.
"""
import colorsys
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import socket
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy import kivy_data_dir
from kivy.core.image import Image as CoreImage
from kivy.core.text import DEFAULT_FONT, LabelBase
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.graphics import (Translate, Color, Ellipse, Line, PopMatrix, PushMatrix,
                           Mesh, Rectangle, Rotate, RoundedRectangle, Triangle)
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
from kivy.uix.modalview import ModalView
from kivy.uix.label import Label
from kivy.utils import platform

import playercore as pc

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


# ---- persistent library cache: the list is on screen the moment the app opens
def _cache_file():
    return os.path.join(pc.data_dir(), "library.json")


def _hidden_file():
    return os.path.join(pc.data_dir(), "hidden.json")


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def load_hidden():
    try:
        with open(_hidden_file(), encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_hidden(hidden):
    _write_json(_hidden_file(), sorted(hidden))


def load_cache(hidden):
    try:
        with open(_cache_file(), encoding="utf-8") as f:
            raw = json.load(f)
        return [{"path": r[0], "title": r[1], "artist": r[2], "mid": r[3],
                 "size": r[4], "mt": r[5]} for r in raw if r[0] not in hidden]
    except Exception:
        return []


def save_cache(songs):
    _write_json(_cache_file(), [[s["path"], s["title"], s["artist"], s.get("mid"),
                                 s.get("size"), s.get("mt")] for s in songs])


def list_audio():
    """Fast pass: only paths + file info, no tag reading."""
    out, seen = [], set()
    if platform == "android":
        cursor = None
        try:
            from jnius import autoclass
            MediaAudio = autoclass("android.provider.MediaStore$Audio$Media")
            resolver = get_activity().getContentResolver()
            cursor = resolver.query(MediaAudio.EXTERNAL_CONTENT_URI,
                                    ["_id", "_data", "_size", "date_modified"],
                                    "is_music != 0", None, None)
            if cursor:
                i_id = cursor.getColumnIndex("_id")
                i_data = cursor.getColumnIndex("_data")
                i_size = cursor.getColumnIndex("_size")
                i_mod = cursor.getColumnIndex("date_modified")
                while cursor.moveToNext():
                    path = cursor.getString(i_data)
                    if not path or not os.path.isfile(path):
                        continue
                    real = os.path.realpath(path)
                    if real in seen:
                        continue
                    seen.add(real)
                    out.append({"path": path, "mid": cursor.getLong(i_id),
                                "size": cursor.getLong(i_size), "mt": cursor.getLong(i_mod)})
        except Exception:
            out = []
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
                        st = os.stat(p)
                        if rp in seen or st.st_size < MIN_SIZE:
                            continue
                        seen.add(rp)
                        out.append({"path": p, "mid": None, "size": st.st_size,
                                    "mt": int(st.st_mtime)})
                    except Exception:
                        pass
    out.sort(key=lambda d: os.path.basename(d["path"]).lower())
    return out


def scan(cache, hidden, publish=None):
    """Songs whose size/mtime match the cache are reused (no tag reading);
    only new or changed files are read. Interim lists are published so the UI
    can show songs while the rest are still being read."""
    songs, todo = [], []
    for f in list_audio():
        if f["path"] in hidden:
            continue
        c = cache.get(f["path"])
        if c and c.get("size") == f["size"] and c.get("mt") == f["mt"] and c.get("title"):
            t, a = c["title"], c.get("artist") or "Unknown artist"
        else:
            t, a = guess_names(f["path"])
            a = a or "Unknown artist"
            todo.append(len(songs))
        songs.append({"path": f["path"], "title": t, "artist": a, "mid": f["mid"],
                      "size": f["size"], "mt": f["mt"]})
    if publish and todo:
        publish([dict(s) for s in songs])
    for k, i in enumerate(todo):
        s = songs[i]
        try:
            s["title"], s["artist"] = read_tags(s["path"])
        except Exception:
            pass
        if publish and (k + 1) % 250 == 0 and k + 1 < len(todo):
            publish([dict(x) for x in songs])
    return songs


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


# ------------------------------------------------------------ covers (texture + disk cache)
THUMBS = os.path.join(pc.data_dir(), "thumbs")
try:
    os.makedirs(THUMBS, exist_ok=True)
except OSError:
    pass


def _thumb_path(path, size, mt):
    h = hashlib.md5(("%s|%s|%d" % (path, mt, size)).encode("utf-8", "ignore")).hexdigest()
    return os.path.join(THUMBS, h + ".jpg")


class Covers:
    def __init__(self):
        self.cache = OrderedDict()
        self.pending = {}
        self.bytes = 0
        self.ex = ThreadPoolExecutor(3)

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

    def _load(self, path, size):
        if not Image:
            return None
        try:
            mt = int(os.path.getmtime(path))
        except OSError:
            mt = 0
        tp = _thumb_path(path, size, mt)
        if os.path.isfile(tp):                       # already extracted once: instant
            im = Image.open(tp).convert("RGBA")
        elif os.path.isfile(tp + ".no"):             # known to have no artwork
            return None
        else:
            r = read_cover(path)
            if not r:
                try:
                    open(tp + ".no", "wb").close()
                except OSError:
                    pass
                return None
            im = Image.open(io.BytesIO(r[0]))
            try:
                im.draft("RGB", (size * 2, size * 2))
            except Exception:
                pass
            im = ImageOps.fit(im.convert("RGBA"), (size, size))
            try:
                im.convert("RGB").save(tp, "JPEG", quality=88)
            except Exception:
                pass
        px = list(im.resize((8, 8)).convert("RGB").getdata())
        rr = sum(p[0] for p in px) / 64.0
        gg = sum(p[1] for p in px) / 64.0
        bb = sum(p[2] for p in px) / 64.0
        hue = None
        if max(rr, gg, bb) - min(rr, gg, bb) >= 12:
            hue = colorsys.rgb_to_hsv(rr / 255, gg / 255, bb / 255)[0] * 360
        return (size, size, im.tobytes(), hue)

    def _work(self, key):
        path, size = key
        try:
            res = self._load(path, size)
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


# ------------------------------------------------------------ player link
class Remote:
    """UI side of the player.
    Android: talks to the foreground service (service.py) over localhost UDP.
    Desktop: runs the same PlayerCore in-process (testing only)."""

    def __init__(self, app):
        self.app = app
        self.local = None
        self.sock = None
        self.alive = False
        self.pending = []
        self.last = 0.0
        self.last_hello = 0.0
        self.started = 0.0
        self.last_poll = 0.0
        if platform == "android":
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", pc.UI_PORT))
                s.setblocking(False)
                self.sock = s
            except Exception:
                self.sock = None
        else:
            self.local = pc.PlayerCore(None)

    @property
    def ready(self):
        return self.local is not None or self.alive

    def _tx(self, msg):
        if self.sock:
            try:
                self.sock.sendto(json.dumps(msg).encode("utf-8"),
                                 ("127.0.0.1", pc.CMD_PORT))
            except Exception:
                pass

    def hello(self, now=None):
        self.last_hello = now or time.time()
        self._tx({"c": "hello"})

    def notify(self, msg):
        """Fire-and-forget (fg flag etc.); never starts the service."""
        if self.local is None:
            self._tx(msg)

    def _launch(self):
        now = time.time()
        if now - self.started < 6:
            return
        self.started = now
        try:
            from jnius import autoclass
            act = get_activity()
            autoclass(act.getPackageName() + ".ServicePlayer").start(act, "")
        except Exception:
            self.app.toast("Player service failed to start")

    def send(self, msg, start=False):
        if self.local is not None:
            self.local.handle(msg)
        elif self.alive:
            self._tx(msg)
        elif start:
            self.pending.append(msg)
            del self.pending[:-6]
            self._launch()
            self.hello()

    def pump(self, now):
        """Returns the list of messages that arrived since the last call."""
        out = []
        if self.local is not None:
            self.local.poll()
            if now - self.last_poll >= .25:
                self.last_poll = now
                st = self.local.state()
                st["e"] = "state"
                out.append(st)
            return out
        if not self.sock:
            return out
        while True:
            try:
                data, _a = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError, OSError):
                break
            try:
                m = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            self.last = now
            if not self.alive:
                self.alive = True
                for p in self.pending:
                    self._tx(p)
                self.pending = []
            out.append(m)
        if self.alive and now - self.last > 3.0:
            self.alive = False
        if not self.alive and self.pending and now - self.last_hello > .6:
            self.hello(now)
        return out


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
    elif k == "close":
        L((6, 6), (18, 18))
        L((18, 6), (6, 18))
    elif k == "trash":
        L((4.5, 7), (19.5, 7))
        L((9, 7), (9, 4.5), (15, 4.5), (15, 7))
        L((6.5, 7), (7.7, 19.5), (16.3, 19.5), (17.5, 7))
        L((10.3, 10.5), (10.3, 16.5))
        L((13.7, 10.5), (13.7, 16.5))
    elif k == "all":
        L((2.5, 13), (6.5, 17), (13.5, 8))
        L((10, 16), (11, 17), (21, 7))
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
            app.seek_frac(self.value / 1000.0)
        return r

    def on_value(self, *a):
        app = App.get_running_app()
        if app.drag:
            app.t0 = fmt(self.value / 1000.0 * app.r_dur)


def rr_ring(hw, hh, r, n=6):
    """Closed rounded-rectangle outline (counter-clockwise)."""
    pts = []
    for ccx, ccy, a0 in ((hw - r, hh - r, 0), (-(hw - r), hh - r, 90),
                         (-(hw - r), -(hh - r), 180), (hw - r, -(hh - r), 270)):
        for i in range(n + 1):
            a = math.radians(a0 + 90.0 * i / n)
            pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
    return pts


def ring_template(n=6, rings=3):
    """Unit rounded square as a triangle mesh (centre + concentric rings)."""
    ring = rr_ring(1.0, 1.0, .21, n)
    N = len(ring)
    base = [(0.0, 0.0)] + [(k * x / rings, k * y / rings)
                           for k in range(1, rings + 1) for x, y in ring]
    idx = []
    for i in range(N):
        idx += [0, 1 + i, 1 + (i + 1) % N]
    for k in range(1, rings):
        a, b = 1 + (k - 1) * N, 1 + k * N
        for i in range(N):
            j = (i + 1) % N
            idx += [a + i, b + i, b + j, a + i, b + j, a + j]
    return base, idx, N


def make_proj(cx, cy, S, ay, ax, az, zpush):
    """Perspective projection of a plane point (x, y) lying at depth zl."""
    cyr, syr, cxr, sxr = math.cos(ay), math.sin(ay), math.cos(ax), math.sin(ax)
    czr, szr = math.cos(az), math.sin(az)
    D = S * 2.4

    def proj(x, y, zl):
        x, y = x * czr - y * szr, x * szr + y * czr
        x1 = x * cyr + zl * syr
        z1 = -x * syr + zl * cyr
        y2 = y * cxr - z1 * sxr
        z2 = y * sxr + z1 * cxr + zpush
        f = D / max(D * .25, D - z2)
        return cx + x1 * f, cy + y2 * f
    return proj


class Stage(Widget):
    """Cover art as a real 3D card (perspective mesh) with layers at different
    depths: glow (far) < back picture < cover < glass edge (near).
    It keeps swaying by itself like a looping video; the phone's tilt / spin
    and a finger add to the motion."""
    tx = NumericProperty(0)      # finger tilt (-1..1)
    ty = NumericProperty(0)
    px = NumericProperty(0)      # final tilt = auto + sensors + finger
    py = NumericProperty(0)
    hue = NumericProperty(0)
    letter = StringProperty("")
    path = StringProperty("")
    texture = ObjectProperty(None, allownone=True)
    bg = ListProperty([.6, .2, .8, 1])

    def __init__(self, **kw):
        super().__init__(**kw)
        self.touching = False
        self.amp = 0.0
        self.kx = self.ky = self.kr = 0.0
        self.t = 0.0
        self.bass_v = 0.0
        self._ltex = None
        self._lkey = None
        self.base, idx, self.N = ring_template()
        self.uv = [(x * .5 + .5, y * .5 + .5) for x, y in self.base]
        zeros = [0.0] * (4 * len(self.base))
        t = Texture.create(size=(64, 1), colorfmt="rgba")
        buf = bytearray()
        for i in range(64):
            d = abs(i / 63.0 - .5) / .16
            buf += bytes((255, 255, 255, int((max(0.0, 1.0 - d) ** 2) * .38 * 255)))
        t.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
        t.mag_filter = t.min_filter = "linear"
        self.sheen_tex = t
        with self.canvas:
            self.glow_c = Color(1, 1, 1, .07)
            self.glow = [Ellipse() for _ in range(7)]
            self.back_c = Color(1, 1, 1, .34)
            self.back = Mesh(vertices=list(zeros), indices=idx, mode="triangles")
            self.cov_c = Color(1, 1, 1, 1)
            self.cov = Mesh(vertices=list(zeros), indices=idx, mode="triangles")
            self.let_c = Color(1, 1, 1, 0)
            self.let = Mesh(vertices=[0.0] * 16, indices=[0, 1, 2, 0, 2, 3], mode="triangles")
            self.sh_c = Color(1, 1, 1, 1)
            self.sheen = Mesh(vertices=list(zeros), indices=idx, mode="triangles",
                              texture=self.sheen_tex)
            self.edge_c = Color(1, 1, 1, .30)
            self.edge = Line(points=[0, 0, 1, 1, 2, 2], width=dp(1.2), close=True)

    # ---- artwork
    def on_hue(self, *a):
        r, g, b = colorsys.hls_to_rgb((self.hue % 360) / 360., .42, .8)
        self.bg = [r, g, b, 1]

    def set_cover(self, path, size, cb=None):
        if path == self.path and self.texture is not None:
            return
        self.path = path
        self.texture = None
        App.get_running_app().covers.get(path, size, lambda t, h: self._got(path, t))

    def _got(self, path, tex):
        if path == self.path:
            self.texture = tex

    def _letter_tex(self):
        key = self.letter
        if key != self._lkey:
            self._lkey = key
            try:
                lb = CoreLabel(text=key or "\u266a", font_size=160)
                lb.refresh()
                self._ltex = lb.texture
            except Exception:
                self._ltex = None
        return self._ltex

    # ---- motion
    def update3d(self, dt, t, playing, bass, sx, sy, wx, wy, wz):
        dt = max(.001, min(dt, .1))
        self.t, self.bass_v = t, bass
        goal = 0.0 if self.touching else (1.0 if playing else .55)
        self.amp += (goal - self.amp) * min(1.0, dt * 3)
        ax_ = (math.sin(t * .9) * .60 + math.sin(t * .37 + 1.3) * .28) * self.amp
        ay_ = (math.cos(t * .71) * .42 + math.sin(t * .29 + .5) * .20) * self.amp
        d = math.exp(-dt * 2.4)
        self.kx = max(-1.0, min(1.0, (self.kx - wy * dt * .9) * d))     # phone rolled left/right
        self.ky = max(-1.0, min(1.0, (self.ky + wx * dt * .9) * d))     # phone pitched
        self.kr = max(-14.0, min(14.0, (self.kr - wz * dt * 40) * math.exp(-dt * 2.0)))  # spun
        self.px = max(-1.0, min(1.0, ax_ + sx + self.kx + self.tx))
        self.py = max(-1.0, min(1.0, ay_ + sy + self.ky + self.ty))
        self.redraw()

    def redraw(self):
        S = min(self.width, self.height)
        if S < 4:
            return
        app = App.get_running_app()
        hs = S * .47
        cx, cy = self.center
        bass = self.bass_v
        zpush = (.02 + bass * .12 + math.sin(self.t * .8) * .012
                 - (abs(self.px) + abs(self.py)) * .04) * S
        proj = make_proj(cx, cy, S, math.radians(self.px * 22), math.radians(-self.py * 17),
                         math.radians(self.kr), zpush)
        tex, bg = self.texture, self.bg

        def verts(scale, zl, uoff=0.0):
            out = []
            for (bx, by), (u, v) in zip(self.base, self.uv):
                X, Y = proj(bx * hs * scale, by * hs * scale, zl)
                out += (X, Y, u + uoff, v)
            return out

        # far glow
        gx, gy = proj(0, 0, -.7 * S)
        c1 = app.c1
        self.glow_c.rgba = (c1[0], c1[1], c1[2], .07)
        for i, e in enumerate(self.glow):
            r = S * .75 * (1 - i / 7.0) * (1 + bass * .25)
            e.pos, e.size = (gx - r, gy - r), (2 * r, 2 * r)
        # back picture (deeper layer -> moves more)
        self.back_c.rgba = (1, 1, 1, .34) if tex else (bg[0], bg[1], bg[2], .30)
        self.back.texture = tex
        self.back.vertices = verts(1.2, -.45 * S)
        # cover
        self.cov_c.rgba = (1, 1, 1, 1) if tex else tuple(bg)
        self.cov.texture = tex
        self.cov.vertices = verts(1.0, 0)
        # letter when there is no artwork
        lt = None if tex else self._letter_tex()
        if lt is not None:
            k = (S * .40) / 160.0
            w, h = lt.width * k / 2, lt.height * k / 2
            q = []
            for (x, y), (u, v) in zip(((-w, -h), (w, -h), (w, h), (-w, h)),
                                      ((0, 0), (1, 0), (1, 1), (0, 1))):
                X, Y = proj(x, y, .01 * S)
                q += (X, Y, u, 1 - v)
            self.let.texture = lt
            self.let.vertices = q
            self.let_c.rgba = (1, 1, 1, 1)
        else:
            self.let_c.rgba = (1, 1, 1, 0)
        # glossy band slides with the tilt
        self.sheen.vertices = verts(1.0, .02 * S, -self.px * .5)
        # glass edge (nearest layer)
        pts = []
        for x, y in self.base[-self.N:]:
            pts += proj(x * hs, y * hs, .07 * S)
        self.edge.points = pts

    # ---- finger
    def _tilt(self, t):
        rx = (t.x - self.x) / max(1, self.width) - .5
        ry = (t.y - self.y) / max(1, self.height) - .5
        Animation.cancel_all(self, "tx", "ty")
        self.tx = max(-1, min(1, rx * 2.4))
        self.ty = max(-1, min(1, ry * 2.4))

    def on_touch_down(self, t):
        if self.collide_point(*t.pos):
            t.grab(self)
            self.touching = True
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
            self.touching = False
            Animation(tx=0, ty=0, d=.45, t="out_cubic").start(self)
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
        App.get_running_app().tilt_enable(True)
        self.opacity = 1
        Animation.cancel_all(self, "y")
        Animation(y=0, d=.45, t="out_cubic").start(self)

    def hide(self):
        self.opened = False
        App.get_running_app().tilt_enable(False)
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
    sel = BooleanProperty(False)

    ox = NumericProperty(0)          # horizontal slide-in offset (scroll animation)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.long_fired = False
        self._lp_ev = None
        self._lp_y = 0
        self._in_ev = None
        # whole row (background + children) is drawn shifted by `ox`
        self._tr = Translate(0, 0)
        self.canvas.before.insert(0, PushMatrix())
        self.canvas.before.insert(1, self._tr)
        self.canvas.after.add(PopMatrix())
        self.bind(ox=lambda w, v: setattr(w._tr, "x", v))

    def _cancel_in(self):
        if self._in_ev:
            self._in_ev.cancel()
            self._in_ev = None
        Animation.cancel_all(self)

    def _slide_in(self, d, delay):
        """d=+1: user scrolls down -> row comes from the left; d=-1: from the right."""
        self._cancel_in()
        self.opacity = 0
        self.ox = -d * dp(150)

        def go(dt):
            self._in_ev = None
            Animation(ox=0, opacity=1, d=.32, t="out_cubic").start(self)
        self._in_ev = Clock.schedule_once(go, delay)

    def refresh_view_attrs(self, rv, index, data):
        old = self.idx
        super().refresh_view_attrs(rv, index, data)
        self.ids.cov.set_cover(self.path, 160)
        app = App.get_running_app()
        d = app.scroll_anim_dir() if app else 0
        if d and old != self.idx:
            self._slide_in(d, app.scroll_anim_delay())
        elif self.ox or self.opacity < 1:
            self._cancel_in()
            self.ox = 0
            self.opacity = 1

    def _cancel_lp(self):
        if self._lp_ev:
            self._lp_ev.cancel()
            self._lp_ev = None

    def _lp(self, dt):
        self._lp_ev = None
        self.long_fired = True
        App.get_running_app().row_long(self)

    def on_touch_down(self, t):
        if self.collide_point(*t.pos):
            self.long_fired = False
            self._lp_y = t.y
            self._cancel_lp()
            self._lp_ev = Clock.schedule_once(self._lp, .5)
        return super().on_touch_down(t)

    def on_touch_move(self, t):
        if self._lp_ev and abs(t.y - self._lp_y) > dp(12):
            self._cancel_lp()
        return super().on_touch_move(t)

    def on_touch_up(self, t):
        self._cancel_lp()
        return super().on_touch_up(t)


class Root(FloatLayout):
    pass


class TxtBtn(ButtonBehavior, Label):
    bgc = ListProperty([1, 1, 1, .12])


class Confirm(ModalView):
    title = StringProperty("")
    msg = StringProperty("")


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
    on_release: app.row_tap(self)
    canvas.after:
        Color:
            rgba: app.c2[0], app.c2[1], app.c2[2], (.9 if (self.cur or self.sel) else 0)
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
        opacity: 0 if app.sel_mode else 1
    Widget:
        size_hint: None, None
        size: dp(26), dp(26)
        pos_hint: {'center_y': .5}
        opacity: 1 if app.sel_mode else 0
        canvas:
            Color:
                rgba: (app.c2[0], app.c2[1], app.c2[2], 1) if root.sel else (1, 1, 1, .4)
            Line:
                circle: self.center_x, self.center_y, dp(11)
                width: dp(1.6)
            Color:
                rgba: app.c2[0], app.c2[1], app.c2[2], (1 if root.sel else 0)
            Ellipse:
                pos: self.center_x - dp(11), self.center_y - dp(11)
                size: dp(22), dp(22)
            Color:
                rgba: 0, 0, 0, (1 if root.sel else 0)
            Line:
                points: [self.center_x - dp(5), self.center_y, self.center_x - dp(1.5), self.center_y - dp(4), self.center_x + dp(5), self.center_y + dp(4)]
                width: dp(1.8)
                cap: 'round'
                joint: 'round'

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
            hue: app.cur_hue
            letter: app.cur_letter
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
                on_release: app.rescan(False, True)
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
        text: app.empty_text
        halign: 'center'
        color: 1, 1, 1, .6
        pos_hint: {'center_x': .5, 'center_y': .45}
        opacity: 0 if app.nsongs else 1
    BoxLayout:
        size_hint: 1, None
        height: dp(70)
        pos_hint: {'x': 0, 'top': 1}
        padding: dp(14), dp(12), dp(14), dp(8)
        spacing: dp(10)
        opacity: 1 if app.sel_mode else 0
        disabled: not app.sel_mode
        canvas.before:
            Color:
                rgba: .04, .03, .09, 1
            Rectangle:
                pos: self.pos
                size: self.size
        GBtn:
            kind: 'close'
            style: 'glass'
            isz: .5
            size_hint: None, None
            size: dp(44), dp(44)
            on_release: app.sel_clear()
        Label:
            text: app.sel_text
            font_size: sp(18)
            halign: 'left'
            valign: 'middle'
            text_size: self.size
        GBtn:
            kind: 'all'
            style: 'glass'
            isz: .5
            size_hint: None, None
            size: dp(44), dp(44)
            on_release: app.sel_all()
        GBtn:
            kind: 'trash'
            style: 'accent'
            isz: .5
            size_hint: None, None
            size: dp(44), dp(44)
            on_release: app.ask_delete()
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
<TxtBtn>:
    size_hint_y: None
    height: dp(48)
    font_size: sp(15)
    canvas.before:
        Color:
            rgba: self.bgc[0], self.bgc[1], self.bgc[2], self.bgc[3] + (.14 if self.state == 'down' else 0)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(16)]

<Confirm>:
    size_hint: .9, None
    height: dp(340)
    background: ''
    background_color: 0, 0, 0, 0
    overlay_color: 0, 0, 0, .65
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(10)
        canvas.before:
            Color:
                rgba: .08, .06, .16, .97
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(26)]
            Color:
                rgba: 1, 1, 1, .2
            Line:
                rounded_rectangle: (self.x, self.y, self.width, self.height, dp(26))
                width: 1
        Label:
            text: root.title
            font_size: sp(20)
            size_hint_y: None
            height: dp(34)
        Label:
            text: root.msg
            font_size: sp(13)
            color: 1, 1, 1, .72
            text_size: self.size
            halign: 'center'
            valign: 'middle'
        TxtBtn:
            text: 'Delete from phone'
            bgc: .95, .25, .3, .85
            on_release: root.dismiss(); app.do_delete(True)
        TxtBtn:
            text: 'Remove from list only'
            on_release: root.dismiss(); app.do_delete(False)
        TxtBtn:
            text: 'Cancel'
            bgc: 1, 1, 1, .05
            on_release: root.dismiss()
'''


# ------------------------------------------------------------ app
DEL_REQ = 7311


def prep(s):
    """Display-ready strings for a song dict (Persian shaping, colour, letter)."""
    if "td" not in s:
        s["td"], s["ad"] = fa(s["title"]), fa(s["artist"])
        s["hue"] = hue_of(s["title"])
        s["rtl"] = bool(_AR.search(s["title"]))
        s["letter"] = (s["title"] or "\u266a")[0]
    return s


class GlassApp(App):
    title = "DCED Plyer"
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
    empty_text = StringProperty("Loading your music...")
    nsongs = NumericProperty(0)
    t0 = StringProperty("0:00")
    t1 = StringProperty("0:00")
    frac = NumericProperty(0)
    eqt = NumericProperty(0)
    bass = NumericProperty(0)
    sel_mode = BooleanProperty(False)
    sel_text = StringProperty("")

    def build(self):
        setup_font()
        Window.clearcolor = (.03, .024, .07, 1)
        Window.softinput_mode = "below_target"
        self.songs, self.view, self.q = [], [], ""
        self._sc_y, self._sc_t, self._sc_dir, self._sc_n = 1.0, 0.0, 1, 0
        self.by_path = {}
        self.selected = set()
        self.hidden = load_hidden()
        self.fg = True
        self.cur_path = None
        self.hue, self.t, self.ang = 260, 0.0, 0.0
        self.bars = [0.0] * 44
        self.drag = False
        self.r_pos = self.r_dur = 0.0
        self.r_t = time.time()
        self.play_lock = 0.0
        self.queue_dirty = True
        self._scanning = False
        self._scanned_once = False
        self._acc = None
        self._acc_on = False
        self._gyro = None
        self._gyro_on = False
        self._pitch = None
        self.tilt_x = self.tilt_y = 0.0
        self._del_paths = []
        self.covers = Covers()
        self.remote = Remote(self)
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        return Root()

    # ---- list scroll animation: rows slide in one by one (down: from left, up: from right)
    def _on_rv_scroll(self, rv, y):
        now = time.time()
        if abs(y - self._sc_y) > 1e-4:
            if now - self._sc_t > .25:
                self._sc_n = 0                       # new gesture: restart the stagger
            self._sc_dir = 1 if y < self._sc_y else -1   # scroll_y falls = moving down the list
            self._sc_t = now
        self._sc_y = y

    def scroll_anim_dir(self):
        if time.time() - self._sc_t > .12:
            return 0
        return self._sc_dir

    def scroll_anim_delay(self):
        n = self._sc_n
        self._sc_n += 1
        return min(n * .045, .22)

    def on_start(self):
        ids = self.root.ids
        self.sheet, self.rv, self.mini = ids.sheet, ids.rv, ids.mini
        self.rv.bind(scroll_y=self._on_rv_scroll)
        self.sk = self.sheet.ids.sk
        self.viz = self.sheet.ids.viz
        self.stage = self.sheet.ids.stage
        # 1) the saved library is on screen immediately - never an empty list
        cached = load_cache(self.hidden)
        if cached:
            self.songs = cached
            self._index()
            self.refresh()
            self.empty_text = ""
        Clock.schedule_interval(self.tick, 1 / 30.)
        self.show_welcome()
        # 2) re-attach to the playback service if music is already running
        self.remote.hello()
        if platform == "android":
            try:
                from android import activity
                activity.bind(on_activity_result=self._on_activity_result)
            except Exception:
                pass
            if self.have_audio_perm():
                self.rescan(True)
            Clock.schedule_once(lambda dt: self.request_audio_permissions(), .8)
        else:
            self.rescan(True)

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
            Color(1, 1, 1, 1)
            panel._g1 = RoundedRectangle(
                pos=panel.pos, size=panel.size, radius=[R],
                texture=grad_tex(self.c1[:3] + [.34], self.c2[:3] + [.28]))
            panel._glass = RoundedRectangle(
                pos=panel.pos, size=panel.size, radius=[R],
                texture=grad_tex((1, 1, 1, .20), (1, 1, 1, .03), True))
            Color(1, 1, 1, .34)
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
        Animation(opacity=1, d=.4, t="out_quad").start(panel)
        overlay.opacity = 0
        Animation(opacity=1, d=.25, t="out_quad").start(overlay)
        Clock.schedule_once(lambda dt: self._hide_welcome(overlay), 2.0)

    def _hide_welcome(self, overlay):
        Animation.cancel_all(overlay._shim)
        anim = Animation(opacity=0, d=.35, t="in_quad")
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
        if not self.songs or not self._scanned_once:
            self.rescan(True)
        Clock.schedule_once(lambda dt: self.ask_battery(), 1.5)

    def ask_battery(self):
        """One-time system prompt so the OS doesn't kill the player in the background."""
        if platform != "android":
            return
        flag = os.path.join(pc.data_dir(), "batt.flag")
        try:
            if os.path.exists(flag):
                return
            from jnius import autoclass
            act = get_activity()
            pkg = act.getPackageName()
            if act.getSystemService("power").isIgnoringBatteryOptimizations(pkg):
                return
            i = autoclass("android.content.Intent")("android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS")
            i.setData(autoclass("android.net.Uri").parse("package:" + pkg))
            act.startActivity(i)
            open(flag, "w").close()
        except Exception:
            pass

    def have_audio_perm(self):
        try:
            from android.permissions import check_permission
            return bool(check_permission(self._audio_perm()[0]))
        except Exception:
            return True

    # ---- lifecycle: the service keeps playing, we only tell it whether we are visible
    def on_pause(self):
        self.fg = False
        self.remote.notify({"c": "fg", "v": 0})
        self.tilt_enable(False)
        return True

    def on_resume(self):
        self.fg = True
        self.remote.last = time.time()
        self.remote.hello()
        if self.sheet.opened:
            self.tilt_enable(True)
        if not self.songs:
            self.rescan()

    def on_stop(self):
        self.remote.notify({"c": "fg", "v": 0})
        self.tilt_enable(False)      # music + notification live on in the service

    def on_key(self, win, key, *a):
        if key == 27:
            if self.sel_mode:
                self.sel_clear()
                return True
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
    def _index(self):
        self.by_path = {s["path"]: s for s in self.songs}
        self.selected &= set(self.by_path)

    def rescan(self, first=False, restore=False):
        if self._scanning:
            return
        if platform == "android" and not first and not self.have_audio_perm():
            self.request_audio_permissions()
            return
        if restore:
            self.hidden.clear()
            save_hidden(self.hidden)
        self._scanning = True
        if self.songs:
            self.toast("Scanning...")
        else:
            self.empty_text = "Loading your music..."
        cache = {s["path"]: s for s in self.songs}
        hidden = set(self.hidden)
        threading.Thread(target=self._scan_job, args=(cache, hidden), daemon=True).start()

    def _scan_job(self, cache, hidden):
        def publish(songs):
            Clock.schedule_once(lambda dt: self._scanned(songs, False))
        try:
            songs = scan(cache, hidden, publish)
        except Exception:
            songs = None
        Clock.schedule_once(lambda dt: self._scanned(songs, True))
        if songs is not None:
            save_cache(songs)

    def _scanned(self, songs, final):
        if final:
            self._scanning = False
            self._scanned_once = True
        if songs is not None:
            self.songs = songs
            self._index()
            self.refresh()
        if final:
            self.empty_text = ("" if self.songs else
                               "No songs found.\nAllow music access when asked,\nthen tap refresh.")
            if songs is not None:
                self.toast("%d songs found" % len(songs))

    def set_query(self, text):
        self.q = text.strip().lower()
        self.refresh()

    def refresh(self):
        q = self.q
        self.view = [s for s in self.songs if q in (s["title"] + " " + s["artist"]).lower()]
        self.queue_dirty = True
        data = []
        for i, s in enumerate(self.view):
            prep(s)
            data.append({"idx": i, "path": s["path"], "title": s["td"], "artist": s["ad"],
                         "letter": s["letter"], "hue": s["hue"],
                         "ha": "right" if s["rtl"] else "left",
                         "cur": s["path"] == self.cur_path,
                         "sel": s["path"] in self.selected})
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

    def write_queue(self):
        """The service reads the play queue from disk (can be thousands of songs)."""
        _write_json(pc.queue_file(), [[s["path"], s["title"], s["artist"]] for s in self.view])
        self.queue_dirty = False

    # ---- selection + delete
    def row_long(self, row):
        self.sel_mode = True
        self.toggle_sel(row.path, True)

    def row_tap(self, row):
        if row.long_fired:
            return
        if self.sel_mode:
            self.toggle_sel(row.path)
        else:
            self.play_view(row.idx)

    def toggle_sel(self, path, force=None):
        on = (path not in self.selected) if force is None else force
        if on:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self._sel_changed()

    def sel_all(self):
        self.selected = set(s["path"] for s in self.view)
        self._sel_changed()

    def sel_clear(self):
        self.selected.clear()
        self.sel_mode = False
        self._sel_changed()

    def _sel_changed(self):
        n = len(self.selected)
        self.sel_text = "%d selected" % n
        if n == 0:
            self.sel_mode = False
        for d in self.rv.data:
            d["sel"] = d["path"] in self.selected
        self.rv.refresh_from_data()

    def ask_delete(self):
        n = len(self.selected)
        if not n:
            return
        Confirm(title="Delete %d song%s?" % (n, "" if n == 1 else "s"),
                msg="Delete from phone removes the audio files.\n"
                    "Remove from list only hides them here\n(the refresh button brings them back).").open()

    def do_delete(self, files):
        songs = [s for s in self.songs if s["path"] in self.selected]
        paths = [s["path"] for s in songs]
        self.sel_clear()
        if not songs:
            return
        if not files:
            self.hidden |= set(paths)
            save_hidden(self.hidden)
            self._drop(paths)
        elif platform == "android":
            self._delete_android(songs)
        else:
            gone = []
            for s in songs:
                try:
                    os.remove(s["path"])
                    gone.append(s["path"])
                except Exception:
                    pass
            self._drop(gone)

    def _delete_android(self, songs):
        try:
            from jnius import autoclass
            sdk = autoclass("android.os.Build$VERSION").SDK_INT
            act = get_activity()
            resolver = act.getContentResolver()
            MediaAudio = autoclass("android.provider.MediaStore$Audio$Media")
            ContentUris = autoclass("android.content.ContentUris")
            uris, by_uri = [], []
            for s in songs:
                if s.get("mid") is not None:
                    uris.append(ContentUris.withAppendedId(MediaAudio.EXTERNAL_CONTENT_URI,
                                                           int(s["mid"])))
                    by_uri.append(s["path"])
            if sdk >= 30 and uris:
                # Android shows its own "allow this app to delete N songs?" dialog
                MediaStore = autoclass("android.provider.MediaStore")
                lst = autoclass("java.util.ArrayList")()
                for u in uris:
                    lst.add(u)
                pi = MediaStore.createDeleteRequest(resolver, lst)
                self._del_paths = by_uri
                act.startIntentSenderForResult(pi.getIntentSender(), DEL_REQ, None, 0, 0, 0)
                return
            gone = []
            for s in songs:
                ok = False
                try:
                    os.remove(s["path"])
                    ok = True
                except Exception:
                    pass
                if not ok and s.get("mid") is not None:
                    try:
                        u = ContentUris.withAppendedId(MediaAudio.EXTERNAL_CONTENT_URI,
                                                       int(s["mid"]))
                        ok = resolver.delete(u, None, None) > 0
                    except Exception:
                        ok = False
                if ok:
                    gone.append(s["path"])
            if len(gone) < len(songs):
                self.toast("Some files can't be deleted on this Android version")
            self._drop(gone)
        except Exception:
            self.toast("Delete failed")

    def _on_activity_result(self, code, result, intent):
        if code != DEL_REQ:
            return
        paths = list(self._del_paths)
        Clock.schedule_once(lambda dt: self._after_delete(result == -1, paths))

    def _after_delete(self, ok, paths):
        if ok:
            self._drop(paths)
        else:
            self.toast("Delete cancelled")

    def _drop(self, paths):
        ps = set(paths)
        if not ps:
            return
        self.songs = [s for s in self.songs if s["path"] not in ps]
        self._index()
        self.refresh()
        threading.Thread(target=save_cache, args=(list(self.songs),), daemon=True).start()
        if self.cur_path in ps:
            self.remote.send({"c": "stop"})
            self.on_stopped()
        elif self.remote.ready:
            self.write_queue()
            self.remote.send({"c": "queue"})
        self.toast("%d song%s removed" % (len(ps), "" if len(ps) == 1 else "s"))

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

    # ---- playback (all real work happens in the service)
    def play_view(self, i, open_sheet=True):
        if not (0 <= i < len(self.view)):
            return
        self.cmd_play(self.view[i])
        if open_sheet:
            self.sheet.show()

    def cmd_play(self, s):
        reload_q = self.queue_dirty or not self.remote.ready
        if reload_q:
            self.write_queue()
        now = time.time()
        self.apply_song_ui(s)
        self.playing = True
        self.r_pos, self.r_t = 0.0, now
        self.play_lock = now + .8
        self.remote.send({"c": "play", "path": s["path"], "title": s["title"],
                          "artist": s["artist"], "reload": 1 if reload_q else 0,
                          "shuffle": self.shuffle, "repeat": self.repeat}, start=True)

    def apply_song_ui(self, s):
        prep(s)
        self.cur_path = s["path"]
        self.cur_title, self.cur_artist = s["td"], s["ad"]
        self.cur_letter = s["letter"]
        self.cur_hue = s["hue"]
        self.has_song = True
        self.set_hue(self.cur_hue)
        self.mini.ids.cov.set_cover(s["path"], 160)
        self.stage.set_cover(s["path"], 400)
        self.frac, self.t0 = 0, "0:00"
        self.mark_current()

    def on_state(self, m):
        now = time.time()
        path = m.get("path") or ""
        if not path:
            return
        if path != self.cur_path:
            self.apply_song_ui(self.by_path.get(path) or
                               {"path": path, "title": m.get("title", ""),
                                "artist": m.get("artist", "")})
        if now >= self.play_lock:
            self.playing = bool(m.get("playing"))
            self.shuffle = bool(m.get("shuffle"))
            self.repeat = int(m.get("repeat", 0))
        self.r_pos = float(m.get("pos", 0))
        self.r_dur = float(m.get("dur", 0))
        self.r_t = now

    def on_stopped(self):
        """Closed from the notification (or the playing song was deleted)."""
        self.playing = False
        self.has_song = False
        self.cur_path = None
        self.cur_title = self.cur_artist = ""
        self.r_pos = self.r_dur = 0.0
        self.remote.alive = False
        if self.sheet.opened:
            self.sheet.hide()
        self.mark_current()

    def _step_local(self, step):
        if not self.view:
            return
        if step > 0 and self.shuffle:
            i = random.randrange(len(self.view))
        else:
            i = (max(self.cur_idx(), 0) + step) % len(self.view)
        self.play_view(i, False)

    def go_next(self, *a):
        if self.remote.ready:
            self.remote.send({"c": "next"})
        else:
            self._step_local(1)

    def go_prev(self, *a):
        if self.remote.ready:
            self.remote.send({"c": "prev"})
        else:
            self._step_local(-1)

    def toggle(self):
        if not self.cur_path:
            if self.view:
                self.play_view(0, False)
            return
        if not self.remote.ready:               # service was killed: restart this song
            s = self.by_path.get(self.cur_path)
            if s:
                self.cmd_play(s)
            return
        self.playing = not self.playing
        self.play_lock = time.time() + .8
        self.remote.send({"c": "toggle"})

    def _mode(self):
        self.play_lock = time.time() + .8
        self.remote.send({"c": "mode", "shuffle": self.shuffle, "repeat": self.repeat})

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self._mode()

    def cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self._mode()

    def seek_frac(self, f):
        if self.r_dur > 0:
            self.r_pos, self.r_t = f * self.r_dur, time.time()
            self.remote.send({"c": "seek", "sec": self.r_pos})

    # ---- phone tilt (accelerometer) + spin (gyroscope) drive the 3D cover
    def tilt_enable(self, on):
        if platform != "android":
            return
        for name, attr, flag in (("accelerometer", "_acc", "_acc_on"),
                                 ("gyroscope", "_gyro", "_gyro_on")):
            try:
                mod = getattr(__import__("plyer", fromlist=[name]), name)
                setattr(self, attr, mod)
                if on and not getattr(self, flag):
                    mod.enable()
                    setattr(self, flag, True)
                elif not on and getattr(self, flag):
                    mod.disable()
                    setattr(self, flag, False)
            except Exception:
                setattr(self, flag, False)

    def update_tilt(self, dt):
        tx = ty = 0.0
        if self._acc_on and self._acc is not None:
            try:
                ax, ay, az = self._acc.acceleration
                if ax is not None:
                    tx = max(-1.0, min(1.0, -ax / 6.0))
                    if self._pitch is None:
                        self._pitch = ay
                    self._pitch += (ay - self._pitch) * .02     # neutral = how you hold it
                    ty = max(-1.0, min(1.0, (ay - self._pitch) / 6.0))
            except Exception:
                pass
        self.tilt_x += (tx - self.tilt_x) * .2
        self.tilt_y += (ty - self.tilt_y) * .2
        wx = wy = wz = 0.0
        if self._gyro_on and self._gyro is not None:
            try:
                g = self._gyro.rotation
                if g and g[0] is not None:
                    wx, wy, wz = g
            except Exception:
                pass
        self.stage.update3d(dt, self.t, self.playing, self.bass,
                            self.tilt_x, self.tilt_y, wx, wy, wz)

    # ---- per-frame
    def tick(self, dt):
        now = time.time()
        self.t += dt
        for m in self.remote.pump(now):
            e = m.get("e")
            if e == "state":
                self.on_state(m)
            elif e == "stopped":
                self.on_stopped()
        if self.cur_path and self.r_dur > 0:
            pos = self.r_pos + ((now - self.r_t) if self.playing else 0)
            pos = max(0.0, min(self.r_dur, pos))
            if not self.drag:
                self.frac = pos / self.r_dur
                self.sk.value = self.frac * 1000
                self.t0 = fmt(pos)
            self.t1 = fmt(self.r_dur)
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
            self.update_tilt(dt)
            if self.playing or max(self.bars) > .01:
                self.viz.draw()


if __name__ == "__main__":
    GlassApp().run()
