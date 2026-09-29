#!/usr/bin/env python3
"""Compile the Android shell (apps/android/src) into apps/android/classes.dex.

    python3 scripts/build_shell.py

Needs a JDK and two Ubuntu/Debian packages — no Android Studio, no Gradle:

    apt-get install openjdk-21-jdk-headless android-sdk-platform-23 dalvik-exchange

`android-sdk-platform-23` provides android.jar (API 23 stubs, enough for
everything the shell calls; minSdk stays 21), and `dalvik-exchange` is `dx`.
The result is committed, so `scripts/build_apk.py` — which only zips and
signs — keeps working on a machine with neither.

The shell changes rarely: v4.3 is the first change since the WebView
activity was written, to add the file chooser the sign-up photo needs.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "apps" / "android" / "src"
OUT = ROOT / "apps" / "android" / "classes.dex"
ANDROID_JAR = pathlib.Path(os.environ.get(
    "ANDROID_JAR", "/usr/lib/android-sdk/platforms/android-23/android.jar"))


def main() -> int:
    dx = shutil.which("dalvik-exchange") or shutil.which("dx")
    if not ANDROID_JAR.exists() or not dx or not shutil.which("javac"):
        print("needs javac, dalvik-exchange (dx) and android.jar — see the docstring", file=sys.stderr)
        return 2
    sources = sorted(str(p) for p in SRC.rglob("*.java"))
    with tempfile.TemporaryDirectory() as tmp:
        classes = pathlib.Path(tmp) / "classes"
        classes.mkdir()
        subprocess.run(["javac", "-source", "8", "-target", "8", "-Xlint:-options", "-nowarn",
                        "-bootclasspath", str(ANDROID_JAR), "-d", str(classes), *sources], check=True)
        subprocess.run([dx, "--dex", "--min-sdk-version=21", f"--output={OUT}", str(classes)], check=True)
    print(f"  {OUT.relative_to(ROOT)}  {OUT.stat().st_size:,} bytes from {len(sources)} source file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
