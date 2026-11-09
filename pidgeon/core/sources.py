"""Fontes de entrada: arquivo, UDP/RTP (multicast/unicast, SSM), HTTP e stdin."""

import fcntl
import os
import selectors
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request

SIOCGIFADDR = 0x8915
IP_MULTICAST_ALL = 49
IP_ADD_SOURCE_MEMBERSHIP = 39


def list_interfaces():
    """[(nome, ipv4)] das interfaces com endereço IPv4."""
    out = []
    try:
        names = [n for _, n in socket.if_nameindex()]
    except OSError:
        names = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for n in names:
            try:
                r = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", n.encode()[:15]))
                out.append((n, socket.inet_ntoa(r[20:24])))
            except OSError:
                pass
    finally:
        s.close()
    return out


def iface_ip(iface):
    """Aceita um IP ou um nome de interface (eth0, enp3s0…) e devolve o IPv4."""
    if not iface:
        return "0.0.0.0"
    try:
        socket.inet_aton(iface)
        return iface
    except OSError:
        pass
    for n, ip in list_interfaces():
        if n == iface:
            return ip
    raise ValueError("Interface de rede '%s' não encontrada" % iface)


def is_multicast(ip):
    try:
        return 224 <= int(ip.split(".")[0]) <= 239
    except ValueError:
        return False


class SourceSpec:
    """Interpreta a descrição de entrada.

    Exemplos:
      /caminho/arquivo.ts            arquivo (.ts, .trp, .bts, .m2ts…)
      udp://239.1.1.1:1234           multicast (RTP detectado automaticamente)
      udp://@239.1.1.1:1234          idem (estilo VLC)
      rtp://239.1.1.1:5000           força RTP
      udp://10.0.0.5@232.1.1.1:5000  SSM: fonte 10.0.0.5
      udp://:1234  ou udp://0.0.0.0:1234   unicast em todas as interfaces
      udp://239.1.1.1:1234?iface=enp3s0&buffer=16
      rtp://239.1.1.1:5000?fec=on    RTP + recuperação FEC SMPTE 2022-1 (portas +2 e +4)
      http://servidor/stream.ts
      -                              stdin
    """

    def __init__(self, text):
        self.text = text.strip()
        self.kind = "file"
        self.path = None
        self.group = None
        self.port = None
        self.source = None
        self.iface = None
        self.rtp = "auto"
        self.buffer_mb = 16
        self.fec = False
        self.url = None
        t = self.text
        low = t.lower()
        if t == "-":
            self.kind = "stdin"
        elif low.startswith(("udp://", "rtp://")):
            self.kind = "udp"
            u = urllib.parse.urlsplit(t)
            if low.startswith("rtp://"):
                self.rtp = "on"
            q = urllib.parse.parse_qs(u.query)
            netloc = u.netloc
            if "@" in netloc:
                src, netloc = netloc.rsplit("@", 1)
                self.source = src or None
            host, _, port = netloc.rpartition(":")
            if not port:
                raise ValueError("Informe a porta: %s" % t)
            self.group = host or "0.0.0.0"
            self.port = int(port)
            self.iface = (q.get("iface") or q.get("localaddr") or q.get("interface") or [None])[0]
            if "source" in q:
                self.source = q["source"][0]
            if "rtp" in q:
                self.rtp = q["rtp"][0]
            if "buffer" in q:
                self.buffer_mb = int(q["buffer"][0])
            if "fec" in q:
                self.fec = q["fec"][0].lower() not in ("0", "off", "nao", "não", "false")
                if self.fec and self.rtp == "auto":
                    self.rtp = "on"

        elif low.startswith(("http://", "https://")):
            self.kind = "http"
            self.url = t
        else:
            self.kind = "file"
            self.path = t[7:] if low.startswith("file://") else t
            self.path = os.path.expanduser(self.path)

    def label(self):
        if self.kind == "udp":
            proto = "RTP" if self.rtp == "on" else "UDP"
            mode = "multicast" if is_multicast(self.group) else "unicast"
            s = "%s %s %s:%d" % (proto, mode, self.group, self.port)
            if self.source:
                s += " (SSM fonte %s)" % self.source
            if self.fec:
                s += " + FEC 2022-1 (portas %d/%d)" % (self.port + 2, self.port + 4)
            if self.iface:
                s += " via %s" % self.iface
            return s
        if self.kind == "file":
            return os.path.basename(self.path)
        if self.kind == "http":
            return self.url
        return "stdin"


class BaseSource(threading.Thread):
    def __init__(self, spec, analyzer):
        super().__init__(daemon=True, name="pidgeon-source")
        self.spec = spec
        self.an = analyzer
        self.stop_ev = threading.Event()
        self.bytes = 0
        self.error = None
        self.finished = False
        self.signal = None

    def stop(self):
        self.stop_ev.set()

    def _signal(self, ok):
        if ok != self.signal:
            self.signal = ok
            self.an.log("Sinal de entrada detectado" if ok else "Perda de sinal de entrada",
                        "info" if ok else "error")

    def run(self):
        try:
            self.loop()
        except Exception as exc:
            self.error = str(exc)
            self.an.log("Erro na entrada: %s" % exc, "error")
        finally:
            self.finished = True


class FileSource(BaseSource):
    def __init__(self, spec, analyzer, realtime=True, loop=False, chunk_packets=140):
        super().__init__(spec, analyzer)
        self.realtime = realtime
        self.loop_file = loop
        self.chunk = 204 * chunk_packets
        self.size = os.path.getsize(spec.path)
        self.pos = 0
        self.paused = threading.Event()

    def _raw_rate(self):
        an = self.an
        ps = an.packet_size or 188
        if an.mux_rate:
            return an.mux_rate * ps / 188.0
        return 32.507936e6 if ps == 204 else 20e6

    def loop(self):
        self.an.log("Abrindo arquivo %s (%.1f MB)" % (self.spec.path, self.size / 1e6))
        while not self.stop_ev.is_set():
            with open(self.spec.path, "rb") as f:
                start = time.monotonic()
                sent = 0
                rate = self._raw_rate()
                self._signal(True)
                while not self.stop_ev.is_set():
                    while self.paused.is_set() and not self.stop_ev.is_set():
                        time.sleep(0.05)
                        start = time.monotonic()
                        sent = 0
                    data = f.read(self.chunk)
                    if not data:
                        break
                    self.bytes += len(data)
                    self.pos = f.tell()
                    self.an.push_bytes(data, time.monotonic())
                    if self.realtime:
                        sent += len(data)
                        r = self._raw_rate()
                        if abs(r - rate) / rate > 0.02:
                            rate, start, sent = r, time.monotonic(), 0
                            continue
                        ahead = start + sent * 8 / rate - time.monotonic()
                        if ahead > 0:
                            time.sleep(min(ahead, 0.5))
                        elif ahead < -1.0:
                            start, sent = time.monotonic(), 0
            if not self.loop_file:
                break
            self.an.log("Fim do arquivo — reiniciando (loop)")
            self.an.mark_discontinuity()
        self.an.log("Fim do arquivo (%d bytes lidos)" % self.bytes)


class UdpSource(BaseSource):
    def __init__(self, spec, analyzer):
        super().__init__(spec, analyzer)
        self.datagrams = 0
        self.rtp_detected = None
        self.rtp_seq = None
        self.rtp_lost = 0
        self.rtp_pt = None
        self.sock = None

    def _open(self, port=None, quiet=False):
        sp = self.spec
        port = port or sp.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, sp.buffer_mb * 1024 * 1024)
        except OSError:
            pass
        local_ip = iface_ip(sp.iface)
        mc = is_multicast(sp.group)
        if mc:
            sock.bind((sp.group, port))
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
            except OSError:
                pass
            if sp.source:
                # struct ip_mreq_source (Linux): multiaddr, interface, sourceaddr
                mreq = (socket.inet_aton(sp.group) + socket.inet_aton(local_ip) +
                        socket.inet_aton(sp.source))
                sock.setsockopt(socket.IPPROTO_IP, IP_ADD_SOURCE_MEMBERSHIP, mreq)
            else:
                mreq = socket.inet_aton(sp.group) + socket.inet_aton(local_ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            if not quiet:
                self.an.log("IGMP join %s%s na interface %s" % (
                    sp.group, (" (SSM fonte %s)" % sp.source) if sp.source else "", local_ip))
        else:
            bind_ip = sp.group if sp.group not in ("", "0.0.0.0") else local_ip
            sock.bind((bind_ip, port))
            if not quiet:
                self.an.log("Escutando UDP unicast em %s:%d" % (bind_ip, port))
        sock.settimeout(1.0)
        return sock

    def _strip_rtp(self, d):
        mode = self.spec.rtp
        if mode == "off":
            return d
        if d[0] == 0x47 and mode == "auto":
            if self.rtp_detected is None:
                self.rtp_detected = False
                self.an.log("Encapsulamento: UDP puro (TS direto no datagrama)")
            return d
        if (d[0] & 0xC0) != 0x80 or len(d) < 12:
            return d
        cc = d[0] & 0x0F
        hdr = 12 + 4 * cc
        if d[0] & 0x10 and len(d) >= hdr + 4:
            hdr += 4 + ((d[hdr + 2] << 8) | d[hdr + 3]) * 4
        end = len(d)
        if d[0] & 0x20:
            end -= d[-1]
        seq = (d[2] << 8) | d[3]
        if self.rtp_detected is not True:
            self.rtp_detected = True
            self.rtp_pt = d[1] & 0x7F
            self.an.log("Encapsulamento: RTP (payload type %d)" % self.rtp_pt)
        if self.rtp_seq is not None:
            gap = (seq - self.rtp_seq - 1) & 0xFFFF
            if 0 < gap < 0x8000:
                self.rtp_lost += gap
                self.an.log("RTP: %d datagrama(s) perdido(s) (seq %d -> %d)" % (gap, self.rtp_seq, seq), "warn")
        self.rtp_seq = seq
        return d[hdr:end]

    def loop(self):
        if self.spec.fec:
            return self._loop_fec()
        self.sock = self._open()
        buf = bytearray(65536)
        view = memoryview(buf)
        try:
            while not self.stop_ev.is_set():
                try:
                    n = self.sock.recv_into(buf)
                except socket.timeout:
                    self._signal(False)
                    continue
                if n <= 0:
                    continue
                self._signal(True)
                self.datagrams += 1
                self.bytes += n
                data = self._strip_rtp(bytes(view[:n]))
                self.an.push_bytes(data, time.monotonic())
        finally:
            self.sock.close()


    def _loop_fec(self):
        """RTP com FEC SMPTE 2022-1: mídia na porta, colunas na porta+2 e linhas na porta+4."""
        from .fec import RtpFecReceiver, FecPacket, parse_rtp
        sp = self.spec
        self.sock = self._open()
        fec_socks = [self._open(sp.port + 2, quiet=True), self._open(sp.port + 4, quiet=True)]
        self.fec_rx = rx = RtpFecReceiver()
        self.an.log("FEC SMPTE 2022-1 ativo: colunas na porta %d, linhas na porta %d" % (sp.port + 2, sp.port + 4))
        for s in [self.sock] + fec_socks:
            s.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(self.sock, selectors.EVENT_READ, "m")
        for s in fec_socks:
            sel.register(s, selectors.EVENT_READ, "f")
        last_report = time.monotonic()
        reported = (0, 0)
        try:
            while not self.stop_ev.is_set():
                ev = sel.select(1.0)
                if not ev:
                    self._signal(False)
                    continue
                # lê primeiro toda a mídia disponível, depois o FEC (evita processar o FEC antes
                # dos datagramas que ele protege e que ainda estão no buffer do socket)
                datagrams = []
                for key, _ in sorted(ev, key=lambda e: e[0].data != "m"):
                    for _ in range(256):
                        try:
                            datagrams.append((key.data, key.fileobj.recv(65536)))
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:
                            break
                for kind, d in datagrams:
                    if kind == "m":
                        self._signal(True)
                        self.datagrams += 1
                        self.bytes += len(d)
                        r = parse_rtp(d)
                        if r is None:
                            self.an.push_bytes(d, time.monotonic())
                            continue
                        if self.rtp_detected is not True:
                            self.rtp_detected = True
                            self.rtp_pt = r[2]
                            self.an.log("Encapsulamento: RTP (payload type %d) com FEC" % self.rtp_pt)
                        out = rx.media(r[0], r[2], r[1], r[4])
                    else:
                        f = FecPacket.parse(d)
                        out = rx.fec(f) if f else []
                    if out:
                        self.an.push_bytes(b"".join(out), time.monotonic())
                self.rtp_lost = rx.lost
                now = time.monotonic()
                if now - last_report > 5 and (rx.recovered, rx.lost) != reported:
                    last_report = now
                    reported = (rx.recovered, rx.lost)
                    self.an.log("FEC: %d datagrama(s) recuperado(s), %d perdido(s) sem recuperação "
                                "(L=%s D=%s)" % (rx.recovered, rx.lost, rx.dec.L, rx.dec.D),
                                "warn" if rx.lost else "info")
        finally:
            self.sock.close()
            for s in fec_socks:
                s.close()


class HttpSource(BaseSource):
    def loop(self):
        req = urllib.request.Request(self.spec.url, headers={"User-Agent": "PIDgeon"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self._signal(True)
            while not self.stop_ev.is_set():
                data = r.read(65536)
                if not data:
                    break
                self.bytes += len(data)
                self.an.push_bytes(data, time.monotonic())


class StdinSource(BaseSource):
    def loop(self):
        f = sys.stdin.buffer
        self._signal(True)
        while not self.stop_ev.is_set():
            data = f.read1(65536) if hasattr(f, "read1") else f.read(65536)
            if not data:
                break
            self.bytes += len(data)
            self.an.push_bytes(data, time.monotonic())


def open_source(text, analyzer, realtime=True, loop=False):
    spec = SourceSpec(text)
    if spec.kind == "file":
        if not os.path.isfile(spec.path):
            raise FileNotFoundError(spec.path)
        return FileSource(spec, analyzer, realtime=realtime, loop=loop)
    if spec.kind == "udp":
        return UdpSource(spec, analyzer)
    if spec.kind == "http":
        return HttpSource(spec, analyzer)
    return StdinSource(spec, analyzer)
