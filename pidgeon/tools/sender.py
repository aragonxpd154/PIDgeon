"""Envia um arquivo TS/BTS por UDP ou RTP (unicast/multicast) na taxa do mux — útil para testes.

Mantido por compatibilidade; o pidgeon-gerar faz o mesmo e muito mais (conversão de formato,
RTP + FEC SMPTE 2022-1, DSCP, interface de saída).

Uso:  pidgeon-enviar arquivo.bts 239.1.1.1:1234 [--rtp] [--fec 10x10] [--ttl 4] [--iface IP] [--loop]
"""

import argparse

from ..core.convert import Generator, GenConfig, probe
from ..core.netout import NetConfig


def send(path, dest, rtp=False, ttl=4, iface=None, loop=False, rate=None, seconds=None, fec=None, log=None):
    host, port = dest.rsplit(":", 1)
    pr = probe(path)
    net = NetConfig(host=host, port=int(port), proto="rtp" if rtp else "udp", iface=iface, ttl=ttl)
    if fec:
        c, _, r = fec.lower().partition("x")
        net.proto, net.fec_cols, net.fec_rows = "rtp-fec", int(c), int(r or c)
    cfg = GenConfig(source=path, fmt="original", net=net, loop=loop, rate_bps=rate, seconds=seconds)
    gen = Generator(cfg, pr, log=log)
    gen.run()
    return gen.stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Envia TS/BTS por UDP/RTP (formato original)")
    ap.add_argument("arquivo")
    ap.add_argument("destino", help="ip:porta (unicast ou multicast)")
    ap.add_argument("--rtp", action="store_true")
    ap.add_argument("--fec", help="RTP + FEC SMPTE 2022-1 LxD (ex.: 10x10)")
    ap.add_argument("--ttl", type=int, default=4)
    ap.add_argument("--iface", help="IP ou nome da interface de saída")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--taxa", type=float, help="bps (só para TS sem PCR)")
    a = ap.parse_args(argv)
    try:
        send(a.arquivo, a.destino, a.rtp or bool(a.fec), a.ttl, a.iface, a.loop, a.taxa, fec=a.fec, log=print)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
