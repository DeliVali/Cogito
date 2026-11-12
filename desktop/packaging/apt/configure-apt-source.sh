#!/bin/sh
# Register COGITO's signed APT repository, as VS Code's and Chrome's .debs do.
#
# Shipped in the .deb (resources/apt/) and run as root by its postinst on first install and
# on every upgrade, so a rotated signing key reaches the keyring with the next release.
# Writes two files, both removed on purge (packaging/linux/after-purge.sh):
#
#   /usr/share/keyrings/cogito-archive-keyring.gpg   the public key shipped beside this file
#   /etc/apt/sources.list.d/cogito.sources           deb822, Signed-By that keyring only
#
# Never apt-key, trusted.gpg or trusted.gpg.d: the key is trusted for this repository only
# (Debian's third-party repository guidance). A package built without the key (a local
# build) registers nothing, so apt is never pointed at a repository it cannot verify.
#
# COGITO_APT_ROOT prefixes every path, for tests only; dpkg never sets it.

set -eu

here=$(dirname "$0")
root=${COGITO_APT_ROOT:-}
repository=https://delivali.github.io/Cogito/apt
key_source="$here/cogito-archive-keyring.gpg"
keyring=/usr/share/keyrings/cogito-archive-keyring.gpg
sources=/etc/apt/sources.list.d/cogito.sources

if [ ! -s "$key_source" ]; then
    exit 0
fi
architecture=$(dpkg --print-architecture 2>/dev/null || echo amd64)

mkdir -p "$root/usr/share/keyrings" "$root/etc/apt/sources.list.d"
cp "$key_source" "$root$keyring.new"
chmod 0644 "$root$keyring.new"
mv -f "$root$keyring.new" "$root$keyring"

cat > "$root$sources.new" <<EOF
# Written by the cogito package. Purging the package (apt purge cogito) removes it.
Types: deb
URIs: $repository
Suites: stable
Components: main
Architectures: $architecture
Signed-By: $keyring
EOF
chmod 0644 "$root$sources.new"
mv -f "$root$sources.new" "$root$sources"

# A one-line .list for the same repository would make apt see it twice.
rm -f "$root/etc/apt/sources.list.d/cogito.list"
