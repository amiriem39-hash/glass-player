"""python-for-android hook: sign every build with the SAME key, so a new APK installs
as an update over the old one (no uninstall). Runs before gradle builds the APK."""
import glob
import os
import shutil


def _find_keystore():
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.getcwd(), os.environ.get("GITHUB_WORKSPACE", "")]
    for r in roots:
        d = r
        for _ in range(8):
            if d:
                p = os.path.join(d, "p4a_keys", "debug.keystore")
                if os.path.isfile(p):
                    return p
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    for p in glob.glob(os.path.join(os.getcwd(), "**", "p4a_keys", "debug.keystore"), recursive=True):
        return p
    return None


def before_apk_build(toolchain):
    src = _find_keystore()
    if not src:
        print("[dced] fixed keystore not found - using the default debug key")
        return
    homes = {os.path.expanduser("~")}
    for k in ("ANDROID_SDK_HOME", "ANDROID_USER_HOME"):
        if os.environ.get(k):
            homes.add(os.environ[k])
    for h in homes:
        d = h if os.path.basename(h) == ".android" else os.path.join(h, ".android")
        try:
            os.makedirs(d, exist_ok=True)
            shutil.copyfile(src, os.path.join(d, "debug.keystore"))
            print("[dced] fixed signing key installed in", d)
        except Exception as e:
            print("[dced] could not install key:", e)
