# Try Upkeep on your own project (5 minutes, no API keys)

Upkeep tells you **which lines of your code a dependency upgrade would break**, before you upgrade.
The command below needs no account and no keys.

## 1. Install (once)

```bash
git clone https://github.com/Siddhartha20242/upkeep.git && cd upkeep
python3 -m venv .venv && .venv/bin/pip install -e .
```

## 2. Run it on your repo (one command)

If your project has a `requirements.txt` with exact pins (`name==version`), run:

```bash
.venv/bin/upkeep audit --repo /path/to/your/project
```

For every pinned library it fetches the newest release from PyPI and lists the lines of **your** code that upgrade would
break. It also lists the libraries it could **not** check and why (no source archive, too big, package folder not found).
Skipped libraries are never counted as safe. Add `--only pyyaml jinja2` to limit it.

How to read the output:
- **GREEN:** no breaking change touches code in your repo (that Upkeep can see; see the limits below).
- **RED:** some breaking change hits your code. Each hit shows `file:line` and what changed.
- Exit code: 0 green, 1 amber, 2 red, 3 nothing to audit. That makes it usable in a CI script.

## 3. Or check one library and two versions you choose

```bash
.venv/bin/upkeep check --repo /path/to/your/project --package PyYAML --from 5.4.1 --to 6.0.2 --module yaml
```

`--module` is only needed when the import name differs from the PyPI name (PyYAML is imported as `yaml`).
Examples that really broke code: `PyYAML 5.4.1 -> 6.0.2`, `Jinja2 3.0.3 -> 3.1.4`, `Werkzeug 2.3.8 -> 3.0.3`.

## What leaves your machine

- `upkeep audit` and `upkeep check` **only download** the two public releases from PyPI. It reads your repo **locally** and uploads nothing.
- `upkeep fix` (needs your own model key) sends **code snippets around the affected lines** and test output to the model
  provider. Do not use it on code you are not allowed to share.
- It never modifies your repo. It never runs your code unless you pass `--test-backend local --unsandboxed`.

## Limits you should know

- Python only. It sees signature-level breaks (renamed, removed or reordered parameters, removed functions).
  A change that keeps the same signature but behaves differently is **invisible** to it. So is a parameter that
  stops accepting a value it used to accept (for example Werkzeug 3 rejecting `method="sha256"`).
- Calls on objects (`obj.method()`) are not resolved.

## Five questions (please answer honestly, a quick "no" is useful)

1. Did it find a real breakage in your project? If it flagged lines, were they truly affected?
2. Did it show any false alarm (a flagged line that would have been fine)?
3. Did it miss something you already know broke when you upgraded?
4. Was anything confusing in the output or the steps?
5. Would you run it before an upgrade? Why or why not?

Send the answers (and which package and versions you tried) to the person who gave you this link.
