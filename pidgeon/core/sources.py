"""Fontes de entrada: arquivo, UDP/RTP (multicast/unicast, SSM), HTTP e stdin."""

import os
import selectors
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import netif

IP_MULTICAST_ALL = 49
IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)
IP_ADD_SOURCE_MEMBERSHIP = 39
IP_DROP_SOURCE_MEMBERSHIP = 40


def list_interfaces():
    """[(nome, ipv4)] das interfaces com IPv4 (relê o kernel a cada chamada)."""
    return netif.list_interfaces()


def iface_ip(iface):
    """Aceita um IP ou um nome de interface (eth0, enp3s0…) e devolve o IPv4."""
    if not iface:
        return "0.0.0.0"
    if netif.is_ipv4(iface):
        return iface
    i = netif.find(iface)
    if i is None:
        raise ValueError("O adaptador de rede '%s' não existe nesta máquina" % iface)
    if not i.ipv4:
        raise ValueError("O adaptador '%s' está sem endereço IPv4" % iface)
    return i.ipv4


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
    """UDP/RTP unicast ou multicast (ASM e SSM), com FEC 2022-1 opcional.

    A interface é escolhida pelo NOME do adaptador (ou automática) e o join é feito pelo índice da
    interface. A cada 2 s o PIDgeon relê os adaptadores: se a placa trocar, sumir, voltar, mudar de IP
    ou recuperar o cabo, o join é refeito sozinho. No automático, depois que o tráfego chega, ele trava
    no adaptador por onde o fluxo veio (IP_PKTINFO) e sai dos outros grupos.
    """

    WATCH_PERIOD = 2.0

    def __init__(self, spec, analyzer):
        super().__init__(spec, analyzer)
        self.datagrams = 0
        self.rtp_detected = None
        self.rtp_seq = None
        self.rtp_lost = 0
        self.rtp_pt = None
        self.sock = None
        self.socks = []           # [(socket, porta)]
        self.joined = []          # NetIf com join ativo
        self.locked = None        # adaptador por onde o fluxo está chegando (automático)
        self.mode = "auto"
        self.iface_note = ""
        self.prepared = False
        self.multicast = is_multicast(spec.group)
        self._snap = None
        self._last_watch = 0.0
        self._carrier = {}
        self.rejoins = 0

    # ------------------------------------------------------------ abertura
    def _make_socket(self, port, bind_ip):
        sp = self.spec
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
        try:
            sock.bind((bind_ip, port))
        except PermissionError:
            sock.close()
            raise PermissionError("Sem permissão para abrir a porta %d: no Linux portas abaixo de 1024 exigem "
                                  "root. Use uma porta ≥ 1024." % port)
        except OSError as exc:
            sock.close()
            if exc.errno == 99:   # EADDRNOTAVAIL
                raise OSError("O endereço %s não pertence a nenhum adaptador desta máquina" % bind_ip)
            raise
        if self.multicast:
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
            except OSError:
                pass
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
            except OSError:
                pass
        sock.settimeout(0.5)
        return sock

    def _ports(self):
        sp = self.spec
        return [sp.port] + ([sp.port + 2, sp.port + 4] if sp.fec else [])

    def _join(self, ifs, verb="IGMP join"):
        sp = self.spec
        ok = []
        errors = []
        for i in ifs:
            try:
                for sock, _port in self.socks:
                    if sp.source:
                        if not i.ipv4:
                            raise OSError("o adaptador não tem IPv4 (necessário para SSM)")
                        mreq = socket.inet_aton(sp.group) + socket.inet_aton(i.ipv4) + socket.inet_aton(sp.source)
                        sock.setsockopt(socket.IPPROTO_IP, IP_ADD_SOURCE_MEMBERSHIP, mreq)
                    else:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, netif.mreqn(sp.group, i.index))
                ok.append(i)
            except OSError as exc:
                if exc.errno == 98:          # EADDRINUSE: já inscrito
                    ok.append(i)
                    continue
                errors.append("%s: %s" % (i.name, exc.strerror or exc))
        for i in ok:
            self.an.log("%s %s%s em %s (%s)" % (verb, sp.group, (" (SSM fonte %s)" % sp.source) if sp.source else "",
                                                i.name, i.ipv4 or "sem IPv4"))
        for e in errors:
            self.an.log("Falha no join em %s" % e, "warn")
        return ok, errors

    def _leave(self, ifs):
        sp = self.spec
        for i in ifs:
            for sock, _port in self.socks:
                try:
                    if sp.source and i.ipv4:
                        mreq = socket.inet_aton(sp.group) + socket.inet_aton(i.ipv4) + socket.inet_aton(sp.source)
                        sock.setsockopt(socket.IPPROTO_IP, IP_DROP_SOURCE_MEMBERSHIP, mreq)
                    else:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, netif.mreqn(sp.group, i.index))
                except OSError:
                    pass

    def prepare(self):
        """Abre os sockets e faz o join já na chamada (erros aparecem na hora, com explicação)."""
        if self.prepared:
            return
        sp = self.spec
        if self.multicast:
            r = netif.resolve(sp.iface, sp.group)
            self.mode = r.mode
            self.iface_note = r.note
            if r.missing and not r.ifaces:
                if r.mode == "fixed":
                    raise OSError("Não foi possível usar a interface: %s." % r.note)
                raise OSError("Nenhum adaptador de rede conectado com IPv4. Confira o cabo/Wi-Fi e o "
                              "endereço do adaptador (Ajuda → Diagnóstico de rede).")
            if r.missing:
                self.an.log("Atenção: %s — o PIDgeon tenta o join e refaz quando o adaptador voltar" % r.note, "warn")
            elif r.note and "não existe mais" in r.note:
                self.an.log(r.note, "warn")
            self.socks = [(self._make_socket(p, sp.group), p) for p in self._ports()]
            ok, errors = self._join(r.ifaces)
            if not ok:
                self.close()
                raise OSError("O join IGMP em %s falhou (%s). Confira o adaptador escolhido ou use o automático."
                              % (sp.group, "; ".join(errors)))
            self.joined = ok
        else:
            bind_ip = sp.group if sp.group not in ("", "0.0.0.0") else "0.0.0.0"
            if bind_ip == "0.0.0.0" and sp.iface and sp.iface not in ("auto", "all", "todas"):
                i = netif.find(sp.iface)
                if i is None:
                    if not netif.is_ipv4(sp.iface):
                        raise OSError("O adaptador '%s' não existe nesta máquina" % sp.iface)
                    self.an.log("O IP %s não existe mais nesta máquina — escutando em todos os adaptadores"
                                % sp.iface, "warn")
                elif i.ipv4:
                    bind_ip = i.ipv4
                    self.mode = "fixed"
                    self.joined = [i]
            self.socks = [(self._make_socket(p, bind_ip), p) for p in self._ports()]
            self.iface_note = "todos os adaptadores" if bind_ip == "0.0.0.0" else bind_ip
            self.an.log("Escutando UDP unicast em %s:%d" % (bind_ip, sp.port))
        self.sock = self.socks[0][0]
        self._snap = netif.snapshot()
        self._carrier = {i.name: i.carrier for i in netif.interfaces()}
        self._last_watch = time.monotonic()
        self.prepared = True

    def close(self):
        for sock, _p in self.socks:
            try:
                sock.close()
            except OSError:
                pass
        self.socks = []

    # ------------------------------------------------------------ vigia de adaptadores
    def iface_status(self):
        if not self.multicast:
            return self.iface_note or "-"
        if self.locked is not None:
            return "%s (%s)%s" % (self.locked.name, self.locked.ipv4 or "sem IPv4",
                                  " · automático" if self.mode == "auto" else "")
        if self.joined:
            return ", ".join(i.name for i in self.joined) + (" · automático" if self.mode == "auto" else "")
        return "aguardando adaptador"

    def _watch(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_watch < self.WATCH_PERIOD:
            return
        self._last_watch = now
        snap = netif.snapshot()
        if snap == self._snap and not force:
            return
        self._snap = snap
        ifs = netif.interfaces()
        cur = {i.name: i for i in ifs}
        # eventos de adaptador
        for i in ifs:
            before = self._carrier.get(i.name)
            if before is None:
                self.an.log("Adaptador %s apareceu (%s)" % (i.name, i.ipv4 or "sem IPv4"))
            elif before != i.carrier:
                self.an.log("Adaptador %s %s" % (i.name, "conectado" if i.carrier else "sem cabo / desconectado"),
                            "info" if i.carrier else "warn")
        for n in self._carrier:
            if n not in cur:
                self.an.log("Adaptador %s foi removido" % n, "warn")
        carrier_up = {n for n, i in cur.items() if i.carrier and not self._carrier.get(n, False)}
        self._carrier = {n: i.carrier for n, i in cur.items()}
        sp = self.spec
        if not self.multicast:
            if self.mode == "fixed" and self.joined:
                old = self.joined[0]
                new = cur.get(old.name)
                if new is not None and new.ipv4 and new.ipv4 != old.ipv4:
                    self.an.log("%s mudou de IP (%s → %s) — reabrindo a escuta" % (old.name, old.ipv4, new.ipv4))
                    self.close()
                    self.socks = [(self._make_socket(p, new.ipv4), p) for p in self._ports()]
                    self.sock = self.socks[0][0]
                    self.joined = [new]
                    self.iface_note = new.ipv4
            return
        # multicast: decide o conjunto desejado
        if self.locked is not None:
            li = cur.get(self.locked.name)
            if li is not None and li.index == self.locked.index and li.up and (li.carrier or self.mode == "fixed"):
                desired = [li]
            else:
                self.an.log("O adaptador %s por onde o fluxo chegava não está mais disponível — procurando de novo"
                            % self.locked.name, "warn")
                self.locked = None
                desired = netif.resolve(sp.iface, sp.group).ifaces
        else:
            r = netif.resolve(sp.iface, sp.group)
            desired = r.ifaces
            self.iface_note = r.note
        old = {(i.name, i.index): i for i in self.joined}
        new = {(i.name, i.index): i for i in desired}
        gone = [i for k, i in old.items() if k not in new]
        added = [i for k, i in new.items() if k not in old]
        # cabo voltou num adaptador já inscrito: refaz o join para o switch (IGMP snooping) reaprender
        renew = [i for k, i in new.items() if k in old and i.name in carrier_up]
        if gone:
            self._leave(gone)
        if renew:
            self._leave(renew)
        joined = [i for k, i in old.items() if k in new and i not in renew]
        if added or renew:
            ok, _ = self._join(added + renew, "IGMP join refeito:" if renew else "IGMP join")
            joined += ok
            self.rejoins += 1
        self.joined = [new[(i.name, i.index)] for i in joined if (i.name, i.index) in new]
        if not self.joined:
            self.an.log("Nenhum adaptador disponível para receber %s — aguardando" % sp.group, "warn")

    def _lock(self, ifindex):
        if self.locked is not None or len(self.joined) < 2:
            if self.locked is None and len(self.joined) == 1:
                self.locked = self.joined[0]
            return
        hit = [i for i in self.joined if i.index == ifindex]
        if not hit:
            return
        self.locked = hit[0]
        others = [i for i in self.joined if i is not hit[0]]
        if others:
            self._leave(others)
            self.an.log("Fluxo chegando por %s — saindo do grupo em %s" % (hit[0].name, ", ".join(i.name for i in others)))
        self.joined = [hit[0]]

    def _recv(self, sock, buf):
        """recv com o índice do adaptador de chegada (IP_PKTINFO) enquanto o automático não travou."""
        if self.multicast and self.locked is None:
            n, anc, _flags, _addr = sock.recvmsg_into([buf], 64)
            for level, typ, data in anc:
                if level == socket.IPPROTO_IP and typ == IP_PKTINFO and len(data) >= 4:
                    self._lock(struct.unpack("@i", data[:4])[0])
            return n
        return sock.recv_into(buf)

    # ------------------------------------------------------------ RTP
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

    # ------------------------------------------------------------ laços
    def loop(self):
        self.prepare()
        if self.spec.fec:
            return self._loop_fec()
        buf = bytearray(65536)
        view = memoryview(buf)
        try:
            while not self.stop_ev.is_set():
                self._watch()
                try:
                    n = self._recv(self.sock, buf)
                except socket.timeout:
                    self._signal(False)
                    continue
                except OSError as exc:
                    if exc.errno in (9, 88):   # socket fechado durante a troca de adaptador
                        continue
                    raise
                if n <= 0:
                    continue
                self._signal(True)
                self.datagrams += 1
                self.bytes += n
                data = self._strip_rtp(bytes(view[:n]))
                self.an.push_bytes(data, time.monotonic())
        finally:
            self.close()

    def _loop_fec(self):
        """RTP com FEC SMPTE 2022-1: mídia na porta, colunas na porta+2 e linhas na porta+4."""
        from .fec import RtpFecReceiver, FecPacket, parse_rtp
        sp = self.spec
        self.fec_rx = rx = RtpFecReceiver()
        self.an.log("FEC SMPTE 2022-1 ativo: colunas na porta %d, linhas na porta %d" % (sp.port + 2, sp.port + 4))
        buf = bytearray(65536)
        last_report = time.monotonic()
        reported = (0, 0)
        sel = None
        socks_id = None
        try:
            while not self.stop_ev.is_set():
                self._watch()
                if socks_id != [id(s) for s, _ in self.socks]:
                    if sel is not None:
                        sel.close()
                    sel = selectors.DefaultSelector()
                    for i, (s, _p) in enumerate(self.socks):
                        s.setblocking(False)
                        sel.register(s, selectors.EVENT_READ, "m" if i == 0 else "f")
                    socks_id = [id(s) for s, _ in self.socks]
                ev = sel.select(0.5)
                if not ev:
                    self._signal(False)
                    continue
                # lê primeiro toda a mídia disponível, depois o FEC (evita processar o FEC antes
                # dos datagramas que ele protege e que ainda estão no buffer do socket)
                datagrams = []
                for key, _ in sorted(ev, key=lambda e: e[0].data != "m"):
                    for _ in range(256):
                        try:
                            n = self._recv(key.fileobj, buf) if key.data == "m" else key.fileobj.recv_into(buf)
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:
                            break
                        datagrams.append((key.data, bytes(buf[:n])))
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
            if sel is not None:
                sel.close()
            self.close()


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
