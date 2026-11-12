"""The three search levels, fixed: documentation and web search always on, the browser
asked for in the chat.

Until 2026-10-26 the window's Búsqueda tab let the person switch each level, and the
choice was stored under ``[search]`` in config.toml. The person asked for them to be always
on, keeping only the questions the chat asks (decided 2026-10-26: "lo demás siempre déjalo
activo"). A ``[search]`` table left in an older config.toml is ignored, so a web level
switched off before stays on now; ``settings.parse`` ignores unknown tables, so it never
breaks anything either.

Kept out of ``Settings`` on purpose. ``Settings.fingerprint()`` goes into a benchmark run
header, and only the app reads these levels (`cli._web_wanted`), so they stay out of it.
"""

from __future__ import annotations

#: key -> its value. ``navegador`` stays ``ask``: a page that needs the browser is asked
#: for in the chat (`setup.service.ComponentService.level_for`).
DEFAULTS: dict[str, str] = {"documentacion": "on", "web": "on", "navegador": "ask"}

#: Whether a web-search tool is registered anywhere in this build (`cli.py`'s tool
#: registry: `tools/web/`). `setup.service.ComponentService.search_levels` passes this to
#: the desktop, whose Búsqueda tab shows only while it is True (the "hide until real" rule).
AVAILABLE = True


def load() -> dict[str, str]:
    """Every level."""
    return dict(DEFAULTS)
