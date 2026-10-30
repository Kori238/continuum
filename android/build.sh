#!/usr/bin/env bash
# Build the phone app and drop the APK in dist/.
#
# This exists because there is no java on PATH and no JAVA_HOME on this
# machine: the JDK was installed bare, so `./gradlew` dies at the wrapper
# before Gradle starts and before it could read org.gradle.java.home. Setting
# it here rather than in the user environment keeps the requirement next to the
# thing that requires it.
set -euo pipefail

export JAVA_HOME="${JAVA_HOME:-/c/Program Files/Microsoft/jdk-17.0.20.8-hotspot}"
[ -x "$JAVA_HOME/bin/javac.exe" ] || { echo "no JDK at $JAVA_HOME" >&2; exit 1; }

cd "$(dirname "$0")"
./gradlew.bat "${@:-assembleDebug}" --console=plain

# Named, not app-debug.apk: this is what gets served to the phone, and a file
# called "debug" is easy to mistake for something disposable.
mkdir -p dist
cp app/build/outputs/apk/debug/app-debug.apk dist/continuum-phone.apk

# Record the built version next to the APK so the desktop can tell the phone
# whether a newer build is waiting (served at /api/app/version). Written by the
# same step that produces the APK, so the two never disagree.
vc=$(grep -oE 'versionCode[[:space:]]*=[[:space:]]*[0-9]+' app/build.gradle.kts | grep -oE '[0-9]+' | head -1)
vn=$(grep -oE 'versionName[[:space:]]*=[[:space:]]*"[^"]+"' app/build.gradle.kts | sed -E 's/.*"([^"]+)".*/\1/' | head -1)
printf '{"versionCode": %s, "versionName": "%s"}\n' "${vc:-0}" "${vn:-0}" > dist/version.json

echo "dist/continuum-phone.apk  $(du -h dist/continuum-phone.apk | cut -f1)  v${vn:-?} (code ${vc:-?})"
