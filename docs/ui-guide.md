# UI guide

Start: `python -m webui --image-dir crfs_images --open` → http://127.0.0.1:8765/ (loopback only).
Images live in the image directory; the toolbar creates, opens and selects them. The same `.img` opens
with `python -m src.cli crfs_images/demo.img`.

| Screen | What it shows (all from the live backend) | Actions |
|---|---|---|
| **System overview** | blocks/inodes used, journal ring usage and head/tail, checkpoint txid, last recovery, last fsck, cache model, I/O counters (marked "this mount") | Force checkpoint |
| **Disk explorer** | region map with block ranges and byte offsets, one cell per block (allocated vs free in the data region), decoded superblock, inode table, hex of any block | click regions, blocks, inodes |
| **File browser** | tree, directory listing, stat, content read back from disk | New file / folder, Edit, Append, Rename, Delete (typed `DELETE`), rmdir (typed `RMDIR`). Each reports its txid. |
| **Journal & transactions** | ring occupancy, head/tail, every transaction found on disk with records and CRC status; lifecycle PREPARE→JOURNAL WRITE→FLUSH→COMMIT→APPLY→CHECKPOINT lit only for stages the engine emitted | select a transaction |
| **Crash lab** | 14 scenarios, 6 operations, disk model; step list from the real worker processes (shows SIGKILL return code −9); **Expected** (fixed before the run) vs **Actual** (read from the recovered image); checks; recovery report; event trace | Run scenario (confirmation dialog). Runs on a temp image; your mounted image is untouched |
| **Consistency checker** | pass/fail, findings with code and reason, what was examined | Run again |
| **Event log** | real events with time, severity, type, txid | filter by type, severity, txid; Clear |

**Crash demo on your own file (File browser → red "Crash demo…" button).** Pick *overwrite an existing file* or
*create a new file with content*, type the path and text, then type `CRASH THIS IMAGE`. A separate process
performs the operation and is SIGKILLed right after the commit is durable. The image is reopened **read-only
without recovery**, a red bar says "CRASHED · recovery has NOT run", and you can open the file:
* overwrite → the file still shows the OLD text;
* create → the file exists but is EMPTY;
* *Journal & transactions* shows the last transaction as `committed-pending-replay`.
Press **Run recovery** in the red bar: the file now shows the NEW text and the journal entry becomes `checkpointed`.
This runs on the image you have mounted (it stays consistent), so use a throw-away image for demos.

Notes
* Errors from the engine appear as red toasts with the engine's message.
* Format/delete of an image require typing the exact phrase (API: `FORMAT <name>`, `DELETE <name>`).
* Long operations (crash lab) run in the background; the page stays responsive.
* Values marked *configuration* or *this mount* are settings/counters, not on-disk facts.
