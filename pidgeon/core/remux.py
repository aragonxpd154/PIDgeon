"""Remultiplexação TS -> BTS ISDB-Tb (204 bytes) para laboratório e testes.

Monta o quadro de multiplexação a partir dos parâmetros de transmissão (modo, intervalo de guarda
e modulação/CR/segmentos de cada camada), distribui os TSPs de cada camada no quadro, insere a IIP
(PID 0x1FF0) uma vez por quadro, preenche com nulos e reescreve o PCR de acordo com a nova posição
de cada pacote. Cada pacote recebe os 8 bytes de "ISDB-T information" (camada, contador TSP,
cabeçalho de quadro) e, opcionalmente, 8 bytes de paridade RS(204,196).

Referências: ARIB STD-B31 / ABNT NBR 15601 (cap. 5 do anexo — sinal BTS), tabela de TSPs por quadro.
"""

from dataclasses import dataclass, field

from .crc import crc32_mpeg
from .isdbt import layer_capacity_bps
from .names import ISDB_MOD_BITS, ISDB_CODERATE_VAL, ISDB_GUARD_VAL, ISDB_MODULATION, ISDB_CODERATE, ISDB_GUARD
from .rs import rs_204_196

BTS_RATE = 32507936.507936          # bps, pacotes de 204 bytes (4 × 512/63 MHz)
TSP_PERIOD = 204 * 8 / BTS_RATE     # s por TSP (vale também para o BTS 188: mesma taxa de pacotes)
PCR_WRAP = (1 << 33) * 300
NULL_188 = b"\x47\x1f\xff\x10" + b"\xff" * 184
IIP_PID = 0x1FF0
NULL_PID = 0x1FFF

# PIDs de SI compartilhados que, com recepção parcial, vão para a camada A (o 1-seg só recebe a A)
SHARED_SI_ONESEG = (0x00, 0x10, 0x11, 0x14, 0x23, 0x24, 0x27, 0x28, 0x29)


@dataclass
class Layer:
    mod: int = 3      # 0 DQPSK, 1 QPSK, 2 16-QAM, 3 64-QAM
    cr: int = 2       # 0 1/2, 1 2/3, 2 3/4, 3 5/6, 4 7/8
    ti: int = 2       # código do entrelaçamento temporal (0..3)
    segs: int = 12

    def label(self):
        return "%s %s, %d seg" % (ISDB_MODULATION.get(self.mod), ISDB_CODERATE.get(self.cr), self.segs)


@dataclass
class IsdbParams:
    mode: int = 3
    gi: int = 1                      # 0 1/32, 1 1/16, 2 1/8, 3 1/4
    partial: bool = True             # recepção parcial (1-seg) na camada A
    layers: list = field(default_factory=lambda: [Layer(1, 1, 3, 1), Layer(3, 2, 2, 12), None])

    @classmethod
    def from_iip(cls, iip):
        """Constrói a partir do dicionário de parse_iip() (configuração atual)."""
        if not iip or not iip.get("valid"):
            return None
        layers = []
        for lp in iip.get("layers") or []:
            if lp["mod"] == 7 or lp["segs"] in (0, 15):
                layers.append(None)
            else:
                layers.append(Layer(lp["mod"], lp["cr"], lp["ti"], lp["segs"]))
        while len(layers) < 3:
            layers.append(None)
        return cls(mode=iip.get("mode") or 3, gi=iip.get("gi") or 0, partial=bool(iip.get("partial")),
                   layers=layers[:3])

    def validate(self):
        if self.mode not in (1, 2, 3):
            raise ValueError("Modo deve ser 1, 2 ou 3")
        if self.gi not in ISDB_GUARD_VAL:
            raise ValueError("Intervalo de guarda inválido")
        used = [l for l in self.layers if l]
        if not used:
            raise ValueError("Pelo menos uma camada precisa estar ativa")
        for l in used:
            if l.mod not in ISDB_MOD_BITS or l.cr not in ISDB_CODERATE_VAL or not (1 <= l.segs <= 13) \
               or l.ti not in (0, 1, 2, 3):
                raise ValueError("Parâmetros de camada inválidos: %s" % (l,))
        total = sum(l.segs for l in used)
        if total != 13:
            raise ValueError("A soma dos segmentos das camadas deve ser 13 (está %d)" % total)
        if self.partial and (not self.layers[0] or self.layers[0].segs != 1):
            raise ValueError("Recepção parcial (1-seg) exige a camada A com 1 segmento")
        return self

    def frame_tsps(self):
        """TSPs por quadro de multiplexação (BTS a 32,507936 Mbps)."""
        return int(round(1024 * (1 << (self.mode - 1)) * (1 + ISDB_GUARD_VAL[self.gi])))

    def layer_tsps(self, i):
        """TSPs da camada i (0=A) por quadro: 12·2^(m-1)·bits·CR por segmento."""
        l = self.layers[i]
        if not l:
            return 0
        return int(round(12 * (1 << (self.mode - 1)) * ISDB_MOD_BITS[l.mod] * ISDB_CODERATE_VAL[l.cr] * l.segs))

    def capacity_bps(self, i):
        l = self.layers[i]
        return layer_capacity_bps(l.mod, l.cr, l.segs, self.gi) if l else 0.0

    def describe(self):
        parts = ["Modo %d, GI %s" % (self.mode, ISDB_GUARD.get(self.gi))]
        for name, l, i in zip("ABC", self.layers, range(3)):
            if l:
                parts.append("%s: %s (%.3f Mbps)" % (name, l.label(), self.capacity_bps(i) / 1e6))
        if self.partial:
            parts.append("1-seg na A")
        return " · ".join(parts)


def frame_layout(params):
    """Lista com o indicador de camada de cada TSP do quadro (1=A, 2=B, 3=C, 0=nulo, 8=IIP)."""
    n = params.frame_tsps()
    want = [params.layer_tsps(i) for i in range(3)]
    if sum(want) >= n:
        raise ValueError("Parâmetros excedem a capacidade do quadro")
    slots = [0] * n
    credit = [0.0, 0.0, 0.0]
    got = [0, 0, 0]
    for s in range(n):
        best = -1
        for i in range(3):
            credit[i] += want[i] / n
            if got[i] < want[i] and credit[i] >= 1.0 - 1e-9 and (best < 0 or credit[i] > credit[best]):
                best = i
        if best >= 0:
            credit[best] -= 1.0
            got[best] += 1
            slots[s] = best + 1
    # completa eventuais faltas de arredondamento nos últimos nulos
    for i in range(3):
        s = n - 1
        while got[i] < want[i] and s >= 0:
            if slots[s] == 0:
                slots[s] = i + 1
                got[i] += 1
            s -= 1
    iip_slot = slots.index(0)
    slots[iip_slot] = 8
    return slots


class _Bits:
    def __init__(self):
        self.bits = []

    def put(self, v, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def tobytes(self):
        b = self.bits
        return bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))


def build_iip_payload(params, pointer=0, nsi=b""):
    """Payload (184 bytes) do IIP com a configuração atual = próxima. nsi: network_synchronization_information."""
    w = _Bits()
    w.put(1, 1)            # TMCC_synchronization_word
    w.put(0, 1)            # AC_data_effective_position
    w.put(3, 2)            # reservado
    w.put(0, 4)            # initialization_timing_indicator
    w.put(params.mode, 2)
    w.put(params.gi, 2)
    w.put(params.mode, 2)
    w.put(params.gi, 2)
    w.put(0, 2)            # system_identifier: TV digital terrestre
    w.put(15, 4)           # count_down_index
    w.put(0, 1)            # alerta (EWBS)
    for _ in range(2):     # configuração atual e próxima
        w.put(1 if params.partial else 0, 1)
        for l in params.layers:
            if l:
                w.put(l.mod, 3)
                w.put(l.cr, 3)
                w.put(l.ti, 3)
                w.put(l.segs, 4)
            else:
                w.put(7, 3)
                w.put(7, 3)
                w.put(7, 3)
                w.put(15, 4)
    w.put(7, 3)            # phase_correction_of_CP
    w.put(0xFFF, 12)       # TMCC_reserved_future_use
    w.put(0x3FF, 10)       # reservado
    raw = w.tobytes()
    mcci = raw + crc32_mpeg(raw).to_bytes(4, "big")
    payload = bytes(((pointer >> 8) & 0xFF, pointer & 0xFF)) + mcci + b"\x00\x00" + bytes((len(nsi),)) + nsi
    return (payload + b"\xff" * 184)[:184]


def set_iip_pointer(payload, pointer):
    return bytes(((pointer >> 8) & 0xFF, pointer & 0xFF)) + bytes(payload[2:184])


def pcr_of(p):
    """PCR (27 MHz) de um pacote de 188 bytes, ou None."""
    if not (p[3] & 0x20) or p[4] < 7 or not (p[5] & 0x10):
        return None
    base = (p[6] << 25) | (p[7] << 17) | (p[8] << 9) | (p[9] << 1) | (p[10] >> 7)
    return base * 300 + (((p[10] & 1) << 8) | p[11])


def with_pcr(p, pcr):
    pcr %= PCR_WRAP
    base = pcr // 300
    ext = pcr % 300
    b = bytearray(p)
    b[6] = (base >> 25) & 0xFF
    b[7] = (base >> 17) & 0xFF
    b[8] = (base >> 9) & 0xFF
    b[9] = (base >> 1) & 0xFF
    b[10] = ((base & 1) << 7) | 0x7E | (ext >> 8)
    b[11] = ext & 0xFF
    return bytes(b)


def bts_trailer(layer, tsp_counter, frame_head, frame_odd):
    b0 = (2 << 6) | (1 << 5) | ((1 << 2) if frame_head else 0) | ((1 << 1) if frame_head else 0) | (frame_odd & 1)
    return bytes((b0, (layer << 4) | 0x0F, 0x80 | ((tsp_counter >> 8) & 0x1F), tsp_counter & 0xFF)) + b"\xff" * 4


class BtsMux:
    """Recebe (t_entrada, pacote188) e produz (t_saida, pacote) no BTS.

    layer_of_pid: função pid -> 1/2/3 (camada). Pacotes nulos e IIP da entrada são descartados.
    out_size: 204 (BTS completo) ou 188 (BTS sem os 16 bytes finais).
    """

    def __init__(self, params, layer_of_pid, out_size=204, rs_parity=False, iip_payload=None,
                 delay=0.25, log=None):
        params.validate()
        self.p = params
        self.layer_of_pid = layer_of_pid
        self.out_size = out_size
        self.rs = rs_204_196() if rs_parity else None
        self.slots = frame_layout(params)
        self.frame_len = len(self.slots)
        self.iip_slot = self.slots.index(8)
        ptr = self.frame_len - self.iip_slot
        self.iip = set_iip_pointer(iip_payload, ptr) if iip_payload else build_iip_payload(params, ptr)
        self.iip_cc = 0
        self.delay = delay
        self.queues = {1: [], 2: [], 3: []}
        self.qpos = {1: 0, 2: 0, 3: 0}
        self.slot = 0
        self.frame = 0
        self.t0 = None
        self.log = log or (lambda m: None)
        self.dropped = {1: 0, 2: 0, 3: 0}
        self.late_warned = set()
        self.packets_in = 0
        self.max_late = 0.0
        self.pcr_state = {}
        self.pcr_out = {}
        self.pcr_inserted = 0

    # ------------------------------------------------------------ entrada
    def push(self, t, pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid in (NULL_PID, IIP_PID):
            return
        if self.t0 is None:
            self.t0 = t
        layer = self.layer_of_pid(pid)
        if layer not in (1, 2, 3) or not self.p.layers[layer - 1]:
            layer = self._fallback_layer()
        self.queues[layer].append((t - self.t0, pkt))
        self.packets_in += 1

    def _fallback_layer(self):
        best = max(range(3), key=lambda i: self.p.capacity_bps(i))
        return best + 1

    def _restamp(self, pkt, pcr, t_slot, t_in):
        """PCR regenerado pelo relógio do BTS: linear no tempo do slot (precisão exata), com o
        deslocamento de cada PID fixado no primeiro PCR e refeito em descontinuidades."""
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        now = int(round(t_slot * 27e6))
        st = self.pcr_state.get(pid)
        if st is not None:
            d = (pcr - st[1]) % PCR_WRAP
            disc = (pkt[5] & 0x80) or not (0 < d < int(0.7 * 27e6))
            if not disc:
                st[1] = pcr
                return now + st[0]
        off = pcr - now - int(round((t_in + self.delay - t_slot) * 27e6))
        self.pcr_state[pid] = [off, pcr]
        return now + off

    PCR_GAP = 0.030      # s: insere PCR antes de chegar aos 40 ms da TR 101 290 (2.3a)

    def _pcr_track(self, pkt, layer, t_slot, has_pcr):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        st = self.pcr_out.get(pid)
        if has_pcr:
            if st is None:
                st = self.pcr_out[pid] = {"layer": layer}
            st["t"] = t_slot
        if st is not None:
            st["cc"] = pkt[3] & 0x0F

    def _pcr_insert(self, layer, t_slot, q, i):
        """Pacote só com adaptation field + PCR (CC não incrementa) quando um PID de PCR da camada
        ficaria mais de PCR_GAP sem PCR — os picos de taxa (quadros I) atrasam o pacote original."""
        for pid, st in self.pcr_out.items():
            if st["layer"] != layer or t_slot - st["t"] <= self.PCR_GAP or pid not in self.pcr_state:
                continue
            if i < len(q):
                head = q[i][1]
                if ((head[1] & 0x1F) << 8 | head[2]) == pid and pcr_of(head) is not None \
                        and q[i][0] + self.delay <= t_slot:
                    continue          # o PCR original já sai neste slot
            off = self.pcr_state[pid][0]
            pcr = (int(round(t_slot * 27e6)) + off) % PCR_WRAP
            pkt = bytes((0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x20 | st["cc"], 183, 0x10)) + bytes(6) + \
                b"\xff" * 176
            st["t"] = t_slot
            self.pcr_inserted += 1
            return with_pcr(pkt, pcr)
        return None

    def horizon(self):
        """Tempo de entrada até onde os slots já podem ser decididos."""
        return self.slot * TSP_PERIOD - self.delay

    # ------------------------------------------------------------ saída
    def _emit(self, layer, pkt, pos):
        if self.out_size == 188:
            return pkt
        tr = bts_trailer(layer, pos, pos == 0, self.frame)
        if self.rs:
            return pkt + tr + self.rs.encode(pkt + tr)
        return pkt + tr + b"\x00" * 8

    def pull(self, until_t=None, flush=False):
        """Gera slots enquanto houver dados de entrada suficientes. Retorna lista de (t, pacote)."""
        out = []
        while True:
            t_slot = self.slot * TSP_PERIOD
            if not flush and until_t is not None and t_slot - self.delay > until_t:
                break
            pos = self.slot % self.frame_len
            if flush and pos == 0 and not any(len(q) > self.qpos[k] for k, q in self.queues.items()):
                break  # termina num limite de quadro
            if pos == 0 and self.slot:
                self.frame += 1
            layer = self.slots[pos]
            pkt = None
            if layer == 8:
                pkt = bytes((0x47, 0x1F, 0xF0, 0x10 | self.iip_cc)) + self.iip
                self.iip_cc = (self.iip_cc + 1) & 0x0F
            elif layer:
                q = self.queues[layer]
                i = self.qpos[layer]
                pkt = self._pcr_insert(layer, t_slot, q, i)
            if layer and layer != 8 and pkt is None:
                # descarta o que está atrasado demais (camada sobrecarregada)
                while i < len(q) and t_slot - (q[i][0] + self.delay) > 2.0:
                    i += 1
                    self.dropped[layer] += 1
                if i < len(q) and q[i][0] + self.delay <= t_slot:
                    t_in, pkt = q[i]
                    i += 1
                    late = t_slot - (t_in + self.delay)
                    if late > self.max_late:
                        self.max_late = late
                    if late > 0.5 and layer not in self.late_warned:
                        self.late_warned.add(layer)
                        self.log("Camada %s sobrecarregada: a taxa de entrada excede a capacidade (%.3f Mbps)"
                                 % ("ABC"[layer - 1], self.p.capacity_bps(layer - 1) / 1e6))
                    pcr = pcr_of(pkt)
                    if pcr is not None:
                        pkt = with_pcr(pkt, self._restamp(pkt, pcr, t_slot, t_in))
                    self._pcr_track(pkt, layer, t_slot, pcr is not None)
                if i > 4096:
                    del q[:i]
                    i = 0
                self.qpos[layer] = i
            if pkt is None:
                pkt = NULL_188
            out.append((t_slot, self._emit(layer, pkt, pos)))
            self.slot += 1
        return out
