"""Decodificação das tabelas PSI/SI (MPEG, DVB, ISDB-Tb)."""

from .node import Node
from .names import table_name, stream_type_name, SERVICE_TYPES, RUNNING_STATUS
from .descriptors import parse_descriptor_loop, merge_infos
from .text import mjd_utc, bcd_duration, short_hex


class SectionHeader:
    __slots__ = ("table_id", "long", "length", "ext", "version", "current", "number", "last")

    def __init__(self, s):
        self.table_id = s[0]
        self.long = bool(s[1] & 0x80)
        self.length = ((s[1] & 0x0F) << 8) | s[2]
        if self.long and len(s) >= 8:
            self.ext = (s[3] << 8) | s[4]
            self.version = (s[5] >> 1) & 0x1F
            self.current = s[5] & 1
            self.number = s[6]
            self.last = s[7]
        else:
            self.ext = 0
            self.version = 0
            self.current = 1
            self.number = 0
            self.last = 0


def _u16(b, i):
    return (b[i] << 8) | b[i + 1]


def _header_node(h, pid):
    title = "%s (table_id 0x%02X)" % (table_name(h.table_id), h.table_id)
    n = Node(title)
    n.add("PID", "%d (0x%04X)" % (pid, pid))
    n.add("section_length", h.length)
    if h.long:
        n.add("table_id_extension", "%d (0x%04X)" % (h.ext, h.ext))
        n.add("version", h.version)
        n.add("current_next", h.current)
        n.add("section", "%d / %d" % (h.number, h.last))
    return n


def parse_section(pid, s):
    """Decodifica uma seção completa. Retorna (SectionHeader, Node, data)."""
    h = SectionHeader(s)
    node = _header_node(h, pid)
    data = {}
    body = s[8:-4] if h.long else s[3:]
    tid = h.table_id
    try:
        if tid == 0x00:
            _pat(h, body, node, data)
        elif tid == 0x01:
            _cat(h, body, node, data)
        elif tid == 0x02:
            _pmt(h, body, node, data)
        elif tid in (0x40, 0x41):
            _nit(h, body, node, data)
        elif tid in (0x42, 0x46):
            _sdt(h, body, node, data)
        elif 0x4E <= tid <= 0x6F:
            _eit(h, body, node, data)
        elif tid == 0x70:
            _tdt(h, s[3:8], node, data)
        elif tid == 0x73:
            _tot(h, s[3:-4], node, data)
        elif tid == 0xC4:
            _bit(h, body, node, data)
        elif tid == 0xC3:
            _sdtt(h, body, node, data)
        elif tid == 0xC8:
            _cdt(h, body, node, data)
        elif tid == 0x74:
            _ait(h, body, node, data)
        elif tid in (0x3B, 0x3C):
            _dsmcc(h, body, node, data)
        else:
            node.add("dados", short_hex(body, 48))
    except Exception as exc:
        node.add("ERRO de decodificação", str(exc))
    return h, node, data


def _pat(h, b, node, data):
    progs = {}
    for i in range(0, len(b) - 3, 4):
        pn = _u16(b, i)
        pid = _u16(b, i + 2) & 0x1FFF
        progs[pn] = pid
        if pn == 0:
            node.add("NIT", "PID %d (0x%04X)" % (pid, pid))
        else:
            node.add("programa %d (0x%04X)" % (pn, pn), "PMT PID %d (0x%04X)" % (pid, pid))
    data["tsid"] = h.ext
    data["programs"] = progs


def _cat(h, b, node, data):
    nodes, infos = parse_descriptor_loop(b)
    node.extend(nodes)
    data["ca"] = merge_infos(infos).get("ca", [])


def _pmt(h, b, node, data):
    pcr = _u16(b, 0) & 0x1FFF
    pil = _u16(b, 2) & 0x0FFF
    node.add("program_number", "%d (0x%04X)" % (h.ext, h.ext))
    node.add("PCR_PID", "%d (0x%04X)" % (pcr, pcr))
    pnodes, pinfos = parse_descriptor_loop(b[4:4 + pil])
    if pnodes:
        node.add("descritores do programa", None, pnodes)
    pinfo = merge_infos(pinfos)
    streams = []
    i = 4 + pil
    while i + 5 <= len(b):
        st = b[i]
        pid = _u16(b, i + 1) & 0x1FFF
        el = _u16(b, i + 3) & 0x0FFF
        dnodes, dinfos = parse_descriptor_loop(b[i + 5:i + 5 + el])
        inf = merge_infos(dinfos)
        sn = node.add("ES PID %d (0x%04X)" % (pid, pid), "0x%02X %s" % (st, stream_type_name(st)), dnodes)
        _ = sn
        streams.append({"pid": pid, "stream_type": st, "info": inf,
                        "descriptor_tags": [t for t, _ in dinfos]})
        i += 5 + el
    data.update(program_number=h.ext, pcr_pid=pcr, streams=streams, info=pinfo)


def _nit(h, b, node, data):
    ndl = _u16(b, 0) & 0x0FFF
    nodes, infos = parse_descriptor_loop(b[2:2 + ndl])
    node.add("network_id", "%d (0x%04X)" % (h.ext, h.ext))
    if nodes:
        node.add("descritores da rede", None, nodes)
    ninfo = merge_infos(infos)
    i = 2 + ndl
    tll = _u16(b, i) & 0x0FFF
    i += 2
    end = i + tll
    tss = []
    while i + 6 <= end:
        tsid = _u16(b, i)
        onid = _u16(b, i + 2)
        dl = _u16(b, i + 4) & 0x0FFF
        dnodes, dinfos = parse_descriptor_loop(b[i + 6:i + 6 + dl])
        node.add("TS 0x%04X / ONID 0x%04X" % (tsid, onid), None, dnodes)
        tss.append({"tsid": tsid, "onid": onid, "info": merge_infos(dinfos)})
        i += 6 + dl
    data.update(network_id=h.ext, info=ninfo, ts=tss, actual=(h.table_id == 0x40))


def _sdt(h, b, node, data):
    onid = _u16(b, 0)
    node.add("transport_stream_id", "%d (0x%04X)" % (h.ext, h.ext))
    node.add("original_network_id", "%d (0x%04X)" % (onid, onid))
    services = {}
    i = 3
    while i + 5 <= len(b):
        sid = _u16(b, i)
        flags = b[i + 2]
        rs = b[i + 3] >> 5
        fca = (b[i + 3] >> 4) & 1
        dl = _u16(b, i + 3) & 0x0FFF
        dnodes, dinfos = parse_descriptor_loop(b[i + 5:i + 5 + dl])
        inf = merge_infos(dinfos)
        sn = node.add("serviço %d (0x%04X)" % (sid, sid), inf.get("service_name", ""))
        sn.add("EIT_schedule", (flags >> 1) & 1)
        sn.add("EIT_present_following", flags & 1)
        sn.add("running_status", RUNNING_STATUS.get(rs, rs))
        sn.add("free_CA_mode", fca)
        sn.extend(dnodes)
        services[sid] = {"name": inf.get("service_name", ""), "provider": inf.get("provider", ""),
                         "type": inf.get("service_type"), "running": rs, "free_ca": fca,
                         "eit_pf": flags & 1, "eit_sched": (flags >> 1) & 1}
        i += 5 + dl
    data.update(tsid=h.ext, onid=onid, services=services, actual=(h.table_id == 0x42))


def _eit(h, b, node, data):
    tsid = _u16(b, 0)
    onid = _u16(b, 2)
    node.add("service_id", "%d (0x%04X)" % (h.ext, h.ext))
    node.add("transport_stream_id", "0x%04X" % tsid)
    node.add("original_network_id", "0x%04X" % onid)
    node.add("segment_last_section_number", b[4])
    node.add("last_table_id", "0x%02X" % b[5])
    events = []
    i = 6
    while i + 12 <= len(b):
        eid = _u16(b, i)
        start = mjd_utc(b[i + 2:i + 7])
        dur = bcd_duration(b[i + 7:i + 10])
        rs = b[i + 10] >> 5
        dl = _u16(b, i + 10) & 0x0FFF
        dnodes, dinfos = parse_descriptor_loop(b[i + 12:i + 12 + dl])
        inf = merge_infos(dinfos)
        en = node.add("evento 0x%04X" % eid, inf.get("event_name", ""))
        en.add("início", str(start) if start else "indefinido")
        en.add("duração", str(dur) if dur is not None else "indefinida")
        en.add("running_status", RUNNING_STATUS.get(rs, rs))
        en.extend(dnodes)
        events.append({"event_id": eid, "start": start, "duration": dur, "running": rs,
                       "name": inf.get("event_name", ""), "text": inf.get("event_text", ""),
                       "ext_text": inf.get("ext_text", ""), "ext_items": inf.get("ext_items", []),
                       "genre": inf.get("genre", ""), "rating": inf.get("rating", ""),
                       "lang": inf.get("lang", "")})
        i += 12 + dl
    data.update(service_id=h.ext, tsid=tsid, onid=onid, events=events,
                pf=h.table_id in (0x4E, 0x4F), section=h.number)


def _tdt(h, b, node, data):
    t = mjd_utc(b)
    node.add("UTC_time", str(t))
    data["time"] = t


def _tot(h, b, node, data):
    t = mjd_utc(b[0:5])
    node.add("UTC_time", str(t))
    dl = _u16(b, 5) & 0x0FFF
    nodes, infos = parse_descriptor_loop(b[7:7 + dl])
    node.extend(nodes)
    data["time"] = t
    data["lto"] = merge_infos(infos).get("lto")


def _bit(h, b, node, data):
    node.add("original_network_id", "0x%04X" % h.ext)
    node.add("broadcast_view_propriety", (b[0] >> 4) & 1)
    fdl = _u16(b, 0) & 0x0FFF
    nodes, _ = parse_descriptor_loop(b[2:2 + fdl])
    if nodes:
        node.add("descritores gerais", None, nodes)
    i = 2 + fdl
    names = []
    while i + 3 <= len(b):
        bid = b[i]
        dl = _u16(b, i + 1) & 0x0FFF
        dnodes, dinfos = parse_descriptor_loop(b[i + 3:i + 3 + dl])
        inf = merge_infos(dinfos)
        node.add("broadcaster_id 0x%02X" % bid, inf.get("broadcaster_name", ""), dnodes)
        names.append(inf.get("broadcaster_name", ""))
        i += 3 + dl
    data["broadcasters"] = names


def _sdtt(h, b, node, data):
    node.add("maker_id", "0x%02X" % (h.ext >> 8))
    node.add("model_id", "0x%02X" % (h.ext & 0xFF))
    node.add("transport_stream_id", "0x%04X" % _u16(b, 0))
    node.add("original_network_id", "0x%04X" % _u16(b, 2))
    node.add("service_id", "0x%04X" % _u16(b, 4))
    node.add("num_of_contents", b[6])
    node.add("conteúdo", short_hex(b[7:], 48))


def _cdt(h, b, node, data):
    node.add("download_data_id", "0x%04X" % h.ext)
    node.add("original_network_id", "0x%04X" % _u16(b, 0))
    node.add("data_type", "0x%02X %s" % (b[2], "(logotipo)" if b[2] == 1 else ""))
    dl = _u16(b, 3) & 0x0FFF
    nodes, _ = parse_descriptor_loop(b[5:5 + dl])
    node.extend(nodes)
    rest = b[5 + dl:]
    node.add("data_module", "%d bytes" % len(rest))


_AIT_CONTROL = {1: "AUTOSTART", 2: "PRESENT", 3: "DESTROY", 4: "KILL", 5: "PREFETCH",
                6: "REMOTE", 7: "DISABLED", 8: "PLAYBACK_AUTOSTART"}


def _ait(h, b, node, data):
    node.add("application_type", "0x%04X %s" % (h.ext & 0x7FFF,
             {0x0009: "Ginga-NCL", 0x000A: "Ginga-J", 0x0010: "HbbTV"}.get(h.ext & 0x7FFF, "")))
    cdl = _u16(b, 0) & 0x0FFF
    nodes, _ = parse_descriptor_loop(b[2:2 + cdl])
    if nodes:
        node.add("descritores comuns", None, nodes)
    i = 2 + cdl
    all_ = _u16(b, i) & 0x0FFF
    i += 2
    end = i + all_
    apps = []
    while i + 9 <= end:
        org = int.from_bytes(b[i:i + 4], "big")
        aid = _u16(b, i + 4)
        cc = b[i + 6]
        dl = _u16(b, i + 7) & 0x0FFF
        dnodes, _ = parse_descriptor_loop(b[i + 9:i + 9 + dl])
        node.add("app org 0x%08X id 0x%04X" % (org, aid), _AIT_CONTROL.get(cc, "0x%02X" % cc), dnodes)
        apps.append((org, aid, cc))
        i += 9 + dl
    data["apps"] = apps


_DSMCC_MSG = {0x1006: "DSI", 0x1002: "DII", 0x1003: "DDB"}


def _dsmcc(h, b, node, data):
    if len(b) < 12:
        return
    node.add("protocolDiscriminator", "0x%02X" % b[0])
    node.add("dsmccType", "0x%02X" % b[1])
    mid = _u16(b, 2)
    node.add("messageId", "0x%04X %s" % (mid, _DSMCC_MSG.get(mid, "")))
    node.add("transaction/download_id", "0x%08X" % int.from_bytes(b[4:8], "big"))
    al = b[9]
    ml = _u16(b, 10)
    node.add("messageLength", ml)
    p = 12 + al
    if mid == 0x1003 and len(b) >= p + 6:
        node.add("moduleId", "0x%04X" % _u16(b, p))
        node.add("moduleVersion", b[p + 2])
        node.add("blockNumber", _u16(b, p + 4))
    elif mid == 0x1002 and len(b) >= p + 18:
        node.add("blockSize", _u16(b, p + 4))
        cdl = _u16(b, p + 16)
        q = p + 18 + cdl
        if len(b) >= q + 2:
            nm = _u16(b, q)
            node.add("numberOfModules", nm)
            q += 2
            for _ in range(nm):
                if len(b) < q + 8:
                    break
                mi_len = b[q + 7]
                node.add("módulo 0x%04X" % _u16(b, q), "%d bytes, versão %d" % (
                    int.from_bytes(b[q + 2:q + 6], "big"), b[q + 6]))
                q += 8 + mi_len


def describe_service_type(st):
    if st is None:
        return ""
    return SERVICE_TYPES.get(st, "0x%02X" % st)
