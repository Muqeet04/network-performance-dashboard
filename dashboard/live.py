"""Continuous TCP + UDP probing for the live view.

Runs in background threads inside the dashboard process (start it on Computer A).
Every probe result is appended to an in-memory ring buffer that the browser polls.
"""
import os
import socket
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
import test_runner as tr  # noqa: E402

UDP_TIMEOUT = 1.0   # an echo later than this counts as lost
TCP_TIMEOUT = 2.0
STATS_WINDOW = 10   # seconds used for the KPI cards


class Prober:
    def __init__(self):
        self.lock = threading.Lock()
        self.points = deque(maxlen=6000)
        self.n = 0
        self.running = False
        self.server = None
        self.error = None
        self._ev = threading.Event()
        self._threads = []

    # ------------------------------------------------------------ control
    def start(self, host, interval, tcp_port, udp_port, size=64):
        self.stop()
        self._ev = threading.Event()
        self.server, self.error, self.running = host, None, True
        with self.lock:
            self.points.clear()
            self.n = 0
        self._threads = [
            threading.Thread(target=self._udp_loop, args=(self._ev, host, udp_port, interval, size), daemon=True),
            threading.Thread(target=self._tcp_loop, args=(self._ev, host, tcp_port, interval, size), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._ev.set()
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []
        self.running = False

    # ------------------------------------------------------------ probes
    def _add(self, proto, rtt, lost):
        with self.lock:
            self.n += 1
            self.points.append(dict(i=self.n, t=time.time(), proto=proto, rtt=rtt, lost=lost))

    def _udp_loop(self, ev, host, port, interval, size):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((host, port))
        except OSError as e:
            self.error = f"UDP: {e}"
            return
        pending, seq = {}, 0
        nxt = time.perf_counter()
        while not ev.is_set():
            now = time.perf_counter()
            if now >= nxt:
                try:
                    s.send(tr.make_msg(seq, size))
                    pending[seq] = now
                except OSError:
                    pass
                seq += 1
                nxt = max(nxt + interval, now)
            s.settimeout(max(0.001, min(0.05, nxt - time.perf_counter())))
            try:
                data = s.recv(65535)
                q, ts = tr.HDR.unpack(data[:tr.HDR.size])
                if q in pending:
                    del pending[q]
                    self._add("UDP", (time.perf_counter() - ts) * 1000, False)
            except socket.timeout:
                pass
            except OSError:
                ev.wait(0.05)  # e.g. ICMP port unreachable when the server is down
            cutoff = time.perf_counter() - UDP_TIMEOUT
            for q, t0 in list(pending.items()):
                if t0 < cutoff:
                    del pending[q]
                    self._add("UDP", None, True)
        s.close()

    def _tcp_loop(self, ev, host, port, interval, size):
        while not ev.is_set():
            s = None
            try:
                s = socket.create_connection((host, port), timeout=TCP_TIMEOUT)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(TCP_TIMEOUT)
                s.sendall(f"ECHO {size}\n".encode())
                seq, nxt = 0, time.perf_counter()
                while not ev.is_set():
                    s.sendall(tr.make_msg(seq, size))
                    data = tr.recv_exact(s, size)
                    ts = tr.HDR.unpack(data[:tr.HDR.size])[1]
                    self._add("TCP", (time.perf_counter() - ts) * 1000, False)
                    seq += 1
                    nxt += interval
                    ev.wait(max(0, nxt - time.perf_counter()))
            except (OSError, ConnectionError):
                if not ev.is_set():
                    self._add("TCP", None, True)  # timeout / reset / refused
                    ev.wait(0.5)
            finally:
                if s:
                    s.close()

    # ------------------------------------------------------------ stats
    @staticmethod
    def _stats(pts):
        ok = [p["rtt"] for p in pts if not p["lost"]]
        n = len(pts)
        out = dict(n=n, lost=n - len(ok), loss_pct=(100.0 * (n - len(ok)) / n) if n else None,
                   last=ok[-1] if ok else None)
        out.update(tr.rtt_stats(ok))
        return out

    def snapshot(self, since=0, history=70):
        now = time.time()
        with self.lock:
            pts = list(self.points)
        recent = [p for p in pts if p["t"] >= now - STATS_WINDOW]
        new = [p for p in pts if p["i"] > since and (since > 0 or p["t"] >= now - history)]
        return dict(running=self.running, server=self.server, error=self.error, now=now,
                    points=new, last_i=self.n, window=STATS_WINDOW,
                    stats={pr: self._stats([p for p in recent if p["proto"] == pr]) for pr in ("TCP", "UDP")})
