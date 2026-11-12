# Licenses

Cogito is Apache-2.0 (`LICENSE`).

## SearXNG (AGPL-3.0-or-later)

In the desktop app, web search is helped by a [SearXNG](https://github.com/searxng/searxng)
that the core downloads on first use and runs on loopback. SearXNG is licensed under the
GNU Affero General Public License v3.0 or later. Its licence text is in the unpacked tree
(`<components>/searxng/<commit>/searxng-<commit>/LICENSE`), and its source is the commit
named in `core/cogito/setup/searxng.py` (`COMMIT`), at
`https://github.com/searxng/searxng`.

How this stays within the licence:

- **Cogito does not contain it.** Nothing in this repository or in the installers is
  SearXNG code. The app fetches upstream's own archive, checks its SHA-256, and runs it as a
  separate program in its own virtual environment. Cogito talks to it over HTTP on
  127.0.0.1 and imports none of it. Two programs communicating over a socket do not make one
  work, so Cogito's Apache-2.0 terms are unchanged. This is also why it is not bundled in
  the installer: bundling would make the installer a distributor of AGPL code, with the
  source and notice duties that follow.
- **It is not modified.** The only file added to the tree is a generated
  `searx/version_frozen.py`, so SearXNG does not call `git` for its version. The settings
  file Cogito writes is configuration, not code.
- **Section 13 (network use) is not triggered.** It applies to people who interact with a
  modified version remotely. Here it is unmodified and reachable from the one machine it
  runs on, by the one person using it. If you expose its port, or change its code, you take
  on the AGPL's duties for that: offer the corresponding source to its users.
- **Its dependencies** (Flask, lxml, curl_cffi and others, listed with hashes in
  `core/cogito/setup/searxng_lock.py`) are installed from PyPI under their own licences.

Not legal advice. This describes how the code is arranged, and what the arrangement relies
on is that SearXNG stays a separate, unmodified program.

## xterm.js (MIT)

The desktop app's terminal tabs draw with [xterm.js](https://github.com/xtermjs/xterm.js)
6.0.0 (`@xterm/xterm`) and its fit addon 0.11.0 (`@xterm/addon-fit`), shipped unmodified in
`desktop/src/renderer/vendor/xterm/`: the ESM builds, the stylesheet, and each package's
licence (`LICENSE-xterm`, `LICENSE-addon-fit`), which the installers carry with them.

    Copyright (c) 2017-2019, The xterm.js authors (https://github.com/xtermjs/xterm.js)
    Copyright (c) 2014-2016, SourceLair Private Company (https://www.sourcelair.com)
    Copyright (c) 2012-2013, Christopher Jeffrey (https://github.com/chjj/)

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.

The fit addon carries the same terms under "Copyright (c) 2019, The xterm.js authors".
The xterm.js build also includes code from Microsoft's VS Code (MIT) and credits Fabrice
Bellard's jslinux, whose notices are kept in the vendored files' headers.

## Playwright's injected script (Apache-2.0)

The desktop app's browser tabs read pages through Playwright's injected script, which builds
the accessibility snapshot and resolves its refs. It is shipped unmodified in
`desktop/src/main/browser/injected-script.js`: the string literal of
`packages/playwright-core/src/generated/injectedScriptSource.ts`, taken from
`playwright/driver/package/lib/coreBundle.js` of playwright-python 1.64.1 (playwright-core
1.64.0-beta-1791568591000). Nothing else of Playwright is shipped, and Playwright is not a
dependency.

    Playwright
    Copyright (c) Microsoft Corporation

    This software contains code derived from the Puppeteer project
    (https://github.com/puppeteer/puppeteer), available under the Apache 2.0 license
    (https://github.com/puppeteer/puppeteer/blob/master/LICENSE).

    Licensed under the Apache License, Version 2.0 (the "License"); you may not use this
    file except in compliance with the License. You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software distributed under
    the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
    KIND, either express or implied. See the License for the specific language governing
    permissions and limitations under the License.

The text above is Playwright's `NOTICE` and the Apache License's notice; the licence's full
text is the same as Cogito's own `LICENSE`.
