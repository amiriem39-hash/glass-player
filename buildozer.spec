[app]
title = DCED Plyer
package.name = glassplayer
package.domain = org.glass
source.dir = .
source.include_exts = py,ttf,png,jpg
source.exclude_dirs = .github,bin
version = 1.0
icon.filename = %(source.dir)s/icon.png
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pillow,pyjnius,android,mutagen,arabic-reshaper==3.0.0,python-bidi==0.4.2,six
orientation = portrait
fullscreen = 0
android.permissions = READ_EXTERNAL_STORAGE,READ_MEDIA_AUDIO,POST_NOTIFICATIONS,MANAGE_EXTERNAL_STORAGE,WAKE_LOCK
android.api = 33
android.minapi = 24
android.ndk = 25b
p4a.branch = v2024.01.21
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
