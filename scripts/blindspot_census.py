"""How big is the instance-method blind spot, really? Count real breaking API changes by kind.

For real library upgrades, diff the public API INCLUDING class methods and report how many breaking changes are on
module-level functions, class constructors, or methods. Methods of removed classes are not double counted.
Run:  PYTHONPATH=. python scripts/blindspot_census.py
"""
import sys
from collections import Counter

from upkeep.apidiff import diff_api, extract_api
from upkeep.pypi import sources_for

PAIRS = [  # (PyPI name, import name, old, new): real upgrades chosen BEFORE looking at the method share
    ("PyYAML", "yaml", "5.4.1", "6.0.2"), ("Jinja2", "jinja2", "3.0.3", "3.1.4"), ("Werkzeug", "werkzeug", "2.3.8", "3.0.3"),
    ("Flask", "flask", "2.2.5", "2.3.3"), ("click", "click", "7.1.2", "8.0.4"), ("urllib3", "urllib3", "1.26.18", "2.0.7"),
    ("redis", "redis", "3.5.3", "4.0.0"), ("jsonschema", "jsonschema", "3.2.0", "4.0.0"), ("Markdown", "markdown", "2.6.11", "3.0"),
    ("requests", "requests", "2.25.1", "2.31.0"), ("attrs", "attr", "21.4.0", "23.1.0"), ("docutils", "docutils", "0.17.1", "0.20.1"),
]


def main() -> int:
    total, rows = Counter(), []
    for dist, mod, old, new in PAIRS:
        try:
            o, n, m = sources_for(dist, old, new, mod)
            oa, na = extract_api(o, m, include_methods=True), extract_api(n, m, include_methods=True)
        except Exception as e:  # keep going
            print(f"{dist} {old}->{new}: skipped ({type(e).__name__}: {str(e)[:60]})", flush=True)
            continue
        changes = diff_api(oa, na)
        kinds, seen_methods, seen_defs = Counter(), set(), set()
        for c in changes:
            sig = oa.get(c.symbol)
            if sig is None:
                continue
            if sig.is_method:
                parent = c.symbol.rsplit(".", 1)[0]
                if parent not in na:  # the whole class vanished: counted once, as a class
                    continue
                # an inherited method that changed shows up once per subclass: count DISTINCT changes only
                key = (c.symbol.rsplit(".", 1)[1], c.kind, c.param)
                if key in seen_methods:
                    continue
                seen_methods.add(key)
                kinds["method"] += 1
            else:
                # a re-exported name appears under several keys, and one changed base constructor under every subclass:
                # count each underlying definition and change once
                key = (sig.ctor_from or sig.qualname, c.kind, c.param)
                if key in seen_defs:
                    continue
                seen_defs.add(key)
                kinds["class" if sig.is_class else "function"] += 1
        total += kinds
        rows.append((dist, old, new, kinds))
        print(f"{dist:11} {old:>8} -> {new:<8} functions={kinds['function']:3} classes={kinds['class']:3} methods={kinds['method']:3}", flush=True)
    t = sum(total.values()) or 1
    print(f"\nALL {len(rows)} upgrades: {sum(total.values())} breaking changes: " +
          ", ".join(f"{k}={total[k]} ({100 * total[k] / t:.0f}%)" for k in ("function", "class", "method")))
    print("per-upgrade method share: " + ", ".join(f"{d}={100 * k['method'] / max(1, sum(k.values())):.0f}%" for d, _, _, k in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
