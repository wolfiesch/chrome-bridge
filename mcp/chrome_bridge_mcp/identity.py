"""Per-instance token provisioning and a session-hold auto-lease helper.

This module lets each MCP server instance present its OWN named token to the
bridge host (so concurrent agents are distinguishable and the cooperative
lease can attribute ownership) and hold the shared-Chrome lease for the life
of the process, renewing on TTL.

Both pieces are opt-in: nothing here runs at import time. ``server.main`` wires
them up only when running for real; ``build_server`` does not, so unit tests
are unaffected.

Stdlib only.
"""
import atexit
import fcntl
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
from collections import namedtuple


def _tokens_file(repo_root):
    return os.environ.get(
        "BRIDGE_TOKENS_FILE", os.path.join(repo_root, "bridge_tokens.txt")
    )


def _name_of(line):
    # A registry line is ``name:token`` split on the first colon.
    return line.split(":", 1)[0].strip()


def _rewrite_registry(tokens_path, mutate):
    """Read-modify-write ``tokens_path`` atomically under an exclusive lock.

    ``mutate(lines)`` receives the existing non-empty lines and returns the new
    list of lines to persist. The lock is taken on a sibling ``.lock`` file so
    concurrent provisioners serialize and never lose each other's entries.
    """
    lock_path = tokens_path + ".lock"
    directory = os.path.dirname(tokens_path) or "."
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(tokens_path, "r") as fh:
                existing = [ln.rstrip("\n") for ln in fh if ln.strip()]
        except FileNotFoundError:
            existing = []

        new_lines = mutate(existing)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".bridge_tokens.")
        try:
            with os.fdopen(tmp_fd, "w") as out:
                for ln in new_lines:
                    out.write(ln + "\n")
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, tokens_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.chmod(tokens_path, 0o600)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


Identity = namedtuple("Identity", ["name", "token", "token_file", "cleanup"])


def provision_identity(repo_root, on_shutdown=None):
    """Register a unique named token for this process and point transport at it.

    Returns an ``Identity`` namedtuple whose ``cleanup()`` deregisters this
    instance and removes the private token file. ``cleanup`` is also registered
    via ``atexit`` and on SIGTERM/SIGINT.

    ``on_shutdown`` (optional) runs ONCE at the very start of cleanup, before
    the registry entry and token file are removed, so a signal-driven shutdown
    releases the cooperative lease BEFORE deleting this token (otherwise the
    lease would stick until TTL).
    """
    name = "mcp-{}-{}".format(os.getpid(), secrets.token_hex(4))
    token = secrets.token_hex(32)
    tokens_path = _tokens_file(repo_root)

    def _add(lines):
        kept = [ln for ln in lines if _name_of(ln) != name]
        kept.append("{}:{}".format(name, token))
        return kept

    _rewrite_registry(tokens_path, _add)

    # Private 0600 file holding only this token; transport reads it.
    tf = tempfile.NamedTemporaryFile(
        mode="w", prefix="bridge_token_", suffix=".txt", delete=False
    )
    try:
        tf.write(token)
        tf.flush()
    finally:
        tf.close()
    os.chmod(tf.name, 0o600)
    token_file = tf.name
    os.environ["BRIDGE_TOKEN_FILE"] = token_file

    cleaned = threading.Event()

    def cleanup():
        if cleaned.is_set():
            return
        cleaned.set()

        # Release the lease (or any caller-provided shutdown step) BEFORE we
        # delete this instance's token, so a stuck lease can never outlive the
        # token that owns it.
        if on_shutdown is not None:
            try:
                on_shutdown()
            except Exception:
                pass

        def _drop(lines):
            return [ln for ln in lines if _name_of(ln) != name]

        try:
            _rewrite_registry(tokens_path, _drop)
        except OSError:
            pass
        try:
            os.unlink(token_file)
        except OSError:
            pass

    atexit.register(cleanup)
    _install_signal_handlers(cleanup)

    return Identity(name=name, token=token, token_file=token_file, cleanup=cleanup)


def _install_signal_handlers(cleanup):
    """Run ``cleanup`` on SIGTERM/SIGINT, then defer to the prior handler.

    Preserves any existing handler chain: after cleaning up we re-invoke the
    previous handler if it is callable, otherwise reproduce the default
    behavior (terminate on SIGTERM, raise KeyboardInterrupt on SIGINT).
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def _handler(signum, frame, _prev=prev, _sig=sig):
            cleanup()
            if callable(_prev):
                _prev(signum, frame)
                return
            if _prev == signal.SIG_IGN:
                return
            # SIG_DFL or unknown: reproduce default disposition.
            if _sig == signal.SIGINT:
                raise KeyboardInterrupt()
            sys.exit(143)

        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not on the main thread; atexit still covers normal shutdown.
            pass


class LeaseManager:
    """Thread-safe holder of the cooperative shared-Chrome lease.

    ``call`` is a callable matching ``transport.call(action, payload)``. The
    manager acquires the lease lazily on the first mutating action and renews
    it once more than half the TTL has elapsed.
    """

    def __init__(self, call, ttl_ms=300000, renew_fraction=0.5):
        self._call = call
        self._ttl_ms = int(ttl_ms)
        self._renew_fraction = renew_fraction
        self._lock = threading.Lock()
        self._expires_at = None  # local epoch-ms estimate
        self._acquired_at = None

    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    def ensure(self, min_remaining_ms=0):
        """Acquire or renew the lease if needed.

        ``min_remaining_ms`` guarantees the lease will stay valid for at least
        that long after this call; when the current lease would expire sooner
        (e.g. a long human handoff outlasting the default TTL), it reacquires
        with a TTL of ``min_remaining_ms`` plus headroom so another agent cannot
        mutate the profile mid-wait.

        Raises ``BridgeError`` (propagated from ``call``) when the lease is held
        by another agent, so the caller can back off.
        """
        with self._lock:
            now = self._now_ms()
            ttl = self._ttl_ms
            if min_remaining_ms > 0:
                # Cover the requested window with 30s of headroom.
                ttl = max(ttl, int(min_remaining_ms) + 30000)
            if self._expires_at is not None and self._acquired_at is not None:
                elapsed = now - self._acquired_at
                span = self._expires_at - self._acquired_at
                covers = self._expires_at - now >= min_remaining_ms
                # Renew only past the renew window; otherwise the lease stands --
                # but only if it still covers the requested remaining window.
                if span > 0 and elapsed < span * self._renew_fraction and covers:
                    return
            result = self._call("lease", {"ttlMs": ttl})
            self._acquired_at = self._now_ms()
            expires = None
            if isinstance(result, dict):
                expires = result.get("expiresAt")
            self._expires_at = expires if expires is not None else (
                self._acquired_at + ttl
            )

    def release(self):
        """Best-effort release; never raises."""
        with self._lock:
            self._expires_at = None
            self._acquired_at = None
        try:
            self._call("release", {})
        except Exception:
            pass

    def invalidate(self):
        """Forget locally-held lease state WITHOUT calling the host.

        Used when a manual ``release``/``lease`` tool already talked to the host
        directly, so the next ``ensure()`` reacquires instead of assuming the
        stale lease still stands.
        """
        with self._lock:
            self._expires_at = None
            self._acquired_at = None
