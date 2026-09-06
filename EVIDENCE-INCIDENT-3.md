# Evidence bank — Incident 3: watchdog force-kill cascade strands the shared detection queue (core layer, not the plugin)

**Status:** published 2026-09-05.
**Captured:** 2026-08-31, live, by a scripted read-only capture tool (24 files
per capture: stats snapshots 10 s apart, container logs, py-spy stacks of every
Frigate process, /proc state, zmq socket byte counters, guard logs). Four
captures were taken that day; excerpts below cite their capture, and every
figure traces to a capture file.
**Why this file exists:** Incidents 1 and 2
([EVIDENCE.md](https://github.com/tempered-steel/Frigate_zmq_silent-detection_failure/blob/main/EVIDENCE.md),
[EVIDENCE-INCIDENT-2.md](https://github.com/tempered-steel/Frigate_zmq_silent-detection_failure/blob/main/EVIDENCE-INCIDENT-2.md)
— sibling repo, same plant, same investigation) are failures *inside the
`zmq_ipc` detector plugin* —
a silent zero-detection state the plugin enters and never leaves. Incident 3 is
a different animal in a different layer: **Frigate core's detector watchdog
force-kills its way into a stranded `multiprocessing.Queue` lock, after which
no replacement detector can ever run.** The plugin is a bystander: any detector
type whose stop request goes unobserved for 30 s can be killed into this state.
Upstream has since merged a targeted fix for the main entry path
(blakeblackshear/frigate PR #24146, merged 2026-09-02, branch 0.19); this file
is the field evidence from a production plant, captured before that PR existed.

**Sanitization note:** `<NVR>` = the Frigate Docker host (LAN address),
`<SPARK>` = the NVIDIA DGX Spark (routed VLAN address), `<ROUTER>` = the site
gateway. All times Zulu. (Same conventions as the sibling repo.)

---

## 1. Environment

Same plant as Incidents 1–2 (sibling repo `EVIDENCE.md` §1): Frigate `0.17.2-3d4dd3a` in
Docker, `zmq` detector, `endpoint: tcp://<SPARK>:5590`, house-built ONNX
detector server on the DGX Spark, model `yolov9t-uint8.onnx`. Healthy reference
inference_speed 10–16 ms across that day's captures.

**Material disclosure:** at the time of the wedge the plugin file was our
**patched v1.2** (sha `04866933…`), whose in-call recovery path holds a detect
call open for up to 30 s while the peer is unreachable. That is what armed the
core watchdog here. The pristine plugin reaches >10 s detect calls through its
own paths (e.g. model transfer against a slow or absent peer), so the cascade
below is not patch-specific — but the trigger timing in this incident is shaped
by our v1.2. Our v1.3 (in `fixes/` here: the full plugin file and the
upstream-to-v1.3 diff) bounds in-call recovery to 2 s specifically so the
watchdog never arms;
§5 shows the same trigger replayed under v1.3 with no watchdog activity at all.

## 2. Event index (all 2026-08-31)

| # | Time (Z) | Event | Capture |
|---|---|---|---|
| E1 | 01:30:24 | `<ROUTER>` restarted (operator OS upgrade). V1 plant wedged silently ~11 h. First plugin-level symptom in the log: `01:31:15 Failed to check and transfer model: Resource temporarily unavailable`. | none mid-wedge — cured by operator container restart *before* capture, ~12:22:43Z per the operator's session log (not a capture file; corroborated by container start 12:22:50Z in the `0839`/`0910` capture metas — the later captures' metas show the 14:43:36Z v1.3 restart instead) |
| E0 | 13:39 | Healthy baseline capture, v1.2. | `20260831-0839` |
| E2 | 14:01 | Controlled `<ROUTER>` reboot #1, v1.2. **Wedge reproduced; the cascade below.** Cured by operator container restart ~14:22:30Z (operator session log; corroborated by the 14:22:42Z model-ready line in the `1004` capture's 24 h plugin log); a synthetic-event probe passed ~28 s after start (session log, not captured). | `20260831-0910` |
| — | 14:43:36 | v1.3 deployed (container restart; sha verified in-container). | meta |
| E3 | 14:50 | Controlled `<ROUTER>` reboot #2, v1.3. **Self-recovered 14:52:25Z. No watchdog activity.** | `20260831-1004` |
| E4 | 19:45 | Steady-state capture, v1.3 healthy. | `20260831-1445` |

## 3. The cascade (E2, from the Frigate log)

Two watchdog condemnation cycles, ten seconds apart at onset, each ending in a
force-kill after the 30 s graceful-stop window expires:

```
14:02:35 frigate.watchdog  INFO : Detection appears to be stuck. Restarting detection process...
14:02:35 root              INFO : Waiting for detection process to exit gracefully...
14:03:05 root              INFO : Detection process didn't exit. Force killing...
14:03:05 root              INFO : Detection process has exited...
14:03:15 frigate.watchdog  INFO : Detection appears to be stuck. Restarting detection process...
14:03:15 root              INFO : Waiting for detection process to exit gracefully...
14:03:45 root              INFO : Detection process didn't exit. Force killing...
14:03:45 root              INFO : Detection process has exited...
```

(Log lines excerpted: dates and fractional seconds stripped, columns
condensed; content otherwise as logged.)

Source verification (2026-09-05, tag v0.17.2) supplies the lifecycle facts the
cascade turns on: the watchdog condemns on `detection_start > 0` and age > 10 s
(`frigate/watchdog.py:26-33`); `start_or_restart()` zeroes that marker as its
first act (`frigate/object_detection/base.py:347`); but `stop()`
(`base.py:333-344`) then just `join(timeout=30)`s **without signaling the
condemned process in any way** — no stop event is set, and the runner's loop
condition (`while not self.stop_event.is_set()`, `base.py:149`) never learns it
is condemned. The marker is armed when a detect begins (`base.py:169`) and
reset only when that detect *completes* (`base.py:179`); the runner idles in
`Queue.get(timeout=1)` (`base.py:151`), which holds the queue's shared reader
lock through the wait portion of each one-second cycle. This is precisely the
lifecycle hole upstream PR #24142 described.

The plugin's own log interleaves with the cascade and pins down what each
victim was doing (`02-frigate-log-zmq-24h.txt`, excerpted):

```
14:02:18  Initializing model: yolov9t-uint8.onnx          <- gen-0, in-call recovery, stalls
14:02:48  Failed to check and transfer model: Resource temporarily unavailable
14:02:48  Failed to initialize model / Initializing model  <- gen-0 completes one call, begins another
          (no completion before the 14:03:05 kill)
14:03:05  Initializing model                               <- replacement's startup init
14:03:32  Model yolov9t-uint8.onnx is ready                <- replacement RECOVERED
14:03:45  Initializing model / Model is ready (4.6 ms)     <- final generation: instant init,
                                                              then never one inference
```

Two things to note. The 14:02:48 failure/re-init pair is gen-0 — never
signaled by its 14:02:35 condemnation — completing one detect call mid-grace
and beginning another (which re-arms `detection_start` at ~14:02:48; killed
mid-detect at 14:03:05, the reset at `base.py:179` never runs, and the 27 s-old
marker is what condemns the *next* generation at 14:03:15). And the final
generation's model init completing in 4.6 ms at 14:03:45 shows the network was
fully healthy by then — what it starves on afterward is not the peer but the
queue lock.

After 14:03:45 there are **no further watchdog lines**: the final replacement
is never condemned — and never runs — for the remaining ~19 minutes until the
operator restart. That is the silent part.

## 4. Post-state: the stranded lock (capture `20260831-0910`, ~7 min into the wedge)

**py-spy, the surviving detector process** (`05-pyspy-frigate_detector_spark.txt`):

```
Process <pid>: frigate.detector:spark
Python v3.11.2 (/usr/bin/python3.11)

Thread <tid> (active): "process:frigate.detector:spark"
    get (multiprocessing/queues.py:108)
    run (base.py:151)
    _bootstrap (multiprocessing/process.py:314)
    _main (multiprocessing/spawn.py:133)
    _serve_one (multiprocessing/forkserver.py:313)
    main (multiprocessing/forkserver.py:274)
    <module> (<string>:1)
```

(Full stack as captured, pid/tid redacted; the forkserver frames also document
the start method in use.)

In the deployed CPython 3.11.2, `multiprocessing/queues.py:108` is:

```python
if not self._rlock.acquire(block, timeout):
```

— the timed acquire of the queue's shared **reader lock**. The process spends
its life failing to acquire a lock whose last holder was SIGKILLed;
`multiprocessing` locks are plain semaphores with no owner-death recovery.

**Kernel view** (`04-procs.txt`): the detector process sits in
`futex_do_wait` — the same fact one layer down.

**Stats** (`01-stats-summary.txt`, two snapshots taken 10 s apart; the
service-uptime fields differ by 15 s because Frigate emits stats on its own
cadence — the payloads are distinct emissions, not one cached read):

- `inference_speed` frozen at a garbage sentinel (`4476105068.97`), unchanged
  across the interval; `detection_start: 0.0`.
- `detection_fps` sum **0.0** while `skipped_fps` sum 35.9 and per-camera
  `camera_fps` healthy (~5–6 fps on 8 of the 11 detect-enabled cameras; the
  other three read 0.0 in every capture that day, healthy ones included —
  offline, not victims) — cameras fine, zero inference.
- zmq socket `<NVR>` → `<SPARK>:5590`: **0 bytes in either direction over
  10 s** (`07-zmq-bytes-A/B.txt`, `ss -ti`); the socket is ESTABlished and
  idle — beyond the few-hundred-byte startup handshake it never sends a single
  inference request.

Discriminator worth reusing: *healthy-looking cameras + `skipped_fps` > 0 +
`detection_fps` = 0 + a 0-byte 10 s delta on the detector socket* = this wedge,
distinguishable from a plugin-level wedge (Incidents 1–2) where the detector
process still cycles.

**The detector server side** (`08-spark.txt`): the REP server on `<SPARK>` had
been up since 07-25 with 0 restarts and answered every handshake sent to it —
nothing was wrong at the far end.

## 5. Counter-test: same trigger, bounded in-call recovery (E3, v1.3)

`<ROUTER>` rebooted again at 14:50Z with v1.3's 2 s in-call recovery budget in
place of v1.2's 30 s. The plugin fails fast, warns honestly, and retries; the
watchdog never sees a >10 s call; no process is condemned (detector pid
unchanged across the event); the model is ready 96 s after the first logged
failure (14:50:49 → 14:52:25):

```
14:50:47 zmq_ipc INFO  : Initializing model: yolov9t-uint8.onnx (in-call recovery, budget 2000 ms)
14:50:49 zmq_ipc ERROR : Failed to check and transfer model: Resource temporarily unavailable
14:50:49 zmq_ipc ERROR : Failed to initialize model yolov9t-uint8.onnx
14:50:49 zmq_ipc WARN  : Model not ready, returning zero detections
14:50:56 zmq_ipc INFO  : Initializing model: yolov9t-uint8.onnx (in-call recovery, budget 2000 ms)
[...two more bounded attempts, 14:50:58 and 14:51:06-14:51:08, same shape...]
14:52:25 zmq_ipc INFO  : Initializing model: yolov9t-uint8.onnx (in-call recovery, budget 2000 ms)
14:52:25 zmq_ipc INFO  : Model yolov9t-uint8.onnx is ready
```

(The quiet 77 s between the last failed attempt and the successful one is not
backoff: recovery attempts are made in-call, and with the camera streams
themselves down through the router reboot there were no frames — hence no
detect calls — until ~14:52:25.)

Roughly 12 minutes later (`20260831-1004` stats): inference 15.44 ms,
detection_fps sum 82.0, 232 MB sent on the detector socket over 10 s. Same
plant, same trigger class, no cascade — because the watchdog was never armed.

## 6. Observed vs inferred — stated plainly

**Observed:** the two condemnation/force-kill cycles (log); the second
condemnation 10 s after a fresh start (log); the final replacement pinned on
`queues.py:108` / `_rlock.acquire` / `futex_do_wait` (py-spy + /proc); zero
detector traffic and frozen stats thereafter (stats + ss); no further watchdog
action for ~19 min (log); full recovery only via container restart.

**Inferred (one step):** that a force-killed generation died *holding* the
queue's reader lock, stranding it. No sampler was attached during the 70 s
cascade itself, so the lock holder at each kill instant was not directly
observed. The inference is forced by the post-state — a semaphore nobody holds
cannot block `acquire` indefinitely.

**The inferred step, independently supported (added 2026-09-05):**

1. *Mechanism demonstration:* `tools/repro-queue-strand.py` reduces the strand
   to its essentials — one `multiprocessing.Queue`, a reader SIGKILLed while
   blocked in `get()`, then: `qsize() > 0`, yet every later reader (two fresh
   processes and the parent) receives only `Empty`, while a control victim
   killed *outside* `get()` leaves the queue fully readable. 6/6 checks pass;
   banked run: `tools/repro-queue-strand-output-20260905.txt`.
2. *Which kill:* the plugin log (§3) says kill #1's victim was mid-detect —
   its 14:02:48 model re-init never completed, so it died inside a zmq recv,
   holding no queue lock. Kill #2's victim, by contrast, had finished its
   startup init at 14:03:32 ("Model ready") and then had nothing to read: the
   camera ffmpeg processes had crashed at 14:02:27 (`02-frigate-log-45m.txt`),
   so the queue was drained and the recovered detector was idling in
   `get(timeout=1)` — holding the reader lock through the wait portion of each
   one-second cycle — when the 14:03:45 kill landed. For an idle-in-`get`
   victim the lock window covers most of each second, so the strand is the
   likely outcome of that kill, not bad luck. (This likelihood argument applies
   to the idle victim of kill #2 only — kill #1's mid-detect victim was outside
   the lock window essentially always.)

The reconstruction, labeled as such: kill #1 hit gen-0 mid-detect (harmless to
the lock, but its detect — begun at ~14:02:48 after an unsignaled mid-grace
completion — left `detection_start` armed, which is what condemned the next
generation at 14:03:15); kill #2 hit a *fully recovered, healthy* replacement
idling in `get()` and stranded the reader lock; the final generation then
spins on the dead semaphore with `detection_start` at 0.0 forever — which is
why the watchdog itself goes permanently silent. Not observation — but every
load-bearing element is a verified log line, verified source, or demonstrated
mechanism. Note what this implies: **the detector that poisoned the plant was
killed while healthy.**

## 7. Relation to upstream

- PR #24142 (2026-08-31, closed unmerged — template bot) and its successor
  **PR #24146 (merged 2026-09-02 into the 0.19 branch)** address the entry
  path: a detector whose marker shows recovery is no longer condemned. Our E2
  capture is independent, same-day field evidence for that failure mode, from a
  different trigger (site router restart) than the reporter's.
- The residual this capture leaves open: any path that still reaches the
  force-kill of a process holding the queue's reader lock re-creates the
  stranded state, and neither graceful stop (unobservable from inside a blocked
  call) nor SIGKILL (releases nothing) helps. Two directions would close the
  class, and neither is small: signaling the condemned generation needs
  per-generation stop events (the broader #24142 design, which upstream
  declined as too invasive), and discarding/recreating the queue after a
  force-kill is complicated by the fact that every running camera process holds
  a reference to the old queue (`base.py:389,418`) — a bare swap inside
  `ObjectDetectProcess` would leave cameras feeding a queue nobody reads. We
  flag the directions and the constraint; the design trade-off is upstream's.
- No fix exists in any 0.17/0.18 release (v0.18.0-rc1 predates the merge;
  verified 2026-09-05 against the upstream repo).

## 8. Local fixture manifest

Each capture is 24 read-only files. This document quotes the load-bearing
excerpts; the underlying fixtures are retained locally in full and specific
files can be shared on request after re-redaction (raw fixtures carry site
addressing and host identifiers that this document's sanitization removes).
Capture directory names use site-local time; every timestamp in this document
is Zulu:

- `20260831-0839` dry-run baseline (healthy, v1.2)
- `20260831-0910` **the wedge** (E2; py-spy, procs, stats, sockets, logs)
- `20260831-1004` v1.3 counter-test (E3)
- `20260831-1445` v1.3 steady state (E4)

Published alongside this document:

- `tools/repro-queue-strand.py` — the sandbox mechanism demonstration (§6)
- `tools/repro-queue-strand-output-20260905.txt` — banked 6/6 runs (fork,
  forkserver, and spawn start methods)
