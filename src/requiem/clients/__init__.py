"""Phase B per-tool clients — git-aware filesystem, gh, twig, etc.

Each module here owns one external tool (or one tightly-related family
of OS ops). Clients raise typed errors from their own hierarchy; the
verb layer converts those into outcomes per the contract in
`src/requiem/outcomes.py`.
"""
