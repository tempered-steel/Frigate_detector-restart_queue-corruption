# Frigate detector-restart queue corruption — field evidence, minimal repro, and mitigation

**What this is:** the evidence bank for a **bug** in Frigate core's detector
watchdog/restart path, captured live on a production plant on 2026-08-31 and
reduced to a standalone reproduction. It is not a fork and not a feature; the
one fix included (`fixes/`) is a *trigger-avoidance* mitigation for the
detector plugin we run, not a fix for the core bug itself.

**The bug in one paragraph:** when the watchdog condemns a "stuck" detector,
`ObjectDetectProcess.stop()` waits 30 s **without signaling the condemned
process** and then SIGKILLs it. A detector whose stalled call returns during
that unsignaled window resumes its loop and re-enters
`Queue.get(timeout=1)` — which holds the shared request queue's reader lock
through most of every idle second. A SIGKILL landing there strands the lock
(`multiprocessing` locks are plain semaphores, no owner-death recovery), and
every replacement detector then spins forever on `_rlock.acquire`, running
zero inferences while all processes look healthy and the watchdog — whose
staleness marker is now permanently 0.0 — stays silent. Recovery requires a
full container restart by a human.

Upstream context: independently reported and analyzed by Li-Xingyu in
[PR #24142](https://github.com/blakeblackshear/frigate/pull/24142) (closed by
template bot) and [PR #24146](https://github.com/blakeblackshear/frigate/pull/24146)
(narrowed fix, **merged 2026-09-02 into the 0.19 branch**). The merged fix
closes the main entrance to the force-kill; the force-kill itself remains
unsafe — that residual is what the evidence here demonstrates.

## Contents

| Path | What it is |
|---|---|
| `EVIDENCE-INCIDENT-3.md` | The full evidence document: event index, the cascade log excerpts, py-spy/`/proc`/stats/socket post-state, v0.17.2 source verification with file:line cites, observed-vs-inferred accounting, upstream relation. ("Incident 3" continues the numbering of the same plant's investigation; incidents 1–2 — plugin-layer bugs — live in the sibling repo linked below.) |
| `tools/repro-queue-strand.py` | Standalone 149-line reproduction (Linux/POSIX): SIGKILL a reader blocked in `multiprocessing.Queue.get()`; the queue then holds items no process can ever read. No Frigate, no network. `python3 tools/repro-queue-strand.py` → exit 0 = 6/6 checks; passes under fork, forkserver, and spawn start methods. |
| `tools/repro-queue-strand-output-20260905.txt` | Banked 6/6 runs of the above under fork, forkserver, and spawn. |
| `fixes/zmq_ipc.py` | Our patched `zmq` detector plugin, v1.3 (sha `b6b68eca…`): bounds in-call recovery to 2 s so a network stall never arms the 10 s watchdog — the same router-restart trigger replayed under v1.3 self-recovered with zero watchdog activity (EVIDENCE §5). Trigger-avoidance only. |
| `fixes/zmq_ipc_fix.patch` | Unified diff, pristine v0.17.2 plugin → v1.3. |

## The direction of a core-side fix (upstream's to design)

Two directions would close the class, and neither is small: **signaling the
condemned generation** needs per-generation stop events (the broader #24142
design, which upstream declined as too invasive), and **recreating the request
queue after a force-kill** is complicated by every running camera process
holding a reference to the old queue — a bare swap would leave cameras feeding
a queue nobody reads. The evidence here names the constraint; the trade-off is
upstream's. Details: `EVIDENCE-INCIDENT-3.md` §7.

## Related

- Sibling repo (same plant, incidents 1–2, plugin layer):
  [Frigate_zmq_silent-detection_failure](https://github.com/tempered-steel/Frigate_zmq_silent-detection_failure)
- Plugin-layer upstream report:
  [discussion #23883](https://github.com/blakeblackshear/frigate/discussions/23883)

## License

MIT © Tempered-Steel. The captures are yours to reuse; a link back helps
others find the full record.
