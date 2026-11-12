'use strict';

/**
 * The git branch chip (1a). The main process runs `git` itself, with no core round-trip
 * or server route. `context/prompt.py` computes the same fact for the model's prompt but
 * does not expose it to the window, and this was the cheapest way to show it.
 */

const { execFile } = require('node:child_process');

/** The current branch name for `root`, or null when it is not a git checkout, HEAD is
 * detached, or git is not on PATH. */
function currentBranch(root) {
  return new Promise((resolve) => {
    execFile(
      'git',
      ['rev-parse', '--abbrev-ref', 'HEAD'],
      { cwd: root, timeout: 3000 },
      (error, stdout) => {
        if (error) {
          resolve(null);
          return;
        }
        const branch = stdout.trim();
        resolve(branch && branch !== 'HEAD' ? branch : null);
      },
    );
  });
}

module.exports = { currentBranch };
