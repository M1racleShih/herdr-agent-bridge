"""Shared audited hc calls, assertions and failure-safe acceptance cleanup."""

import signal
import subprocess

from herdr_agent_bridge.bridge_common import BridgeError, run_hc, save


class AuditRunner:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.drop_next_send_reply = False

    def __call__(self, args, env, timeout):
        call = {"args": args, "phase": "attempted"}
        self.calls.append(call)
        save(self.path, self.calls)
        try:
            result = run_hc(args, env, timeout)
        except BaseException as exc:
            call["error_type"] = type(exc).__name__
            save(self.path, self.calls)
            raise
        call["phase"] = "returned"
        call["result"] = result
        if args[0] == "send" and self.drop_next_send_reply:
            self.drop_next_send_reply = False
            call["reply_discarded_for_test"] = True
            save(self.path, self.calls)
            raise subprocess.TimeoutExpired(["hc", "send"], timeout)
        save(self.path, self.calls)
        return result


def require(condition, message):
    if not condition:
        raise BridgeError(message)


def finish_report(bridge, report, output):
    """Keep cleanup interruptible as evidence, without abandoning owned cleanup."""
    signals = []

    def record_signal(signum, frame):
        signals.append(signum)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, record_signal)
    try:
        if bridge is not None:
            cleanup = bridge.close()
            report["cleanup"] = cleanup
            if not cleanup or not all(c["passed"] for c in cleanup):
                report["passed"] = False
        else:
            report["passed"] = False
    except (Exception, KeyboardInterrupt) as exc:
        report["passed"] = False
        report["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if signals:
            report["signals_during_cleanup"] = signals
            report["passed"] = False
        report["phase"] = "finished"
        try:
            save(output, report)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
