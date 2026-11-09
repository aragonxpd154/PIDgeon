"""Geração de BTS 204, BTS 188, TS 188 e TS 204 a partir de uma gravação.

Fluxo:  arquivo ─► Framer ─► carimbo de tempo (posição no BTS ou PCR) ─► conversão ─► (t, pacote)
                                                                          │
                              ┌───────────────────────────────────────────┤
                              ▼                                           ▼
                       arquivo de saída                     UDP puro / RTP / RTP + FEC 2022-1
                                                             (unicast ou multicast, na taxa real)

Formatos de saída
  bts204  BTS ISDB-Tb completo: 188 + 8 bytes de informação ISDB-T + 8 bytes (zeros ou RS(204,196))
  bts188  o mesmo BTS sem os 16 bytes finais (mantém nulos e IIP, taxa constante de 29,958 Mbps)
  ts188   TS MPEG-2 comum (sem IIP; nulos opcionais)
  ts204   TS 188 + paridade Reed-Solomon RS(204,188)
"""

import os
import threading
import time
from dataclasses import dataclass, field

from .analyzer import Analyzer
from .framer import Framer
from .remux import (BtsMux, IsdbParams, TSP_PERIOD, PCR_WRAP, NULL_PID, NULL_188, IIP_PID,
                    SHARED_SI_ONESEG, pcr_of)
from .rs import add_rs204, looks_like_rs204, rs_204_196
from .netout import NetConfig, NetSender

FORMATS = {
    "bts204": "BTS 204 (ISDB-Tb)",
    "bts188": "BTS 188 (sem os 16 bytes finais)",
    "ts188": "TS 188",
    "ts204": "TS 204 (Reed-Solomon 204,188)",
    "original": "Original (sem conversão)",
}
SOURCE_KINDS = {
    "bts204": "BTS 204 (ISDB-Tb)",
    "bts188": "BTS 188 (com IIP, sem trailer)",
    "ts188": "TS 188",
    "ts204": "TS 204 (RS 204,188)",
    "204raw": "TS 204 (16 bytes finais sem paridade)",
    "m2ts192": "M2TS 192",
}
LAYER_LETTER = {1: "A", 2: "B", 3: "C"}
LETTER_LAYER = {"A": 1, "B": 2, "C": 3}


def fmt_size(fmt, pr=None):
    if fmt == "original":
        return pr.size if pr else 188
    return 204 if fmt in ("bts204", "ts204") else 188


# ================================================================ sondagem da gravação
@dataclass
class Probe:
    path: str
    size: int = 188
    kind: str = "ts188"
    an: Analyzer = None
    iip_params: IsdbParams = None
    iip_payload: bytes = None
    pcr_pid: int = None
    mux_rate: float = None      # bps em 188
    file_bytes: int = 0
    layer_by_pid: dict = field(default_factory=dict)

    @property
    def is_bts(self):
        return self.kind in ("bts204", "bts188")

    def duration(self):
        npk = self.file_bytes // self.size
        if self.is_bts:
            return npk * TSP_PERIOD
        if self.mux_rate:
            return npk * 188 * 8 / self.mux_rate
        return None

    def services(self):
        """[(sid, nome, 1-seg?, camada atual ou None, pids)]"""
        out = []
        an = self.an
        for sid, s in sorted(an.services.items()):
            if sid == 0:
                continue
            pids = service_pids(s)
            ly = None
            for es in s.video_streams() + s.audio_streams():
                ly = self.layer_by_pid.get(es["pid"])
                if ly:
                    break
            out.append((sid, s.label, an.is_one_seg(s), ly, pids))
        return out

    def summary(self):
        parts = [SOURCE_KINDS.get(self.kind, self.kind)]
        d = self.duration()
        if d:
            parts.append("%.1f s" % d)
        if self.mux_rate and not self.is_bts:
            parts.append("%.3f Mbps" % (self.mux_rate / 1e6))
        nsv = len([1 for s in self.an.services if s])
        parts.append("%d serviço(s)" % nsv)
        if self.iip_params:
            parts.append("IIP: " + self.iip_params.describe())
        return " · ".join(parts)


def service_pids(s):
    pids = set()
    if s.pmt_pid is not None:
        pids.add(s.pmt_pid)
    if s.pcr_pid is not None and s.pcr_pid != NULL_PID:
        pids.add(s.pcr_pid)
    for es in s.streams:
        pids.add(es["pid"])
    return pids


def probe(path, max_bytes=24 * 1024 * 1024):
    """Lê o começo da gravação e identifica formato, serviços, IIP, PCR e camadas."""
    size_total = os.path.getsize(path)
    an = Analyzer(clock="stream")
    an.log = lambda *a, **k: None
    fr = Framer()
    first = []
    trailer_layers = set()
    iip_payload = None
    layer_by_pid_count = {}
    with open(path, "rb") as f:
        read = 0
        while read < max_bytes:
            data = f.read(1024 * 1024)
            if not data:
                break
            read += len(data)
            pkts = fr.push(data)
            if not pkts:
                continue
            an.feed_packets(pkts)
            size = len(pkts[0])
            for p in pkts:
                if len(first) < 64:
                    first.append(p)
                off = 4 if size == 192 else 0
                pid = ((p[off + 1] & 0x1F) << 8) | p[off + 2]
                if pid == IIP_PID and iip_payload is None:
                    afc = (p[off + 3] >> 4) & 3
                    if afc == 1:
                        iip_payload = bytes(p[off + 4:off + 188])
                if size == 204:
                    ly = p[189] >> 4
                    trailer_layers.add(ly)
                    if ly in (1, 2, 3):
                        c = layer_by_pid_count.setdefault(pid, [0, 0, 0, 0])
                        c[ly] += 1
            if an.services and sum(st.pcr_count for st in an.pids.values()) > 20 and read >= 6 * 1024 * 1024:
                break
    if not first:
        raise ValueError("Não foi possível sincronizar pacotes TS em %s" % path)
    size = len(first[0])
    pr = Probe(path=path, size=size, an=an, file_bytes=size_total)
    if size == 204:
        if looks_like_rs204(first):
            pr.kind = "ts204"
        elif trailer_layers & {1, 2, 3, 8}:
            pr.kind = "bts204"
        else:
            pr.kind = "204raw"
    elif size == 192:
        pr.kind = "m2ts192"
    else:
        pr.kind = "bts188" if (IIP_PID in an.pids and iip_payload is not None) else "ts188"
    if pr.kind == "bts204":
        for pid, c in layer_by_pid_count.items():
            pr.layer_by_pid[pid] = max((1, 2, 3), key=lambda i: c[i])
    if iip_payload is not None:
        from .isdbt import parse_iip
        _, d = parse_iip(iip_payload)
        if d.get("valid") and d.get("crc_ok"):
            pr.iip_payload = iip_payload
            try:
                pr.iip_params = IsdbParams.from_iip(d).validate()
            except (ValueError, TypeError):
                pr.iip_params = None
    best = None
    for pid, st in an.pids.items():
        if st.pcr_count and (best is None or st.pcr_count > an.pids[best].pcr_count):
            best = pid
    pr.pcr_pid = best
    if pr.is_bts:
        pr.mux_rate = 188 * 8 / TSP_PERIOD
    else:
        pr.mux_rate = an.mux_rate
    return pr


# ================================================================ carimbo de tempo por PCR
class PcrTimer:
    """Atribui a cada pacote um instante (s) interpolando o PCR de referência.

    push(pkt188, item) devolve a lista de (t, item) cujos tempos já são conhecidos.
    """

    def __init__(self, pcr_pid, period, t_start=0.0):
        self.pid = pcr_pid
        self.period = period
        self.buf = []
        self.last_pcr = None
        self.t_last = t_start - period
        self.t_start = t_start

    def push(self, p, item):
        self.buf.append(item)
        if self.pid is None:
            return self._flush_rate()
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid != self.pid:
            if len(self.buf) > 50000:      # PCR sumiu: não acumula indefinidamente
                return self._flush_rate()
            return []
        pcr = pcr_of(p)
        if pcr is None:
            return []
        n = len(self.buf)
        if self.last_pcr is None:
            # primeiro PCR: os pacotes anteriores usam a taxa estimada na sondagem
            t_pcr = self.t_start + (n - 1) * self.period
            out = [(t_pcr - (n - 1 - k) * self.period, it) for k, it in enumerate(self.buf)]
        else:
            d = (pcr - self.last_pcr) % PCR_WRAP
            if 0 < d < 0.7 * 27e6:
                dt = d / 27e6
                self.period = dt / n
            else:
                dt = n * self.period        # descontinuidade
            out = [(self.t_last + dt * (k + 1) / n, it) for k, it in enumerate(self.buf)]
        self.t_last = out[-1][0]
        self.last_pcr = pcr
        self.buf = []
        return out

    def _flush_rate(self):
        out = []
        t = self.t_last
        for it in self.buf:
            t += self.period
            out.append((t, it))
        self.t_last = t
        self.buf = []
        return out

    def flush(self):
        return self._flush_rate()


# ================================================================ configuração
@dataclass
class GenConfig:
    source: str
    fmt: str = "bts204"
    out_file: str = None
    net: NetConfig = None
    isdb: IsdbParams = None            # None = automático (IIP da gravação ou padrão Brasil)
    force_remux: bool = False          # remultiplexa mesmo quando a origem já é BTS
    service_layers: dict = field(default_factory=dict)   # sid -> 'A'/'B'/'C'
    pid_layers: dict = field(default_factory=dict)       # pid -> 'A'/'B'/'C'
    remove_nulls: bool = False         # TS: remove nulos (fica VBR; o envio segue o PCR)
    keep_layers: tuple = None          # BTS -> TS: (1,2,3) camadas mantidas; None = todas
    bts_rs_parity: bool = False        # BTS 204: paridade RS(204,196) nos 8 últimos bytes
    loop: bool = False
    realtime: bool = None              # None = tempo real se houver saída de rede
    rate_bps: float = None             # força a taxa de envio (bps sobre o pacote de saída)
    speed: float = 1.0
    seconds: float = None              # limite de duração (tempo de mídia)

    def describe(self):
        dst = []
        if self.out_file:
            dst.append(self.out_file)
        if self.net:
            dst.append(self.net.describe())
        return "%s -> %s" % (FORMATS[self.fmt], " + ".join(dst) or "?")


@dataclass
class GenStats:
    packets_in: int = 0
    packets_out: int = 0
    bytes_out: int = 0
    media_t: float = 0.0
    progress: float = 0.0
    loops: int = 0
    started: float = 0.0
    finished: bool = False
    error: str = None
    late_s: float = 0.0
    dropped: int = 0
    net: dict = field(default_factory=dict)

    def rate_bps(self):
        el = time.monotonic() - self.started if self.started else 0
        return self.bytes_out * 8 / el if el > 0 else 0.0


def needs_remux(cfg, pr):
    if cfg.fmt not in ("bts204", "bts188"):
        return False
    if pr.kind == "bts204":
        return cfg.force_remux
    if pr.kind == "bts188":
        return cfg.fmt == "bts204" or cfg.force_remux
    return True


def resolve_isdb(cfg, pr):
    if cfg.isdb is not None:
        return cfg.isdb.validate()
    if pr.iip_params is not None:
        return pr.iip_params
    return IsdbParams().validate()


def build_layer_map(cfg, pr, params):
    """dict pid -> camada (1..3) para a remultiplexação."""
    used = [i + 1 for i, l in enumerate(params.layers) if l]
    big = max(used, key=lambda i: params.capacity_bps(i - 1))
    m = {}
    if pr.kind == "bts204" and pr.layer_by_pid:
        m.update(pr.layer_by_pid)
    else:
        one_seg = params.partial and params.layers[0] is not None
        for sid, s in pr.an.services.items():
            if not sid:
                continue
            ly = 1 if (one_seg and pr.an.is_one_seg(s)) else big
            for pid in service_pids(s):
                m.setdefault(pid, ly)
        if one_seg:
            for pid in SHARED_SI_ONESEG:
                m[pid] = 1
    for sid, letter in (cfg.service_layers or {}).items():
        s = pr.an.services.get(sid)
        if s is not None:
            for pid in service_pids(s):
                m[pid] = LETTER_LAYER[letter.upper()]
    for pid, letter in (cfg.pid_layers or {}).items():
        m[pid] = LETTER_LAYER[letter.upper()]
    for pid in list(m):
        if not params.layers[m[pid] - 1]:
            m[pid] = big
    return lambda pid: m.get(pid, big)


def layer_load(cfg, pr, params):
    """[(bps_necessários, capacidade_bps)] por camada, estimado pela sondagem."""
    an = pr.an
    total = sum(st.count for st in an.pids.values()) or 1
    rate = pr.mux_rate or 0
    lmap = build_layer_map(cfg, pr, params)
    need = [0.0, 0.0, 0.0]
    for pid, st in an.pids.items():
        if pid in (NULL_PID, IIP_PID):
            continue
        need[lmap(pid) - 1] += st.count / total * rate
    return [(need[i], params.capacity_bps(i)) for i in range(3)]


# ================================================================ gerador
class Generator:
    """Executa a conversão e envia para arquivo e/ou rede."""

    def __init__(self, cfg, pr=None, log=None):
        if cfg.fmt not in FORMATS:
            raise ValueError("Formato de saída desconhecido: %s" % cfg.fmt)
        if not cfg.out_file and not cfg.net:
            raise ValueError("Escolha pelo menos um destino: arquivo ou rede")
        if cfg.loop and not cfg.net and not cfg.seconds:
            raise ValueError("Loop gravando só em arquivo não termina nunca: transmita na rede ou limite a duração")
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.pr = pr or probe(cfg.source)
        self.stats = GenStats()
        self.stop_ev = threading.Event()
        self.mux = None
        self.params = None
        if needs_remux(cfg, self.pr):
            self.params = resolve_isdb(cfg, self.pr)
            if self.pr.mux_rate is None and self.pr.pcr_pid is None and not cfg.rate_bps:
                raise ValueError("A gravação não tem PCR: informe a taxa de entrada")
        self.remove_nulls = bool(cfg.remove_nulls)
        self.layer_load = layer_load(cfg, self.pr, self.params) if self.params else None

    def capacity_warnings(self):
        out = []
        for i, (need, cap) in enumerate(self.layer_load or []):
            if cap and need > cap * 0.98:
                out.append("Camada %s: a gravação ocupa %.3f Mbps e a capacidade é %.3f Mbps — vai faltar banda"
                           % ("ABC"[i], need / 1e6, cap / 1e6))
            elif need and not cap:
                out.append("Camada %s não está ativa, mas há PIDs atribuídos a ela" % "ABC"[i])
        return out

    def stop(self):
        self.stop_ev.set()

    # ------------------------------------------------------------ leitura
    def _read(self):
        """Gera (t_entrada, pacote188, camada_original) a partir do arquivo (com loop)."""
        pr = self.pr
        cfg = self.cfg
        size = pr.size
        t_off = 0.0
        period = TSP_PERIOD if pr.is_bts else 188 * 8 / (cfg.rate_bps or pr.mux_rate or 20e6)
        while not self.stop_ev.is_set():
            fr = Framer(forced_size=size)
            timer = None if pr.is_bts else PcrTimer(pr.pcr_pid if not cfg.rate_bps else None, period, t_off)
            idx = 0
            pos = 0
            with open(pr.path, "rb") as f:
                while not self.stop_ev.is_set():
                    data = f.read(512 * 1024)
                    if not data:
                        break
                    pos += len(data)
                    self.stats.progress = pos / max(1, pr.file_bytes)
                    for p in fr.push(data):
                        self.stats.packets_in += 1
                        if size == 204:
                            p188 = p[:188]
                            ly = p[189] >> 4 if pr.kind == "bts204" else 0
                        elif size == 192:
                            p188 = p[4:]
                            ly = 0
                        else:
                            p188 = p
                            ly = 0
                        if timer is None:
                            yield t_off + idx * TSP_PERIOD, p188, ly, p
                            idx += 1
                        else:
                            for t, it in timer.push(p188, (p188, p)):
                                yield t, it[0], 0, it[1]
            if timer is not None:
                for t, it in timer.flush():
                    yield t, it[0], 0, it[1]
                t_off = timer.t_last + timer.period
            else:
                t_off += idx * TSP_PERIOD
            if not cfg.loop:
                break
            self.stats.loops += 1
            self.log("Fim da gravação — reiniciando (loop %d)" % self.stats.loops)

    # ------------------------------------------------------------ conversão
    def packets(self):
        """Gera (t, pacote_de_saída)."""
        cfg = self.cfg
        pr = self.pr
        fmt = cfg.fmt
        if self.params is not None:
            lmap = build_layer_map(cfg, pr, self.params)
            iip = pr.iip_payload if (cfg.isdb is None and pr.iip_params is not None) else None
            self.mux = mux = BtsMux(self.params, lmap, out_size=fmt_size(fmt), rs_parity=cfg.bts_rs_parity,
                                    iip_payload=iip, log=self.log)
            self.log("Remultiplexando para %s — %s" % (FORMATS[fmt], self.params.describe()))
            for t, p188, _ly, _raw in self._read():
                mux.push(t, p188)
                if mux.packets_in % 256 == 0:
                    for item in mux.pull(until_t=t):
                        yield item
            for item in mux.pull(flush=True):
                yield item
            return
        if fmt == "original":
            for t, _p188, _ly, raw in self._read():
                yield t, raw
            return
        keep = set(cfg.keep_layers) if cfg.keep_layers else None
        rm_null = self.remove_nulls
        rs = fmt == "ts204"
        rs196 = rs_204_196() if cfg.bts_rs_parity else None
        for t, p188, ly, raw in self._read():
            if fmt == "bts204":                      # bts204 -> bts204
                if cfg.bts_rs_parity:
                    raw = raw[:196] + rs196.encode(raw[:196])
                yield t, raw
                continue
            if fmt == "bts188":
                yield t, p188
                continue
            # TS 188 / TS 204
            pid = ((p188[1] & 0x1F) << 8) | p188[2]
            if (pr.is_bts and pid == IIP_PID) or \
               (keep is not None and pr.kind == "bts204" and ly in (1, 2, 3) and ly not in keep):
                if rm_null:
                    continue
                p188 = NULL_188     # mantém a taxa constante (PCR continua exato)
                pid = NULL_PID
            elif pid == NULL_PID and rm_null:
                continue
            yield t, (add_rs204(p188) if rs else p188)

    # ------------------------------------------------------------ execução
    def run(self):
        cfg = self.cfg
        st = self.stats
        st.started = time.monotonic()
        realtime = cfg.realtime if cfg.realtime is not None else bool(cfg.net)
        fout = None
        net = None
        try:
            if cfg.out_file:
                d = os.path.dirname(os.path.abspath(cfg.out_file))
                os.makedirs(d, exist_ok=True)
                fout = open(cfg.out_file, "wb", buffering=4 * 1024 * 1024)
            if cfg.net:
                net = NetSender(cfg.net, log=self.log)
                st.net = net.stats
            self.log("Gerando %s a partir de %s (%s)" % (cfg.describe(), os.path.basename(cfg.source),
                                                       SOURCE_KINDS.get(self.pr.kind)))
            for w in self.capacity_warnings():
                self.log("Atenção: " + w)
            out_size = fmt_size(cfg.fmt, self.pr)
            t0 = None
            wall0 = None
            fixed = cfg.rate_bps if (cfg.rate_bps and self.params is None and not self.pr.is_bts) else None
            sent_bits = 0
            batch = []
            per_dgram = cfg.net.pkts_per_dgram if cfg.net else 7
            speed = cfg.speed or 1.0
            for t, p in self.packets():
                if t0 is None:
                    t0 = t
                    wall0 = time.monotonic()
                mt = t - t0
                if cfg.seconds and mt >= cfg.seconds:
                    break
                if fout is not None:
                    fout.write(p)
                st.packets_out += 1
                st.bytes_out += out_size
                st.media_t = mt
                if net is not None:
                    batch.append(p)
                    if len(batch) >= per_dgram:
                        if realtime:
                            if fixed:
                                sent_bits += len(batch) * out_size * 8
                                due = wall0 + sent_bits / fixed / speed
                            else:
                                due = wall0 + mt / speed
                            ahead = due - time.monotonic()
                            if ahead > 0.002:
                                time.sleep(ahead)
                            elif ahead < -1.0:
                                st.late_s = -ahead
                                wall0 -= ahead  # não tenta recuperar atrasos grandes em rajada
                        net.send(batch, mt)
                        batch = []
                elif realtime:
                    due = wall0 + mt / speed
                    ahead = due - time.monotonic()
                    if ahead > 0.005:
                        time.sleep(ahead)
                if self.stop_ev.is_set():
                    break
            if net is not None and batch:
                net.send(batch, st.media_t)
            if self.mux is not None:
                st.dropped = sum(self.mux.dropped.values())
                if st.dropped:
                    self.log("Atenção: %d pacotes descartados por excesso de taxa nas camadas" % st.dropped)
            self.log("Concluído: %d pacotes (%.1f MB, %.1f s de mídia)" % (st.packets_out, st.bytes_out / 1e6,
                                                                         st.media_t))
        except Exception as exc:
            st.error = str(exc)
            self.log("Erro: %s" % exc)
            raise
        finally:
            if fout is not None:
                fout.close()
            if net is not None:
                net.close()
            st.finished = True
        return st
