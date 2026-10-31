# Security

This is what Upkeep protects against, how, and where it stops. It is a hackathon project that one person wrote and tested,
so read the last section before you point it at code you care about.

## What a run does

`upkeep audit` and `upkeep check` download public releases from PyPI and read them with Python's `ast` module. They never import
or execute the library, and they send nothing anywhere. They read your repo locally.

`upkeep fix` does more. It runs your tests (and a few probe calls) against the old and new library, inside Docker. It also sends
snippets of your code and your test output to a model provider (Baseten for Muse Spark Open by default, the Meta API if you
choose it).

## What it does to protect you

Downloads
- Every PyPI release is checked against the SHA-256 that PyPI publishes for it. A mismatch, or a release with no published hash,
  is refused before anything is unpacked.
- Archives are unpacked with path checks (zip) and Python's `data` filter (tar), so a hostile archive cannot write outside
  the cache. There is a size limit.

The dependency install (the only step that uses the network)
- Every requirement line from your repo is checked against a narrow pattern before it reaches `pip`. Options, URLs, local
  paths and environment markers are dropped, so a repo cannot slip `--index-url` or a download link into the install.
- Wheels only by default, so no package's build script runs. Building from source needs `--allow-source-builds`.

Test runs
- Docker with no network, all Linux capabilities dropped, `no-new-privileges`, a memory and process limit, a non-root user and
  a read-only dependency folder. None of your environment variables are passed in.
- `--test-backend local --unsandboxed` runs your tests on your own machine with no isolation. It exists for code you trust and
  needs the explicit flag.

The model
- It can only edit the lines the radar flagged. Code enforces this, not the prompt.
- Edits are rejected if they add `eval`, `open`, network or OS imports, dunder tricks, a `raise`, an unsafe YAML loader, or a
  string that looks like a credential.
- It cannot read environment files, keys or credentials (`.env`, `*.pem`, `id_rsa`, `.ssh/`, `.aws/`, `.git/` and similar).
- Secret-like strings (cloud keys, tokens, private key blocks, passwords assigned in code, credentials in URLs) are replaced
  with `[REDACTED:...]` in everything sent to the model: file contents, test output, behavior results and the function source
  used to propose probe inputs. A redacted placeholder cannot be written back over the real value.
- Release notes, test output and library source are wrapped as untrusted data, and instructions found in them are flagged.
- The model's claim that it fixed something is ignored. Code re-runs the tests and the behavior check.

What it produces
- `fix.patch` is scanned for secret-like strings. If it has any, `PROOF.md` says so and `--open-pr` refuses to run.
- `--open-pr` only ever opens a draft pull request, and only does a dry run unless you add `--confirm`.
- `MANIFEST.json` holds the SHA-256 of `fix.patch`, `PROOF.md` and `result.json`, and `upkeep verify` fails if any was edited.
  You can sign it with an Ed25519 key. `PROOF.md` and `result.json` also record which model and provider produced the run.

## Where it stops

- Your code goes to a third party. Redaction is a list of patterns. It will miss some secrets and hide some harmless strings.
  Do not run `upkeep fix` on code you are not allowed to share.
- The content check is a list of things to deny, and I found 13 ways around an earlier version of it. The Docker sandbox is the
  real boundary, and Docker is a strong boundary but not a perfect one.
- A passing behavior check is evidence. It is not proof that a fix is correct, and `PROOF.md` lists what was not verified.
- Signing proves who wrote the files. It does not prove the fix is right.
- The checks on downloads trust PyPI's published hash. They catch corruption and tampering in transit, not a malicious
  release that PyPI itself serves.

## Reporting a problem

Open an issue on the GitHub repository. Please do not post secrets or exploit details in a public issue. Say that you
have something sensitive and I will find a private way to talk.
