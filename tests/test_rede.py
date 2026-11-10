"""Adaptadores de rede: escolha automática, troca de placa/cabo e mensagens de erro (sem precisar de root)."""

import pytest

from pidgeon.core import netif
from pidgeon.core.analyzer import Analyzer
from pidgeon.core.netif import NetIf
from pidgeon.core.sources import open_source, SourceSpec


def nic(name, idx, ip, prefix=24, carrier=True, wireless=False, up=True):
    return NetIf(name=name, index=idx, ipv4=ip, prefix=prefix, up=up, carrier=carrier, wireless=wireless)


class FakeNet:
    def __init__(self, monkeypatch, ifs):
        self.ifs = list(ifs)
        monkeypatch.setattr(netif, "interfaces", self.interfaces)
        monkeypatch.setattr(netif, "snapshot", lambda: tuple(i.key() for i in self.ifs))
        monkeypatch.setattr(netif, "route_iface", lambda dest, ifs=None: None)

    def interfaces(self, include_loopback=False):
        return [i for i in self.ifs if include_loopback or not i.loopback]


def test_auto_one_per_subnet_wired_first(monkeypatch):
    FakeNet(monkeypatch, [nic("wlp2s0", 3, "192.168.0.50", wireless=True), nic("enp3s0", 2, "192.168.0.10"),
                          nic("enx00e04c", 5, "10.20.0.5"), nic("enp4s0", 4, "172.16.0.2", carrier=False)])
    names = [i.name for i in netif.auto_join_set("239.1.1.1")]
    assert names == ["enp3s0", "enx00e04c"]       # Wi-Fi na mesma sub-rede do cabo fica de fora; sem cabo também


def test_resolve_fixed_missing_and_legacy_ip(monkeypatch):
    FakeNet(monkeypatch, [nic("enp3s0", 2, "192.168.0.10")])
    r = netif.resolve("enp9s9", "239.1.1.1")
    assert r.missing and r.mode == "fixed" and "não existe" in r.note and "enp3s0" in r.note
    r = netif.resolve("10.99.99.99", "239.1.1.1")           # IP salvo por versões antigas
    assert r.mode == "auto" and [i.name for i in r.ifaces] == ["enp3s0"]
    r = netif.resolve("192.168.0.10", "239.1.1.1")          # IP que ainda existe -> o adaptador
    assert r.mode == "fixed" and r.ifaces[0].name == "enp3s0"


def _fake_source(monkeypatch, url, net):
    an = Analyzer(clock="wall")
    an.log = lambda m, lvl="info": logs.append(m)
    logs = []
    src = open_source(url, an)
    calls = []

    def join(ifs, verb="IGMP join"):
        calls.append(("join", [i.name for i in ifs]))
        return list(ifs), []

    def leave(ifs):
        calls.append(("leave", [i.name for i in ifs]))
    monkeypatch.setattr(src, "_join", join)
    monkeypatch.setattr(src, "_leave", leave)
    monkeypatch.setattr(src, "_make_socket", lambda port, ip: None)
    src.prepare()
    return src, calls, logs


def test_auto_follows_adapter_swap(monkeypatch):
    net = FakeNet(monkeypatch, [nic("eth0", 2, "192.0.2.2"), nic("enx1", 7, "10.10.1.1")])
    src, calls, logs = _fake_source(monkeypatch, "udp://239.7.7.7:6000", net)
    assert calls[-1] == ("join", ["eth0", "enx1"])
    src._lock(7)                                            # o fluxo chegou pela placa USB
    assert src.locked.name == "enx1" and calls[-1] == ("leave", ["eth0"])
    net.ifs = [nic("eth0", 2, "192.0.2.2")]                 # placa USB removida
    src._watch(force=True)
    assert src.locked is None and ("join", ["eth0"]) in calls
    assert any("removido" in m for m in logs)
    net.ifs = [nic("eth0", 2, "192.0.2.2"), nic("enx2", 9, "10.10.2.1")]   # outra placa plugada
    src._watch(force=True)
    assert calls[-1] == ("join", ["enx2"])
    src._lock(9)
    assert src.iface_status().startswith("enx2")


def test_cable_back_forces_rejoin(monkeypatch):
    net = FakeNet(monkeypatch, [nic("enp3s0", 2, "192.168.0.10")])
    src, calls, logs = _fake_source(monkeypatch, "udp://239.7.7.7:6000?iface=enp3s0", net)
    src._lock(2)
    net.ifs = [nic("enp3s0", 2, "192.168.0.10", carrier=False)]
    src._watch(force=True)
    assert src.joined and src.joined[0].name == "enp3s0"   # fixo: continua inscrito aguardando o cabo
    net.ifs = [nic("enp3s0", 2, "192.168.0.10")]
    n = len(calls)
    src._watch(force=True)
    assert calls[n:] == [("leave", ["enp3s0"]), ("join", ["enp3s0"])]   # IGMP refeito para o switch
    assert src.rejoins == 1 and any("conectado" in m for m in logs)


def test_fixed_adapter_missing_gives_clear_error(monkeypatch):
    FakeNet(monkeypatch, [nic("enp3s0", 2, "192.168.0.10")])
    src = open_source("udp://239.7.7.7:6000?iface=enp9s9", Analyzer())
    with pytest.raises(OSError) as e:
        src.prepare()
    assert "enp9s9" in str(e.value) and "enp3s0" in str(e.value)


def test_no_adapter_connected(monkeypatch):
    FakeNet(monkeypatch, [nic("enp3s0", 2, None, carrier=False)])
    with pytest.raises(OSError) as e:
        open_source("udp://239.7.7.7:6000", Analyzer()).prepare()
    assert "Nenhum adaptador" in str(e.value)


def test_real_interfaces_and_diagnose_do_not_need_root():
    ifs = netif.interfaces(include_loopback=True)
    assert any(i.loopback for i in ifs)
    res = netif.diagnose("239.1.1.1", 80, "", sample=0.1)
    assert any("Porta 80" in t for _, t in res)
    assert isinstance(netif.igmp_memberships(), dict)


def test_unicast_real_socket_any_interface():
    src = open_source("udp://0.0.0.0:0", Analyzer())
    src.prepare()
    assert src.iface_status() == "todos os adaptadores"
    src.close()


def test_spec_keeps_iface_name():
    assert SourceSpec("rtp://239.1.1.1:5000?iface=enx00e04c680a1b&fec=on").iface == "enx00e04c680a1b"
