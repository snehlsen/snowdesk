#!/usr/bin/env bash
#
# Build, sign, notarize and staple SnowDesk (spec 12).  The release workflow
# runs this on every tag, and it runs the same way on a Mac that holds the
# certificate.
#
#     packaging/sign.sh                 # build, sign, dmg, notarize, staple
#     packaging/sign.sh --no-build      # sign the dist/SnowDesk.app already there
#     packaging/sign.sh --no-notarize   # stop at a signed, un-notarized dmg
#     packaging/sign.sh --no-selftest   # do not run the signed bundle
#
# Leaves dist/SnowDesk-<version>-arm64.dmg and dist/SHA256SUMS.
#
# No secret goes on the command line, and the script writes none to disk:
#
#   * The Developer ID Application certificate and its key come from the
#     keychain search list: the login keychain on a Mac, a throwaway one in
#     CI.  With exactly one there it is found on its own; otherwise set
#     SNOWDESK_SIGN_IDENTITY to its SHA-1 or full name.  On a Mac the first
#     signature asks for keychain access; "Always Allow" covers codesign from
#     then on.
#   * Notarization uses a keychain profile made once with
#
#         xcrun notarytool store-credentials snowdesk-notary \
#             --key AuthKey_XXXXXXXXXX.p8 --key-id XXXXXXXXXX --issuer <issuer-uuid>
#
#     after which the .p8 can go somewhere safe.  SNOWDESK_NOTARY_PROFILE
#     names a different profile.  CI has no profile, so it sets
#     SNOWDESK_NOTARY_KEY (the .p8's path), SNOWDESK_NOTARY_KEY_ID and
#     SNOWDESK_NOTARY_ISSUER instead.
#
# SNOWDESK_SIGN_IDENTITY=- signs ad hoc instead, without timestamps, and skips
# notarization: a rehearsal of the signing order and the hardened runtime
# before there is a certificate to sign with.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP="$ROOT/dist/SnowDesk.app"
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"
PROFILE=${SNOWDESK_NOTARY_PROFILE:-snowdesk-notary}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

build=1
notarize=1
selftest=1
for arg in "$@"; do
    case "$arg" in
        --no-build) build=0 ;;
        --no-notarize) notarize=0 ;;
        --no-selftest) selftest=0 ;;
        -h | --help)
            awk 'NR > 2 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# --- Preflight: fail before a several-minute build, not after it ----------

[ "$(uname -s)" = Darwin ] || die "this only runs on macOS"
xcrun --find notarytool >/dev/null 2>&1 || die "notarytool not found; install the Xcode command line tools"

if [ -n "${SNOWDESK_SIGN_IDENTITY:-}" ]; then
    IDENTITY=$SNOWDESK_SIGN_IDENTITY
else
    # The SHA-1, not the name: unambiguous even with a renewed certificate
    # beside the old one.
    ids=$(security find-identity -v -p codesigning |
        sed -n 's/^ *[0-9]*) \([0-9A-F]\{40\}\) "Developer ID Application: .*"$/\1/p')
    case $(printf '%s' "$ids" | grep -c .) in
        0) die "no Developer ID Application identity in the keychain (security find-identity -v -p codesigning)" ;;
        1) IDENTITY=$ids ;;
        *) die "several Developer ID Application identities; set SNOWDESK_SIGN_IDENTITY to one of: $(echo $ids)" ;;
    esac
fi

if [ "$IDENTITY" = - ]; then
    TIMESTAMP=--timestamp=none
    notarize=0
    # Library validation wants every library under the app's Team ID, and an
    # ad hoc signature has none, so with it on the rehearsal cannot even load
    # Python.  Turn it off in a throwaway copy; the real build keeps it on.
    cp "$ENTITLEMENTS" "$work/entitlements.plist"
    /usr/libexec/PlistBuddy -c "Add :com.apple.security.cs.disable-library-validation bool true" "$work/entitlements.plist"
    ENTITLEMENTS="$work/entitlements.plist"
    echo "Signing ad hoc: a rehearsal, not something Gatekeeper will accept."
    echo "Library validation is off for it, so only a Developer ID build tests that."
else
    # An expired or revoked certificate still shows up without -v, so check
    # against the valid list rather than trusting the name.
    match=$(security find-identity -v -p codesigning | grep -F "$IDENTITY" || true)
    [ -n "$match" ] || die "no valid codesigning identity matches '$IDENTITY'"
    TIMESTAMP=--timestamp
    echo "Signing as: $(printf '%s\n' "$match" | sed -n '1s/^[^"]*"\(.*\)"$/\1/p')"
fi

if [ -n "${SNOWDESK_NOTARY_KEY:-}" ]; then
    [ -n "${SNOWDESK_NOTARY_KEY_ID:-}" ] && [ -n "${SNOWDESK_NOTARY_ISSUER:-}" ] ||
        die "SNOWDESK_NOTARY_KEY needs SNOWDESK_NOTARY_KEY_ID and SNOWDESK_NOTARY_ISSUER too"
    [ -f "$SNOWDESK_NOTARY_KEY" ] || die "SNOWDESK_NOTARY_KEY is not a file"
    notary_auth=(--key "$SNOWDESK_NOTARY_KEY" --key-id "$SNOWDESK_NOTARY_KEY_ID" --issuer "$SNOWDESK_NOTARY_ISSUER")
    notary_what="the API key in SNOWDESK_NOTARY_KEY"
else
    notary_auth=(--keychain-profile "$PROFILE")
    notary_what="keychain profile '$PROFILE' (see store-credentials at the top of this script)"
fi

if [ "$notarize" = 1 ]; then
    xcrun notarytool history "${notary_auth[@]}" >/dev/null 2>&1 ||
        die "notarytool rejected $notary_what"
fi

VERSION=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/src/snowdesk/__init__.py")
[ -n "$VERSION" ] || die "could not read __version__ from src/snowdesk/__init__.py"
DMG="$ROOT/dist/SnowDesk-${VERSION}-arm64.dmg"

# --- Build -----------------------------------------------------------------

if [ "$build" = 1 ]; then
    step "Building SnowDesk $VERSION"
    (cd "$ROOT" && uv sync --locked && uv run pyinstaller packaging/snowdesk.spec --noconfirm)
fi
[ -d "$APP" ] || die "$APP does not exist; build it or drop --no-build"

# --- Sign the app, inside out ----------------------------------------------
#
# codesign seals nested code by the signature it already has, so everything
# inside must be signed before whatever contains it: loose libraries first,
# then each .framework as a bundle, then the app.  --deep would do this too,
# but it applies the app's entitlements to every library and Apple advises
# against it.  --force replaces PyInstaller's ad hoc signatures and whatever
# the wheels shipped with; library validation needs every Mach-O under the
# same Team ID anyway.

# codesign reports every --force with "replacing existing signature"; a
# hundred of those bury anything that matters.
sign() {
    local out
    if ! out=$(codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP" "$@" 2>&1); then
        printf '%s\n' "$out" >&2
        return 1
    fi
    printf '%s\n' "$out" | grep -v ': replacing existing signature$' || true
}

EXE=$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")

# Deepest path first, so a framework is signed after what is inside it.
deepest_first() { awk -F/ '{ print NF "\t" $0 }' | sort -t "$(printf '\t')" -k1,1nr | cut -f2-; }

machos=()
while IFS= read -r f; do
    machos+=("$f")
done < <(find "$APP/Contents" -type f -print0 |
    xargs -0 file --mime-type -N -F "$(printf '\t')" |
    awk -F '\t' '$2 ~ /x-mach-binary/ { print $1 }')
[ "${#machos[@]}" -gt 0 ] || die "no Mach-O files found in $APP"

loose=()
for f in "${machos[@]}"; do
    [ "$f" = "$APP/Contents/MacOS/$EXE" ] && continue
    case "$f" in
        # A framework's own binary is signed with the framework, below.
        */*.framework/Versions/*/*)
            fw=${f%.framework/*}
            [ "${f##*/}" = "${fw##*/}" ] && continue
            ;;
    esac
    loose+=("$f")
done

step "Signing ${#loose[@]} libraries and extension modules"
sign "${loose[@]}"

frameworks=()
while IFS= read -r fw; do
    frameworks+=("$fw")
done < <(find "$APP/Contents" -type d -name '*.framework' | deepest_first)

step "Signing ${#frameworks[@]} frameworks"
for fw in "${frameworks[@]}"; do
    sign "$fw"
done

step "Signing SnowDesk.app with $(basename "$ENTITLEMENTS")"
sign --entitlements "$ENTITLEMENTS" "$APP"

# --- Check the signature before anyone else does --------------------------

step "Verifying the signature"
codesign --verify --strict --deep "$APP"

# Captured rather than piped: under pipefail, grep -q exiting at its first
# match would SIGPIPE codesign and fail the check it just passed.
app_info=$(codesign -d --verbose=2 "$APP" 2>&1)
printf '%s\n' "$app_info" | grep -q '^CodeDirectory.*flags=.*runtime' ||
    die "the app is not signed with the hardened runtime"

# --verify passes an unsigned library in a folder codesign treats as plain
# resources, while notarization rejects it and library validation refuses to
# load it.  So check each Mach-O by hand: same Team ID as the app, hardened
# runtime on.
app_team=$(printf '%s\n' "$app_info" | sed -n 's/^TeamIdentifier=//p')
bad=0
for f in "${machos[@]}"; do
    info=$(codesign -d --verbose=2 "$f" 2>&1) || {
        echo "  unsigned: ${f#"$APP"/}"
        bad=1
        continue
    }
    team=$(printf '%s\n' "$info" | sed -n 's/^TeamIdentifier=//p')
    if [ "$team" != "$app_team" ]; then
        echo "  Team ID '$team', not '$app_team': ${f#"$APP"/}"
        bad=1
    fi
    if ! printf '%s\n' "$info" | grep -q '^CodeDirectory.*flags=.*runtime'; then
        echo "  no hardened runtime: ${f#"$APP"/}"
        bad=1
    fi
done
[ "$bad" = 0 ] || die "some binaries are not signed the way notarization needs"
echo "${#machos[@]} Mach-O files signed, Team ID ${app_team}, hardened runtime on all of them"

echo "Entitlements:"
codesign -d --entitlements - --xml "$APP" 2>/dev/null | plutil -p - | sed 's/^/  /'

# Library validation only bites at load time, so the signed bundle has to
# actually run; the source tree passing says nothing about it.  The release
# workflow skips it here and runs it in a job of its own, so that the
# bundle's third-party code never runs on a runner that holds the key.
if [ "$selftest" = 1 ]; then
    step "Self-testing the signed bundle"
    QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/$EXE" --selftest
fi

# --- dmg, as the release workflow builds it -------------------------------

step "Building $(basename "$DMG")"
stage="$work/dmg"
mkdir "$stage"
# ditto, not cp -R: it keeps the bundle's symlinks and extended attributes,
# which the framework layout and the signature depend on.
ditto "$APP" "$stage/SnowDesk.app"
ln -s /Applications "$stage/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname SnowDesk -srcfolder "$stage" -fs HFS+ -format UDZO "$DMG"

step "Signing the dmg"
codesign --force --sign "$IDENTITY" "$TIMESTAMP" "$DMG"
codesign --verify --verbose=2 "$DMG"

# --- Notarize and staple ---------------------------------------------------

if [ "$notarize" = 1 ]; then
    # Submitting the dmg notarizes the app inside it as well, so one round
    # trip covers both.
    step "Notarizing (usually a few minutes)"
    result=$(xcrun notarytool submit "$DMG" "${notary_auth[@]}" --wait --output-format json)
    id=$(printf '%s' "$result" | plutil -extract id raw -o - -)
    status=$(printf '%s' "$result" | plutil -extract status raw -o - -)
    echo "Submission $id: $status"
    if [ "$status" != Accepted ]; then
        xcrun notarytool log "$id" "${notary_auth[@]}" || true
        die "notarization finished as '$status'; the log above names the offending files"
    fi

    # The ticket goes into the dmg, so the first launch passes Gatekeeper even
    # offline; the app copied out of it is looked up online by its own hash.
    step "Stapling"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"

    step "Asking Gatekeeper"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
    spctl --assess --type execute --verbose=2 "$APP"
fi

(cd "$ROOT/dist" && shasum -a 256 "$(basename "$DMG")" >SHA256SUMS)

step "Done"
echo "  $DMG"
echo "  $ROOT/dist/SHA256SUMS"
if [ "$notarize" = 0 ]; then
    echo "Not notarized: macOS will still quarantine this dmg on other Macs."
fi
