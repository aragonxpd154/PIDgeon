"""Gravação (mux completo, serviço com tabelas, PIDs) e filtro de serviço para o player."""

import collections
import os
import threading
import time

from .crc import crc32_mpeg
from .psi import SectionAssembler, section_to_packets

SI_KEEP = (0x10, 0x11, 0x12, 0x14, 0x23, 0x24, 0x26, 0x27, 0x28, 0x29)


def to188(p, size):
    if size == 188:
        return p
    if size == 192:
        return p[4:]
    return p[:188]


def build_pat(tsid, programs, version=0):
    body = bytearray()
    for pn, pid in sorted(programs.items()):
        body += bytes((pn >> 8, pn & 0xFF, 0xE0 | (pid >> 8), pid & 0xFF))
    slen = 5 + len(body) + 4
    sec = bytearray((0x00, 0xB0 | (slen >> 8), slen & 0xFF, (tsid >> 8) & 0xFF, tsid & 0xFF,
                     0xC1 | ((version & 0x1F) << 1), 0x00, 0x00))
    sec += body
    crc = crc32_mpeg(sec)
    sec += crc.to_bytes(4, "big")
    return bytes(sec)


def filter_pmt(section, keep_pids):
    """Reescreve uma PMT mantendo apenas os ES em keep_pids."""
    s = section
    pil = ((s[10] & 0x0F) << 8) | s[11]
    head = bytearray(s[:12 + pil])
    i = 12 + pil
    end = len(s) - 4
    loop = bytearray()
    while i + 5 <= end:
        pid = ((s[i + 1] & 0x1F) << 8) | s[i + 2]
        el = ((s[i + 3] & 0x0F) << 8) | s[i + 4]
        if pid in keep_pids:
            loop += s[i:i + 5 + el]
        i += 5 + el
    sec = head + loop
    slen = len(sec) - 3 + 4
    sec[1] = (sec[1] & 0xF0) | (slen >> 8)
    sec[2] = slen & 0xFF
    sec += crc32_mpeg(sec).to_bytes(4, "big")
    return bytes(sec)


class ServiceFilter:
    """Extrai um serviço do mux gerando TS 188 válido (PAT reescrita, PMT, SI, PCR, ES).

    sink(list_of_188_bytes) recebe os pacotes produzidos.
    only_pids: se informado, mantém só esses ES (e reescreve a PMT) — usado no player.
    """

    def __init__(self, analyzer, sid, sink, only_pids=None, include_si=True):
        self.an = analyzer
        self.sid = sid
        self.sink = sink
        self.only = set(only_pids) if only_pids else None
        self.include_si = include_si
        self.pat_cc = 0
        self.pmt_cc = 0
        self.pmt_asm = SectionAssembler()
        self._pids = set()
        self._pmt_pid = None
        self._ver = None
        self._last_pat = 0.0

    def _refresh(self):
        s = self.an.services.get(self.sid)
        if s is None or s.pmt_pid is None:
            return False
        key = (s.pmt_pid, s.pmt_version, len(s.streams))
        if key != self._ver:
            self._ver = key
            self._pmt_pid = s.pmt_pid
            pids = set()
            for es in s.streams:
                if self.only is None or es["pid"] in self.only:
                    pids.add(es["pid"])
            if s.pcr_pid is not None:
                pids.add(s.pcr_pid)
            self._pids = pids
        return True

    def _pat_packets(self):
        progs = {self.sid: self._pmt_pid}
        if self.include_si:
            progs[0] = 0x10
        sec = build_pat(self.an.tsid or 1, progs)
        pk, self.pat_cc = section_to_packets(0, sec, self.pat_cc)
        return pk

    def __call__(self, pkts, size, t):
        if not self._refresh():
            return
        out = []
        pmt_pid = self._pmt_pid
        pids = self._pids
        si = self.include_si
        off = 4 if size == 192 else 0
        for p in pkts:
            pid = ((p[off + 1] & 0x1F) << 8) | p[off + 2]
            if pid == 0:
                if p[off + 1] & 0x40:
                    out.extend(self._pat_packets())
                    self._last_pat = t
            elif pid == pmt_pid:
                if self.only is None:
                    out.append(to188(p, size))
                else:
                    ts = to188(p, size)
                    afc = (ts[3] >> 4) & 3
                    if not afc & 1:
                        continue
                    ps = 4 + (1 + ts[4] if afc & 2 else 0)
                    for sec in self.pmt_asm.feed(ts[ps:], ts[1] & 0x40):
                        if sec[0] == 0x02 and crc32_mpeg(sec) == 0:
                            pk, self.pmt_cc = section_to_packets(pmt_pid, filter_pmt(sec, pids), self.pmt_cc)
                            out.extend(pk)
            elif pid in pids or (si and pid in SI_KEEP):
                out.append(to188(p, size))
        if out:
            self.sink(out)


def _split_name(path, n):
    if n == 0:
        return path
    base, ext = os.path.splitext(path)
    return "%s_%03d%s" % (base, n, ext or ".ts")


class Recorder:
    """Grava pacotes do analisador em disco.

    mode: 'full' (mux inteiro), 'service' (serviço + tabelas), 'pids' (lista de PIDs)
    keep_raw: no modo 'full', mantém o tamanho original (BTS 204 / M2TS 192)
    """

    def __init__(self, analyzer, path, mode="full", sid=None, pids=None, keep_raw=True,
                 split_seconds=0, split_mb=0):
        self.an = analyzer
        self.path = path
        self.mode = mode
        self.keep_raw = keep_raw
        self.split_s = split_seconds
        self.split_b = int(split_mb * 1024 * 1024)
        self.pids = set(pids or [])
        self.bytes = 0
        self.file_bytes = 0
        self.packets = 0
        self.file_index = 0
        self.started = time.time()
        self.file_started = self.started
        self.files = []
        self.lock = threading.Lock()
        self.f = None
        self.closed = False
        self._open()
        self.filter = ServiceFilter(analyzer, sid, self._write) if mode == "service" else None

    def _open(self):
        name = _split_name(self.path, self.file_index)
        os.makedirs(os.path.dirname(os.path.abspath(name)), exist_ok=True)
        self.f = open(name, "wb", buffering=1024 * 1024)
        self.files.append(name)
        self.file_bytes = 0
        self.file_started = time.time()
        self.an.log("Gravando em %s" % name)

    def _maybe_split(self):
        if (self.split_b and self.file_bytes >= self.split_b) or \
           (self.split_s and time.time() - self.file_started >= self.split_s):
            self.f.close()
            self.file_index += 1
            self._open()

    def _write(self, pkts):
        with self.lock:
            if self.closed:
                return
            data = b"".join(pkts)
            self.f.write(data)
            self.bytes += len(data)
            self.file_bytes += len(data)
            self.packets += len(pkts)
            self._maybe_split()

    def __call__(self, pkts, size, t):
        if self.mode == "service":
            self.filter(pkts, size, t)
        elif self.mode == "full":
            if self.keep_raw or size == 188:
                self._write(pkts)
            else:
                self._write([to188(p, size) for p in pkts])
        else:
            off = 4 if size == 192 else 0
            sel = self.pids
            self._write([to188(p, size) for p in pkts
                         if (((p[off + 1] & 0x1F) << 8) | p[off + 2]) in sel])

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.f.close()
                self.an.log("Gravação encerrada: %.1f MB em %d arquivo(s)" % (self.bytes / 1e6, len(self.files)))


class BlockingPipe:
    """Fila de bytes thread-safe com leitura bloqueante (alimenta o decodificador)."""

    def __init__(self, max_bytes=8 * 1024 * 1024):
        self.q = collections.deque()
        self.size = 0
        self.max = max_bytes
        self.cv = threading.Condition()
        self.closed = False
        self.dropped = 0

    def write_packets(self, pkts):
        data = b"".join(pkts)
        with self.cv:
            if self.closed:
                return
            if self.size + len(data) > self.max:
                self.dropped += len(data)
                return
            self.q.append(data)
            self.size += len(data)
            self.cv.notify()

    def read(self, n=-1):
        with self.cv:
            while not self.q and not self.closed:
                self.cv.wait(0.5)
            if not self.q:
                return b""
            if n is None or n < 0:
                n = self.size
            out = bytearray()
            while self.q and len(out) < n:
                chunk = self.q.popleft()
                need = n - len(out)
                if len(chunk) > need:
                    out += chunk[:need]
                    self.q.appendleft(chunk[need:])
                else:
                    out += chunk
            self.size -= len(out)
            return bytes(out)

    def readable(self):
        return True

    def seekable(self):
        return False

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
