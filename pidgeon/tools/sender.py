"""Envia um arquivo TS/BTS por UDP ou RTP (unicast/multicast) na taxa do mux — útil para testes.

Uso:  python -m pidgeon.tools.sender arquivo.bts 239.1.1.1:1234 [--rtp] [--ttl 4] [--iface IP] [--loop]
"""

import argparse
import socket
import struct
import time


def send(path, dest, rtp=False, ttl=4, iface=None, loop=False, rate=None, seconds=None):
    host, port = dest.rsplit(":", 1)
    port = int(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    if iface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    with open(path, "rb") as f:
        head = f.read(204 * 6)
    size = 204 if all(head[i * 204] == 0x47 for i in range(5)) and not all(
        head[i * 188] == 0x47 for i in range(6)) else 188
    if rate is None:
        rate = 32507936.5 if size == 204 else 20e6
    chunk = size * 7
    seq = 0
    ssrc = 0x50494447
    t_start = time.monotonic()
    while True:
        with open(path, "rb") as f:
            start = time.monotonic()
            sent = 0
            while True:
                data = f.read(chunk)
                if not data:
                    break
                if rtp:
                    ts90 = int((time.monotonic() - t_start) * 90000) & 0xFFFFFFFF
                    data = struct.pack("!BBHII", 0x80, 33, seq & 0xFFFF, ts90, ssrc) + data
                    seq += 1
                sock.sendto(data, (host, port))
                sent += len(data)
                ahead = start + sent * 8 / rate - time.monotonic()
                if ahead > 0:
                    time.sleep(ahead)
                if seconds and time.monotonic() - t_start > seconds:
                    return
        if not loop:
            break


def main(argv=None):
    ap = argparse.ArgumentParser(description="Envia TS/BTS por UDP/RTP")
    ap.add_argument("arquivo")
    ap.add_argument("destino", help="ip:porta (unicast ou multicast)")
    ap.add_argument("--rtp", action="store_true")
    ap.add_argument("--ttl", type=int, default=4)
    ap.add_argument("--iface", help="IP local para saída multicast")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--taxa", type=float, help="bps (padrão: 32,5 Mbps p/ BTS, 20 Mbps p/ TS)")
    a = ap.parse_args(argv)
    send(a.arquivo, a.destino, a.rtp, a.ttl, a.iface, a.loop, a.taxa)


if __name__ == "__main__":
    main()
