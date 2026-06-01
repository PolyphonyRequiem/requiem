"""Per-tool typed clients that live behind the Toolbelt.

Phase A shipped `git` and `files` inline in `toolbelt.py`. Phase B grows the
fleet (`gh`, `twig`, ...) — each tool gets its own module under this package
so version-coupling stays per-tool. The Toolbelt remains the single object
verbs receive.
"""
