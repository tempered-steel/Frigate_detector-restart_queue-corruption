#!/usr/bin/env python3
# repro-queue-strand.py -- sandbox demonstration for EVIDENCE-INCIDENT-3.md §6:
# SIGKILLing a process that is blocked in multiprocessing.Queue.get() strands
# the queue's shared reader lock; every later reader is locked out forever,
# even when the queue holds items.
#
# This is the inferred step of Incident 3 reduced to its mechanism: no Frigate,
# no network, no plant contact -- one Queue, two child readers, one SIGKILL.
# CPython's mp.Queue.get(block=True) acquires the shared reader semaphore
# (_rlock) and holds it while waiting for data; SIGKILL gives the holder no
# chance to release, and mp locks are plain POSIX semaphores with no
# owner-death recovery (unlike robust futexes).
#
# Run: python3 repro-queue-strand.py [fork|forkserver|spawn]
#      (exit 0 = all checks pass; start method defaults to the platform's.
#       All three methods pass identically -- the lock is a POSIX semaphore
#       either way. Frigate itself uses forkserver.)
# Platform: Linux/POSIX only. SIGKILL does not exist on Windows, and
# Queue.qsize() raises NotImplementedError on macOS -- the demonstration
# targets the platform the incident occurred on.
# The demonstration PASSES when:
#   A) control reader (killed while NOT holding the lock) -> next reader OK
#   B) reader SIGKILLed while blocked in get() -> queue has items, qsize()>0,
#      yet a fresh reader gets nothing but Empty; a fresh reader in a THIRD
#      process is equally locked out (it is the shared semaphore, not local
#      state).
# Total runtime is bounded (~15 s); every wait is timed, nothing can hang.

import multiprocessing as mp
import os
import signal
import sys
import time
from queue import Empty


def blocked_reader(q, started):
    started.set()          # parent may kill us as soon as we are (about to be) in get()
    q.get()                # block=True, no timeout: holds _rlock while waiting
    sys.exit(0)            # never reached in the strand scenario


def idle_child(started):
    started.set()          # holds NO queue lock; control victim
    time.sleep(60)


def try_read(q, attempts, timeout, out):
    done = got = 0
    for _ in range(attempts):
        try:
            q.get(timeout=timeout)   # timed path: _rlock.acquire(block, timeout)
            got += 1
        except Empty:
            pass
        done += 1
    out.put((done, got))  # report attempts completed too, so a crashed child
                          # cannot masquerade as "locked out" (vacuous pass)


def reader_in_new_process(q, attempts, timeout):
    out = mp.SimpleQueue()  # SimpleQueue: independent lock, safe reporting channel
    p = mp.Process(target=try_read, args=(q, attempts, timeout, out), daemon=True)
    p.start()
    p.join(attempts * timeout + 10)
    alive = p.is_alive()
    if alive:
        p.terminate()
        p.join(5)
    if out.empty():          # child crashed or hung before reporting: not a
        return -1, alive     # valid "got 0" -- callers treat -1 as FAIL
    done, got = out.get()
    if done != attempts:
        return -1, alive
    return got, alive


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    if len(sys.argv) > 1:
        mp.set_start_method(sys.argv[1])
    print(f"python {sys.version.split()[0]}, start method: {mp.get_start_method()}")

    # --- Scenario A (control): SIGKILL a child that holds no queue lock ------
    print("Scenario A (control): victim killed while NOT in get()")
    qa = mp.Queue()
    started = mp.Event()
    victim = mp.Process(target=idle_child, args=(started,), daemon=True)
    victim.start()
    started.wait(5)
    time.sleep(0.3)
    os.kill(victim.pid, signal.SIGKILL)
    victim.join(5)
    for i in range(3):
        qa.put(i)
    time.sleep(0.3)
    got, hung = reader_in_new_process(qa, attempts=3, timeout=2.0)
    check("later reader drains the queue normally", got == 3 and not hung, f"got {got}/3")

    # --- Scenario B (incident): SIGKILL a child blocked in get() -------------
    print("Scenario B (incident): victim SIGKILLed while blocked in Queue.get()")
    qb = mp.Queue()
    started = mp.Event()
    victim = mp.Process(target=blocked_reader, args=(qb, started), daemon=True)
    victim.start()
    started.wait(5)
    time.sleep(1.0)        # let it reach get() and take _rlock (queue is empty).
                           # If this window were somehow too short, the victim
                           # would die before taking the lock and the lockout
                           # checks below would FAIL loudly -- the race cannot
                           # produce a false PASS.
    os.kill(victim.pid, signal.SIGKILL)
    victim.join(5)
    check("victim is dead", not victim.is_alive())

    for i in range(5):     # writers use _wlock; unaffected -- items DO queue up
        qb.put(i)
    time.sleep(0.5)        # let the feeder thread flush to the pipe
    size = qb.qsize()
    check("queue reports items present after the kill", size > 0, f"qsize={size}")

    got, hung = reader_in_new_process(qb, attempts=3, timeout=2.0)
    check("fresh reader (new process) is locked out despite items",
          got == 0 and not hung, f"got {got}, expected 0; Empty on every timed get")

    got2, hung2 = reader_in_new_process(qb, attempts=2, timeout=2.0)
    check("second fresh reader equally locked out (shared semaphore, not local state)",
          got2 == 0 and not hung2, f"got {got2}")

    # parent-side single timed get, same expectation
    try:
        qb.get(timeout=2.0)
        parent_got = True
    except Empty:
        parent_got = False
    check("parent process locked out too", not parent_got)

    print(f"{'ALL CHECKS PASS' if all(checks) else 'CHECK FAILURES PRESENT'} "
          f"({sum(checks)}/{len(checks)})")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
