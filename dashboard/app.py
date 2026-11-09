#!/usr/bin/env python3
"""Dashboard: python3 dashboard/app.py  ->  http://localhost:8000
Run on Computer A (it probes Computer B live). Results are read from data/results.db."""
import os
import sqlite3

from flask import Flask, jsonify, render_template, request

from live import Prober, tr

DB = os.path.abspath(os.environ.get("RESULTS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "results.db")))
app = Flask(__name__)
prober = Prober()


def q(sql, args=()):
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- live view
@app.post("/api/live/start")
def live_start():
    j = request.get_json(force=True)
    prober.start(j["server"], max(0.02, float(j.get("interval_ms", 200)) / 1000),
                 int(j.get("tcp_port", 5001)), int(j.get("udp_port", 5002)))
    return jsonify(ok=True)


@app.post("/api/live/stop")
def live_stop():
    prober.stop()
    return jsonify(ok=True)


@app.get("/api/live")
def live():
    return jsonify(prober.snapshot(int(request.args.get("since", 0)),
                                   min(600, int(request.args.get("history", 70)))))


@app.post("/api/throughput")
def throughput():
    """On-demand throughput tests (stored in the DB under the given label)."""
    j = request.get_json(force=True)
    host, label = j["server"], j.get("label") or "live"
    tcp_port, udp_port = int(j.get("tcp_port", 5001)), int(j.get("udp_port", 5002))
    rate, size, secs = float(j.get("udp_rate", 5.0)), 1200, 3.0
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    out = []
    try:
        res, smp = tr.tcp_bulk(host, tcp_port, float(j.get("bulk_mb", 5)))
        tr.save(DB, label, res, smp)
        out.append(res)
        iv = size * 8 / (rate * 1e6)
        res, smp = tr.udp_test(host, udp_port, "udp_throughput", int(secs / iv), size, iv)
        tr.save(DB, label, res, smp)
        out.append(res)
    except Exception as e:  # server down, timeouts, ...
        return jsonify(error=str(e)), 502
    return jsonify(out)


@app.get("/api/throughput/latest")
def throughput_latest():
    rows = q("SELECT test, throughput_mbps, loss_pct, condition, ts FROM runs "
             "WHERE test IN ('tcp_bulk','udp_throughput') ORDER BY id DESC LIMIT 20")
    latest = {}
    for r in rows:
        latest.setdefault(r["test"], r)
    return jsonify(latest)


# ---------------------------------------------------------------- experiments
@app.route("/api/summary")
def summary():
    """Averages over repeated runs, per condition + test."""
    return jsonify(q("""
        SELECT condition, protocol, test, COUNT(*) AS runs, MIN(id) AS first_id,
               AVG(rtt_min) rtt_min, AVG(rtt_avg) rtt_avg, AVG(rtt_max) rtt_max,
               AVG(rtt_p95) rtt_p95, AVG(jitter_ms) jitter_ms, AVG(loss_pct) loss_pct,
               AVG(throughput_mbps) throughput_mbps, AVG(retrans) retrans,
               AVG(connect_ms) connect_ms, AVG(reordered) reordered
        FROM runs GROUP BY condition, protocol, test ORDER BY first_id"""))


@app.route("/api/runs")
def runs():
    return jsonify(q("SELECT * FROM runs ORDER BY id DESC LIMIT 300"))


@app.route("/api/samples")
def samples():
    """RTT samples of the latest run for a condition + test."""
    cond, test = request.args.get("condition"), request.args.get("test")
    return jsonify(q("""SELECT seq, rtt_ms FROM samples WHERE run_id =
                        (SELECT MAX(id) FROM runs WHERE condition=? AND test=?)
                        ORDER BY seq""", (cond, test)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False, threaded=True)
