# -*- coding: utf-8 -*-
"""
Shared player core (queue + audio engine).

* On Android it runs inside the foreground service (service.py), so the music
  keeps playing - and the notification player keeps working - even when the
  app is in the background or its window is gone.
* On desktop the UI uses it in-process (testing only).

This module must NOT import kivy (the service process has no window).
"""
import json
import os
import time
import random

CMD_PORT = 47811      # UI      -> service   (UDP, localhost)
UI_PORT = 47812       # service -> UI        (UDP, localhost)
ANDROID = "ANDROID_ARGUMENT" in os.environ or "ANDROID_PRIVATE" in os.environ


def data_dir():
    base = os.environ.get("ANDROID_PRIVATE") or os.path.join(
        os.path.expanduser("~"), ".dced_plyer")
    d = os.path.join(base, "dced")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def queue_file():
    return os.path.join(data_dir(), "queue.json")


def resume_file():
    return os.path.join(data_dir(), "resume.json")


def hue_of(s):
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) % 360
    return h


# ------------------------------------------------------------ engines
if ANDROID:
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

    class _Focus(PythonJavaClass):
        __javainterfaces__ = ["android/media/AudioManager$OnAudioFocusChangeListener"]
        __javacontext__ = "app"

        def __init__(self, cb):
            super().__init__()
            self.cb = cb

        @java_method("(I)V")
        def onAudioFocusChange(self, change):
            self.cb(change)

    class Engine:
        """MediaPlayer wrapper. Java callbacks only set flags; the owner polls."""

        def __init__(self, ctx):
            self.ctx = ctx
            self.mp = None
            self.gen = 0
            self.ended_gen = self.failed_gen = -1
            self.focus_ev = None
            self._refs = []
            self.am = None
            try:
                self.am = ctx.getSystemService("audio")
            except Exception:
                pass
            self._focus = _Focus(self._on_focus)
            self.cfg = {}
            self.fx = []

        def set_fx(self, cfg):
            self.cfg = cfg or {}
            self._apply_fx()

        def _rel_fx(self):
            for f in self.fx:
                try:
                    f.release()
                except Exception:
                    pass
            self.fx = []

        def _apply_fx(self):
            self._rel_fx()
            cfg = self.cfg or {}
            if not self.mp or not cfg.get("on"):
                return
            try:
                sid = self.mp.getAudioSessionId()
            except Exception:
                return
            import math

            def mk(name, *a):
                f = autoclass("android.media.audiofx." + name)(*a)
                f.setEnabled(True)
                self.fx.append(f)
                return f
            try:
                eq = mk("Equalizer", 0, sid)
                lo, hi = eq.getBandLevelRange()[0], eq.getBandLevelRange()[1]
                bands = list(cfg.get("bands") or [0] * 5)[:5]
                bands += [0] * (5 - len(bands))
                tg = (60, 230, 910, 3600, 14000)
                for b in range(eq.getNumberOfBands()):
                    f = max(1.0, eq.getCenterFreq(b) / 1000.0)
                    j = min(range(5), key=lambda k: abs(math.log(f / tg[k])))
                    eq.setBandLevel(b, int(max(lo, min(hi, bands[j] * 100))))
            except Exception:
                pass
            try:
                mk("BassBoost", 0, sid).setStrength(int(cfg.get("bass", 0)))
            except Exception:
                pass
            try:
                mk("Virtualizer", 0, sid).setStrength(int(cfg.get("virt", 0)))
            except Exception:
                pass
            try:
                mk("LoudnessEnhancer", sid).setTargetGain(int(cfg.get("loud", 0)))
            except Exception:
                pass

        def _on_focus(self, change):
            self.focus_ev = change

        def _mark(self, attr, g):
            setattr(self, attr, g)

        def want_focus(self):
            try:
                if self.am:
                    self.am.requestAudioFocus(self._focus, 3, 1)
            except Exception:
                pass

        def drop_focus(self):
            try:
                if self.am:
                    self.am.abandonAudioFocus(self._focus)
            except Exception:
                pass

        def release(self):
            self._rel_fx()
            if self.mp:
                try:
                    self.mp.release()
                except Exception:
                    pass
            self.mp = None

        def stop(self):
            self.release()
            self.drop_focus()

        def play(self, path):
            self.gen += 1
            g = self.gen
            self.release()
            mp = None
            try:
                mp = MediaPlayer()
                done = _Done(lambda: self._mark("ended_gen", g))
                err = _Err(lambda: self._mark("failed_gen", g))
                self._refs = [done, err]
                mp.setOnCompletionListener(done)
                mp.setOnErrorListener(err)
                try:
                    AB = autoclass("android.media.AudioAttributes$Builder")
                    mp.setAudioAttributes(AB().setUsage(1).setContentType(2).build())
                except Exception:
                    pass
                try:
                    mp.setWakeMode(self.ctx, 1)      # PARTIAL_WAKE_LOCK
                except Exception:
                    pass
                mp.setDataSource(path)
                mp.prepare()
                self.want_focus()
                mp.start()
                self.mp = mp
                self._apply_fx()
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
            self.want_focus()
            self._c(lambda: self.mp.start())

        def seek(self, sec):
            self._c(lambda: self.mp.seekTo(int(sec * 1000)))

        def volume(self, v):
            self._c(lambda: self.mp.setVolume(float(v), float(v)))
else:
    class Engine:  # desktop fallback (testing only)
        def __init__(self, ctx=None):
            from kivy.core.audio import SoundLoader
            self._loader = SoundLoader
            self.s = None
            self.gen = 0
            self.ended_gen = self.failed_gen = -1
            self.focus_ev = None
            self._m, self._p = False, 0
            self.cfg = {}

        def set_fx(self, cfg):
            self.cfg = cfg or {}

        def release(self):
            if self.s:
                self._m = True
                self.s.stop()
                self._m = False
            self.s = None

        stop = release

        def play(self, path):
            self.gen += 1
            g = self.gen
            self.release()
            s = self._loader.load(path)
            if not s:
                return False

            def on_stop(*a):
                if not self._m and g == self.gen:
                    self.ended_gen = g
            s.bind(on_stop=on_stop)
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

        def volume(self, v):
            if self.s:
                self.s.volume = v


# ------------------------------------------------------------ core
class PlayerCore:
    def __init__(self, ctx=None):
        self.engine = Engine(ctx)
        try:
            with open(os.path.join(data_dir(), "eq.json"), encoding="utf-8") as f:
                self.engine.cfg = json.load(f)
        except Exception:
            pass
        self.items, self.lookup = [], {}
        self.idx = -1
        self.cur = None
        self.shuffle, self.repeat = False, 0
        self.dirty = True          # set whenever something a notification shows changed
        self.seq = 0               # bumps on every track change
        self.errs = 0
        self.focus_paused = False

    # ---- queue
    def load_queue(self):
        try:
            with open(queue_file(), encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        self.items = [{"path": r[0], "title": r[1], "artist": r[2]} for r in raw]
        self.lookup = {it["path"]: i for i, it in enumerate(self.items)}
        self.idx = self.lookup.get(self.cur["path"], -1) if self.cur else -1

    # ---- commands
    def handle(self, m):
        c = m.get("c")
        if "shuffle" in m:
            self.shuffle = bool(m["shuffle"])
            self.dirty = True
        if "repeat" in m:
            self.repeat = int(m["repeat"])
            self.dirty = True
        if c == "play":
            if m.get("reload"):
                self.load_queue()
            self.errs = 0
            self.play_path(m.get("path", ""), m)
        elif c == "queue":
            self.load_queue()
        elif c == "toggle":
            self.toggle()
        elif c == "resume":
            self.resume()
        elif c == "pause":
            self.pause()
        elif c == "next":
            self.errs = 0
            self.next()
        elif c == "prev":
            self.errs = 0
            self.prev()
        elif c == "seek":
            self.engine.seek(float(m.get("sec", 0)))
            self.dirty = True
        elif c == "stop":
            self.stop()
        elif c == "eq":
            self.engine.set_fx(m)

    def play_path(self, path, m=None):
        if not path:
            return
        i = self.lookup.get(path)
        if i is None:
            m = m or {}
            self.items.append({"path": path, "title": m.get("title") or os.path.basename(path),
                               "artist": m.get("artist") or ""})
            i = len(self.items) - 1
            self.lookup[path] = i
        self.step_to(i)

    def step_to(self, j):
        n = len(self.items)
        tries = 0
        while n and tries < n:
            self.idx = j
            self.cur = self.items[j]
            self.seq += 1
            self.dirty = True
            self.focus_paused = False
            if self.engine.play(self.cur["path"]):
                return True
            tries += 1
            j = (j + 1) % n
        self.engine.release()
        return False

    def next(self):
        n = len(self.items)
        if not n:
            return
        if self.shuffle and n > 1:
            j = random.randrange(n)
            while j == self.idx:
                j = random.randrange(n)
        else:
            j = (self.idx + 1) % n
        self.step_to(j)

    def prev(self):
        n = len(self.items)
        if not n:
            return
        if self.engine.pos() > 3:
            self.engine.seek(0)
            self.dirty = True
            return
        self.step_to((max(self.idx, 0) - 1) % n)

    def pause(self):
        self.engine.pause()
        self.focus_paused = False
        self.dirty = True

    def resume(self):
        if self.cur:
            self.engine.resume()
            self.focus_paused = False
            self.dirty = True

    def toggle(self):
        if not self.cur:
            if self.items:
                self.step_to(0)
            return
        if self.engine.playing():
            self.pause()
        else:
            self.resume()

    def stop(self):
        self.engine.stop()
        self.cur, self.idx = None, -1
        self.dirty = True

    # ---- called regularly by the owner (service loop / UI tick)
    def poll(self):
        e = self.engine
        if e.ended_gen == e.gen:
            e.ended_gen = -1
            self._on_end()
        if e.failed_gen == e.gen:
            e.failed_gen = -1
            self.errs += 1
            if self.errs <= max(1, len(self.items)):
                self.next()
        ev, e.focus_ev = e.focus_ev, None
        if ev is not None:
            if ev in (-1, -2):                    # LOSS / LOSS_TRANSIENT
                if e.playing():
                    e.pause()
                    self.focus_paused = ev == -2
                    self.dirty = True
            elif ev == -3:                        # CAN_DUCK
                e.volume(.25)
            elif ev == 1:                         # GAIN
                e.volume(1.0)
                if self.focus_paused and self.cur:
                    self.focus_paused = False
                    e.resume()
                    self.dirty = True

    # ---- survive the system killing the service (swipe from recents on some phones)
    def save_resume(self, clean=False):
        try:
            it = self.cur
            d = {"clean": bool(clean or it is None), "ts": time.time(),
                 "path": it["path"] if it else "",
                 "title": it["title"] if it else "", "artist": it["artist"] if it else "",
                 "pos": self.engine.pos() if it else 0,
                 "playing": bool(it and self.engine.playing()),
                 "shuffle": self.shuffle, "repeat": self.repeat}
            tmp = resume_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, resume_file())
        except Exception:
            pass

    def load_resume(self):
        """After an unclean kill: bring back the song (and keep playing if it was playing)."""
        try:
            with open(resume_file(), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return False
        try:
            if d.get("clean") or not d.get("path") or not os.path.isfile(d["path"]):
                return False
            self.shuffle = bool(d.get("shuffle"))
            self.repeat = int(d.get("repeat", 0))
            self.load_queue()
            if not self.items:
                self.items = []
            self.play_path(d["path"], d)
            pos = float(d.get("pos", 0))
            if pos > 1:
                self.engine.seek(pos)
            fresh = time.time() - float(d.get("ts", 0)) < 120
            if not (d.get("playing") and fresh):
                self.engine.pause()
            self.dirty = True
            return True
        except Exception:
            return False

    def _on_end(self):
        if self.repeat == 2:
            self.engine.seek(0)
            self.engine.resume()
        elif self.repeat == 1 or self.shuffle or self.idx < len(self.items) - 1:
            self.next()
        self.dirty = True

    def state(self):
        it = self.cur
        e = self.engine
        pos = e.pos()
        if pos > 2:
            self.errs = 0
        return {"path": it["path"] if it else "",
                "title": it["title"] if it else "",
                "artist": it["artist"] if it else "",
                "playing": e.playing() if it else False,
                "pos": pos if it else 0, "dur": e.dur() if it else 0,
                "shuffle": self.shuffle, "repeat": self.repeat,
                "seq": self.seq}
