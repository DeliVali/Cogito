'use strict';

/**
 * The command the package-manager card shows (design 3b) under «Hay una versión nueva.
 * Actualízala con tu gestor de paquetes.», for the manager that installed COGITO. This path
 * never downloads or restarts: dpkg owns those files.
 *
 * The MVP ships one manager, apt, backed by COGITO's own signed APT repository. The `.deb`
 * adds it on install (`packaging/linux/configure-apt-source.sh`) and every release
 * republishes it on GitHub Pages, which is how `--only-upgrade cogito` finds the new
 * version. The design also lists dnf, pacman, zypper, Flatpak, Snap, winget, Scoop,
 * Chocolatey and Homebrew. Each gets a row here once COGITO publishes through it.
 */

const UPGRADE_COMMANDS = Object.freeze({
  apt: 'sudo apt update && sudo apt install --only-upgrade cogito',
});

function upgradeCommand(packageManager) {
  return Object.hasOwn(UPGRADE_COMMANDS, packageManager ?? '') ? UPGRADE_COMMANDS[packageManager] : null;
}

module.exports = { UPGRADE_COMMANDS, upgradeCommand };
