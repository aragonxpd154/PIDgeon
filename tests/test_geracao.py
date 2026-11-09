"""Geração de BTS 204 / BTS 188 / TS 188 / TS 204, Reed-Solomon, FEC SMPTE 2022-1 e saída de rede."""

import os
import socket
import threading
import time

import pytest

from pidgeon.core.analyzer import Analyzer
from pidgeon.core.convert import Generator, GenConfig, probe
from pidgeon.core.fec import FecEncoder, FecPacket, RtpFecReceiver, parse_rtp
from pidgeon.core.isdbt import parse_iip
from pidgeon.core.netout import NetConfig, NetSender
from pidgeon.core.remux import IsdbParams, Layer, build_iip_payload, frame_layout
from pidgeon.core.rs import rs_204_188, rs_204_196, looks_like_rs204, add_rs204
from pidgeon.core.sources import open_source
from pidgeon.tools import testgen, gerar


@pytest.fixture(scope="module")
def bts(tmp_path_factory):
    d = tmp_path_factory.mktemp("gen")
    path = str(d / "src.bts")
    testgen.generate(path, 4, log=lambda m: None)
    return path


def analyze(path):
    an = Analyzer(clock="stream")
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            an.push_bytes(b)
    an._close_window(an.t)
    return an


def errors(an):
    return {e.code: e.count for e in an.etr.values() if e.count}


def gen(src, fmt, out, **kw):
    g = Generator(GenConfig(source=src, fmt=fmt, out_file=out, **kw))
    g.run()
    return g


# ------------------------------------------------------------------ Reed-Solomon
def test_rs_table_matches_lfsr_and_syndromes():
    for enc, k in ((rs_204_188(), 188), (rs_204_196(), 196)):
        for seed in range(5):
            data = bytes([0x47]) + os.urandom(k - 1)
            par = enc.encode(data)
            assert par == enc.encode_lfsr(data)
            assert enc.syndromes_ok(data + par)
            bad = bytearray(data + par)
            bad[10] ^= 0x5A
            assert not enc.syndromes_ok(bytes(bad))


def test_rs_known_vector_zero_packet():
    # pacote todo zero => paridade zero (código linear)
    assert rs_204_188().encode(bytes(188)) == bytes(16)
    p = add_rs204(b"\x47" + bytes(187))
    assert len(p) == 204 and looks_like_rs204([p])


# ------------------------------------------------------------------ ISDB-T
def test_frame_structure():
    p = IsdbParams()
    assert p.frame_tsps() == 4352 and p.layer_tsps(0) == 64 and p.layer_tsps(1) == 2592
    assert IsdbParams(gi=2).frame_tsps() == 4608 and IsdbParams(mode=1, gi=3).frame_tsps() == 1280
    sl = frame_layout(p)
    assert len(sl) == 4352 and sl.count(1) == 64 and sl.count(2) == 2592 and sl.count(8) == 1
    with pytest.raises(ValueError):
        IsdbParams(layers=[Layer(1, 1, 3, 1), Layer(3, 2, 2, 11), None]).validate()   # 12 segmentos
    with pytest.raises(ValueError):
        IsdbParams(partial=True, layers=[None, Layer(3, 2, 2, 13), None]).validate()


def test_iip_builder_roundtrip():
    p = IsdbParams(mode=2, gi=2, partial=False,
                   layers=[Layer(2, 0, 1, 5), Layer(3, 3, 2, 8), None])
    _, d = parse_iip(build_iip_payload(p, pointer=123))
    assert d["valid"] and d["crc_ok"] and d["mode"] == 2 and d["gi"] == 2 and not d["partial"]
    a, b, c = d["layers_info"]
    assert (a["mod"], a["cr"], a["segs"]) == ("16-QAM", "1/2", 5)
    assert (b["mod"], b["cr"], b["segs"]) == ("64-QAM", "5/6", 8) and c is None
    assert IsdbParams.from_iip(d).describe() == p.describe()


# ------------------------------------------------------------------ conversões
def test_probe_detects_kinds(bts, tmp_path):
    assert probe(bts).kind == "bts204"
    gen(bts, "bts188", str(tmp_path / "x.bts188"))
    assert probe(str(tmp_path / "x.bts188")).kind == "bts188"
    gen(bts, "ts204", str(tmp_path / "x.ts204"))
    assert probe(str(tmp_path / "x.ts204")).kind == "ts204"
    gen(bts, "ts188", str(tmp_path / "x.ts"))
    assert probe(str(tmp_path / "x.ts")).kind == "ts188"


@pytest.mark.parametrize("fmt,size", [("ts188", 188), ("ts204", 204), ("bts188", 188), ("bts204", 204)])
def test_bts_to_formats_are_clean(bts, tmp_path, fmt, size):
    out = str(tmp_path / ("o." + fmt))
    g = gen(bts, fmt, out)
    an = analyze(out)
    assert os.path.getsize(out) % size == 0 and an.packet_size == size
    assert {0x4240, 0x4258} <= set(an.services)
    assert errors(an) == {}, errors(an)
    assert abs(an.mux_rate - 29.958e6) < 0.02e6
    if fmt == "ts204":
        assert an.rs204 and not an.is_bts and an.rs_errors == 0
    if fmt in ("ts188", "ts204"):
        assert 0x1FF0 not in an.pids             # IIP removida
    else:
        assert an.iip and an.iip["crc_ok"]
    if fmt == "bts204":
        assert an.layer_of(0x1011) == "B" and an.layer_of(0x1001) == "A"
    assert g.stats.packets_out > 0


def test_ts_vbr_to_bts_remux(bts, tmp_path):
    ts = str(tmp_path / "vbr.ts")
    gen(bts, "ts188", ts, remove_nulls=True)          # TS sem nulos (VBR)
    an = analyze(ts)
    assert 0x1FFF not in an.pids
    out = str(tmp_path / "remux.bts")
    g = gen(ts, "bts204", out)
    an = analyze(out)
    assert an.is_bts and an.bts_pkts_per_frame == 4352 and an.bts_tsp_errors == 0
    assert an.layer_of(0x1011) == "B" and an.layer_of(0x1001) == "A" and an.layer_of(0) == "A"
    assert an.iip and an.iip["crc_ok"] and an.iip["mode"] == 3
    assert errors(an) == {}, errors(an)
    assert g.mux.max_late < 0.1 and not g.capacity_warnings()


def test_remux_custom_params_and_service_layer(bts, tmp_path):
    out = str(tmp_path / "custom.bts")
    p = IsdbParams(mode=3, gi=2, partial=False, layers=[None, Layer(3, 4, 2, 13), None])
    g = Generator(GenConfig(source=bts, fmt="bts204", out_file=out, isdb=p, force_remux=True))
    g.run()
    an = analyze(out)
    assert an.bts_pkts_per_frame == 4608
    assert an.layer_of(0x1011) == "B" and an.layer_of(0x1001) == "B"
    assert not an.iip["partial"]
    assert errors(an) == {}, errors(an)


def test_capacity_warning(bts):
    p = IsdbParams(layers=[Layer(1, 0, 3, 1), Layer(0, 0, 2, 12), None])   # B em DQPSK 1/2: 3,5 Mbps
    g = Generator(GenConfig(source=bts, fmt="bts204", out_file=os.devnull, isdb=p, force_remux=True))
    assert any("Camada B" in w for w in g.capacity_warnings())


def test_keep_only_layer_a(bts, tmp_path):
    out = str(tmp_path / "oneseg.ts")
    gen(bts, "ts188", out, keep_layers=(1,), remove_nulls=True)
    an = analyze(out)
    assert 0x1001 in an.pids and 0x1011 not in an.pids


def test_bts_rs_parity(bts, tmp_path):
    out = str(tmp_path / "rs.bts")
    gen(bts, "bts204", out, bts_rs_parity=True)
    enc = rs_204_196()
    with open(out, "rb") as f:
        for _ in range(50):
            p = f.read(204)
            assert enc.encode(p[:196]) == p[196:204]


# ------------------------------------------------------------------ FEC
def test_fec_column_and_row_recovery():
    enc = FecEncoder(5, 4, row_fec=True)
    rx = RtpFecReceiver()
    payloads = [os.urandom(1316) for _ in range(200)]
    media, fec = [], []
    for i, pl in enumerate(payloads):
        seq = (65500 + i) & 0xFFFF                     # cruza o limite de 16 bits
        c, r = enc.push(seq, i * 100, pl)
        media.append((seq, i * 100, pl))
        fec += c + r
    lost = {3, 4, 17, 40, 41, 42, 101, 150}            # inclui perdas vizinhas (precisa de 2D)
    out = []
    fi = 0
    for i, (seq, ts, pl) in enumerate(media):
        if i not in lost:
            out += rx.media(seq, 33, ts, pl)
        while fi < len(fec) and fi < (i // 5) * 2:      # FEC chega um pouco depois
            f = FecPacket.parse(fec[fi])
            out += rx.fec(f)
            fi += 1
    for d in fec[fi:]:
        out += rx.fec(FecPacket.parse(d))
    out += rx.flush()
    assert rx.recovered == len(lost) and rx.lost == 0
    assert out == payloads


def test_netconfig_url():
    c = NetConfig.from_url("rtp://239.1.2.3:5000?fec=20x5&iface=lo&ttl=8&dscp=34&pkts=4")
    assert (c.proto, c.fec_cols, c.fec_rows, c.iface, c.ttl, c.dscp, c.pkts_per_dgram) == \
        ("rtp-fec", 20, 5, "lo", 8, 34, 4) and c.multicast
    c = NetConfig.from_url("udp://10.0.0.20:1234")
    assert c.proto == "udp" and not c.multicast
    assert NetConfig.from_url(c.url()).url() == c.url()
    with pytest.raises(ValueError):
        NetConfig.from_url("udp://1.2.3.4:5000?fec=10x10")
    with pytest.raises(ValueError):
        NetConfig.from_url("rtp://1.2.3.4:5000?fec=30x10")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.parametrize("fmt,proto,drop", [("ts188", "udp", 0), ("bts204", "rtp", 0),
                                            ("bts204", "rtp-fec", 41), ("ts204", "rtp-fec", 37)])
def test_network_output_reception(bts, fmt, proto, drop, monkeypatch):
    port = _free_port()
    while port + 4 > 65535:
        port = _free_port()
    an = Analyzer(clock="wall")
    url = "%s://127.0.0.1:%d%s" % ("udp" if proto == "udp" else "rtp", port,
                                   "?fec=on" if proto == "rtp-fec" else "")
    src = open_source(url, an)
    src.start()
    time.sleep(0.3)
    if drop:
        orig = NetSender._sendto
        n = {"i": 0}

        def lossy(self, data, addr):
            if addr == self.addr:
                n["i"] += 1
                if n["i"] % drop == 0:
                    return True
            return orig(self, data, addr)
        monkeypatch.setattr(NetSender, "_sendto", lossy)
    net = NetConfig("127.0.0.1", port, proto, fec_cols=10, fec_rows=10, fec_2d=False)
    g = Generator(GenConfig(source=bts, fmt=fmt, net=net, seconds=1.6))
    th = threading.Thread(target=g.run, daemon=True)
    th.start()
    th.join(10)
    time.sleep(0.4)
    src.stop()
    src.join(2)
    assert an.packet_size == (204 if fmt in ("bts204", "ts204") else 188)
    assert 0x4240 in an.services
    lost = getattr(src, "fec_rx", None) and src.fec_rx.lost
    assert an.etr["1.4"].count == 0, "CC %d, perdidos %s, sock %s" % (an.etr["1.4"].count, lost, src.rtp_lost)
    if proto == "rtp-fec":
        assert src.fec_rx.recovered > 0 and src.fec_rx.lost == 0
        assert g.stats.net["fec_col"] > 0


def test_cli_gerar(bts, tmp_path):
    out = str(tmp_path / "cli.ts")
    assert gerar.main([bts, "-f", "ts188", "-o", out, "-q"]) == 0
    assert analyze(out).packet_size == 188
    out = str(tmp_path / "cli.bts")
    assert gerar.main([str(bts), "-f", "bts204", "--remux", "--modo", "3", "--gi", "1/8",
                       "--camada-b", "64qam,5/6,12,2", "-o", out, "-q"]) == 0
    an = analyze(out)
    assert an.bts_pkts_per_frame == 4608 and errors(an) == {}


def test_rtp_parse_roundtrip():
    from pidgeon.core.fec import rtp_header
    d = rtp_header(7, 9000, 0xABCD) + b"\x47" * 188
    seq, ts, pt, ssrc, pl = parse_rtp(d)
    assert (seq, ts, pt, ssrc, len(pl)) == (7, 9000, 33, 0xABCD, 188)
