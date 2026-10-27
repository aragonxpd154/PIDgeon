"""Motor de análise do PIDgeon: PIDs, PSI/SI, PCR, ETR 290 (TR 101 290), ISDB-T/BTS."""

import collections
import datetime
import threading
import time

from .framer import Framer
from .psi import SectionAssembler
from .tables import parse_section, describe_service_type
from .crc import crc32_mpeg
from .isdbt import BTSInfo, parse_iip
from .names import (WELL_KNOWN_PIDS, SI_PIDS, NULL_PID, IIP_PID, LAYER_NAMES,
                    VIDEO_STREAM_TYPES, AUDIO_STREAM_TYPES, SECTION_STREAM_TYPES,
                    stream_type_name, component_tag_kind, table_name)

PCR_WRAP = (1 << 33) * 300
HISTORY = 600  # pontos (1 por segundo) guardados para os gráficos

ETR_DEFS = [
    # código, prioridade, nome, descrição
    ("1.1", 1, "TS_sync_loss", "Perda de sincronismo do TS"),
    ("1.2", 1, "Sync_byte_error", "Byte de sincronismo diferente de 0x47"),
    ("1.3", 1, "PAT_error", "PAT ausente > 500 ms, table_id errado ou embaralhada"),
    ("1.4", 1, "Continuity_count_error", "Erro de contador de continuidade"),
    ("1.5", 1, "PMT_error", "PMT ausente > 500 ms ou embaralhada"),
    ("1.6", 1, "PID_error", "PID de áudio/vídeo referenciado ausente > 5 s"),
    ("2.1", 2, "Transport_error", "Transport Error Indicator ligado"),
    ("2.2", 2, "CRC_error", "CRC de seção PSI/SI inválido"),
    ("2.3a", 2, "PCR_repetition_error", "Intervalo entre PCRs > 40 ms"),
    ("2.3b", 2, "PCR_discontinuity_error", "Salto de PCR > 100 ms sem discontinuity_indicator"),
    ("2.4", 2, "PCR_accuracy_error", "Precisão do PCR fora de ±500 ns"),
    ("2.5", 2, "PTS_error", "Intervalo entre PTS > 700 ms"),
    ("2.6", 2, "CAT_error", "Pacotes embaralhados sem CAT ou table_id errado no PID 1"),
    ("3.1", 3, "NIT_error", "NIT-actual ausente > 10 s"),
    ("3.4", 3, "Unreferenced_PID", "PID não referenciado por nenhuma tabela"),
    ("3.5", 3, "SDT_error", "SDT-actual ausente > 2 s"),
    ("3.6", 3, "EIT_error", "EIT-p/f actual ausente > 2 s"),
    ("3.8", 3, "TDT/TOT_error", "TDT/TOT ausente > 30 s"),
    ("B.1", 3, "ISDB_TSP_counter", "Contador TSP do BTS descontínuo (pacotes BTS perdidos)"),
    ("B.2", 3, "ISDB_IIP_error", "IIP ausente > 1 s ou com CRC inválido (BTS)"),
]


class EtrItem:
    __slots__ = ("code", "priority", "name", "desc", "count", "last_t", "last_wall", "last_msg",
                 "_last_log")

    def __init__(self, code, pr, name, desc):
        self.code, self.priority, self.name, self.desc = code, pr, name, desc
        self.count = 0
        self.last_t = None
        self.last_wall = None
        self.last_msg = ""
        self._last_log = -1e9


class PidStats:
    __slots__ = ("pid", "count", "cc", "cc_errors", "dup", "tei", "scrambled", "pusi",
                 "first_t", "last_t", "rate", "win_count", "history", "layers", "last_raw",
                 "last_pcr", "last_pcr_idx", "last_pcr_t", "pcr_count", "pcr_int_max",
                 "pcr_int_last", "pcr_acc_last", "pcr_acc_max", "pcr_hist", "pcr0", "pcr0_t",
                 "pcr_jitter", "discont", "pts_t", "last_pts", "unref_reported", "rate_min",
                 "rate_max", "first_wall")

    def __init__(self, pid, t):
        self.pid = pid
        self.count = 0
        self.cc = None
        self.cc_errors = 0
        self.dup = 0
        self.tei = 0
        self.scrambled = 0
        self.pusi = 0
        self.first_t = t
        self.first_wall = time.time()
        self.last_t = t
        self.rate = 0.0
        self.rate_min = None
        self.rate_max = 0.0
        self.win_count = 0
        self.history = collections.deque(maxlen=HISTORY)
        self.layers = [0] * 16
        self.last_raw = None
        self.last_pcr = None
        self.last_pcr_idx = 0
        self.last_pcr_t = None
        self.pcr_count = 0
        self.pcr_int_max = 0.0
        self.pcr_int_last = None
        self.pcr_acc_last = None
        self.pcr_acc_max = 0.0
        self.pcr_hist = collections.deque(maxlen=3000)
        self.pcr0 = None
        self.pcr0_t = None
        self.pcr_jitter = None
        self.discont = 0
        self.pts_t = None
        self.last_pts = None
        self.unref_reported = False


class TableInfo:
    __slots__ = ("pid", "table_id", "ext", "version", "sections", "last_section", "count",
                 "first_t", "last_t", "versions_seen", "crc_errors", "data")

    def __init__(self, pid, tid, ext, t):
        self.pid, self.table_id, self.ext = pid, tid, ext
        self.version = None
        self.sections = {}
        self.last_section = 0
        self.count = 0
        self.first_t = t
        self.last_t = t
        self.versions_seen = 0
        self.crc_errors = 0
        self.data = {}

    @property
    def name(self):
        return table_name(self.table_id)


class Service:
    def __init__(self, sid):
        self.sid = sid
        self.pmt_pid = None
        self.pcr_pid = None
        self.streams = []        # dicts de _pmt()
        self.name = ""
        self.provider = ""
        self.type = None
        self.running = None
        self.free_ca = 0
        self.present = None
        self.following = None
        self.events = {}
        self.pmt_version = None
        self.prog_info = {}

    @property
    def label(self):
        return self.name or ("Serviço %d" % self.sid)

    def video_streams(self):
        return [s for s in self.streams if s["stream_type"] in VIDEO_STREAM_TYPES]

    def audio_streams(self):
        return [s for s in self.streams if s["stream_type"] in AUDIO_STREAM_TYPES]


class Analyzer:
    def __init__(self, clock="wall"):
        self.lock = threading.RLock()
        self.clock = clock
        self.taps = []
        self.listeners = []
        self.reset()

    # ------------------------------------------------------------ estado
    def reset(self):
        with self.lock:
            self.framer = Framer(on_event=self._framer_event)
            self.pids = {}
            self.assemblers = {}
            self.psi_pids = set(SI_PIDS)
            self.pmt_pids = {}           # pid -> program_number
            self.sec_cache = {}
            self.tables = {}
            self.services = {}
            self.pat = {}
            self.tsid = None
            self.onid = None
            self.network_id = None
            self.network_name = ""
            self.ts_name = ""
            self.remote_key = None
            self.partial_sids = []
            self.frequencies = []
            self.area_code = None
            self.broadcasters = []
            self.ait_apps = []
            self.cat_ca = []
            self.ca_pids = set()
            self.ts_time = None
            self.ts_time_wall = None
            self.lto = None
            self.emergency = []
            self.packet_size = None
            self.total = 0
            self.pkt_index = 0
            self.bytes_in = 0
            self.start_t = None
            self.t = 0.0
            self.win_start = None
            self.total_rate = 0.0
            self.raw_rate = 0.0
            self.nonnull_rate = 0.0
            self.total_hist = collections.deque(maxlen=HISTORY)
            self.nonnull_hist = collections.deque(maxlen=HISTORY)
            self.err_hist = collections.deque(maxlen=HISTORY)
            self._win_total = 0
            self._win_null = 0
            self._win_errors = 0
            self.mux_rate = None          # bps (TS 188) estimado pelo PCR
            self._rate_pid = None
            self._rate_pkts = 0
            self._rate_ticks = 0
            self._sec_per_pkt = 188 * 8 / 20e6
            self._stream_t = 0.0
            # ISDB-T / BTS
            self.is_bts = False
            self.layer_counts = [0] * 16
            self.layer_win = [0] * 16
            self.layer_rates = [0.0] * 16
            self.layer_hist = {k: collections.deque(maxlen=HISTORY) for k in (0, 1, 2, 3, 8)}
            self.bts_last = None
            self.bts_tsp_prev = None
            self.bts_tsp_errors = 0
            self.bts_frames = 0
            self.bts_frame_start_idx = None
            self.bts_pkts_per_frame = None
            self.bts_emergency = 0
            self.iip = None
            self.iip_node = None
            self.iip_raw = None
            self.iip_t = None
            self.iip_count = 0
            # sinalização de tempo das tabelas p/ ETR 290
            self.last_pat_t = None
            self.last_pmt_t = {}
            self.last_nit_t = None
            self.last_sdt_t = None
            self.last_eit_t = None
            self.last_tdt_t = None
            self.etr = collections.OrderedDict((c, EtrItem(c, p, n, d)) for c, p, n, d in ETR_DEFS)
            self.messages = collections.deque(maxlen=5000)
            self.msg_serial = 0
            self.sync_locked = False

    def mark_discontinuity(self):
        """Reinício deliberado do stream (ex.: loop de arquivo): não conta como erro."""
        with self.lock:
            for st in self.pids.values():
                st.cc = None
                st.dup = 0
                st.last_pcr = None
                st.pcr0 = None
                st.pts_t = None
            for asm in self.assemblers.values():
                asm.reset()
            self.bts_tsp_prev = None
            self.bts_frame_start_idx = None
            self.framer.reset()

    def log(self, text, level="info"):
        with self.lock:
            self.messages.append((datetime.datetime.now(), level, text))
            self.msg_serial += 1

    def report(self, code, msg, t=None):
        e = self.etr[code]
        e.count += 1
        e.last_t = self.t if t is None else t
        e.last_wall = datetime.datetime.now()
        e.last_msg = msg
        self._win_errors += 1
        now = time.monotonic()
        if now - e._last_log >= 1.0:
            e._last_log = now
            self.log("TR 101 290 %s %s: %s" % (code, e.name, msg), "error" if e.priority == 1 else "warn")

    def _framer_event(self, kind, size):
        if kind == "sync":
            self.sync_locked = True
            self.packet_size = size
            self.is_bts = size == 204
            label = {188: "TS 188", 204: "204 bytes (BTS / TS com Reed-Solomon)", 192: "M2TS 192"}[size]
            self.log("Sincronizado em pacotes de %s" % label)
        elif kind == "loss":
            self.sync_locked = False
            self.report("1.1", "perda de sincronismo (pacotes de %d bytes)" % size)
        elif kind == "sync_byte":
            self.report("1.2", "byte de sync inválido")

    # ------------------------------------------------------------ entrada
    def push_bytes(self, data, now=None):
        with self.lock:
            self.bytes_in += len(data)
            pkts = self.framer.push(data)
            if pkts:
                self.feed_packets(pkts, now)
            return len(pkts)

    def feed_packets(self, pkts, now=None):
        with self.lock:
            if self.clock == "wall":
                t = time.monotonic() if now is None else now
            else:
                t = self._stream_t
            if self.start_t is None:
                self.start_t = t
                self.win_start = t
            self.t = t
            size = len(pkts[0])
            if size != self.packet_size:
                self.packet_size = size
                self.is_bts = size == 204
            if self.mux_rate is None and size == 204:
                # BTS ISDB-T tem taxa fixa: 32,507936 Mbps em 204 bytes = 29,958 Mbps em 188
                self._sec_per_pkt = 204 * 8 / 32507936.507936
            off = 4 if size == 192 else 0
            bts = size == 204
            pids = self.pids
            psi_pids = self.psi_pids
            stream_clock = self.clock != "wall"
            spp = self._sec_per_pkt
            for p in pkts:
                if stream_clock:
                    t = self._stream_t
                    self._stream_t += spp
                self.pkt_index += 1
                b1 = p[off + 1]
                pid = ((b1 & 0x1F) << 8) | p[off + 2]
                st = pids.get(pid)
                if st is None:
                    st = pids[pid] = PidStats(pid, t)
                    if pid != NULL_PID:
                        self.log("Novo PID %d (0x%04X)" % (pid, pid))
                st.count += 1
                st.last_t = t
                st.last_raw = p
                if bts:
                    b188 = p[188]
                    layer = p[189] >> 4
                    st.layers[layer] += 1
                    self.layer_counts[layer] += 1
                    tsp = ((p[190] & 0x1F) << 8) | p[191]
                    if b188 & 0x02:  # frame_head_packet_flag
                        if self.bts_frame_start_idx is not None:
                            self.bts_pkts_per_frame = self.pkt_index - self.bts_frame_start_idx
                        self.bts_frame_start_idx = self.pkt_index
                        self.bts_frames += 1
                    elif self.bts_tsp_prev is not None and tsp != ((self.bts_tsp_prev + 1) & 0x1FFF):
                        self.bts_tsp_errors += 1
                        self.report("B.1", "TSP_counter %d após %d" % (tsp, self.bts_tsp_prev), t)
                    self.bts_tsp_prev = tsp
                    em = (b188 >> 3) & 1
                    if em != self.bts_emergency:
                        self.bts_emergency = em
                        self.log("BTS: flag de emergência (EWBS) %s" % ("LIGADO" if em else "desligado"),
                                 "warn" if em else "info")
                if pid == NULL_PID:
                    self._win_null += 1
                    continue
                if b1 & 0x80:
                    st.tei += 1
                    self.report("2.1", "TEI no PID %d" % pid, t)
                pusi = b1 & 0x40
                b3 = p[off + 3]
                afc = (b3 >> 4) & 3
                cc = b3 & 0x0F
                sc = b3 >> 6
                disc = False
                pstart = off + 4
                if afc & 2:
                    alen = p[off + 4]
                    pstart = off + 5 + alen
                    if alen:
                        flags = p[off + 5]
                        if flags & 0x80:
                            disc = True
                            st.discont += 1
                        if flags & 0x10 and alen >= 7:
                            q = off + 6
                            base = (p[q] << 25) | (p[q + 1] << 17) | (p[q + 2] << 9) | (p[q + 3] << 1) | (p[q + 4] >> 7)
                            pcr = base * 300 + (((p[q + 4] & 1) << 8) | p[q + 5])
                            self._on_pcr(st, pcr, disc, t)
                # contador de continuidade
                last = st.cc
                if last is not None and not disc:
                    if afc & 1:
                        if cc == last:
                            # um pacote duplicado é permitido; dois seguidos é erro
                            st.dup += 1
                            if st.dup > 1:
                                st.cc_errors += 1
                                self.report("1.4", "PID %d: CC repetido %d" % (pid, cc), t)
                        else:
                            st.dup = 0
                            if cc != ((last + 1) & 0x0F):
                                st.cc_errors += 1
                                self.report("1.4", "PID %d: esperado %d, recebido %d" % (pid, (last + 1) & 0x0F, cc), t)
                    elif cc != last:
                        st.cc_errors += 1
                        self.report("1.4", "PID %d: CC mudou sem payload" % pid, t)
                st.cc = cc
                if sc:
                    st.scrambled += 1
                    if pid == 0:
                        self.report("1.3", "PAT embaralhada", t)
                    elif pid in self.pmt_pids:
                        self.report("1.5", "PMT %d embaralhada" % pid, t)
                    continue
                if pusi:
                    st.pusi += 1
                if not (afc & 1) or pstart >= off + 188:
                    continue
                if pid in psi_pids:
                    asm = self.assemblers.get(pid)
                    if asm is None:
                        asm = self.assemblers[pid] = SectionAssembler()
                    for sec in asm.feed(p[pstart:off + 188], pusi, disc):
                        self._on_section(pid, sec, t)
                elif pid == IIP_PID:
                    self._on_iip(p[pstart:off + 188], t)
                elif pusi and p[pstart] == 0 and p[pstart + 1] == 0 and p[pstart + 2] == 1:
                    q = pstart + 7
                    if q + 6 <= off + 188 and p[q] & 0x80:
                        pts = (((p[q + 2] >> 1) & 7) << 30) | (p[q + 3] << 22) | ((p[q + 4] >> 1) << 15) | (p[q + 5] << 7) | (p[q + 6] >> 1)
                        st.last_pts = pts
                        st.pts_t = t
            self.total += len(pkts)
            self._win_total += len(pkts)
            if bts:
                self.bts_last = pkts[-1][188:196]
            if self.clock != "wall":
                t = self._stream_t
                self.t = t
            if t - self.win_start >= 1.0:
                self._close_window(t)
            for tap in self.taps:
                try:
                    tap(pkts, size, t)
                except Exception as exc:  # um consumidor com defeito não para a análise
                    self.log("Erro em consumidor de pacotes: %s" % exc, "error")

    # ------------------------------------------------------------ PCR
    def _on_pcr(self, st, pcr, disc, t):
        st.pcr_count += 1
        idx = self.pkt_index - 1
        if st.last_pcr is not None and not disc:
            dp = (pcr - st.last_pcr) % PCR_WRAP
            dpk = idx - st.last_pcr_idx
            interval = dp / 27e6
            if dp > 2.7e6 or interval < 0:
                self.report("2.3b", "PID %d: salto de %.1f ms" % (st.pid, interval * 1000), t)
                st.pcr0 = None
                if st.pid == self._rate_pid:
                    self._rate_pkts = 0
                    self._rate_ticks = 0
            else:
                st.pcr_int_last = interval
                if interval > st.pcr_int_max:
                    st.pcr_int_max = interval
                if interval > 0.040:
                    self.report("2.3a", "PID %d: %.1f ms" % (st.pid, interval * 1000), t)
                if self._rate_pid is None:
                    self._rate_pid = st.pid
                if st.pid == self._rate_pid and dp > 0:
                    self._rate_pkts += dpk
                    self._rate_ticks += dp
                    if self._rate_ticks > 2.7e6:  # >= 100 ms acumulados
                        self.mux_rate = self._rate_pkts * 188 * 8 * 27e6 / self._rate_ticks
                        self._sec_per_pkt = 188 * 8 / self.mux_rate
                acc = None
                if self.mux_rate and self._rate_ticks > 27e6:
                    expected = dpk * 188 * 8 / self.mux_rate * 27e6
                    acc = (dp - expected) / 27.0 * 1000.0  # ns
                    st.pcr_acc_last = acc
                    if abs(acc) > abs(st.pcr_acc_max):
                        st.pcr_acc_max = acc
                    if abs(acc) > 500 and self.clock != "wall_strict":
                        self.report("2.4", "PID %d: %.0f ns" % (st.pid, acc), t)
                jit = None
                if self.clock == "wall":
                    if st.pcr0 is None:
                        st.pcr0, st.pcr0_t = pcr, t
                    else:
                        el_pcr = ((pcr - st.pcr0) % PCR_WRAP) / 27e6
                        jit = (t - st.pcr0_t) - el_pcr
                        st.pcr_jitter = jit
                st.pcr_hist.append((t, interval * 1000.0, acc, None if jit is None else jit * 1000.0))
        st.last_pcr = pcr
        st.last_pcr_idx = idx
        st.last_pcr_t = t

    # ------------------------------------------------------------ IIP
    def _on_iip(self, payload, t):
        self.iip_count += 1
        self.iip_t = t
        payload = bytes(payload)
        if payload == self.iip_raw:
            return
        node, data = parse_iip(payload)
        if self.iip is not None and data.get("valid") and data.get("crc_ok"):
            old = self.iip
            if (old.get("layers"), old.get("mode"), old.get("gi")) != (data.get("layers"), data.get("mode"), data.get("gi")):
                self.log("IIP: parâmetros de transmissão mudaram", "warn")
        if data.get("valid") and not data.get("crc_ok"):
            self.report("B.2", "CRC do MCCI inválido", t)
        elif self.iip is None and data.get("valid"):
            self.log("IIP recebido: %s" % ", ".join(
                "%s=%s %s %dseg" % (l["name"], l["mod"], l["cr"], l["segs"]) for l in data["layers_info"] if l))
        self.iip_raw = payload
        self.iip = data
        self.iip_node = node

    # ------------------------------------------------------------ seções
    def _on_section(self, pid, sec, t):
        tid = sec[0]
        if tid == 0xFF or len(sec) < 3:
            return
        long_form = sec[1] & 0x80
        if (long_form or tid == 0x73) and len(sec) >= 8:
            key = (pid, tid, sec[3] << 8 | sec[4], sec[6]) if long_form else (pid, tid, 0, 0)
            cached = self.sec_cache.get(key)
            if cached == sec:
                ti = self.tables.get(key[:3])
                if ti is not None:
                    ti.count += 1
                    ti.last_t = t
                self._section_timing(pid, tid, t)
                return
            if crc32_mpeg(sec) != 0:
                self.report("2.2", "PID %d, %s" % (pid, table_name(tid)), t)
                ti = self.tables.get(key[:3])
                if ti is not None:
                    ti.crc_errors += 1
                return
        else:
            key = (pid, tid, 0, 0)
            if tid != 0x70 and self.sec_cache.get(key) == sec:
                self._section_timing(pid, tid, t)
                return
        if pid == 0 and tid != 0x00:
            self.report("1.3", "table_id 0x%02X no PID 0" % tid, t)
            return
        if pid == 1 and tid != 0x01:
            self.report("2.6", "table_id 0x%02X no PID 1" % tid, t)
            return
        self.sec_cache[key] = sec
        self._section_timing(pid, tid, t)
        h, node, data = parse_section(pid, sec)
        tkey = (pid, tid, h.ext)
        ti = self.tables.get(tkey)
        if ti is None:
            ti = self.tables[tkey] = TableInfo(pid, tid, h.ext, t)
        if ti.version is not None and ti.version != h.version and h.long:
            ti.sections = {}
            if tid in (0x00, 0x02, 0x40, 0x42):
                self.log("%s (PID %d, ext 0x%04X): versão %d -> %d" % (table_name(tid), pid, h.ext, ti.version, h.version), "warn")
        if ti.version is None or ti.version != h.version:
            ti.versions_seen += 1
        ti.version = h.version
        ti.last_section = h.last
        ti.sections[h.number] = node
        ti.count += 1
        ti.last_t = t
        ti.data = data
        self._apply(pid, tid, h, data, t)

    def _section_timing(self, pid, tid, t):
        if tid == 0x00 and pid == 0:
            self.last_pat_t = t
        elif tid == 0x02:
            self.last_pmt_t[pid] = t
        elif tid == 0x40:
            self.last_nit_t = t
        elif tid == 0x42:
            self.last_sdt_t = t
        elif tid == 0x4E:
            self.last_eit_t = t
        elif tid in (0x70, 0x73):
            self.last_tdt_t = t

    def _svc(self, sid):
        s = self.services.get(sid)
        if s is None:
            s = self.services[sid] = Service(sid)
        return s

    def _apply(self, pid, tid, h, data, t):
        if tid == 0x00:
            progs = data.get("programs", {})
            if self.tsid is None:
                self.log("PAT: TSID 0x%04X, %d programa(s)" % (h.ext, len([p for p in progs if p])))
            self.tsid = h.ext
            self.pat = progs
            for pn, ppid in progs.items():
                if pn == 0:
                    self.psi_pids.add(ppid)
                    continue
                self.pmt_pids[ppid] = pn
                self.psi_pids.add(ppid)
                self._svc(pn).pmt_pid = ppid
        elif tid == 0x02:
            s = self._svc(h.ext)
            s.pmt_pid = pid
            s.pcr_pid = data.get("pcr_pid")
            s.streams = data.get("streams", [])
            s.prog_info = data.get("info", {})
            s.pmt_version = h.version
            for ca in s.prog_info.get("ca", []):
                self.ca_pids.add(ca[1])
            if s.prog_info.get("emergency"):
                self._set_emergency(s.prog_info["emergency"])
            for es in s.streams:
                for ca in es["info"].get("ca", []):
                    self.ca_pids.add(ca[1])
                st = es["stream_type"]
                if st in SECTION_STREAM_TYPES or 0x6F in es.get("descriptor_tags", ()):
                    self.psi_pids.add(es["pid"])
        elif tid == 0x01:
            self.cat_ca = data.get("ca", [])
            for ca in self.cat_ca:
                self.ca_pids.add(ca[1])
        elif tid == 0x40:
            info = data.get("info", {})
            self.network_id = data.get("network_id")
            self.network_name = info.get("network_name", self.network_name)
            for ts in data.get("ts", []):
                ti = ts.get("info", {})
                if self.tsid is None or ts["tsid"] == self.tsid:
                    self.onid = ts["onid"]
                    self.ts_name = ti.get("ts_name", self.ts_name)
                    self.remote_key = ti.get("remote_key", self.remote_key)
                    self.partial_sids = ti.get("partial_reception", self.partial_sids)
                    self.frequencies = ti.get("frequencies", self.frequencies)
                    self.area_code = ti.get("area_code", self.area_code)
            if info.get("emergency"):
                self._set_emergency(info["emergency"])
        elif tid == 0x42:
            self.onid = data.get("onid")
            for sid, sv in data.get("services", {}).items():
                s = self._svc(sid)
                s.name = sv["name"]
                s.provider = sv["provider"]
                s.type = sv["type"]
                s.running = sv["running"]
                s.free_ca = sv["free_ca"]
        elif tid == 0x4E or 0x50 <= tid <= 0x5F:
            s = self._svc(h.ext)
            for ev in data.get("events", []):
                s.events[ev["event_id"]] = ev
                if tid == 0x4E:
                    if data.get("section") == 0:
                        s.present = ev
                    elif data.get("section") == 1:
                        s.following = ev
        elif tid in (0x70, 0x73):
            self.ts_time = data.get("time")
            self.ts_time_wall = time.time()
            if data.get("lto"):
                self.lto = data["lto"]
        elif tid == 0xC4:
            self.broadcasters = [b for b in data.get("broadcasters", []) if b]
        elif tid == 0x74:
            self.ait_apps = data.get("apps", [])

    def _set_emergency(self, evs):
        if evs != self.emergency:
            self.emergency = evs
            active = [e for e in evs if e[1]]
            if active:
                self.log("ALERTA DE EMERGÊNCIA (EWBS) ativo para serviço(s) %s" %
                         ", ".join("0x%04X" % e[0] for e in active), "error")

    # ------------------------------------------------------------ janela de 1 s
    def _close_window(self, t):
        dt = t - self.win_start
        if dt <= 0:
            return
        k = 188 * 8 / dt
        for st in self.pids.values():
            d = st.count - st.win_count
            st.win_count = st.count
            st.rate = d * k
            st.history.append(st.rate)
            if st.rate > st.rate_max:
                st.rate_max = st.rate
            if st.rate_min is None or st.rate < st.rate_min:
                st.rate_min = st.rate
        self.total_rate = self._win_total * k
        self.raw_rate = self._win_total * (self.packet_size or 188) * 8 / dt
        self.nonnull_rate = (self._win_total - self._win_null) * k
        self.total_hist.append(self.total_rate)
        self.nonnull_hist.append(self.nonnull_rate)
        self.err_hist.append(self._win_errors)
        self._win_total = self._win_null = self._win_errors = 0
        if self.is_bts:
            for i in range(16):
                d = self.layer_counts[i] - self.layer_win[i]
                self.layer_win[i] = self.layer_counts[i]
                self.layer_rates[i] = d * k
            for key, dq in self.layer_hist.items():
                dq.append(self.layer_rates[key])
        self.win_start = t
        self._periodic_checks(t)

    def _periodic_checks(self, t):
        if t - self.start_t < 1.0:
            return
        age = t - self.start_t
        if self.last_pat_t is None or t - self.last_pat_t > 0.5:
            self.report("1.3", "PAT ausente há %s" % _ago(self.last_pat_t, t), t)
        for ppid, pn in list(self.pmt_pids.items()):
            if self.pat.get(pn) != ppid:
                continue
            lt = self.last_pmt_t.get(ppid)
            if lt is None or t - lt > 0.5:
                self.report("1.5", "PMT %d (programa %d) ausente há %s" % (ppid, pn, _ago(lt, t)), t)
        for s in self.services.values():
            if s.pmt_pid is None or self.pat.get(s.sid) != s.pmt_pid:
                continue
            for es in s.streams:
                if es["stream_type"] in VIDEO_STREAM_TYPES or es["stream_type"] in AUDIO_STREAM_TYPES:
                    st = self.pids.get(es["pid"])
                    if age > 5 and (st is None or t - st.last_t > 5):
                        self.report("1.6", "PID %d do serviço %d ausente" % (es["pid"], s.sid), t)
                    elif st is not None and st.pts_t is not None and t - st.pts_t > 0.7 and t - st.last_t < 0.7:
                        self.report("2.5", "PID %d sem PTS há %.0f ms" % (es["pid"], (t - st.pts_t) * 1000), t)
        if any(st.scrambled for st in self.pids.values()) and not self.cat_ca and (0, 0x01, 0) not in [k[:3] for k in self.tables]:
            self.report("2.6", "pacotes embaralhados e nenhuma CAT", t)
        if age > 10 and (self.last_nit_t is None or t - self.last_nit_t > 10):
            self.report("3.1", "NIT ausente há %s" % _ago(self.last_nit_t, t), t)
        if age > 2 and (self.last_sdt_t is None or t - self.last_sdt_t > 2):
            self.report("3.5", "SDT ausente há %s" % _ago(self.last_sdt_t, t), t)
        if age > 2 and (self.last_eit_t is None or t - self.last_eit_t > 2):
            self.report("3.6", "EIT p/f ausente há %s" % _ago(self.last_eit_t, t), t)
        if age > 30 and (self.last_tdt_t is None or t - self.last_tdt_t > 30):
            self.report("3.8", "TDT/TOT ausente há %s" % _ago(self.last_tdt_t, t), t)
        if self.is_bts and age > 1 and (self.iip_t is None or t - self.iip_t > 1.0):
            self.report("B.2", "IIP ausente há %s" % _ago(self.iip_t, t), t)
        if self.pat:
            refs = self.referenced_pids()
            for st in self.pids.values():
                if not st.unref_reported and st.pid not in refs and t - st.first_t > 0.5:
                    st.unref_reported = True
                    self.report("3.4", "PID %d (0x%04X)" % (st.pid, st.pid), t)

    # ------------------------------------------------------------ consultas
    def referenced_pids(self):
        refs = set(SI_PIDS) | {NULL_PID, IIP_PID} | set(self.ca_pids)
        refs.update(self.pat.values())
        for s in self.services.values():
            if s.pmt_pid is not None:
                refs.add(s.pmt_pid)
            if s.pcr_pid is not None:
                refs.add(s.pcr_pid)
            for es in s.streams:
                refs.add(es["pid"])
        return refs

    def pid_services(self, pid):
        out = []
        for s in self.services.values():
            if pid == s.pmt_pid or pid == s.pcr_pid or any(es["pid"] == pid for es in s.streams):
                out.append(s)
        return out

    def stream_of(self, pid):
        for s in self.services.values():
            for es in s.streams:
                if es["pid"] == pid:
                    return s, es
        return None, None

    def describe_pid(self, pid):
        """(tipo, descrição) no estilo StreamXpert/TS Reader."""
        if pid == 0:
            return "psi", "PAT"
        if pid == NULL_PID:
            return "null", "Null packets"
        if pid == IIP_PID:
            return "iip", "IIP (ISDB-T)"
        if pid in self.pmt_pids:
            return "psi", "PMT"
        s, es = self.stream_of(pid)
        if es is not None:
            st = es["stream_type"]
            tag = es["info"].get("component_tag")
            kind = component_tag_kind(tag)
            name = stream_type_name(st)
            if st in VIDEO_STREAM_TYPES:
                return "video", name
            if st in AUDIO_STREAM_TYPES:
                return "audio", name
            if st == 0x06 and kind in ("Closed Caption", "Superimpose", "Closed Caption 1-seg"):
                return "data", "%s (PES Private)" % kind
            if es["info"].get("data_component"):
                return "data", "%s (%s)" % (name, es["info"]["data_component"])
            return "data", name
        for sv in self.services.values():
            if sv.pcr_pid == pid:
                return "pcr", "PCR"
        if pid == 0x10:
            return "psi", "NIT-actual" if any(k[1] == 0x40 for k in self.tables) else "NIT"
        if pid == 0x11:
            return "psi", "SDT-actual" if any(k[0] == 0x11 and k[1] == 0x42 for k in self.tables) else "SDT/BAT"
        if pid == 0x14:
            has_tot = any(k[0] == 0x14 and k[1] == 0x73 for k in self.tables)
            return "psi", "TOT" if has_tot else "TDT/TOT"
        if pid in WELL_KNOWN_PIDS:
            return "psi", WELL_KNOWN_PIDS[pid]
        if pid in self.ca_pids:
            return "data", "ECM/EMM"
        if any(k[0] == pid for k in self.tables):
            return "psi", "Private Sections"
        return "unknown", "Desconhecido"

    def layer_of(self, pid):
        st = self.pids.get(pid)
        if not self.is_bts or st is None:
            return None
        best = max(range(16), key=lambda i: st.layers[i])
        if st.layers[best] == 0:
            return None
        return LAYER_NAMES.get(best, "D%d" % best)

    def service_layer(self, s):
        for es in s.video_streams() + s.audio_streams():
            ly = self.layer_of(es["pid"])
            if ly:
                return ly
        return None

    def is_one_seg(self, s):
        return s.sid in self.partial_sids or (s.pmt_pid is not None and 0x1FC8 <= s.pmt_pid <= 0x1FCF)

    def tot_now(self):
        if self.ts_time is None:
            return None
        return self.ts_time + datetime.timedelta(seconds=time.time() - self.ts_time_wall)

    def elapsed(self):
        if self.start_t is None:
            return 0.0
        return max(0.0, self.t - self.start_t)

    def summary(self):
        with self.lock:
            return {
                "packet_size": self.packet_size,
                "packets": self.total,
                "bitrate_bps": round(self.total_rate),
                "effective_bitrate_bps": round(self.nonnull_rate),
                "pcr_mux_rate_bps": round(self.mux_rate) if self.mux_rate else None,
                "tsid": self.tsid, "onid": self.onid, "network_id": self.network_id,
                "network_name": self.network_name, "ts_name": self.ts_name,
                "remote_control_key": self.remote_key,
                "elapsed_s": round(self.elapsed(), 3),
            }

    def to_report(self):
        """Relatório completo (JSON-serializável)."""
        with self.lock:
            rep = {"summary": self.summary()}
            pids = []
            for pid in sorted(self.pids):
                st = self.pids[pid]
                kind, desc = self.describe_pid(pid)
                pids.append({
                    "pid": pid, "hex": "0x%04X" % pid, "type": kind, "description": desc,
                    "layer": self.layer_of(pid), "packets": st.count,
                    "bitrate_bps": round(st.rate),
                    "percent": round(100.0 * st.rate / self.total_rate, 3) if self.total_rate else 0,
                    "cc_errors": st.cc_errors, "tei": st.tei, "scrambled": st.scrambled,
                    "pcr": st.pcr_count, "pcr_max_interval_ms": round(st.pcr_int_max * 1000, 3),
                    "pcr_accuracy_max_ns": round(st.pcr_acc_max, 1),
                    "services": [s.sid for s in self.pid_services(pid)],
                })
            rep["pids"] = pids
            svcs = []
            for sid in sorted(self.services):
                s = self.services[sid]
                if sid not in self.pat and not s.name:
                    continue
                svcs.append({
                    "service_id": sid, "name": s.name, "provider": s.provider,
                    "type": describe_service_type(s.type), "pmt_pid": s.pmt_pid, "pcr_pid": s.pcr_pid,
                    "one_seg": self.is_one_seg(s), "layer": self.service_layer(s),
                    "present": s.present["name"] if s.present else None,
                    "following": s.following["name"] if s.following else None,
                    "streams": [{"pid": es["pid"], "stream_type": "0x%02X" % es["stream_type"],
                                 "type": stream_type_name(es["stream_type"]),
                                 "component_tag": es["info"].get("component_tag"),
                                 "lang": es["info"].get("lang")} for es in s.streams],
                })
            rep["services"] = svcs
            rep["tables"] = [{"pid": ti.pid, "table": ti.name, "table_id": "0x%02X" % ti.table_id,
                              "ext": ti.ext, "version": ti.version, "sections": ti.count,
                              "crc_errors": ti.crc_errors}
                             for ti in sorted(self.tables.values(), key=lambda x: (x.pid, x.table_id, x.ext))]
            rep["tr101290"] = [{"code": e.code, "priority": e.priority, "name": e.name,
                                "count": e.count, "last": e.last_msg} for e in self.etr.values()]
            if self.is_bts:
                rep["isdbt"] = {
                    "layers_bps": {LAYER_NAMES.get(i, str(i)): round(self.layer_rates[i])
                                   for i in (0, 1, 2, 3, 8)},
                    "layer_packets": {LAYER_NAMES.get(i, str(i)): self.layer_counts[i]
                                      for i in range(16) if self.layer_counts[i]},
                    "tsp_counter_errors": self.bts_tsp_errors,
                    "frames": self.bts_frames,
                    "packets_per_frame": self.bts_pkts_per_frame,
                    "emergency_flag": self.bts_emergency,
                    "iip": _jsonable(self.iip) if self.iip else None,
                }
            return rep


def _ago(lt, t):
    if lt is None:
        return "o início"
    return "%.0f ms" % ((t - lt) * 1000)


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items() if k != "layers_info" or True}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (int, float, str, bool)) or o is None:
        return o
    return str(o)
