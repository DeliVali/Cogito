# The .deb postinst body, one file for every way dpkg configures the package. fpm puts it
# twice into a `#!/bin/sh` postinst (dash on Debian and Ubuntu): in after_install()
# (`configure` with no previous version) and in after_upgrade() (`configure <old>`, which is
# an upgrade or a reinstall after `apt remove`, whose postrm deleted /usr/bin/cogito).
# So it is POSIX sh, and every run restores everything: the link, the sandbox's mode, the
# MIME and desktop databases, the AppArmor profile and COGITO's APT repository.
#
# The first part is electron-builder 26.15.3's templates/linux/after-install.tpl with two
# changes. Its two macros are written out as `cogito` (linux.executableName in
# electron-builder.yml) and `COGITO` (the sanitized productName), because fpm reads this
# file directly for after_upgrade and substitutes nothing. Its one bash-only test,
# `[[ -L ... ]]`, is `[ -L ... ]`: under dash `[[` is "not found", which read as "no user
# namespaces" and made chrome-sandbox setuid root on every machine.

if type update-alternatives >/dev/null 2>&1; then
    # Remove previous link if it doesn't use update-alternatives
    if [ -L '/usr/bin/cogito' -a -e '/usr/bin/cogito' -a "`readlink '/usr/bin/cogito'`" != '/etc/alternatives/cogito' ]; then
        rm -f '/usr/bin/cogito'
    fi
    update-alternatives --install '/usr/bin/cogito' 'cogito' '/opt/COGITO/cogito' 100 || ln -sf '/opt/COGITO/cogito' '/usr/bin/cogito'
else
    ln -sf '/opt/COGITO/cogito' '/usr/bin/cogito'
fi

# Check if user namespaces are supported by the kernel and working with a quick test:
if ! { [ -L /proc/self/ns/user ] && unshare --user true; }; then
    # Use SUID chrome-sandbox only on systems without user namespaces:
    chmod 4755 '/opt/COGITO/chrome-sandbox' || true
else
    chmod 0755 '/opt/COGITO/chrome-sandbox' || true
fi

if hash update-mime-database 2>/dev/null; then
    update-mime-database /usr/share/mime || true
fi

if hash update-desktop-database 2>/dev/null; then
    update-desktop-database /usr/share/applications || true
fi

# Install apparmor profile. (Ubuntu 24+)
# First check if the version of AppArmor running on the device supports our profile.
# This is in order to keep backwards compatibility with Ubuntu 22.04 which does not support abi/4.0.
# In that case, we just skip installing the profile since the app runs fine without it on 22.04.
#
# Those apparmor_parser flags are akin to performing a dry run of loading a profile.
# https://wiki.debian.org/AppArmor/HowToUse#Dumping_profiles
#
# Unfortunately, at the moment AppArmor doesn't have a good story for backwards compatibility.
# https://askubuntu.com/questions/1517272/writing-a-backwards-compatible-apparmor-profile
if apparmor_status --enabled > /dev/null 2>&1; then
  APPARMOR_PROFILE_SOURCE='/opt/COGITO/resources/apparmor-profile'
  APPARMOR_PROFILE_TARGET='/etc/apparmor.d/cogito'
  if apparmor_parser --skip-kernel-load --debug "$APPARMOR_PROFILE_SOURCE" > /dev/null 2>&1; then
    cp -f "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"

    # Updating the current AppArmor profile is not possible and probably not meaningful in a chroot'ed environment.
    # Use cases are for example environments where images for clients are maintained.
    # There, AppArmor might correctly be installed, but live updating makes no sense.
    if ! { [ -x '/usr/bin/ischroot' ] && /usr/bin/ischroot; } && hash apparmor_parser 2>/dev/null; then
      # Extra flags taken from dh_apparmor:
      # > By using '-W -T' we ensure that any abstraction updates are also pulled in.
      # https://wiki.debian.org/AppArmor/Contribute/FirstTimeProfileImport
      apparmor_parser --replace --write-cache --skip-read-cache "$APPARMOR_PROFILE_TARGET"
    fi
  else
    echo "Skipping the installation of the AppArmor profile as this version of AppArmor does not seem to support the bundled profile"
  fi
fi

# COGITO: the APT repository this package updates from (packaging/apt/). Runs on every
# upgrade too, so a rotated signing key shipped in a release reaches the keyring.
sh '/opt/COGITO/resources/apt/configure-apt-source.sh' || true
