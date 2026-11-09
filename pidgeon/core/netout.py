"""Saída de rede: UDP puro, RTP (RFC 2250) e RTP + FEC SMPTE 2022-1, em unicast ou multicast."""

import os
import random
import socket
import time
import urllib.parse
from dataclasses import dataclass

from .fec import FecEncoder, rtp_header, RTP_PT_MP2T, RTP_CLOCK
from .sources import iface_ip, is_multicast

PROTOCOLS = {"udp": "UDP puro", "rtp": "RTP", "rtp-fec": "RTP + FEC (SMPTE 2022-1)"}


@dataclass
class NetConfig:
    host: str
    port: int
    proto: str = "udp"            # udp | rtp | rtp-fec
    iface: str = None             # nome (enp3s0) ou IP da interface de saída
    ttl: int = 16                 # TTL multicast (unicast usa o padrão do sistema)
    dscp: int = 0                 # DSCP (0–63); ex.: 34 = AF41, 46 = EF
    pkts_per_dgram: int = 7       # pacotes TS por datagrama (1–7)
    fec_cols: int = 10            # L
    fec_rows: int = 10            # D
    fec_2d: bool = False          # também FEC de linha (porta+4)
    loopback: bool = True         # multicast visível na própria máquina
    ssrc: int = None

    @property
    def multicast(self):
        return is_multicast(self.host)

    def validate(self):
        try:
            socket.inet_aton(self.host)
        except OSError:
            try:
                socket.gethostbyname(self.host)
            except OSError:
                raise ValueError("Endereço de destino inválido: %s" % self.host)
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("Porta inválida: %s" % self.port)
        if self.proto not in PROTOCOLS:
            raise ValueError("Protocolo inválido: %s (use udp, rtp ou rtp-fec)" % self.proto)
        if not (1 <= self.pkts_per_dgram <= 7):
            raise ValueError("Pacotes por datagrama: 1 a 7")
        if self.proto == "rtp-fec":
            FecEncoder(self.fec_cols, self.fec_rows, self.fec_2d)  # valida L/D
            if self.port + 4 > 65535:
                raise ValueError("Com FEC a porta precisa deixar espaço para porta+2 e porta+4")
        if not (0 <= self.dscp <= 63):
            raise ValueError("DSCP: 0 a 63")
        if not (1 <= self.ttl <= 255):
            raise ValueError("TTL: 1 a 255")
        return self

    def describe(self):
        s = "%s %s %s:%d" % (PROTOCOLS[self.proto], "multicast" if self.multicast else "unicast",
                             self.host, self.port)
        if self.proto == "rtp-fec":
            s += " (FEC %s L=%d D=%d: colunas na porta %d%s)" % (
                "2D" if self.fec_2d else "1D", self.fec_cols, self.fec_rows, self.port + 2,
                (", linhas na porta %d" % (self.port + 4)) if self.fec_2d else "")
        if self.iface:
            s += " via %s" % self.iface
        return s

    def url(self):
        scheme = "udp" if self.proto == "udp" else "rtp"
        q = []
        if self.proto == "rtp-fec":
            q.append("fec=%dx%d%s" % (self.fec_cols, self.fec_rows, "2d" if self.fec_2d else ""))
        if self.iface:
            q.append("iface=%s" % self.iface)
        if self.ttl != 16:
            q.append("ttl=%d" % self.ttl)
        if self.dscp:
            q.append("dscp=%d" % self.dscp)
        if self.pkts_per_dgram != 7:
            q.append("pkts=%d" % self.pkts_per_dgram)
        return "%s://%s:%d%s" % (scheme, self.host, self.port, ("?" + "&".join(q)) if q else "")

    @classmethod
    def from_url(cls, text):
        """udp://ip:porta  |  rtp://ip:porta?fec=10x10[2d]&iface=enp3s0&ttl=8&dscp=34&pkts=7"""
        u = urllib.parse.urlsplit(text.strip())
        scheme = u.scheme.lower()
        if scheme not in ("udp", "rtp"):
            raise ValueError("Destino deve começar com udp:// ou rtp://")
        host, _, port = u.netloc.lstrip("@").rpartition(":")
        if not host or not port:
            raise ValueError("Informe ip:porta no destino: %s" % text)
        q = urllib.parse.parse_qs(u.query)

        def g(k, default=None):
            return q[k][0] if k in q else default
        c = cls(host=host, port=int(port), proto=scheme)
        fec = g("fec")
        if fec and fec.lower() not in ("0", "off", "nao", "não"):
            if scheme != "rtp":
                raise ValueError("FEC SMPTE 2022-1 exige RTP (rtp://)")
            f = fec.lower()
            c.fec_2d = f.endswith("2d")
            f = f[:-2] if c.fec_2d else f
            if "x" in f:
                a, b = f.split("x", 1)
                c.fec_cols, c.fec_rows = int(a), int(b)
            c.proto = "rtp-fec"
        c.iface = g("iface") or g("localaddr")
        c.ttl = int(g("ttl", 16))
        c.dscp = int(g("dscp", 0))
        c.pkts_per_dgram = int(g("pkts", 7))
        c.loopback = g("loop", "1") not in ("0", "off")
        return c.validate()


class NetSender:
    def __init__(self, cfg, log=None):
        cfg.validate()
        self.cfg = cfg
        self.log = log or (lambda m: None)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        if cfg.dscp:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (cfg.dscp << 2) & 0xFF)
        local = iface_ip(cfg.iface) if cfg.iface else None
        if cfg.multicast:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, cfg.ttl)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if cfg.loopback else 0)
            if local:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        else:
            if cfg.ttl != 16:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, cfg.ttl)
            if local:
                s.bind((local, 0))
        self.sock = s
        self.addr = (socket.gethostbyname(cfg.host), cfg.port)
        self.addr_col = (self.addr[0], cfg.port + 2)
        self.addr_row = (self.addr[0], cfg.port + 4)
        self.rtp = cfg.proto in ("rtp", "rtp-fec")
        self.fec = FecEncoder(cfg.fec_cols, cfg.fec_rows, cfg.fec_2d) if cfg.proto == "rtp-fec" else None
        self.seq = random.randint(0, 0xFFFF)
        self.ts_base = random.randint(0, 0xFFFFFFFF)
        self.ssrc = cfg.ssrc if cfg.ssrc is not None else int.from_bytes(os.urandom(4), "big")
        self.pending_col = []
        self.stats = {"datagrams": 0, "bytes": 0, "fec_col": 0, "fec_row": 0, "errors": 0}
        self._last_err = 0.0
        self.log("Saída de rede: %s" % cfg.describe())

    def _sendto(self, data, addr):
        try:
            self.sock.sendto(data, addr)
            return True
        except OSError as exc:
            self.stats["errors"] += 1
            now = time.monotonic()
            if now - self._last_err > 5:
                self._last_err = now
                self.log("Erro ao enviar para %s:%d — %s" % (addr[0], addr[1], exc))
            return False

    def send(self, pkts, media_t):
        payload = b"".join(pkts)
        st = self.stats
        if self.rtp:
            ts = (self.ts_base + int(media_t * RTP_CLOCK)) & 0xFFFFFFFF
            data = rtp_header(self.seq, ts, self.ssrc, RTP_PT_MP2T) + payload
            self._sendto(data, self.addr)
            if self.fec is not None:
                cols, rows = self.fec.push(self.seq, ts, payload)
                self.pending_col.extend(cols)
                for r in rows:
                    if self._sendto(r, self.addr_row):
                        st["fec_row"] += 1
                # intercala: no máximo um pacote de coluna por datagrama de mídia
                if self.pending_col:
                    if self._sendto(self.pending_col.pop(0), self.addr_col):
                        st["fec_col"] += 1
            self.seq = (self.seq + 1) & 0xFFFF
        else:
            data = payload
            self._sendto(data, self.addr)
        st["datagrams"] += 1
        st["bytes"] += len(data)

    def close(self):
        for c in self.pending_col:
            if self._sendto(c, self.addr_col):
                self.stats["fec_col"] += 1
        self.pending_col = []
        try:
            self.sock.close()
        except OSError:
            pass
