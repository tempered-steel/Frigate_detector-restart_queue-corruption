# fixes/ — trigger-avoidance mitigation (NOT a fix for the core bug)

`zmq_ipc.py` is v1.3 of our patched `zmq` detector plugin for Frigate 0.17.2
(sha256 `b6b68eca2269e1e93ee86c8bf8df67251355e85cac59bb30b8086fde6dd7f125`);
`zmq_ipc_fix.patch` is the unified diff from the pristine v0.17.2 file.

Relevance to this repo's bug: v1.3 bounds the plugin's in-call recovery to a
2 s budget (`recovery_timeout_ms`, default 2000) so that no network stall can
hold a detect call past Frigate's 10 s detector watchdog — the watchdog never
condemns, the force-kill never happens, the queue lock is never at risk. The
2026-08-31 counter-test (EVIDENCE-INCIDENT-3.md §5) replayed the same
router-restart trigger under v1.3: the plugin failed fast, retried, and the
model was ready 96 s after the first logged failure (14:50:49Z → 14:52:25Z),
with zero watchdog activity and an unchanged detector pid.

This protects only deployments using this plugin, and only against triggers
that enter through long plugin calls. Any other path to the force-kill
(any detector type, any >10 s stall) still reaches the core bug. The core-side
fix is described in EVIDENCE-INCIDENT-3.md §7.

The plugin patch's design rationale and its regression harness live with the
plugin-layer investigation in the sibling repo (which currently carries the
pre-v1.3 generation of the patch; a v1.3 refresh there is pending):
https://github.com/tempered-steel/Frigate_zmq_silent-detection_failure

Attribution: `zmq_ipc.py` is a derivative of
`frigate/detectors/plugins/zmq_ipc.py` from
[Frigate](https://github.com/blakeblackshear/frigate), © Blake Blackshear and
the Frigate authors, MIT License. The modifications are © 2026 Tempered-Steel,
MIT License.
