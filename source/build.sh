#!/bin/bash
# Rebuilds ../benev-install.sh: install-body.sh followed by the dashboard (app/BENEV plus
# ../README.md) as a base64-encoded tar.gz. Run with Git Bash on Windows or on any Linux.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../benev-install.sh"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

cp -r "$here/app/BENEV" "$stage/BENEV"
cp "$here/../README.md" "$stage/BENEV/README.md"
find "$stage" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} +
# VERSION line 1 is what the updater compares: the release tag (BENEV_VERSION, set by the
# GitHub workflow) or, for local builds, git's description (dev builds count as older).
version=${BENEV_VERSION:-$(git -C "$here" describe --tags --always --dirty 2>/dev/null || echo dev)}
printf '%s\nbuilt %s\n' "$version" "$(date -u +%Y-%m-%dT%H:%MZ)" > "$stage/BENEV/VERSION"
case "$version" in
    *-dirty|dev)
        echo "WARNING: this is a development build ($version): it has changes that are not committed." >&2
        echo "         Do not ship it. Release builds come from a tag: git tag vX.Y.Z && git push origin vX.Y.Z" >&2
        ;;
esac
# Builds know their repository, so installed boards update from it: BENEV_OTA_REPO (set by
# the GitHub workflow), else the GitHub repository of this checkout's git origin.
repo=${BENEV_OTA_REPO:-}
if [[ -z $repo ]]; then
    origin=$(git -C "$here" remote get-url origin 2>/dev/null || true)
    if [[ $origin =~ github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$ ]]; then
        repo=${BASH_REMATCH[1]%.git}
    fi
fi
rm -f "$stage/BENEV/system/ota-repo"
if [[ -n $repo ]]; then
    echo "$repo" > "$stage/BENEV/system/ota-repo"
fi

# Windows line endings would break bash and Python on the Pi.
if grep -rlI $'\r' "$stage" "$here/install-body.sh"; then
    echo "The files above have Windows (CRLF) line endings; convert them to LF first." >&2
    exit 1
fi

{
    cat "$here/install-body.sh"
    echo "__BENEV_PAYLOAD_BELOW__"
    tar -C "$stage" --owner=0 --group=0 --numeric-owner -czf - BENEV | base64 -w 76
} > "$out"
chmod +x "$out"
bash -n "$out"
echo "Built $out ($(wc -c < "$out") bytes), version $version, updates from ${repo:-nowhere (no repository)}"
