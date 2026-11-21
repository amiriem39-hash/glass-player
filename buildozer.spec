[app]
title = DCED Plyer
package.name = glassplayer
package.domain = org.glass
source.dir = .
source.include_exts = py,ttf,png,jpg
source.exclude_dirs = .github,bin,java,blocks,res,p4a_keys
source.exclude_patterns = p4a_hook.py
version = 1.3
icon.filename = %(source.dir)s/icon.png
presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #08061A
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pillow,pyjnius,android,plyer,mutagen,arabic-reshaper==3.0.0,python-bidi==0.4.2,six
orientation = portrait
fullscreen = 0
android.permissions = READ_EXTERNAL_STORAGE,READ_MEDIA_AUDIO,POST_NOTIFICATIONS,WAKE_LOCK,INTERNET,FOREGROUND_SERVICE,FOREGROUND_SERVICE_MEDIA_PLAYBACK,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.api = 33
android.minapi = 24
android.ndk = 25b
p4a.branch = v2024.01.21
android.archs = arm64-v8a
android.accept_sdk_license = True

# playback runs in a foreground service (notification player, keeps playing in background)
services = player:service.py:foreground:sticky
# Java helpers for the media session / notification buttons
android.add_src = java
# notification icons (shuffle / repeat)
android.add_resources = res/drawable/ic_shuffle_on.xml:drawable/ic_shuffle_on.xml,res/drawable/ic_shuffle_off.xml:drawable/ic_shuffle_off.xml,res/drawable/ic_repeat_off.xml:drawable/ic_repeat_off.xml,res/drawable/ic_repeat_all.xml:drawable/ic_repeat_all.xml,res/drawable/ic_repeat_one.xml:drawable/ic_repeat_one.xml
# same signing key on every build -> new APK installs over the old one
p4a.hook = p4a_hook.py

[buildozer]
log_level = 2
warn_on_root = 1
