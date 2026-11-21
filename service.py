# -*- coding: utf-8 -*-
"""
DCED Plyer - playback foreground service.

Owns the MediaPlayer, the MediaSession and the notification player, so music
keeps going (and the notification buttons keep working) while the app is in the
background or closed. The UI talks to it over localhost UDP (see playercore).
"""
import json
import os
import select
import socket
import time
import traceback

from jnius import autoclass

import playercore as pc

PythonService = autoclass("org.kivy.android.PythonService")
svc = PythonService.mService
ctx = svc.getApplicationContext()
PKG = ctx.getPackageName()
NOTIF_ID = 7401
CHANNEL = "dced_music"
SDK = autoclass("android.os.Build$VERSION").SDK_INT
Bridge = autoclass("org.dced.player.Bridge")


def log():
    traceback.print_exc()


class Notifier:
    """MediaSession + MediaStyle notification (prev / play-pause / next / close)."""

    def __init__(self, core):
        self.core = core
        self.session = None
        self.receiver = None
        self.manager = None
        self.fg = False
        self._cov = None
        self.ok = False

    def setup(self):
        self.manager = ctx.getSystemService("notification")
        if SDK >= 26:
            Channel = autoclass("android.app.NotificationChannel")
            ch = Channel(CHANNEL, "DCED Plyer playback", 2)
            ch.setDescription("Music playback controls")
            try:
                ch.setShowBadge(False)
            except Exception:
                pass
            self.manager.createNotificationChannel(ch)
        MediaSession = autoclass("android.media.session.MediaSession")
        Handler = autoclass("android.os.Handler")
        Looper = autoclass("android.os.Looper")
        self.session = MediaSession(ctx, "DCED Plyer")
        self.session.setFlags(3)
        self.session.setCallback(autoclass("org.dced.player.MediaCallback")(),
                                 Handler(Looper.getMainLooper()))
        self.session.setActive(True)
        IntentFilter = autoclass("android.content.IntentFilter")
        f = IntentFilter()
        for a in ("PREV", "TOGGLE", "NEXT", "CLOSE"):
            f.addAction(PKG + "." + a)
        f.addAction("android.media.AUDIO_BECOMING_NOISY")
        self.receiver = autoclass("org.dced.player.ControlReceiver")()
        ctx.registerReceiver(self.receiver, f)
        self.ok = True

    # ---- cover art for the notification / lock screen
    def cover(self, it):
        path = it["path"]
        if self._cov and self._cov[0] == path:
            return self._cov[1]
        BitmapFactory = autoclass("android.graphics.BitmapFactory")
        bmp = None
        try:
            MMR = autoclass("android.media.MediaMetadataRetriever")
            r = MMR()
            try:
                r.setDataSource(path)
                data = r.getEmbeddedPicture()
                if data is not None and len(data) > 0:
                    bmp = BitmapFactory.decodeByteArray(data, 0, len(data))
            finally:
                try:
                    r.release()
                except Exception:
                    pass
        except Exception:
            bmp = None
        if bmp is None:
            folder = os.path.dirname(path)
            for n in ("cover.jpg", "folder.jpg", "cover.png", "folder.png"):
                p = os.path.join(folder, n)
                if os.path.isfile(p):
                    try:
                        bmp = BitmapFactory.decodeFile(p)
                    except Exception:
                        bmp = None
                    if bmp is not None:
                        break
        if bmp is not None:
            try:
                w, h = bmp.getWidth(), bmp.getHeight()
                m = max(w, h)
                if m > 512:
                    k = 512.0 / m
                    bmp = autoclass("android.graphics.Bitmap").createScaledBitmap(
                        bmp, max(1, int(w * k)), max(1, int(h * k)), True)
            except Exception:
                pass
        else:
            bmp = self.letter_bitmap(it)
        self._cov = (path, bmp)
        return bmp

    def letter_bitmap(self, it):
        """Coloured tile with the first letter - used when a song has no artwork."""
        try:
            Bitmap = autoclass("android.graphics.Bitmap")
            Config = autoclass("android.graphics.Bitmap$Config")
            Canvas = autoclass("android.graphics.Canvas")
            Paint = autoclass("android.graphics.Paint")
            Align = autoclass("android.graphics.Paint$Align")
            Color = autoclass("android.graphics.Color")
            b = Bitmap.createBitmap(256, 256, Config.ARGB_8888)
            c = Canvas(b)
            c.drawColor(Color.HSVToColor([float(pc.hue_of(it["title"])), 0.55, 0.72]))
            p = Paint()
            p.setAntiAlias(True)
            p.setColor(-1)
            p.setTextSize(130.0)
            p.setTextAlign(Align.CENTER)
            c.drawText((it["title"] or "\u266a")[0], 128.0, 175.0, p)
            return b
        except Exception:
            return None

    # ---- publish state to the system
    def update(self):
        core = self.core
        it = core.cur
        if not it or not self.ok:
            return
        playing = core.engine.playing()
        title = it["title"] or "DCED Plyer"       # raw text: Android shapes Persian itself
        artist = it["artist"] or ""
        pos = int(core.engine.pos() * 1000)
        dur = int(core.engine.dur() * 1000)
        bmp = self.cover(it)
        try:
            MB = autoclass("android.media.MediaMetadata$Builder")
            mb = MB()
            mb.putString("android.media.metadata.TITLE", title)
            mb.putString("android.media.metadata.ARTIST", artist)
            mb.putLong("android.media.metadata.DURATION", dur)
            if bmp is not None:
                mb.putBitmap("android.media.metadata.ALBUM_ART", bmp)
            self.session.setMetadata(mb.build())
            PS = autoclass("android.media.session.PlaybackState")
            PB = autoclass("android.media.session.PlaybackState$Builder")
            RD = autoclass("android.R$drawable")
            actions = (PS.ACTION_PLAY | PS.ACTION_PAUSE | PS.ACTION_PLAY_PAUSE |
                       PS.ACTION_SKIP_TO_NEXT | PS.ACTION_SKIP_TO_PREVIOUS |
                       PS.ACTION_SEEK_TO | PS.ACTION_STOP)
            pb = PB()
            pb.setActions(actions)
            pb.setState(3 if playing else 2, pos, 1.0 if playing else 0.0)
            try:
                pb.addCustomAction("close", "Close", RD.ic_menu_close_clear_cancel)
            except Exception:
                pass
            self.session.setPlaybackState(pb.build())
        except Exception:
            log()
        try:
            self.notify(title, artist, bmp, playing)
        except Exception:
            log()

    def notify(self, title, artist, bmp, playing):
        NB = autoclass("android.app.Notification$Builder")
        MediaStyle = autoclass("android.app.Notification$MediaStyle")
        PendingIntent = autoclass("android.app.PendingIntent")
        Intent = autoclass("android.content.Intent")
        RD = autoclass("android.R$drawable")
        flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE

        def pending(name, code):
            i = Intent(PKG + "." + name)
            i.setPackage(PKG)
            return PendingIntent.getBroadcast(ctx, code, i, flags)

        b = NB(ctx, CHANNEL) if SDK >= 26 else NB(ctx)
        b.setContentTitle(title)
        b.setContentText(artist)
        b.setSmallIcon(RD.ic_media_pause if playing else RD.ic_media_play)
        b.setOngoing(bool(playing))
        b.setOnlyAlertOnce(True)
        b.setShowWhen(False)
        b.setVisibility(1)
        launch = ctx.getPackageManager().getLaunchIntentForPackage(PKG)
        if launch is not None:
            b.setContentIntent(PendingIntent.getActivity(ctx, 10, launch, flags))
        if bmp is not None:
            b.setLargeIcon(bmp)
        b.addAction(RD.ic_media_previous, "Previous", pending("PREV", 11))
        b.addAction(RD.ic_media_pause if playing else RD.ic_media_play,
                    "Pause" if playing else "Play", pending("TOGGLE", 12))
        b.addAction(RD.ic_media_next, "Next", pending("NEXT", 13))
        b.addAction(RD.ic_menu_close_clear_cancel, "Close", pending("CLOSE", 14))
        style = MediaStyle()
        style.setMediaSession(self.session.getSessionToken())
        style.setShowActionsInCompactView([0, 1, 2])
        b.setStyle(style)
        n = b.build()
        if not self.fg:
            svc.startForeground(NOTIF_ID, n)       # also replaces the default service notification
            self.fg = True
        else:
            self.manager.notify(NOTIF_ID, n)

    def close(self):
        try:
            svc.stopForeground(True)
        except Exception:
            pass
        try:
            self.manager.cancel(NOTIF_ID)
        except Exception:
            pass
        try:
            if self.session:
                self.session.setActive(False)
                self.session.release()
        except Exception:
            pass
        try:
            if self.receiver:
                ctx.unregisterReceiver(self.receiver)
        except Exception:
            pass


class Service:
    def __init__(self):
        self.core = pc.PlayerCore(ctx)
        self.note = Notifier(self.core)
        self.running = True
        self.ui_fg = False
        self.last_act = time.time()
        self.last_send = 0.0
        self.sock = None

    def bind(self):
        for _ in range(30):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", pc.CMD_PORT))
                s.setblocking(False)
                self.sock = s
                return
            except OSError:
                time.sleep(.25)
        raise RuntimeError("cannot bind command port")

    def to_ui(self, msg):
        try:
            self.sock.sendto(json.dumps(msg).encode("utf-8"), ("127.0.0.1", pc.UI_PORT))
        except Exception:
            pass

    def send_state(self):
        st = self.core.state()
        st["e"] = "state"
        self.to_ui(st)
        self.last_send = time.time()

    # ---- commands from the UI
    def on_msg(self, m):
        self.last_act = time.time()
        c = m.get("c")
        if c == "hello":
            self.ui_fg = True
            self.send_state()
        elif c == "fg":
            self.ui_fg = bool(m.get("v"))
            if self.ui_fg:
                self.send_state()
        elif c == "stop":
            self.shutdown()
        else:
            self.core.handle(m)
            if self.ui_fg and c in ("play", "next", "prev", "toggle", "seek"):
                self.send_state()

    # ---- commands from the notification / lock screen / headset
    def on_bridge(self, s):
        self.last_act = time.time()
        core = self.core
        if s == "toggle":
            core.toggle()
        elif s == "play":
            core.resume()
        elif s == "pause":
            core.pause()
        elif s == "next":
            core.errs = 0
            core.next()
        elif s == "prev":
            core.errs = 0
            core.prev()
        elif s == "close":
            self.shutdown()
        elif s.startswith("seek:"):
            try:
                core.engine.seek(int(s[5:]) / 1000.0)
                core.dirty = True
            except ValueError:
                pass
        if self.ui_fg:
            self.send_state()

    def shutdown(self):
        if not self.running:
            return
        self.running = False
        try:
            self.core.engine.stop()
        except Exception:
            pass
        self.note.close()
        self.to_ui({"e": "stopped"})
        try:
            svc.stopSelf()
        except Exception:
            pass

    def run(self):
        try:
            self.note.setup()
        except Exception:
            log()
        self.bind()
        while self.running:
            try:
                r, _, _ = select.select([self.sock], [], [], .1)
                while r:
                    try:
                        data, _a = self.sock.recvfrom(65535)
                    except (BlockingIOError, InterruptedError):
                        break
                    try:
                        self.on_msg(json.loads(data.decode("utf-8")))
                    except Exception:
                        log()
                    r, _, _ = select.select([self.sock], [], [], 0)
                while True:
                    s = Bridge.poll()
                    if s is None:
                        break
                    self.on_bridge(str(s))
                if not self.running:
                    break
                seq = self.core.seq
                self.core.poll()
                if self.core.dirty:
                    self.core.dirty = False
                    self.note.update()
                    if self.ui_fg and self.core.seq != seq:
                        self.send_state()
                now = time.time()
                if self.ui_fg and now - self.last_send >= .25:
                    self.send_state()
                if self.core.cur is None and now - self.last_act > 45:
                    self.shutdown()          # started (or restarted) with nothing to play
            except Exception:
                log()
                time.sleep(.2)


if __name__ == "__main__":
    Service().run()
