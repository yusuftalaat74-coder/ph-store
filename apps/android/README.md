# The Android shell

The app is a `WebView` that loads `file:///android_asset/index.html`. Everything
a pharmacist sees is `apps/ui/index.html`; the files here are the compiled
Android parts around it, and they change only when the package name, the
permissions, the icon or the label change.

```
AndroidManifest.xml   binary AXML — mz.rova.store · INTERNET · usesCleartextTraffic
classes.dex           the WebView activity
resources.arsc        label and icon
res/drawable/         the launcher icon
```

## Building

```bash
python3 scripts/build_apk.py
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
