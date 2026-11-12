#!/bin/sh
# Build COGITO's static, signed APT repository from one release's .deb, for GitHub Pages.
#
# usage: build-apt-repo.sh DEB OUTDIR SIGNING_KEY [ARCH]
#
#   OUTDIR/pool/main/c/cogito/<the .deb>
#   OUTDIR/dists/stable/main/binary-ARCH/Packages, Packages.gz
#   OUTDIR/dists/stable/Release, InRelease (clearsigned), Release.gpg (detached)
#
# OUTDIR is served at https://delivali.github.io/Cogito/apt, the URI the .deb registers
# (configure-apt-source.sh). Stateless on purpose: the release workflow rebuilds it from the
# newest .deb on every tag and keeps only that version, so there is no repository database
# to carry between runs. That is why this uses apt-ftparchive, not reprepro or aptly.
# Upgrading only needs the newest version.
#
# Signed with the OpenPGP key SIGNING_KEY (fingerprint) from the caller's GnuPG home, with
# SHA-512 digests: apt 3.0's verifier (sqv) rejects SHA-1 since 2026-02-01. No Valid-Until:
# the repository changes only with a release, and an expiry would break apt in between.

set -eu

if [ "$#" -lt 3 ]; then
    echo "usage: build-apt-repo.sh DEB OUTDIR SIGNING_KEY [ARCH]" >&2
    exit 2
fi
deb=$1
out=$2
key=$3
arch=${4:-amd64}

if [ ! -f "$deb" ]; then
    echo "build-apt-repo: no such .deb: $deb" >&2
    exit 1
fi

rm -rf "$out"
pool="$out/pool/main/c/cogito"
dist="$out/dists/stable"
index="$dist/main/binary-$arch"
mkdir -p "$pool" "$index"
cp "$deb" "$pool/"

(cd "$out" && apt-ftparchive --arch "$arch" packages pool) > "$index/Packages"
if [ ! -s "$index/Packages" ]; then
    echo "build-apt-repo: apt-ftparchive found no $arch package in $deb" >&2
    exit 1
fi
gzip -9 -n -c "$index/Packages" > "$index/Packages.gz"

release=$(mktemp)
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=COGITO \
    -o APT::FTPArchive::Release::Label=COGITO \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o "APT::FTPArchive::Release::Architectures=$arch" \
    -o APT::FTPArchive::Release::Components=main \
    -o "APT::FTPArchive::Release::Description=COGITO desktop" \
    release "$dist" > "$release"
mv "$release" "$dist/Release"
chmod 0644 "$dist/Release"

gpg --batch --yes --local-user "$key" --digest-algo SHA512 --clearsign --output "$dist/InRelease" "$dist/Release"
gpg --batch --yes --local-user "$key" --digest-algo SHA512 --armor --detach-sign --output "$dist/Release.gpg" "$dist/Release"

echo "build-apt-repo: $out ready ($(sed -n 's/^Version: //p' "$index/Packages"), $arch)"
