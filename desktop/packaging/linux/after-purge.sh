#!/bin/sh
# The deb postrm's purge step, put into the built .deb by inject-purge.py (release.yml).
# A purge removes the APT source and keyring the postinst wrote. A plain remove keeps them,
# so `apt install cogito` still works afterwards.
rm -f /etc/apt/sources.list.d/cogito.sources /usr/share/keyrings/cogito-archive-keyring.gpg
