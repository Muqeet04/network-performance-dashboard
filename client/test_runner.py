#!/usr/bin/env python3
"""Measurement client. Run this on Computer A.

Runs four tests against the echo server and stores results in SQLite:
  tcp_rtt        - RTT of small request/echo messages over TCP (+ connect time)
  tcp_bulk       - TCP throughput (bulk transfer)
  udp_rtt        - RTT, loss, jitter, reordering over UDP at a low message rate
  udp_throughput - UDP goodput / loss at a fixed offered rate

All timing uses the client's own clock (RTT), so no clock sync is needed.
UDP loss is *round-trip* loss (a message lost in either direction counts).
"""
import argparse
import os
import socket
import sqlite3
import statistics
import struct
import threading
import time

HDR = struct.Struct("!Id")  # seq (4B) + client send timestamp (8B)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, condition TEXT, protocol TEXT, test TEXT,
  payload INTEGER, sent INTEGER, received INTEGER, loss_pct REAL,
  rtt_min REAL, rtt_avg REAL, rtt_max REAL, rtt_p95 REAL, jitter_ms REAL,
  throughput_mbps REAL, retrans INTEGER, connect_ms REAL,
  reordered INTEGER, duplicates INTEGER
);
CREATE TABLE IF NOT EXISTS samples (
  run_id INTEGER, seq INTEGER, rtt_ms REAL
);
"""


def make_msg(seq, size):
    return HDR.pack(seq, time.perf_counter()) + b"x" * (size - HDR.size)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed connection")
        buf += chunk
    return bytes(buf)


def tcp_retrans():
    """System-wide TCP retransmitted segments (Linux)."""
    try:
        with open("/proc/net/snmp") as f:
            lines = [l.split() for l in f if l.startswith("Tcp:")]
        return int(lines[1][lines[0].index("RetransSegs")])
    except Exception:
        return None


def rtt_stats(rtts):
    if not rtts:
        return dict(rtt_min=None, rtt_avg=None, rtt_max=None, rtt_p95=None, jitter_ms=None)
    s = sorted(rtts)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    # jitter: mean absolute difference between consecutive RTTs (arrival order)
    diffs = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
    return dict(rtt_min=s[0], rtt_avg=statistics.fmean(rtts), rtt_max=s[-1], rtt_p95=p95,
                jitter_ms=statistics.fmean(diffs) if diffs else 0.0)


# ------------------------------------------------------------------ tests
def tcp_rtt(host, port, count, size, interval):
    r0 = tcp_retrans()
    t0 = time.perf_counter()
    s = socket.create_connection((host, port), timeout=10)
    connect_ms = (time.perf_counter() - t0) * 1000
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(f"ECHO {size}\n".encode())
    samples = []
    for seq in range(count):
        s.sendall(make_msg(seq, size))
        data = recv_exact(s, size)
        ts = HDR.unpack(data[:HDR.size])[1]
        samples.append((seq, (time.perf_counter() - ts) * 1000))
        if interval:
            time.sleep(interval)
    s.close()
    r1 = tcp_retrans()
    rtts = [r for _, r in samples]
    res = dict(protocol="TCP", test="tcp_rtt", payload=size, sent=count, received=len(rtts),
               loss_pct=0.0, connect_ms=connect_ms,
               retrans=(r1 - r0) if None not in (r0, r1) else None, **rtt_stats(rtts))
    return res, samples


def tcp_bulk(host, port, mbytes):
    total = int(mbytes * 1024 * 1024)
    r0 = tcp_retrans()
    s = socket.create_connection((host, port), timeout=60)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(f"BULK {total}\n".encode())
    chunk = b"x" * 65536
    sent = 0
    t0 = time.perf_counter()
    while sent < total:
        n = min(len(chunk), total - sent)
        s.sendall(chunk[:n])
        sent += n
    while s.recv(16).find(b"DONE") < 0:
        pass
    dur = time.perf_counter() - t0
    s.close()
    r1 = tcp_retrans()
    return dict(protocol="TCP", test="tcp_bulk", payload=65536, sent=total, received=total,
                loss_pct=0.0, throughput_mbps=total * 8 / dur / 1e6,
                retrans=(r1 - r0) if None not in (r0, r1) else None), []


def udp_test(host, port, test, count, size, interval, drain=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((host, port))
    s.settimeout(0.2)
    got = []  # (seq, rtt_ms) in arrival order
    stop = threading.Event()

    def rx():
        while not stop.is_set():
            try:
                data = s.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.perf_counter()
            if len(data) >= HDR.size:
                seq, ts = HDR.unpack(data[:HDR.size])
                got.append((seq, (now - ts) * 1000))

    th = threading.Thread(target=rx, daemon=True)
    th.start()
    start = time.perf_counter()
    nxt = start
    for seq in range(count):
        s.send(make_msg(seq, size))
        if interval > 0:
            nxt += interval
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
    elapsed = time.perf_counter() - start
    time.sleep(drain)
    stop.set()
    th.join()
    s.close()

    seen, uniq, reordered, hi = set(), [], 0, -1
    for seq, rtt in got:
        if seq in seen:
            continue
        seen.add(seq)
        uniq.append((seq, rtt))
        if seq < hi:
            reordered += 1
        hi = max(hi, seq)
    rtts = [r for _, r in uniq]
    res = dict(protocol="UDP", test=test, payload=size, sent=count, received=len(uniq),
               loss_pct=100.0 * (count - len(uniq)) / count,
               throughput_mbps=len(uniq) * size * 8 / elapsed / 1e6,
               reordered=reordered, duplicates=len(got) - len(uniq), **rtt_stats(rtts))
    return res, uniq


# ------------------------------------------------------------------ storage
COLS = ["ts", "condition", "protocol", "test", "payload", "sent", "received", "loss_pct",
        "rtt_min", "rtt_avg", "rtt_max", "rtt_p95", "jitter_ms", "throughput_mbps",
        "retrans", "connect_ms", "reordered", "duplicates"]


def save(db, condition, res, samples):
    res = dict(res, condition=condition, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    cur = con.execute(f"INSERT INTO runs ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                      [res.get(c) for c in COLS])
    rid = cur.lastrowid
    con.executemany("INSERT INTO samples VALUES (?,?,?)", [(rid, q, r) for q, r in samples])
    con.commit()
    con.close()


def fmt(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="IP of Computer B")
    ap.add_argument("--condition", default="baseline", help="label for this network condition")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "results.db"))
    ap.add_argument("--tcp-port", type=int, default=5001)
    ap.add_argument("--udp-port", type=int, default=5002)
    ap.add_argument("--count", type=int, default=200, help="messages per RTT test")
    ap.add_argument("--size", type=int, default=64, help="payload bytes for RTT tests (>=12)")
    ap.add_argument("--interval", type=float, default=0.02, help="seconds between RTT messages")
    ap.add_argument("--bulk-mb", type=float, default=20, help="TCP bulk transfer size in MB")
    ap.add_argument("--udp-rate", type=float, default=5.0, help="UDP offered rate, Mbit/s")
    ap.add_argument("--udp-seconds", type=float, default=5.0)
    ap.add_argument("--udp-size", type=int, default=1200)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tests", default="tcp_rtt,tcp_bulk,udp_rtt,udp_throughput")
    a = ap.parse_args()
    tests = a.tests.split(",")
    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)

    for run in range(1, a.runs + 1):
        for t in tests:
            try:
                if t == "tcp_rtt":
                    res, smp = tcp_rtt(a.server, a.tcp_port, a.count, a.size, a.interval)
                elif t == "tcp_bulk":
                    res, smp = tcp_bulk(a.server, a.tcp_port, a.bulk_mb)
                elif t == "udp_rtt":
                    res, smp = udp_test(a.server, a.udp_port, "udp_rtt", a.count, a.size, a.interval)
                elif t == "udp_throughput":
                    iv = a.udp_size * 8 / (a.udp_rate * 1e6)
                    res, smp = udp_test(a.server, a.udp_port, "udp_throughput",
                                        int(a.udp_seconds / iv), a.udp_size, iv)
                else:
                    print("unknown test", t)
                    continue
            except Exception as e:
                print(f"[{a.condition}] run {run} {t}: FAILED ({e})")
                continue
            save(a.db, a.condition, res, smp)
            print(f"[{a.condition}] run {run} {t:15s} rtt_avg={fmt(res.get('rtt_avg'))}ms "
                  f"p95={fmt(res.get('rtt_p95'))}ms jitter={fmt(res.get('jitter_ms'))}ms "
                  f"loss={fmt(res.get('loss_pct'))}% thr={fmt(res.get('throughput_mbps'))}Mbps "
                  f"retrans={res.get('retrans')}")


if __name__ == "__main__":
    main()
