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
- Edits are rejected if they add `eval`, `open`, network or OS imports, dunder tricks, a `raise`, an unsafe YAML loader
  (`Loader`, `UnsafeLoader`, `FullLoader` and the matching `load` helpers, so the agent has to pick `SafeLoader` or decline), or a
  string that looks like a credential.
- It cannot read environment files, keys or credentials (`.env`, `*.pem`, `id_rsa`, `.ssh/`, `.aws/`, `.git/` and similar).
- Secret-like strings are replaced with `[REDACTED:...]` in everything sent to the model: file contents, test output, behavior
  results, the function source used to propose probe inputs, and the output of `--explore`. Three layers: patterns for common
  token shapes (cloud keys, tokens, private key blocks, passwords assigned in code, credentials in URLs), the exact values of
  secrets that are really there (environment variables named like KEY, TOKEN, SECRET or PASSWORD, and values in the repo's
  `.env` files), and an entropy rule for random-looking keys. A redacted placeholder cannot be written back over the real value.
- Deny by default: a new import must be a listed standard-library module, the upgraded library, or something the project already
  depends on, and a new builtin must be on a short safe list. A trick nobody thought of fails instead of passing.
- Release notes, test output and library source are wrapped as untrusted data, and instructions found in them are flagged.
- The model's claim that it fixed something is ignored. Code re-runs the tests and the behavior check.
- `--explore` (off by default) lets the agent run short snippets of its own code on the old and the new library. They run in the
  same Docker sandbox as your tests (no network, no capabilities, no view of your repo), and the option is unavailable with
  `--test-backend local`.

What it produces
- `fix.patch` is scanned for secret-like strings. If it has any, `PROOF.md` says so and `--open-pr` refuses to run.
- `--open-pr` only ever opens a draft pull request, and only does a dry run unless you add `--confirm`.
- `model_transcript.json` holds everything that was sent to the model in the run, after redaction, so you can check what left
  your machine before you share a run folder.
- `MANIFEST.json` holds the SHA-256 of `fix.patch`, `PROOF.md`, `result.json` and `model_transcript.json`, and `upkeep verify` fails if any was edited.
  You can sign it with an Ed25519 key. `PROOF.md` and `result.json` also record which model and provider produced the run.

## Escalation sends your code to a second model

`upkeep fix --escalate PROVIDER:MODEL` (for example `meta:muse-spark-1.3`) runs a second, stronger model if the first attempt is
declined. That model receives the same redacted code and the first attempt's reason, so with `--escalate` your code goes to two
providers. Without the flag it goes only to the one you configured. Every message to either model is in `model_transcript.json`,
and the same verifier, content check and sandbox judge the second model's patch.

## Where it stops

- Your code goes to a third party. Redaction cannot know a secret of an unfamiliar shape that is not in your environment or your
  `.env` files, and it will hide some harmless strings. Do not run `upkeep fix` on code you are not allowed to share.
- Imports and builtins are deny-by-default, but attribute access cannot be allow-listed, so that part is still a deny list, and I
  found 13 ways around an earlier version of it. The Docker sandbox is the real boundary, and Docker is a strong boundary but not
  a perfect one.
- A passing behavior check is evidence. It is not proof that a fix is correct, and `PROOF.md` lists what was not verified.
- Signing proves who wrote the files. It does not prove the fix is right.
- The checks on downloads trust PyPI's published hash. They catch corruption and tampering in transit, not a malicious
  release that PyPI itself serves.

## Reporting a problem

Open an issue on the GitHub repository. Please do not post secrets or exploit details in a public issue. Say that you
have something sensitive and I will find a private way to talk.
