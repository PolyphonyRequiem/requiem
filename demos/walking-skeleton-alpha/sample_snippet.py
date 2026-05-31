def lookup_or_compute(x, cache={}):
    """Tiny intentionally-flawed snippet for the walking-skeleton demo."""
    if x in cache.keys():
        return cache[x]
    value = int(x) * 2
    cache[x] = value
    return value
