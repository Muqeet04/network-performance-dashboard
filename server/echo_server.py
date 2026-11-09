#!/usr/bin/env python3
"""Echo/sink server. Run this on Computer B.

TCP (port 5001): the first line selects the mode
    ECHO <size>\n   -> echo back every <size>-byte message until the client closes
    BULK <nbytes>\n -> read <nbytes>, then reply "DONE\n"
UDP (port 5002): every datagram is echoed straight back.
"""
import argparse
import socket
import threading


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_line(sock):
    line = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            return None
        if c == b"\n":
            return line.decode()
        line += c


def handle_tcp(conn):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        line = read_line(conn)
        if not line:
            return
        parts = line.split()
        if parts[0] == "ECHO":
            size = int(parts[1])
            while True:
                data = recv_exact(conn, size)
                if data is None:
                    break
                conn.sendall(data)
        elif parts[0] == "BULK":
            remaining = int(parts[1])
            while remaining > 0:
                chunk = conn.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            conn.sendall(b"DONE\n")
    except OSError:
        pass
    finally:
        conn.close()


def tcp_server(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"[TCP] listening on {host}:{port}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_tcp, args=(conn,), daemon=True).start()


def udp_server(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind((host, port))
    print(f"[UDP] listening on {host}:{port}")
    while True:
        data, addr = srv.recvfrom(65535)
        srv.sendto(data, addr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--tcp-port", type=int, default=5001)
    ap.add_argument("--udp-port", type=int, default=5002)
    a = ap.parse_args()
    threading.Thread(target=udp_server, args=(a.host, a.udp_port), daemon=True).start()
    try:
        tcp_server(a.host, a.tcp_port)
    except KeyboardInterrupt:
        print("\nbye")
