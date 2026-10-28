"""Behavior-trap cases: a naive replacement passes the visible tests but changes behavior.

Authored 2026-10-27 AFTER the real Werkzeug result (green-but-wrong 3/3), to test the behavior check.
These are tuned-for cases: they exist to demonstrate the feature, not to measure generalization.
"""

from .evalcases import Case

CASES: list[Case] = [
    Case("b_quote_default", "removed helper; stdlib replacement has different defaults", "fix",
         {"lib/__init__.py": "from urllib.parse import quote\n\n\ndef url_quote(s):\n    return quote(s, safe='/:')\n"},
         {"lib/__init__.py": ""},
         {"app.py": "from lib import url_quote\n\n\ndef quote(s):\n    return url_quote(s)\n",
          "test_app.py": "import app\n\n\ndef test_simple():\n    assert app.quote('a b') == 'a%20b'\n"},
         {"test_hidden.py": "import app\n\n\ndef test_h():\n    assert app.quote('a b/c:d') == 'a%20b/c:d'\n"},
         {"app.py": "from urllib.parse import quote as _quote\n\n\ndef quote(s):\n    return _quote(s, safe='/:')\n"},
         "lib.url_quote was removed; use urllib.parse.quote instead."),
    Case("b_strip_slug", "removed helper; naive inline version forgets whitespace stripping", "fix",
         {"lib/__init__.py": "def slug(s):\n    return s.strip().lower().replace(' ', '-')\n"},
         {"lib/__init__.py": ""},
         {"app.py": "from lib import slug\n\n\ndef make(s):\n    return slug(s)\n",
          "test_app.py": "import app\n\n\ndef test_simple():\n    assert app.make('A B') == 'a-b'\n"},
         {"test_hidden.py": "import app\n\n\ndef test_h():\n    assert app.make('  A B ') == 'a-b' and app.make('X') == 'x'\n"},
         {"app.py": "def make(s):\n    return s.strip().lower().replace(' ', '-')\n"},
         "lib.slug was removed. Write the equivalent inline."),
]
