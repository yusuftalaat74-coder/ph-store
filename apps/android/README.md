# The Android shell

The app is a `WebView` that loads `file:///android_asset/index.html`. Everything
a pharmacist sees is `apps/ui/index.html`; the files here are the compiled
Android parts around it, and they change only when the package name, the
permissions, the icon or the label change.

```
AndroidManifest.xml   binary AXML — mz.rova.store · v4.3 (versionCode 43) · INTERNET ·
                      usesCleartextTraffic · the camera file provider
classes.dex           compiled from src/ (scripts/build_shell.py)
src/mz/rova/store/    MainActivity.java (the WebView + file chooser), Files.java (provider)
resources.arsc        label and icon
res/drawable/         the launcher icon
```

## v4.3 — the file chooser

Up to v4.2 the shell had no `WebChromeClient.onShowFileChooser`, so every
`<input type="file">` in the page was a dead button. v4.3 adds it: a tap on
the Alvará field opens one chooser with the camera plus the gallery / files
(images and PDF). The camera writes into the app's cache through `Files`, a
minimal content provider (`exported=false`, `grantUriPermissions=true`),
because handing a `file://` path to the camera app throws on Android 7+.
No `CAMERA` permission is declared — declaring it without a runtime grant is
what would make the camera intent fail. The page detects the new shell from
` PHStoreShell/4.3 files` at the end of the user agent.

Every build up to v4.2 carried `versionCode=2 / versionName="2.0"`. v4.3 is
`43 / "4.3"`, signed with the same key, so it installs as an update over v4.2
without uninstalling.

## Changing the shell

```bash
apt-get install openjdk-21-jdk-headless android-sdk-platform-23 dalvik-exchange
python3 scripts/build_shell.py                     # src/ -> classes.dex
python3 scripts/bump_manifest.py                   # print the manifest
python3 scripts/bump_manifest.py --version-name 4.4 --version-code 44
```

`bump_manifest.py` reads and rewrites the binary manifest by walking its
chunks; it is also how the `<provider>` was added (`--file-provider`).

## Building

```bash
python3 scripts/build_apk.py --out apps/PH-Store-v4.3.apk --api-base http://api.novaraca.com:8099
```

No Android SDK is needed. The script assembles the archive, signs it with both
a v1 (JAR) and an APK Signature Scheme v2 signature, verifies the result
against its own reading of the spec, and then — when `apksigner` is installed —
against the platform's own verifier as well, because a signer that only agrees
with itself proves nothing.

`--api-base` sets the address the app starts on. It is baked into the copy
inside the APK and never into `apps/ui/index.html`, which is also served from
the API itself where the origin is the right answer.

## `usesCleartextTraffic`

Present because the pilot API is `http://`. **Remove it once TLS is in front of
the API.** While it is there the app will happily talk to an unencrypted
address if one is typed into the Servidor field by mistake.

## The signing key — read this

`pilot-signing-key.pem` and `pilot-signing-cert.pem` are in this repository on
purpose, and it is a trade-off rather than an oversight.

Android ties an app's identity to its signing key: a build signed with a
different key cannot install over one signed with the old key, it has to be
uninstalled first. A key that only ever existed on one machine is a key that
will be lost, and losing it means every pharmacy running the app has to
uninstall and reinstall. Keeping it here is what makes the next build an
update rather than a migration.

The repository is private and this key signs an internally distributed pilot.

**It must be replaced before the app is published anywhere** — a real store
listing, a download link, anything a stranger can reach. At that point the key
belongs in a secret store or a hardware key, not in a git history, and the
first public build is the moment to change it, because after that the same
uninstall problem applies to real users.

The current key was generated on 24 September 2026 and does not match the one
that signed `PH-Store-v2.0.apk` — that key was never saved. Anyone holding v2.0
has to uninstall it before installing a later build.
