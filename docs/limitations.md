# Limitations and unverified claims

**Not claimed:** safety against real power loss on real hardware. Guarantees hold under the simulator's
durability model (volatile write-back cache; atomic block writes except injected torn writes; no reordering
between flushes). See `docs/journaling-and-recovery.md`.

Engine limits
* Superblock is a single copy: a torn/corrupt superblock is *detected* (CRC) and mount fails, but not repaired.
* Maximum file size 48 KiB (12 direct blocks; indirect pointers unused). Directories ≤ 12 blocks.
* No hard links, symlinks, permissions enforcement, or concurrent writers (the UI serialises with a lock).
* Corruption of a committed journal record makes that transaction (and later ones) be discarded with a
  warning rather than repaired.
* Torn writes are injected only on request, not randomly. Disk reordering between flushes is not modelled.
* Metadata is journaled; file data is ordered-mode copy-on-write. A crash before the commit simply loses the
  new data (old content stays intact). Block leaks after recovery were not observed (fsck is clean in every
  crash test) but this is tested, not proved.

Test/tooling limits
* The legacy `test_filesystem.py` needs `pytest`; in the build sandbox it was run through a small shim.
* Browser tests need Playwright + Chromium and are skipped otherwise; only Chromium was tried.
* The write-through crash matrix is a different, weaker model; mid-flush points do not exist there.
* The event log lives in server memory (mirrored to a JSONL file) and is lost on restart; journal contents
  and file state are not.

UI limits
* Single user, one mounted image at a time, loopback only. Lab runs use temp images, never the mounted one.
* "Lifecycle stages" for a transaction are lit only from events seen by the running server; transactions
  from earlier sessions show the disk-derived state instead.
