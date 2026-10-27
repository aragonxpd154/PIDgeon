"""Gerador de streams de teste ISDB-Tb (TS 188 ou BTS 204) com vídeo/áudio reais.

Usa o ffmpeg para codificar um serviço HD (camada B) e um 1-seg (camada A), monta
PAT/PMT/NIT/SDT/EIT/TOT/BIT ao estilo ABNT NBR 15603, IIP com informação de rede SFN
e o trailer ISDB-T de 8 bytes em cada pacote de 204 bytes.

Uso:  python -m pidgeon.tools.testgen saida.bts --segundos 10 [--ts188] [--erros]
"""

import argparse
import datetime
import os
import shutil
import subprocess
import tempfile

from pidgeon.core.crc import crc32_mpeg
from pidgeon.core.psi import section_to_packets
from pidgeon.core.isdbt import layer_capacity_bps

BTS_RATE = 32507936.507936  # bps (pacotes de 204 bytes)
SLOT = 204 * 8 / BTS_RATE   # duração de um TSP no BTS
FRAME_TSP = {(3, 1): 4352, (3, 0): 4224, (3, 2): 4608, (3, 3): 5120}  # (modo, GI) -> TSPs/quadro

TSID = 0x0640
ONID = 0x0640
NETID = 0x0640
SID_HD = 0x4240
SID_1SEG = 0x4258
PMT_HD = 0x1010
VID_HD = 0x1011
AUD_HD = 0x1013
CC_HD = 0x1030
PMT_1SEG = 0x1FC8
VID_1SEG = 0x1001
AUD_1SEG = 0x1003


# ---------------------------------------------------------------- seções
def section(tid, ext, body, version=0, long_form=True, number=0, last=0):
    if long_form:
        slen = 5 + len(body) + 4
        s = bytearray((tid, 0xB0 | (slen >> 8), slen & 0xFF, ext >> 8, ext & 0xFF,
                       0xC1 | (version << 1), number, last))
        s += body
    else:
        slen = len(body) + 4
        s = bytearray((tid, 0x30 | (slen >> 8), slen & 0xFF))
        s += body
    s += crc32_mpeg(s).to_bytes(4, "big")
    return bytes(s)


def desc(tag, body):
    return bytes((tag, len(body))) + bytes(body)


def u16(v):
    return bytes((v >> 8 & 0xFF, v & 0xFF))


def loop12(data, reserved=0xF0):
    return bytes((reserved | (len(data) >> 8), len(data) & 0xFF)) + data


def latin(s):
    return s.encode("iso8859_15")


def mjd_bcd(dt):
    y, m, d = dt.year, dt.month, dt.day
    l = 1 if m in (1, 2) else 0
    mjd = 14956 + d + int((y - 1900 - l) * 365.25) + int((m + 1 + l * 12) * 30.6001)

    def b(v):
        return ((v // 10) << 4) | (v % 10)
    return u16(mjd) + bytes((b(dt.hour), b(dt.minute), b(dt.second)))


def dur_bcd(h, m, s):
    def b(v):
        return ((v // 10) << 4) | (v % 10)
    return bytes((b(h), b(m), b(s)))


def build_tables(now):
    t = {}
    t["pat"] = section(0x00, TSID, u16(0) + u16(0xE000 | 0x10) + u16(SID_HD) + u16(0xE000 | PMT_HD)
                       + u16(SID_1SEG) + u16(0xE000 | PMT_1SEG))

    def es(st, pid, descs):
        return bytes((st,)) + u16(0xE000 | pid) + loop12(descs)

    hd_prog = desc(0xC1, b"\x80") + desc(0xDE, b"\xbe")
    hd_es = (es(0x1B, VID_HD, desc(0x52, b"\x00") + desc(0xC8, bytes((0x01 << 2 | 0x01,))))
             + es(0x11, AUD_HD, desc(0x52, b"\x10") + desc(0x0A, b"por\x00"))
             + es(0x06, CC_HD, desc(0x52, b"\x30") + desc(0xFD, u16(0x0008) + b"\x3d")))
    t["pmt_hd"] = section(0x02, SID_HD, u16(0xE000 | VID_HD) + loop12(hd_prog) + hd_es)
    sg_es = (es(0x1B, VID_1SEG, desc(0x52, b"\x81"))
             + es(0x11, AUD_1SEG, desc(0x52, b"\x83")))
    t["pmt_1seg"] = section(0x02, SID_1SEG, u16(0xE000 | VID_1SEG) + loop12(b"") + sg_es)

    name = latin("PIDgeon Lab")
    nit_desc = desc(0x40, name) + desc(0xFE, b"\x03\x01")
    ts_info = bytes((22, (len(latin("TESTE")) << 2) | 2)) + latin("TESTE") + \
        b"\x0f\x01" + u16(SID_HD) + b"\xaf\x01" + u16(SID_1SEG)
    freq = int(round((587 + 1 / 7) * 7))  # canal 32 (587,142857 MHz)
    tdel = u16((0x2E3 << 4) | (1 << 2) | 2) + u16(freq)
    ts_desc = (desc(0xCD, ts_info) + desc(0xFA, tdel) + desc(0xFB, u16(SID_1SEG))
               + desc(0x41, u16(SID_HD) + b"\x01" + u16(SID_1SEG) + b"\xc0"))
    tsl = u16(TSID) + u16(ONID) + loop12(ts_desc)
    t["nit"] = section(0x40, NETID, loop12(nit_desc) + loop12(tsl))

    def svc(sid, st, prov, nm):
        d = desc(0x48, bytes((st, len(prov))) + prov + bytes((len(nm),)) + nm)
        return u16(sid) + b"\xe3" + bytes((0x80 | (len(d) >> 8), len(d) & 0xFF)) + d
    t["sdt"] = section(0x42, TSID, u16(ONID) + b"\xff"
                       + svc(SID_HD, 0x01, latin("PIDgeon"), latin("TV TESTE HD"))
                       + svc(SID_1SEG, 0xC0, latin("PIDgeon"), latin("TV TESTE 1SEG")))

    def event(eid, start, h, m, name_, text, genre, rating):
        se = desc(0x4D, b"por" + bytes((len(name_),)) + name_ + bytes((len(text),)) + text)
        se += desc(0x54, bytes((genre << 4, 0xEE)))
        se += desc(0x55, b"BRA" + bytes((rating,)))
        return u16(eid) + mjd_bcd(start) + dur_bcd(h, m, 0) + bytes((0x80 | (len(se) >> 8), len(se) & 0xFF)) + se

    start = now.replace(minute=0, second=0, microsecond=0)
    for sid, key in ((SID_HD, "eit_hd"), (SID_1SEG, "eit_1seg")):
        p = event(0x0101, start, 1, 0, latin("Jornal da Região"), latin("Notícias do Espírito Santo"), 0x0, 0x01)
        f = event(0x0102, start + datetime.timedelta(hours=1), 0, 45,
                  latin("Futebol Ao Vivo"), latin("Transmissão esportiva"), 0x1, 0x02)
        hdr = u16(TSID) + u16(ONID)
        t[key + "_p"] = section(0x4E, sid, hdr + b"\x01\x4e" + p, number=0, last=1)
        t[key + "_f"] = section(0x4E, sid, hdr + b"\x01\x4e" + f, number=1, last=1)

    lto = b"BRA" + bytes((0x03 << 2 | 0x01,)) + b"\x03\x00" + mjd_bcd(now.replace(month=1, day=1)) + b"\x03\x00"
    t["tot"] = section(0x73, 0, mjd_bcd(now) + loop12(desc(0x58, lto)), long_form=False)
    bit = loop12(b"", 0xE0) + b"\x01" + loop12(desc(0xD8, latin("PIDgeon Lab")))
    t["bit"] = section(0xC4, ONID, b"\xff"[0:0] + bit)
    return t


def build_iip(mode=3, gi=1):
    """IIP: A=QPSK 2/3 1seg I=4; B=64QAM 3/4 12seg I=2; SFN com 2 equipamentos."""
    bits = []

    def put(v, n):
        for i in range(n - 1, -1, -1):
            bits.append((v >> i) & 1)

    def tp(mod, cr, ti, segs):
        put(mod, 3)
        put(cr, 3)
        put(ti, 3)
        put(segs, 4)

    put(1, 1)          # TMCC_synchronization_word
    put(0, 1)          # AC_data_effective_position
    put(3, 2)          # reservado
    put(0, 4)          # initialization_timing_indicator
    put(mode, 2)
    put(gi, 2)
    put(mode, 2)
    put(gi, 2)
    put(0, 2)          # system_identifier
    put(15, 4)         # count_down_index
    put(0, 1)          # alerta
    for _ in range(2):  # atual e próxima
        put(1, 1)      # recepção parcial
        tp(1, 1, 3, 1)   # A: QPSK 2/3 I=4 (modo 3)
        tp(3, 2, 2, 12)  # B: 64QAM 3/4 I=2
        tp(7, 7, 7, 15)  # C: não usada
    put(7, 3)          # phase correction
    put(0xFFF, 12)     # TMCC reserved
    put(0x3FF, 10)     # reservado
    raw = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
    mcci = raw + crc32_mpeg(raw).to_bytes(4, "big")
    equip = b""
    for eid, off in ((0x001, 150), (0x002, 300)):
        v = (eid << 28) | (1 << 27) | (0 << 26) | (0 << 25) | off
        equip += v.to_bytes(5, "big")
    nsi_body = (1000).to_bytes(3, "big") + (2000).to_bytes(3, "big") + bytes((len(equip),)) + equip
    nsi = b"\x00" + nsi_body + crc32_mpeg(nsi_body).to_bytes(4, "big")
    payload = u16(0) + mcci + b"\x00\x00" + bytes((len(nsi),)) + nsi
    payload += b"\xff" * (184 - len(payload))
    return payload


# ---------------------------------------------------------------- codificação
def _rate(v):
    v = str(v)
    return int(float(v[:-1]) * (1e6 if v[-1] in "Mm" else 1e3)) if v[-1] in "MmKk" else int(v)


def pyav_ts(out, seconds, size, vbr, abr, muxrate, label, fps=30, pcr_ms=30):
    """Codifica com PyAV (FFmpeg embutido no pacote) — não exige o ffmpeg instalado."""
    import av
    from fractions import Fraction
    w, h = size
    vin = av.open("testsrc2=size=%dx%d:rate=%d" % (w, h, fps), format="lavfi")
    ain = av.open("sine=frequency=1000:sample_rate=48000", format="lavfi")
    o = av.open(out, "w", format="mpegts", options={
        "mpegts_flags": "latm", "muxrate": str(muxrate), "pcr_period": str(pcr_ms),
        "mpegts_start_pid": "0x100"})
    vs = o.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
    vs.bit_rate = _rate(vbr)
    vs.codec_context.options = {"preset": "veryfast", "tune": "zerolatency", "g": str(max(1, fps // 2)),
                                "maxrate": str(_rate(vbr)), "bufsize": str(_rate(vbr))}
    as_ = o.add_stream("aac", rate=48000)
    as_.layout = "stereo"
    as_.bit_rate = _rate(abr)
    res = av.AudioResampler(format="fltp", layout="stereo", rate=48000)
    vframes = vin.decode(video=0)
    aframes = ain.decode(audio=0)
    vt = at = 0.0
    n = 0
    samples = 0
    while vt < seconds or at < seconds:
        if vt <= at and vt < seconds:
            f = next(vframes)
            f.pts = n
            f.time_base = Fraction(1, fps)
            n += 1
            vt = n / fps
            for pk in vs.encode(f):
                o.mux(pk)
        else:
            f = next(aframes)
            for rf in res.resample(f):
                rf.pts = samples
                rf.time_base = Fraction(1, 48000)
                samples += rf.samples
                for pk in as_.encode(rf):
                    o.mux(pk)
            at = samples / 48000
    for s in (vs, as_):
        for pk in s.encode(None):
            o.mux(pk)
    o.close()
    vin.close()
    ain.close()


def ffmpeg_ts(out, seconds, size, vbr, abr, muxrate, label, fps=30, pcr_ms=30):
    ff = shutil.which("ffmpeg")
    if not ff:
        try:
            return pyav_ts(out, seconds, size, vbr, abr, muxrate, label, fps, pcr_ms)
        except ImportError:
            raise RuntimeError("Instale o ffmpeg (sudo apt install ffmpeg) ou o PyAV")
    w, h = size
    vf = "drawtext=text='%s %%{pts\\:hms}':x=20:y=20:fontsize=%d:fontcolor=white:box=1:boxcolor=black@0.6" % (label, max(12, h // 18))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d" % (w, h, fps),
           "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
           "-t", str(seconds), "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-g", str(fps // 2),
           "-b:v", vbr, "-maxrate", vbr, "-bufsize", vbr, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", abr, "-ac", "2",
           "-f", "mpegts", "-mpegts_flags", "latm", "-muxrate", str(muxrate), "-pcr_period", str(pcr_ms),
           "-mpegts_start_pid", "0x100", out]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        # sem drawtext (fonte ausente) — tenta sem texto
        cmd[cmd.index("-vf") + 1] = "null"
        subprocess.run(cmd, check=True)


def read_es(path, muxrate, remap):
    """Lê o TS do ffmpeg -> lista de (tempo, pacote 188 com PID remapeado)."""
    with open(path, "rb") as f:
        data = f.read()
    out = []
    dt = 188 * 8 / muxrate
    for i in range(len(data) // 188):
        p = data[i * 188:(i + 1) * 188]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid in remap:
            np_ = remap[pid]
            p = bytes((0x47, (p[1] & 0xE0) | (np_ >> 8), np_ & 0xFF)) + p[3:]
            out.append((i * dt, p))
    return out


def restamp_pcr(p, pcr):
    if not (p[3] & 0x20) or p[4] < 7 or not (p[5] & 0x10):
        return p
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


_SHARED_CC = {}


class Carousel:
    """Repete seções periodicamente. O contador de continuidade é compartilhado por PID
    entre camadas (ex.: PAT nas camadas A e B forma uma única sequência)."""

    def __init__(self, items):
        # items: lista de (pid, seção, período_s)
        self.items = [[pid, sec, per, 0.0] for pid, sec, per in items]
        self.pending = []

    def due(self, t):
        if not self.pending:
            for it in self.items:
                if t >= it[3]:
                    pk, _SHARED_CC[it[0]] = section_to_packets(it[0], it[1], _SHARED_CC.get(it[0], 0))
                    it[3] = t + it[2]
                    self.pending.extend(pk)
                    break
        return self.pending.pop(0) if self.pending else None


def null_packet(cc=0):
    return b"\x47\x1f\xff\x10" + b"\xff" * 184


def generate(out, seconds=10, ts188=False, inject_errors=False, mode=3, gi=1, log=print):
    # ISDB-Tb: horários de TOT/EIT no horário de Brasília (UTC-3)
    now = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)).replace(tzinfo=None)
    _SHARED_CC.clear()
    tables = build_tables(now)
    iip = build_iip(mode, gi)
    tmp = tempfile.mkdtemp(prefix="pidgeon-gen-")
    try:
        hd_ts = os.path.join(tmp, "hd.ts")
        sg_ts = os.path.join(tmp, "1seg.ts")
        log("Codificando serviço HD (1280x720 H.264 + AAC LATM)…")
        ffmpeg_ts(hd_ts, seconds + 1, (1280, 720), "4M", "128k", 6000000, "HD camada B")
        log("Codificando serviço 1-seg (320x180)…")
        ffmpeg_ts(sg_ts, seconds + 1, (320, 180), "150k", "48k", 320000, "1-seg camada A", fps=15, pcr_ms=20)
        hd = read_es(hd_ts, 6000000, {0x100: VID_HD, 0x101: AUD_HD})
        sg = read_es(sg_ts, 320000, {0x100: VID_1SEG, 0x101: AUD_1SEG})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    frame_len = FRAME_TSP.get((mode, gi), 4352)
    nA = 64      # QPSK 2/3, 1 segmento, modo 3
    nB = 2592    # 64QAM 3/4, 12 segmentos, modo 3
    slots = []
    accA = accB = 0.0
    for i in range(frame_len):
        accA += nA / frame_len
        accB += nB / frame_len
        if accA >= 1:
            accA -= 1
            slots.append(1)
        elif accB >= 1:
            accB -= 1
            slots.append(2)
        else:
            slots.append(0)
    iip_slot = slots.index(0)
    slots[iip_slot] = 8

    carA = Carousel([(0x00, tables["pat"], 0.1), (PMT_1SEG, tables["pmt_1seg"], 0.1),
                     (0x10, tables["nit"], 0.1), (0x11, tables["sdt"], 0.3),
                     (0x27, tables["eit_1seg_p"], 0.5), (0x27, tables["eit_1seg_f"], 0.5),
                     (0x14, tables["tot"], 5.0), (0x24, tables["bit"], 2.0)])
    carB = Carousel([(0x00, tables["pat"], 0.1), (PMT_HD, tables["pmt_hd"], 0.1),
                     (0x12, tables["eit_hd_p"], 0.5), (0x12, tables["eit_hd_f"], 0.5)])
    qA = list(reversed(sg))
    qB = list(reversed(hd))
    pcr_off = {}
    total_slots = int(seconds / SLOT)
    idx = 0
    tsp = 0
    frame = 0
    f = open(out, "wb", buffering=4 * 1024 * 1024)
    cnt_err = 0
    log("Multiplexando %s de %.1f s…" % ("TS 188" if ts188 else "BTS 204", seconds))
    while idx < total_slots:
        pos = idx % frame_len
        if pos == 0:
            tsp = 0
            frame += 1
        layer = slots[pos]
        t = idx * SLOT
        pkt = None
        if layer == 8:
            pkt = b"\x47\x1f\xf0" + bytes((0x10 | (frame & 0xF),)) + iip
        elif layer in (1, 2):
            car, q = (carA, qA) if layer == 1 else (carB, qB)
            pkt = car.due(t)
            if pkt is None and q and q[-1][0] <= t:
                _, pkt = q.pop()
                pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                if pkt[3] & 0x20 and pkt[4] >= 7 and pkt[5] & 0x10:
                    base = (pkt[6] << 25) | (pkt[7] << 17) | (pkt[8] << 9) | (pkt[9] << 1) | (pkt[10] >> 7)
                    orig = base * 300 + (((pkt[10] & 1) << 8) | pkt[11])
                    now27 = int(round(t * 27e6))
                    if pid not in pcr_off:
                        pcr_off[pid] = orig - now27
                    pkt = restamp_pcr(pkt, (now27 + pcr_off[pid]) % ((1 << 33) * 300))
                if inject_errors and idx % 50000 == 4999 and pid == VID_HD:
                    pkt = pkt[:3] + bytes(((pkt[3] & 0xF0) | ((pkt[3] + 5) & 0x0F),)) + pkt[4:]
                    cnt_err += 1
        if pkt is None:
            pkt = null_packet()
        if ts188:
            # mantém todos os slots (taxa constante) para o PCR continuar preciso
            f.write(pkt)
        else:
            b0 = (2 << 6) | (1 << 5) | ((1 << 2) if pos == 0 else 0) | ((1 << 1) if pos == 0 else 0) | (frame & 1)
            trailer = bytes((b0, (layer << 4) | 0x0F, 0x80 | (tsp >> 8), tsp & 0xFF)) + b"\xff" * 4 + b"\x00" * 8
            f.write(pkt + trailer)
        tsp = (tsp + 1) & 0x1FFF
        idx += 1
    f.close()
    capA = layer_capacity_bps(1, 1, 1, gi)
    capB = layer_capacity_bps(3, 2, 12, gi)
    log("Gerado %s: %.1f MB (camada A %.3f Mbps, B %.3f Mbps)%s" % (
        out, os.path.getsize(out) / 1e6, capA / 1e6, capB / 1e6,
        (", %d erros de CC injetados" % cnt_err) if inject_errors else ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gera um stream ISDB-Tb de teste (BTS 204 ou TS 188)")
    ap.add_argument("saida")
    ap.add_argument("--segundos", type=float, default=10)
    ap.add_argument("--ts188", action="store_true", help="gera TS 188 sem nulos (em vez de BTS 204)")
    ap.add_argument("--erros", action="store_true", help="injeta erros de continuidade")
    a = ap.parse_args(argv)
    generate(a.saida, a.segundos, a.ts188, a.erros)


if __name__ == "__main__":
    main()
