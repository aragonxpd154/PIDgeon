"""ISDB-T: informação de 8 bytes do BTS (204 bytes) e IIP (PID 0x1FF0).

Referências: ARIB STD-B31 cap. 5 / ABNT NBR 15601 anexo, tabelas 5-5 a 5-11.
"""

from .node import Node
from .crc import crc32_mpeg
from .names import (TMCC_IDENTIFIER, LAYER_LONG, ISDB_MODES, ISDB_GUARD, ISDB_GUARD_VAL,
                    ISDB_MODULATION, ISDB_MOD_BITS, ISDB_CODERATE, ISDB_CODERATE_VAL,
                    ISDB_TIME_INTERLEAVE, ISDB_SYSTEM_ID)


class BTSInfo:
    """Os 8 bytes de 'ISDB-T information' após os 188 bytes do TSP."""
    __slots__ = ("tmcc_id", "buffer_reset", "emergency", "init_timing_head", "frame_head",
                 "frame_indicator", "layer", "countdown", "ac_invalid", "ac_bytes",
                 "tsp_counter", "ac_data")

    def __init__(self, t):
        b0 = t[0]
        self.tmcc_id = b0 >> 6
        self.buffer_reset = (b0 >> 4) & 1
        self.emergency = (b0 >> 3) & 1
        self.init_timing_head = (b0 >> 2) & 1
        self.frame_head = (b0 >> 1) & 1
        self.frame_indicator = b0 & 1
        self.layer = t[1] >> 4
        self.countdown = t[1] & 0xF
        self.ac_invalid = t[2] >> 7
        self.ac_bytes = ((t[2] >> 5) & 3) + 1
        self.tsp_counter = ((t[2] & 0x1F) << 8) | t[3]
        self.ac_data = None if self.ac_invalid else int.from_bytes(t[4:8], "big")

    def to_node(self):
        n = Node("ISDB-T information (BTS)")
        n.add("TMCC_identifier", "%d — %s" % (self.tmcc_id, TMCC_IDENTIFIER.get(self.tmcc_id)))
        n.add("buffer_reset_control_flag", self.buffer_reset)
        n.add("switch-on_control_flag (EWBS)", self.emergency)
        n.add("initialization_timing_head_packet_flag", self.init_timing_head)
        n.add("frame_head_packet_flag", self.frame_head)
        n.add("frame_indicator", "%d (%s)" % (self.frame_indicator, "ímpar" if self.frame_indicator else "par"))
        n.add("layer_indicator", "%d — %s" % (self.layer, LAYER_LONG.get(self.layer, "dados do provedor")))
        n.add("count_down_index", self.countdown)
        n.add("AC_data_invalid_flag", self.ac_invalid)
        n.add("TSP_counter", self.tsp_counter)
        if self.ac_data is not None:
            n.add("AC_data_effective_bytes", self.ac_bytes)
            n.add("AC_data", "0x%08X" % self.ac_data)
        return n


def layer_capacity_bps(mod, cr, segs, gi):
    """Capacidade útil (bps, TS 188) de uma camada ISDB-T para canal de 6 MHz.

    Por segmento: portadoras de dados = 96·2^(m-1), Tu = 252 µs·2^(m-1);
    o modo se cancela, então: 96 · bits · CR · 188/204 / (252e-6 · (1+GI)).
    """
    if mod not in ISDB_MOD_BITS or cr not in ISDB_CODERATE_VAL or not (1 <= segs <= 13):
        return 0.0
    g = ISDB_GUARD_VAL.get(gi, 1 / 8)
    per_seg = 96 * ISDB_MOD_BITS[mod] * ISDB_CODERATE_VAL[cr] * (188 / 204) / (252e-6 * (1 + g))
    return per_seg * segs


def _tp(v):
    """13 bits de parâmetros de transmissão de uma camada."""
    return {"mod": v >> 10, "cr": (v >> 7) & 7, "ti": (v >> 4) & 7, "segs": v & 0xF}


class BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def get(self, n):
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v


def parse_iip(payload):
    """Decodifica o payload (184 bytes) de um pacote IIP. Retorna (Node, data)."""
    p = payload
    root = Node("IIP — ISDB-T Information Packet (PID 0x1FF0)")
    data = {"valid": False}
    if len(p) < 2 + 20 + 3:
        root.add("ERRO", "payload curto")
        return root, data
    ptr = (p[0] << 8) | p[1]
    root.add("IIP_packet_pointer", ptr)
    mcci = p[2:22]
    crc_ok = crc32_mpeg(mcci) == 0
    r = BitReader(mcci)
    sync = r.get(1)
    acpos = r.get(1)
    r.get(2)
    itiming = r.get(4)
    cur_mode = r.get(2)
    cur_gi = r.get(2)
    nxt_mode = r.get(2)
    nxt_gi = r.get(2)
    sysid = r.get(2)
    countdown = r.get(4)
    alert = r.get(1)
    cur_partial = r.get(1)
    cur = [_tp(r.get(13)) for _ in range(3)]
    nxt_partial = r.get(1)
    nxt = [_tp(r.get(13)) for _ in range(3)]
    phase = r.get(3)

    m = root.add("modulation_control_configuration_information",
                 "CRC %s" % ("OK" if crc_ok else "ERRO"))
    m.add("TMCC_synchronization_word", sync)
    m.add("AC_data_effective_position", acpos)
    m.add("initialization_timing_indicator", itiming)
    m.add("modo atual", ISDB_MODES.get(cur_mode))
    m.add("intervalo de guarda atual", ISDB_GUARD.get(cur_gi))
    m.add("próximo modo", ISDB_MODES.get(nxt_mode))
    m.add("próximo intervalo de guarda", ISDB_GUARD.get(nxt_gi))
    t = m.add("TMCC_information")
    t.add("system_identifier", ISDB_SYSTEM_ID.get(sysid))
    t.add("count_down_index", countdown)
    t.add("switch-on_control_flag (alerta EWBS)", alert)

    layers_cur = _layers_node(t.add("configuração ATUAL", "recepção parcial (1-seg): %s"
                                     % ("sim" if cur_partial else "não")), cur, cur_mode, cur_gi)
    _layers_node(t.add("PRÓXIMA configuração", "recepção parcial (1-seg): %s"
                       % ("sim" if nxt_partial else "não")), nxt, nxt_mode, cur_gi)
    t.add("phase_correction_of_CP", phase)

    idx = 22
    branch = p[idx]
    last_branch = p[idx + 1]
    root.add("IIP_branch_number", "%d / %d" % (branch, last_branch))
    idx += 2
    nsi_len = p[idx]
    idx += 1
    nsi = p[idx:idx + nsi_len]
    sfn = None
    if nsi_len and len(nsi) >= 1:
        sfn = _parse_nsi(nsi, root)
    data.update(valid=True, crc_ok=crc_ok, mode=cur_mode, gi=cur_gi, next_mode=nxt_mode,
                next_gi=nxt_gi, countdown=countdown, alert=alert, partial=cur_partial,
                layers=cur, next_layers=nxt, next_partial=nxt_partial, system=sysid,
                sfn=sfn, branch=branch, last_branch=last_branch, layers_info=layers_cur)
    return root, data


def _layers_node(parent, layers, mode, gi):
    out = []
    total = 0.0
    for name, lp in zip("ABC", layers):
        if lp["mod"] == 7 or lp["segs"] in (0, 15):
            parent.add("Camada %s" % name, "não usada")
            out.append(None)
            continue
        cap = layer_capacity_bps(lp["mod"], lp["cr"], lp["segs"], gi)
        total += cap
        ti = ISDB_TIME_INTERLEAVE.get(lp["ti"], {}).get(mode, "?")
        ln = parent.add("Camada %s" % name, "%s, CR %s, %d seg, I=%s — %.3f Mbps" % (
            ISDB_MODULATION.get(lp["mod"], "?"), ISDB_CODERATE.get(lp["cr"], "?"),
            lp["segs"], ti, cap / 1e6))
        ln.add("modulação", ISDB_MODULATION.get(lp["mod"], lp["mod"]))
        ln.add("taxa de código (CR)", ISDB_CODERATE.get(lp["cr"], lp["cr"]))
        ln.add("entrelaçamento temporal", "código %d (I=%s)" % (lp["ti"], ti))
        ln.add("segmentos", lp["segs"])
        ln.add("capacidade (TS 188)", "%.3f Mbps" % (cap / 1e6))
        out.append({"name": name, "mod": ISDB_MODULATION.get(lp["mod"]), "cr": ISDB_CODERATE.get(lp["cr"]),
                    "segs": lp["segs"], "ti": ti, "capacity": cap})
    parent.add("capacidade total", "%.3f Mbps" % (total / 1e6))
    return out


def _parse_nsi(nsi, root):
    sid = nsi[0]
    n = root.add("network_synchronization_information", "synchronization_id 0x%02X" % sid)
    if sid != 0x00:
        n.add("dados", nsi[1:].hex())
        return {"sync_id": sid}
    if len(nsi) < 8:
        return {"sync_id": sid}
    ts = int.from_bytes(nsi[1:4], "big")
    md = int.from_bytes(nsi[4:7], "big")
    ecl = nsi[7]
    n.add("synchronization_time_stamp", "%d (×100 ns = %.4f ms)" % (ts, ts / 1e4))
    n.add("maximum_delay", "%d (×100 ns = %.4f ms)" % (md, md / 1e4))
    equip = []
    i = 8
    end = min(8 + ecl, len(nsi))
    while i + 5 <= end:
        v = int.from_bytes(nsi[i:i + 5], "big")
        eid = v >> 28
        renewal = (v >> 27) & 1
        static = (v >> 26) & 1
        pol = (v >> 25) & 1
        off = v & 0xFFFFFF
        n.add("equipamento 0x%03X" % eid, "renovação %d, atraso estático %d, offset %s%.4f ms"
              % (renewal, static, "-" if pol else "+", off / 1e4))
        equip.append({"id": eid, "renewal": renewal, "static": static,
                      "offset_100ns": -off if pol else off})
        i += 5
    if end + 4 <= len(nsi):
        crc_ok = crc32_mpeg(nsi[1:end + 4]) == 0
        n.add("CRC", "OK" if crc_ok else "ERRO")
    return {"sync_id": sid, "timestamp_100ns": ts, "max_delay_100ns": md, "equipment": equip}
