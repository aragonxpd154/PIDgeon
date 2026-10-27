import os
import socket
import threading
import time

import pytest

from pidgeon.core.analyzer import Analyzer
from pidgeon.core.crc import crc32_mpeg
from pidgeon.core.framer import Framer
from pidgeon.core.isdbt import parse_iip, layer_capacity_bps, BTSInfo
from pidgeon.core.psi import SectionAssembler, section_to_packets
from pidgeon.core.recorder import Recorder, build_pat, filter_pmt
from pidgeon.core.sources import SourceSpec, open_source
from pidgeon.core.text import decode_text, mjd_to_date
from pidgeon.tools import testgen
from pidgeon.tools.sender import send

HERE = os.path.dirname(__file__)


@pytest.fixture(scope="session")
def streams(tmp_path_factory):
    d = tmp_path_factory.mktemp("streams")
    bts = str(d / "t.bts")
    ts = str(d / "t.ts")
    testgen.generate(bts, 4, log=lambda m: None)
    testgen.generate(ts, 3, ts188=True, inject_errors=True, log=lambda m: None)
    return {"bts": bts, "ts": ts, "dir": d}


def analyze(path):
    an = Analyzer(clock="stream")
    with open(path, "rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            an.push_bytes(b)
    an._close_window(an.t)
    return an


def test_crc_known_value():
    pat = build_pat(1, {1: 0x100})
    assert crc32_mpeg(pat) == 0
    assert crc32_mpeg(b"123456789") == 0x0376E6E7


def test_framer_sizes_and_resync():
    for size, off in ((188, 0), (204, 0), (192, 4)):
        pkt = bytearray(size)
        pkt[off] = 0x47
        data = b"\x00\x13\x99" + bytes(pkt) * 20
        f = Framer()
        out = []
        for i in range(0, len(data), 37):
            out += f.push(data[i:i + 37])
        assert f.size == size
        assert len(out) == 20


def test_section_assembler_multi_packet():
    sec = build_pat(7, {i: 0x100 + i for i in range(1, 60)})
    pkts, _ = section_to_packets(0, sec, 0)
    assert len(pkts) > 1
    asm = SectionAssembler()
    got = []
    for p in pkts:
        got += asm.feed(p[4:], bool(p[1] & 0x40))
    assert got == [sec]


def test_text_and_mjd():
    assert decode_text(b"Jornal da Regi\xe3o") == "Jornal da Região"
    assert decode_text(b"\x15Ol\xc3\xa1") == "Olá"
    assert str(mjd_to_date(0xC079)) == "1993-10-13"


def test_iip_roundtrip():
    node, d = parse_iip(testgen.build_iip(3, 1))
    assert d["valid"] and d["crc_ok"]
    assert d["mode"] == 3 and d["gi"] == 1 and d["partial"] == 1
    a, b, c = d["layers_info"]
    assert a["mod"] == "QPSK" and a["cr"] == "2/3" and a["segs"] == 1 and a["ti"] == 4
    assert b["mod"] == "64-QAM" and b["cr"] == "3/4" and b["segs"] == 12 and b["ti"] == 2
    assert c is None
    assert d["sfn"]["max_delay_100ns"] == 2000 and len(d["sfn"]["equipment"]) == 2


def test_layer_capacity_matches_standard():
    # 13 segmentos 64-QAM 3/4 GI 1/16 = 19,33 Mbps (ABNT NBR 15601)
    assert abs(layer_capacity_bps(3, 2, 13, 1) / 1e6 - 19.33) < 0.01
    # 1 segmento QPSK 2/3 GI 1/8 = 416 kbps
    assert abs(layer_capacity_bps(1, 1, 1, 2) / 1e3 - 416.0) < 1


def test_bts_analysis(streams):
    an = analyze(streams["bts"])
    assert an.packet_size == 204 and an.is_bts
    assert abs(an.mux_rate - 29.958e6) < 0.01e6
    assert {0x4240, 0x4258} <= set(an.services)
    hd = an.services[0x4240]
    assert hd.name == "TV TESTE HD" and hd.present["name"] == "Jornal da Região"
    assert an.layer_of(0x1011) == "B" and an.layer_of(0x1001) == "A"
    assert an.layer_of(0x1FF0) == "IIP"
    assert an.is_one_seg(an.services[0x4258])
    assert an.network_name == "PIDgeon Lab" and an.remote_key == 22
    assert an.bts_pkts_per_frame == 4352 and an.bts_tsp_errors == 0
    assert an.iip and an.iip["crc_ok"]
    errors = {e.code: e.count for e in an.etr.values() if e.count}
    assert errors == {}, errors


def test_ts188_cc_error_detected(streams):
    an = analyze(streams["ts"])
    assert an.packet_size == 188 and not an.is_bts
    assert an.etr["1.4"].count >= 1
    assert an.pids[0x1011].cc_errors >= 1


def test_service_recording_is_valid_ts(streams, tmp_path):
    an = Analyzer(clock="stream")
    rec = Recorder(an, str(tmp_path / "svc.ts"), mode="service", sid=0x4258)
    an.taps.append(rec)
    with open(streams["bts"], "rb") as f:
        an.push_bytes(f.read())
    rec.close()
    an2 = analyze(str(tmp_path / "svc.ts"))
    assert an2.packet_size == 188
    assert list(k for k in an2.pat if k) == [0x4258]
    assert {es["pid"] for es in an2.services[0x4258].streams} == {0x1001, 0x1003}
    assert an2.etr["1.4"].count == 0 and an2.etr["2.2"].count == 0


def test_full_recording_keeps_bts(streams, tmp_path):
    an = Analyzer(clock="stream")
    rec = Recorder(an, str(tmp_path / "mux.bts"), mode="full", keep_raw=True, split_mb=5)
    an.taps.append(rec)
    with open(streams["bts"], "rb") as f:
        an.push_bytes(f.read())
    rec.close()
    assert len(rec.files) >= 2
    assert all(os.path.getsize(p) % 204 == 0 for p in rec.files)


def test_filter_pmt():
    an = analyze_small_pmt()
    assert an


def analyze_small_pmt():
    tables = testgen.build_tables(__import__("datetime").datetime(2026, 10, 27, 12))
    sec = filter_pmt(tables["pmt_hd"], {0x1011})
    assert crc32_mpeg(sec) == 0
    from pidgeon.core.tables import parse_section
    _, _, data = parse_section(0x1010, sec)
    return [s["pid"] for s in data["streams"]] == [0x1011]


def test_source_spec():
    s = SourceSpec("udp://10.0.0.5@232.1.1.1:5000?iface=127.0.0.1&buffer=8")
    assert (s.kind, s.group, s.port, s.source, s.iface, s.buffer_mb) == ("udp", "232.1.1.1", 5000, "10.0.0.5", "127.0.0.1", 8)
    s = SourceSpec("rtp://@239.0.0.1:1234")
    assert s.rtp == "on" and s.source is None and s.group == "239.0.0.1"
    assert SourceSpec("udp://:1234").group == "0.0.0.0"
    assert SourceSpec("~/x.ts").kind == "file"


@pytest.mark.parametrize("rtp", [False, True])
def test_udp_unicast_reception(streams, rtp):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    an = Analyzer(clock="wall")
    src = open_source("%s://127.0.0.1:%d" % ("rtp" if rtp else "udp", port), an)
    src.start()
    time.sleep(0.3)
    th = threading.Thread(target=send, args=(streams["bts"], "127.0.0.1:%d" % port),
                          kwargs=dict(rtp=rtp, seconds=1.5), daemon=True)
    th.start()
    th.join(5)
    time.sleep(0.3)
    src.stop()
    assert an.packet_size == 204
    assert src.rtp_detected is rtp
    assert 0x4240 in an.services
    assert an.etr["1.4"].count == 0
