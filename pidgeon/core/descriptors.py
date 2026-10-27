"""Decodificação de descritores MPEG / DVB / ISDB-Tb (ABNT NBR 15603)."""

from .node import Node
from .names import (descriptor_name, SERVICE_TYPES, GENRES_BR, RATING_BR,
                    VIDEO_ENCODE_FORMAT, ISDB_GUARD)
from .text import decode_text, lang_code, short_hex, mjd_to_date, bcd

_AAC_PROFILES = {0x50: "AAC Profile L1", 0x51: "AAC Profile L2", 0x52: "AAC Profile L4",
                 0x58: "HE-AAC L2", 0x59: "HE-AAC L3", 0x5A: "HE-AAC L4", 0x5B: "HE-AAC L5",
                 0x60: "HE-AAC v2 L2", 0x61: "HE-AAC v2 L3", 0x62: "HE-AAC v2 L4"}
_AUDIO_SAMPLING = {1: "16 kHz", 2: "22,05 kHz", 3: "24 kHz", 5: "32 kHz", 6: "44,1 kHz", 7: "48 kHz"}
_AUDIO_COMPONENT_TYPE = {0x01: "mono 1/0", 0x02: "dual mono 1/0+1/0", 0x03: "estéreo 2/0",
                         0x04: "2/1", 0x05: "3/0", 0x06: "2/2", 0x07: "3/1", 0x08: "3/2",
                         0x09: "3/2+LFE (5.1)", 0x40: "descrição de áudio", 0x41: "áudio p/ deficiente auditivo"}
_TRANSMISSION_TYPE = {0x0F: "camada A/B/C (tipo A)", 0xAF: "1-seg (tipo C / recepção parcial)",
                      0x1F: "tipo B"}
_DATA_COMPONENT = {0x0008: "ARIB caption", 0x0009: "ARIB carrossel", 0x000C: "BML",
                   0x0012: "ARIB carrossel (Japão)", 0x00A0: "Ginga-NCL", 0x00A1: "Ginga-J",
                   0x00A2: "Ginga (carrossel de objetos)", 0x00A3: "Ginga-NCL/Lua"}
_COPY_CONTROL = {0: "cópia livre", 1: "definido pelo serviço", 2: "uma geração", 3: "proibida"}
_ISDB_CA_SYSTEMS = {0x0005: "Multi2 (ARIB)"}


def _u16(b, i):
    return (b[i] << 8) | b[i + 1]


def parse_descriptor(tag, body, ctx=None):
    """Decodifica um descritor. Retorna (Node, info: dict)."""
    name = descriptor_name(tag)
    node = Node("0x%02X %s" % (tag, name), "%d bytes" % len(body))
    info = {}
    try:
        _DECODERS.get(tag, _generic)(body, node, info, ctx or {})
    except Exception as exc:  # descritor mal formado não pode derrubar a análise
        node.add("ERRO de decodificação", str(exc))
        node.add("Dados", short_hex(body, 64))
    return node, info


def parse_descriptor_loop(data, ctx=None):
    """Percorre um laço de descritores. Retorna (lista de Node, lista de (tag, info))."""
    nodes, infos = [], []
    i, n = 0, len(data)
    while i + 2 <= n:
        tag, ln = data[i], data[i + 1]
        body = data[i + 2:i + 2 + ln]
        if len(body) < ln:
            nodes.append(Node("descritor truncado 0x%02X" % tag, "%d/%d bytes" % (len(body), ln)))
            break
        nd, inf = parse_descriptor(tag, body, ctx)
        nodes.append(nd)
        infos.append((tag, inf))
        i += 2 + ln
    return nodes, infos


def merge_infos(infos):
    out = {}
    for tag, inf in infos:
        for k, v in inf.items():
            if k in ("ca", "ext_items", "frequencies", "components", "languages"):
                out.setdefault(k, []).extend(v if isinstance(v, list) else [v])
            elif k == "ext_text":
                out[k] = out.get(k, "") + v
            else:
                out.setdefault(k, v)
    return out


# ---------------------------------------------------------------- decoders
def _generic(b, node, info, ctx):
    if b:
        node.add("Dados", short_hex(b, 64))


def _registration(b, node, info, ctx):
    fid = bytes(b[:4])
    txt = fid.decode("latin-1") if all(32 <= c < 127 for c in fid) else fid.hex()
    node.add("format_identifier", txt)
    info["registration"] = txt
    if len(b) > 4:
        node.add("info adicional", short_hex(b[4:]))


def _ca(b, node, info, ctx):
    sysid = _u16(b, 0)
    pid = _u16(b, 2) & 0x1FFF
    node.add("CA_system_id", "0x%04X %s" % (sysid, _ISDB_CA_SYSTEMS.get(sysid, "")))
    node.add("CA_PID", "%d (0x%04X)" % (pid, pid))
    if len(b) > 4:
        node.add("private_data", short_hex(b[4:]))
    info["ca"] = [(sysid, pid)]


def _language(b, node, info, ctx):
    langs = []
    for i in range(0, len(b) - 3, 4):
        lc = lang_code(b[i:i + 3])
        at = b[i + 3]
        node.add("idioma", "%s (audio_type %d)" % (lc, at))
        langs.append(lc)
    if langs:
        info["lang"] = langs[0]
        info["languages"] = langs


def _max_bitrate(b, node, info, ctx):
    v = ((b[0] & 0x3F) << 16 | b[1] << 8 | b[2]) * 400
    node.add("maximum_bitrate", "%d bps" % v)


def _carousel_id(b, node, info, ctx):
    node.add("carousel_id", "0x%08X" % int.from_bytes(b[:4], "big"))
    if len(b) > 4:
        node.add("format_id", b[4])


def _association_tag(b, node, info, ctx):
    node.add("association_tag", "0x%04X" % _u16(b, 0))
    node.add("use", "0x%04X" % _u16(b, 2))


def _stream_identifier(b, node, info, ctx):
    node.add("component_tag", "0x%02X" % b[0])
    info["component_tag"] = b[0]


def _avc_video(b, node, info, ctx):
    prof = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High", 110: "High 10",
            122: "High 4:2:2", 244: "High 4:4:4"}
    node.add("profile_idc", "%d (%s)" % (b[0], prof.get(b[0], "?")))
    node.add("constraint flags", "0x%02X" % b[1])
    node.add("level_idc", "%d (%.1f)" % (b[2], b[2] / 10))
    if len(b) > 3:
        node.add("AVC_still_present", (b[3] >> 7) & 1)
        node.add("AVC_24_hour_picture", (b[3] >> 6) & 1)
    info["video_profile"] = "%s@L%.1f" % (prof.get(b[0], str(b[0])), b[2] / 10)


def _hevc_video(b, node, info, ctx):
    node.add("profile_space", b[0] >> 6)
    node.add("tier", (b[0] >> 5) & 1)
    node.add("profile_idc", b[0] & 0x1F)
    if len(b) >= 12:
        node.add("level_idc", "%d (%.1f)" % (b[11], b[11] / 30))


def _network_name(b, node, info, ctx):
    s = decode_text(b)
    node.add("nome da rede", s)
    info["network_name"] = s


def _service_list(b, node, info, ctx):
    for i in range(0, len(b) - 2, 3):
        sid = _u16(b, i)
        st = b[i + 2]
        node.add("service_id %d (0x%04X)" % (sid, sid), "tipo 0x%02X %s" % (st, SERVICE_TYPES.get(st, "")))


def _service(b, node, info, ctx):
    st = b[0]
    pl = b[1]
    prov = decode_text(b[2:2 + pl])
    nl = b[2 + pl]
    nm = decode_text(b[3 + pl:3 + pl + nl])
    node.add("service_type", "0x%02X %s" % (st, SERVICE_TYPES.get(st, "")))
    node.add("provedor", prov)
    node.add("nome do serviço", nm)
    info.update(service_type=st, provider=prov, service_name=nm)


def _short_event(b, node, info, ctx):
    lc = lang_code(b[0:3])
    nl = b[3]
    nm = decode_text(b[4:4 + nl])
    tl = b[4 + nl]
    tx = decode_text(b[5 + nl:5 + nl + tl])
    node.add("idioma", lc)
    node.add("nome do evento", nm)
    node.add("texto", tx)
    info.update(event_name=nm, event_text=tx, lang=lc)


def _extended_event(b, node, info, ctx):
    node.add("descriptor_number", "%d / %d" % (b[0] >> 4, b[0] & 0xF))
    node.add("idioma", lang_code(b[1:4]))
    il = b[4]
    i = 5
    end = 5 + il
    items = []
    while i < end:
        dl = b[i]
        desc = decode_text(b[i + 1:i + 1 + dl])
        i += 1 + dl
        tl = b[i]
        txt = decode_text(b[i + 1:i + 1 + tl])
        i += 1 + tl
        node.add(desc or "item", txt)
        items.append((desc, txt))
    tl = b[end]
    txt = decode_text(b[end + 1:end + 1 + tl])
    if txt:
        node.add("texto", txt)
    info["ext_items"] = items
    info["ext_text"] = txt


def _component(b, node, info, ctx):
    sc = b[0] & 0x0F
    ct = b[1]
    tag = b[2]
    lc = lang_code(b[3:6])
    txt = decode_text(b[6:])
    node.add("stream_content", "0x%X" % sc)
    node.add("component_type", "0x%02X" % ct)
    node.add("component_tag", "0x%02X" % tag)
    node.add("idioma", lc)
    if txt:
        node.add("texto", txt)
    info["components"] = [(sc, ct, tag, lc, txt)]


def _content(b, node, info, ctx):
    genres = []
    for i in range(0, len(b) - 1, 2):
        n1, n2 = b[i] >> 4, b[i] & 0xF
        g = GENRES_BR.get(n1, "?")
        node.add("gênero", "%s (0x%X/0x%X) user 0x%02X" % (g, n1, n2, b[i + 1]))
        genres.append(g)
    if genres:
        info["genre"] = genres[0]


def _parental_rating(b, node, info, ctx):
    for i in range(0, len(b) - 3, 4):
        cc = lang_code(b[i:i + 3])
        r = b[i + 3]
        if cc.upper() == "BRA":
            age = RATING_BR.get(r & 0x0F, "0x%X" % (r & 0xF))
            cont = []
            if r & 0x10:
                cont.append("drogas")
            if r & 0x20:
                cont.append("violência")
            if r & 0x40:
                cont.append("sexo")
            txt = age + (" — " + ", ".join(cont) if cont else "")
        else:
            txt = "rating 0x%02X" % r
        node.add("país %s" % cc, txt)
        info.setdefault("rating", txt)


def _local_time_offset(b, node, info, ctx):
    for i in range(0, len(b) - 12, 13):
        cc = lang_code(b[i:i + 3])
        reg = b[i + 3] >> 2
        pol = "-" if b[i + 3] & 1 else "+"
        off = "%s%02d:%02d" % (pol, bcd(b[i + 4]), bcd(b[i + 5]))
        d = mjd_to_date(_u16(b, i + 6))
        nxt = "%s%02d:%02d" % (pol, bcd(b[i + 11]), bcd(b[i + 12]))
        node.add("país %s região %d" % (cc, reg), "offset %s, próxima mudança %s %02d:%02d:%02d -> %s"
                 % (off, d, bcd(b[i + 8]), bcd(b[i + 9]), bcd(b[i + 10]), nxt))
        info.setdefault("lto", off)


def _app_signalling(b, node, info, ctx):
    for i in range(0, len(b) - 2, 3):
        node.add("application_type 0x%04X" % (_u16(b, i) & 0x7FFF), "AIT version %d" % (b[i + 2] & 0x1F))


def _aac(b, node, info, ctx):
    node.add("profile_and_level", "0x%02X %s" % (b[0], _AAC_PROFILES.get(b[0], "")))


def _hierarchical(b, node, info, ctx):
    node.add("quality_level", "alta" if b[0] & 1 else "baixa")
    node.add("reference_PID", "%d" % (_u16(b, 1) & 0x1FFF))


def _digital_copy(b, node, info, ctx):
    node.add("digital_recording_control", _COPY_CONTROL.get(b[0] >> 6, "?"))
    has_rate = (b[0] >> 5) & 1
    i = 1
    if has_rate and len(b) > 1:
        node.add("maximum_bitrate", "%.2f Mbps" % (b[1] / 4))
        i = 2
    if (b[0] >> 4) & 1:
        while i < len(b):
            tag = b[i]
            f = b[i + 1]
            s = "%s" % _COPY_CONTROL.get(f >> 6, "?")
            i += 2
            if (f >> 5) & 1:
                s += ", %.2f Mbps" % (b[i] / 4)
                i += 1
            node.add("component 0x%02X" % tag, s)


def _network_identification(b, node, info, ctx):
    node.add("country_code", lang_code(b[0:3]))
    node.add("media_type", "0x%04X" % _u16(b, 3))
    node.add("network_id", "0x%04X" % _u16(b, 5))
    if len(b) > 7:
        node.add("private_data", short_hex(b[7:]))


def _audio_component(b, node, info, ctx):
    sc = b[0] & 0xF
    ct = b[1]
    tag = b[2]
    st = b[3]
    multi = (b[5] >> 7) & 1
    node.add("stream_content", "0x%X" % sc)
    node.add("component_type", "0x%02X %s" % (ct, _AUDIO_COMPONENT_TYPE.get(ct & 0x1F, "")))
    node.add("component_tag", "0x%02X" % tag)
    node.add("stream_type", "0x%02X" % st)
    node.add("simulcast_group_tag", "0x%02X" % b[4])
    node.add("main_component", (b[5] >> 6) & 1)
    node.add("quality_indicator", (b[5] >> 4) & 3)
    node.add("sampling_rate", _AUDIO_SAMPLING.get((b[5] >> 1) & 7, "?"))
    node.add("idioma", lang_code(b[6:9]))
    i = 9
    if multi:
        node.add("idioma 2", lang_code(b[9:12]))
        i = 12
    txt = decode_text(b[i:])
    if txt:
        node.add("texto", txt)
    info["audio_component"] = _AUDIO_COMPONENT_TYPE.get(ct & 0x1F, "0x%02X" % ct)
    info["component_tag"] = tag
    info["lang"] = lang_code(b[6:9])


def _data_content(b, node, info, ctx):
    dcid = _u16(b, 0)
    node.add("data_component_id", "0x%04X %s" % (dcid, _DATA_COMPONENT.get(dcid, "")))
    node.add("entry_component", "0x%02X" % b[2])
    sl = b[3]
    i = 4 + sl
    if sl:
        node.add("selector", short_hex(b[4:4 + sl]))
    nc = b[i]
    i += 1
    tags = [("0x%02X" % c) for c in b[i:i + nc]]
    if tags:
        node.add("component_refs", ", ".join(tags))
    i += nc
    node.add("idioma", lang_code(b[i:i + 3]))
    tl = b[i + 3]
    node.add("texto", decode_text(b[i + 4:i + 4 + tl]))


def _video_decode_control(b, node, info, ctx):
    node.add("still_picture", (b[0] >> 7) & 1)
    node.add("sequence_end_code", (b[0] >> 6) & 1)
    vf = (b[0] >> 2) & 0xF
    node.add("video_encode_format", VIDEO_ENCODE_FORMAT.get(vf, "reservado (%d)" % vf))
    info["video_format"] = VIDEO_ENCODE_FORMAT.get(vf, "?")


def _ts_information(b, node, info, ctx):
    key = b[0]
    nlen = b[1] >> 2
    tcount = b[1] & 3
    name = decode_text(b[2:2 + nlen])
    node.add("remote_control_key_id (canal virtual)", key)
    node.add("ts_name", name)
    info.update(remote_key=key, ts_name=name)
    i = 2 + nlen
    for _ in range(tcount):
        tti = b[i]
        sc = b[i + 1]
        i += 2
        sids = []
        for _ in range(sc):
            sids.append(_u16(b, i))
            i += 2
        node.add("transmission_type 0x%02X" % tti, "%s — serviços %s" % (
            _TRANSMISSION_TYPE.get(tti, ""), ", ".join("0x%04X" % s for s in sids)))


def _extended_broadcaster(b, node, info, ctx):
    bt = b[0] >> 4
    node.add("broadcaster_type", bt)
    if bt in (1, 2) and len(b) >= 4:
        node.add("terrestrial_broadcaster_id", "0x%04X" % _u16(b, 1))
    node.add("dados", short_hex(b[1:]))


def _logo(b, node, info, ctx):
    lt = b[0]
    node.add("logo_transmission_type", lt)
    if lt == 1 and len(b) >= 7:
        node.add("logo_id", _u16(b, 1) & 0x1FF)
        node.add("logo_version", _u16(b, 3) & 0xFFF)
        node.add("download_data_id", _u16(b, 5))
    elif lt == 2 and len(b) >= 3:
        node.add("logo_id", _u16(b, 1) & 0x1FF)
    elif lt == 3:
        node.add("logo_char", decode_text(b[1:]))


def _si_parameter(b, node, info, ctx):
    node.add("parameter_version", b[0])
    node.add("update_time", str(mjd_to_date(_u16(b, 1))))
    i = 3
    while i + 2 <= len(b):
        tid = b[i]
        ln = b[i + 1]
        node.add("table 0x%02X" % tid, short_hex(b[i + 2:i + 2 + ln]))
        i += 2 + ln


def _broadcaster_name(b, node, info, ctx):
    s = decode_text(b)
    node.add("nome da emissora", s)
    info["broadcaster_name"] = s


def _content_availability(b, node, info, ctx):
    node.add("copy_restriction_mode", (b[0] >> 6) & 1)
    node.add("image_constraint_token", (b[0] >> 5) & 1)
    node.add("retention_mode", (b[0] >> 4) & 1)
    node.add("retention_state", (b[0] >> 1) & 7)
    node.add("encryption_mode", b[0] & 1)


def _access_control(b, node, info, ctx):
    sysid = _u16(b, 0)
    tt = b[2] >> 5
    pid = _u16(b, 2) & 0x1FFF
    node.add("CA_system_id", "0x%04X" % sysid)
    node.add("transmission_type", tt)
    node.add("PID", pid)
    info["ca"] = [(sysid, pid)]


def _terrestrial_delivery(b, node, info, ctx):
    v = _u16(b, 0)
    area = v >> 4
    gi = (v >> 2) & 3
    mode = v & 3
    node.add("area_code", "0x%03X" % area)
    node.add("guard_interval", ISDB_GUARD.get(gi, "?"))
    node.add("transmission_mode", {0: "Modo 1", 1: "Modo 2", 2: "Modo 3"}.get(mode, "indefinido"))
    freqs = []
    for i in range(2, len(b) - 1, 2):
        f = _u16(b, i) / 7.0
        freqs.append(f)
        node.add("frequência", "%.3f MHz" % f)
    info["frequencies"] = freqs
    info["area_code"] = area
    info["guard_interval"] = ISDB_GUARD.get(gi)
    info["transmission_mode"] = mode + 1 if mode < 3 else None


def _partial_reception(b, node, info, ctx):
    sids = [_u16(b, i) for i in range(0, len(b) - 1, 2)]
    for s in sids:
        node.add("service_id (1-seg)", "%d (0x%04X)" % (s, s))
    info["partial_reception"] = sids


def _emergency(b, node, info, ctx):
    i = 0
    evs = []
    while i + 4 <= len(b):
        sid = _u16(b, i)
        start = (b[i + 2] >> 7) & 1
        level = (b[i + 2] >> 6) & 1
        al = b[i + 3]
        areas = []
        end = min(i + 4 + al, len(b))
        for j in range(i + 4, end - 1, 2):
            areas.append(_u16(b, j) >> 4)
        i += 4 + al
        sub = node.add("service_id 0x%04X" % sid, "%s, nível %d" % ("INÍCIO (alerta ativo)" if start else "fim", level + 1))
        if areas:
            sub.add("area_codes", ", ".join("0x%03X" % a for a in areas))
        evs.append((sid, start, level, areas))
    info["emergency"] = evs


def _data_component(b, node, info, ctx):
    dcid = _u16(b, 0)
    node.add("data_component_id", "0x%04X %s" % (dcid, _DATA_COMPONENT.get(dcid, "")))
    if len(b) > 2:
        node.add("additional_data_component_info", short_hex(b[2:]))
    info["data_component"] = _DATA_COMPONENT.get(dcid, "0x%04X" % dcid)


def _system_management(b, node, info, ctx):
    v = _u16(b, 0)
    bf = v >> 14
    bt = (v >> 8) & 0x3F
    node.add("broadcasting_flag", {0: "radiodifusão", 1: "radiodifusão", 2: "não-radiodifusão",
                                   3: "não-radiodifusão"}.get(bf))
    node.add("broadcasting_identifier", {3: "ISDB-T (terrestre)", 1: "ISDB-S", 2: "ISDB-S"}.get(bt, bt))
    node.add("additional_broadcasting_identification", "0x%02X" % (v & 0xFF))
    if len(b) > 2:
        node.add("additional_identification_info", short_hex(b[2:]))


def _carousel_compatible(b, node, info, ctx):
    i = 0
    while i + 2 <= len(b):
        node.add("sub-descriptor 0x%02X" % b[i], short_hex(b[i + 2:i + 2 + b[i + 1]]))
        i += 2 + b[i + 1]


def _series(b, node, info, ctx):
    node.add("series_id", "0x%04X" % _u16(b, 0))
    node.add("repeat_label", b[2] >> 4)
    node.add("program_pattern", (b[2] >> 1) & 7)
    if len(b) >= 8:
        node.add("episode_number", _u16(b, 5) >> 4)
        node.add("last_episode_number", _u16(b, 6) & 0xFFF)
        node.add("series_name", decode_text(b[8:]))


def _event_group(b, node, info, ctx):
    node.add("group_type", b[0] >> 4)
    cnt = b[0] & 0xF
    for k in range(cnt):
        i = 1 + 4 * k
        node.add("evento", "service 0x%04X event 0x%04X" % (_u16(b, i), _u16(b, i + 2)))


_DECODERS = {
    0x05: _registration, 0x09: _ca, 0x0A: _language, 0x0E: _max_bitrate,
    0x13: _carousel_id, 0x14: _association_tag, 0x28: _avc_video, 0x38: _hevc_video,
    0x40: _network_name, 0x41: _service_list, 0x48: _service, 0x4D: _short_event,
    0x4E: _extended_event, 0x50: _component, 0x52: _stream_identifier, 0x54: _content,
    0x55: _parental_rating, 0x58: _local_time_offset, 0x6F: _app_signalling, 0x7C: _aac,
    0xC0: _hierarchical, 0xC1: _digital_copy, 0xC2: _network_identification,
    0xC4: _audio_component, 0xC7: _data_content, 0xC8: _video_decode_control,
    0xCD: _ts_information, 0xCE: _extended_broadcaster, 0xCF: _logo, 0xD5: _series,
    0xD6: _event_group, 0xD7: _si_parameter, 0xD8: _broadcaster_name,
    0xDE: _content_availability, 0xF6: _access_control, 0xF7: _carousel_compatible,
    0xFA: _terrestrial_delivery, 0xFB: _partial_reception, 0xFC: _emergency,
    0xFD: _data_component, 0xFE: _system_management,
}
