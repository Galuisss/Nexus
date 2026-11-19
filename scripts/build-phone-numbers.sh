#!/usr/bin/env bash
set -euo pipefail

nexus_source_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
nexus_phone_build_dir=$(mktemp -d)
trap 'rm -rf -- "$nexus_phone_build_dir"' EXIT
nexus_android_jar="$ANDROID_HOME/platforms/android-36/android.jar"

javac --release 11 -cp "$nexus_android_jar" -d "$nexus_phone_build_dir" \
    "$nexus_source_dir/src/nexus/android/NexusPhoneNumbers.java"
"$ANDROID_HOME/build-tools/36.0.0/d8" --min-api 33 --lib "$nexus_android_jar" \
    --output "$nexus_phone_build_dir" "$nexus_phone_build_dir/NexusPhoneNumbers.class"
cp -- "$nexus_phone_build_dir/classes.dex" "$nexus_source_dir/src/nexus/android/phone_numbers.dex"
