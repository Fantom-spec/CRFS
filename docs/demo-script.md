# Demo script (about 6 minutes)

Setup (before the audience): `python -m webui --image-dir /tmp/demo --open`. Press **New image…**, name `demo.img`, 8 MiB.

**0:00 – Why journaling (30 s).** Open *System overview*. "A multi-block metadata update can be cut off by a
crash. We write the change to a log first, make the log durable, commit, then apply it. Recovery replays
committed transactions."

**0:30 – Normal operation (90 s).** *File browser* → New folder `docs`; New file `docs/a.txt` with text.
Switch to *Journal & transactions*: select the newest tx. Point out the records (target block, offset,
length), the lit lifecycle stages and the txid. Open *Disk explorer*: superblock block 0 shows
`checkpoint_txid` and `journal_tail`; click the inode of `a.txt` to show its block pointer.

**2:00 – The crash (120 s).** *Crash lab*. Pick **7 · AFTER_COMMIT_FLUSH**, operation *create file*, Run.
Narrate the steps: a separate process is SIGKILLed (return code −9); the crashed image is inspected
*before* recovery (the new file is not visible yet); a fresh process mounts, replays tx and verifies.
Show Expected = Actual, checks all green, "replayed 1". Then pick **4 · AFTER_JOURNAL_FLUSH** and run: now the
file must be **absent** — journal complete but never committed, so it is discarded.

**4:00 – Prove it is safe to repeat (45 s).** Run **14 · crash during recovery replay**: recovery itself is
killed and the next recovery still reaches the same state (idempotent).

**Optional live proof on your own file (60 s).** *File browser* → select `docs/a.txt` → **Crash demo…** → overwrite
with new text → confirm. The red "CRASHED" bar appears and the file still shows the old text (the update is only
in the journal; see *Journal* → `committed-pending-replay`). Click **Run recovery**: the new text appears.

**4:45 – Consistency (45 s).** *Consistency checker* → Run: PASSED. Optional: explain that the test-suite
seeds real corruption and fsck reports it with a reason.

**5:30 – Honesty slide (30 s).** *Event log* shows every step with timestamps/txids. State the limits: simulated
disk with a modelled volatile cache, not real power-loss safety (`docs/limitations.md`).

Likely viva questions: why flush before commit? (so a durable commit always has durable log records); why is
replay idempotent? (after-images are absolute); what if the commit record is torn? (CRC fails → discarded);
why copy-on-write data? (old file stays valid until the commit).
