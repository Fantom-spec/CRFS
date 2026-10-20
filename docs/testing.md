# Testing

## Suites and commands

```bash
python -m unittest tests.test_engine_integrity -v    # 34 tests, ~3 s
python -m unittest tests.test_crash_recovery -v      # 9 tests (84 + many subtests), ~50 s
python -m unittest tests.test_webui -v               # 26 tests, ~15 s
python -m unittest tests.test_webui_e2e -v           # 7 browser tests; skipped if Playwright missing
python -m pytest tests/test_filesystem.py            # legacy 56 tests (needs pytest)
python -m unittest discover -s tests -t .            # everything except the pytest-only legacy file
```

Recorded results for this revision (Python 3.13, Linux sandbox):

| Suite | Result |
|---|---|
| `test_engine_integrity` | 34 passed |
| `test_crash_recovery` | 9 passed |
| `test_webui` | 26 passed |
| `test_webui_e2e` (Chromium via Playwright) | 7 passed |
| `test_filesystem` (legacy) | 56 passed — run through a small local pytest shim because `pytest` could not be installed in the sandbox. Re-run with real pytest on your machine. |

`unittest discover` reports one import error for `test_filesystem.py` **only when pytest is absent**.

## What each suite proves

* **test_engine_integrity** – staging semantics (no metadata write outside a transaction, abort leaves nothing,
  flush ordering), recovery edge cases (uncommitted never replayed, replay once, repeat is a no-op, crash during
  recovery, corrupt data/commit records, missing records detected via commit count, stale lap records ignored,
  records pointing at the journal region rejected, garbage images), resource exhaustion (disk full, inodes
  full, file too large, journal full), fsck detection of seeded damage, namespace edge cases, repeated remounts.
* **test_crash_recovery** – each test starts **separate OS processes**: `setup` formats an image, `crash`
  runs an operation and is `SIGKILL`ed at a named point, `inspect` looks at the crashed image read-only,
  `verify` mounts, recovers, compares to the expected state and runs fsck, then recovers again to check
  idempotence. Covered: 14 scenarios × 6 operations (volatile), boundary scenarios on write-through,
  mid-flush sweeps (journal flush and home apply cut after every k-th block), torn commit records at 8 prefix
  lengths, an 8-block ring with the crash at every ring position (wraparound), lazy checkpointing with
  several pending transactions.
* **test_webui** – HTTP integration against the real engine: UI-created files exist in the image when mounted by an
  independent engine instance, metadata agrees, restart keeps state, CLI and UI share an image, journal
  events follow the real lifecycle order and match the journal, lab kills a real process (return code −9)
  and validates state, lab doesn't touch the mounted image, checker reports real corruption, destructive calls
  need exact confirmation, token / Host / path / loopback checks.
* **test_webui_e2e** – real Chromium: create image → create file in the UI → verify bytes in the image;
  typed confirmation gating; error toast; lab run shows PASS and SIGKILL; checker and event-log screens.

## The nine required crash boundaries

| Brief item | Scenario(s) |
|---|---|
| before journal write | `crash_before_journal_write` |
| during journal write | `crash_during_journal_write` (+ sweep over every k) |
| after journal flush, before commit | `crash_after_journal_flush_before_commit` (and `..._write_before_flush`) |
| during commit write | `crash_during_commit_write` (+ torn prefixes) |
| after commit, before home apply | `crash_after_commit_flush_before_apply` (and `..._write_before_flush`) |
| during home apply | `crash_during_home_apply` (+ sweep) |
| after home flush, before checkpoint | `crash_after_home_flush_before_checkpoint` |
| during checkpoint | `crash_during_checkpoint` |
| reclamation / wraparound | `TestWraparound` (8-block ring, every position), `TestLazyCheckpoint`, `crash_after_checkpoint` |

Plus `crash_during_recovery_scan` and `crash_during_recovery_replay`.

## Expected-state rules

A scenario's expected outcome is fixed in the scenario table *before* the run: operations whose commit record
was not durable must be absent after recovery; operations whose commit flush completed must be present.
A run passes only if the observed listing/content equals the expectation, the baseline file is intact, fsck
is clean, recovery succeeded and a second recovery is a no-op. No test was disabled or weakened to pass;
the write-through "mid-flush" cases are skipped because that point does not exist in that model.

## Not covered

See `docs/limitations.md` (e.g. real power loss, multi-threaded writers, torn superblock).
