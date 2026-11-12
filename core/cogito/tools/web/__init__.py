"""`web_search` and `web_fetch`: the model's way to look something up on the web.

Read-only and keyless. `urls` holds the rules that decide what may be read, `http` the
client that obeys them, `sources` the search chain, and `fetch` and `search` the two tools.
Always registered in the desktop app and, in `cogito chat`, only
with `[web] in_chat` (`config/settings.py`).
"""
