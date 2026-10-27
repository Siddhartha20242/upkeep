import pytest

from upkeep.patchlint import check_edit

BASE = "import lib\n\n\ndef tok(user):\n    return lib.legacy_token(user)\n"


@pytest.mark.parametrize("new", [
    "import lib\n\n\ndef tok(user):\n    return lib.fetch(user, timeout=5)\n",          # keyword-only fix
    "from lib.text import slugify\n\n\ndef tok(user):\n    return slugify(user)\n",     # moved import
    "import lib\nfrom markupsafe import Markup\n\n\ndef tok(u):\n    return Markup(u)\n",  # legit new third-party import
    "import lib\n\n\n@lib.pass_context\ndef tok(user):\n    return user\n",              # decorator rename
    "from urllib.parse import quote\n\n\ndef tok(user):\n    return quote(user, safe='/:')\n",   # pure string helper
    "from urllib import parse\n\n\ndef tok(user):\n    return parse.quote(user)\n",
    "import urllib.parse\n\n\ndef tok(user):\n    return urllib.parse.quote(user)\n",
])
def test_legitimate_migration_edits_pass(new):
    assert check_edit(BASE, new) == (True, "")


@pytest.mark.parametrize("new,expect", [
    ("import lib\n\n\ndef tok(user):\n    import os; raise AssertionError(os.listdir('.'))\n", "import os"),
    ("import lib\n\n\ndef tok(user):\n    raise Exception(lib.other.__code__.co_consts)\n", "attribute __code__"),
    ("import lib\n\n\ndef tok(user):\n    return eval('1')\n", "call eval()"),
    ("import lib\n\n\ndef tok(user):\n    return open('/etc/passwd').read()\n", "call open()"),
    ("import lib, subprocess\n\n\ndef tok(user):\n    return 1\n", "import subprocess"),
    ("import lib\n\n\ndef tok(user):\n    return getattr(lib, 'x')\n", "call getattr()"),
    ("import urllib.request\n\n\ndef tok(user):\n    return 1\n", "import urllib"),
    ("from urllib import request\n\n\ndef tok(user):\n    return 1\n", "import urllib"),
    ("from os import path\n\n\ndef tok(user):\n    return 1\n", "import os"),
])
def test_probing_and_gaming_edits_are_rejected(new, expect):
    ok, why = check_edit(BASE, new)
    assert not ok and expect in why


def test_unparsable_edit_rejected():
    ok, why = check_edit(BASE, "import lib\n\n\ndef tok(user:\n    return 1\n")
    assert not ok and "unparsable" in why


def test_preexisting_constructs_are_not_penalised():
    old = "import os\n\n\ndef f():\n    return os.getcwd()\n"
    assert check_edit(old, old.replace("getcwd()", "getcwd() + 'x'")) == (True, "")
