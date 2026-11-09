"""pidgeon-gerar — gera BTS 204, BTS 188, TS 188 ou TS 204 a partir de uma gravação e
grava em arquivo e/ou transmite por UDP puro, RTP ou RTP + FEC (SMPTE 2022-1), unicast ou multicast.

Exemplos:
  pidgeon-gerar gravacao.bts -f ts188 -o saida.ts
  pidgeon-gerar gravacao.ts  -f bts204 -o saida.bts                      # remux TS -> BTS (IIP automática)
  pidgeon-gerar gravacao.bts -f bts204 -d udp://239.1.1.1:1234 --iface enp3s0 --ttl 8 --loop
  pidgeon-gerar gravacao.bts -f ts188  -d "rtp://10.0.0.20:5000?fec=10x10"   # unicast com FEC 1D
  pidgeon-gerar gravacao.ts  -f bts188 -d rtp://239.2.2.2:5000 --fec 20x5 --fec-2d
  pidgeon-gerar gravacao.ts  -f bts204 --modo 3 --gi 1/16 --camada-a qpsk,2/3,1,4 \\
                --camada-b 64qam,3/4,12,2 --servico-camada 0xE760=A -o saida.bts
"""

import argparse
import sys
import threading

from .. import __version__
from ..core.convert import Generator, GenConfig, FORMATS, probe, LAYER_LETTER
from ..core.netout import NetConfig
from ..core.remux import IsdbParams, Layer
from ..core.names import ISDB_GUARD

MODS = {"dqpsk": 0, "qpsk": 1, "16qam": 2, "16-qam": 2, "64qam": 3, "64-qam": 3}
CRS = {"1/2": 0, "2/3": 1, "3/4": 2, "5/6": 3, "7/8": 4}
GIS = {v: k for k, v in ISDB_GUARD.items()}
# I (profundidade) -> código, por modo
TI_CODE = {1: {0: 0, 4: 1, 8: 2, 16: 3}, 2: {0: 0, 2: 1, 4: 2, 8: 3}, 3: {0: 0, 1: 1, 2: 2, 4: 3}}


def parse_layer(text, mode):
    """'64qam,3/4,12,2' -> Layer (modulação, CR, segmentos, entrelaçamento I)."""
    if text.lower() in ("off", "nao", "não", "-", "none"):
        return None
    parts = [p.strip().lower() for p in text.split(",")]
    if len(parts) < 3:
        raise ValueError("Camada: use modulação,CR,segmentos[,I] — ex.: 64qam,3/4,12,2")
    mod = MODS.get(parts[0])
    cr = CRS.get(parts[1])
    if mod is None or cr is None:
        raise ValueError("Modulação (qpsk, 16qam, 64qam, dqpsk) ou CR (1/2…7/8) inválidos: %s" % text)
    segs = int(parts[2])
    i = int(parts[3]) if len(parts) > 3 else {1: 8, 2: 4, 3: 2}[mode]
    ti = TI_CODE[mode].get(i)
    if ti is None:
        raise ValueError("Entrelaçamento I=%d inválido para o modo %d (válidos: %s)" % (i, mode, sorted(TI_CODE[mode])))
    return Layer(mod, cr, ti, segs)


def parse_assign(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if v.upper() not in ("A", "B", "C"):
            raise ValueError("Use ID=A|B|C: %s" % it)
        out[int(k, 0)] = v.upper()
    return out


def build_parser():
    ap = argparse.ArgumentParser(prog="pidgeon-gerar", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Gera BTS/TS a partir de uma gravação e grava ou transmite por "
                                             "UDP/RTP/RTP+FEC (unicast ou multicast)",
                                 epilog=__doc__.split("Exemplos:", 1)[1].join(("Exemplos:", "")))
    ap.add_argument("gravacao", help="arquivo .ts / .bts / .trp / .m2ts")
    ap.add_argument("-f", "--formato", choices=list(FORMATS), default="bts204",
                    help="formato de saída (padrão: bts204)")
    ap.add_argument("-o", "--saida", help="arquivo de saída")
    ap.add_argument("-d", "--destino", help="udp://ip:porta ou rtp://ip:porta[?fec=LxD] (unicast ou multicast)")
    g = ap.add_argument_group("rede")
    g.add_argument("--iface", help="interface de saída (nome ou IP), ex.: enp3s0")
    g.add_argument("--ttl", type=int, help="TTL (multicast; padrão 16)")
    g.add_argument("--dscp", type=int, help="DSCP/QoS 0–63 (ex.: 34 = AF41, 46 = EF)")
    g.add_argument("--pacotes", type=int, help="pacotes TS por datagrama (1–7, padrão 7)")
    g.add_argument("--fec", help="ativa RTP + FEC SMPTE 2022-1 com LxD colunas×linhas (ex.: 10x10)")
    g.add_argument("--fec-2d", action="store_true", help="também FEC de linha (porta+4)")
    g.add_argument("--sem-loopback", action="store_true", help="não entrega o multicast na própria máquina")
    g = ap.add_argument_group("reprodução")
    g.add_argument("--loop", action="store_true", help="repete a gravação sem parar")
    g.add_argument("--segundos", type=float, help="limita a duração gerada")
    g.add_argument("--taxa", type=float, help="força a taxa (bps) — só para TS sem PCR utilizável")
    g.add_argument("--velocidade", type=float, default=1.0, help="multiplicador da velocidade de envio")
    g.add_argument("--tempo-real", action="store_true", help="grava o arquivo no ritmo do tempo real")
    g = ap.add_argument_group("TS (188/204)")
    g.add_argument("--remover-nulos", action="store_true", help="remove pacotes nulos (saída VBR)")
    g.add_argument("--camadas", help="BTS -> TS: camadas mantidas, ex.: A ou BC (as demais viram nulo)")
    g = ap.add_argument_group("BTS ISDB-Tb (remultiplexação)")
    g.add_argument("--modo", type=int, choices=(1, 2, 3), help="modo (padrão: o da IIP da gravação, ou 3)")
    g.add_argument("--gi", choices=list(GIS), help="intervalo de guarda (1/32, 1/16, 1/8, 1/4)")
    g.add_argument("--camada-a", help="ex.: qpsk,2/3,1,4  (modulação,CR,segmentos,I) ou off")
    g.add_argument("--camada-b", help="ex.: 64qam,3/4,12,2 ou off")
    g.add_argument("--camada-c", help="ex.: off")
    g.add_argument("--sem-1seg", action="store_true", help="sem recepção parcial na camada A")
    g.add_argument("--servico-camada", action="append", metavar="SID=X", help="coloca o serviço na camada X")
    g.add_argument("--pid-camada", action="append", metavar="PID=X", help="coloca o PID na camada X")
    g.add_argument("--remux", action="store_true", help="remultiplexa mesmo que a origem já seja BTS 204")
    g.add_argument("--paridade-rs", action="store_true",
                   help="BTS 204: grava paridade RS(204,196) nos 8 últimos bytes (padrão: zeros)")
    ap.add_argument("--analisar", action="store_true", help="só mostra o que foi detectado na gravação")
    ap.add_argument("-q", "--quieto", action="store_true")
    ap.add_argument("--version", action="version", version="PIDgeon %s" % __version__)
    return ap


def config_from_args(a, pr):
    net = None
    if a.destino:
        net = NetConfig.from_url(a.destino)
        if a.fec:
            if net.proto == "udp":
                net.proto = "rtp"
            net.proto = "rtp-fec"
            c, _, r = a.fec.lower().partition("x")
            net.fec_cols, net.fec_rows = int(c), int(r or c)
        if a.fec_2d:
            net.fec_2d = True
        if a.iface:
            net.iface = a.iface
        if a.ttl:
            net.ttl = a.ttl
        if a.dscp is not None:
            net.dscp = a.dscp
        if a.pacotes:
            net.pkts_per_dgram = a.pacotes
        if a.sem_loopback:
            net.loopback = False
        net.validate()
    isdb = None
    if any((a.modo, a.gi, a.camada_a, a.camada_b, a.camada_c, a.sem_1seg)):
        base = pr.iip_params or IsdbParams()
        mode = a.modo or base.mode
        isdb = IsdbParams(mode=mode, gi=GIS[a.gi] if a.gi else base.gi, partial=not a.sem_1seg and base.partial,
                          layers=list(base.layers))
        for i, txt in enumerate((a.camada_a, a.camada_b, a.camada_c)):
            if txt:
                isdb.layers[i] = parse_layer(txt, mode)
        # (o código de entrelaçamento mantém a mesma profundidade em tempo em qualquer modo)
        if isdb.layers[0] is None or isdb.layers[0].segs != 1:
            isdb.partial = False
        isdb.validate()
    keep = None
    if a.camadas:
        keep = tuple(sorted({"ABC".index(c) + 1 for c in a.camadas.upper() if c in "ABC"}))
    realtime = True if a.tempo_real else bool(net)
    return GenConfig(source=a.gravacao, fmt=a.formato, out_file=a.saida, net=net, isdb=isdb,
                     force_remux=a.remux, service_layers=parse_assign(a.servico_camada),
                     pid_layers=parse_assign(a.pid_camada), remove_nulls=a.remover_nulos,
                     keep_layers=keep, bts_rs_parity=a.paridade_rs, loop=a.loop, realtime=realtime,
                     rate_bps=a.taxa, speed=a.velocidade, seconds=a.segundos)


def print_probe(pr):
    print("Gravação: %s" % pr.path)
    print("  %s" % pr.summary())
    for sid, name, one, ly, pids in pr.services():
        print("  serviço 0x%04X %-24s %s%s  PIDs %s" % (sid, name, ("camada %s " % LAYER_LETTER[ly]) if ly else "",
                                                      "[1-seg]" if one else "",
                                                      " ".join("0x%04X" % p for p in sorted(pids))))
    if pr.pcr_pid is not None:
        print("  PCR de referência: PID 0x%04X" % pr.pcr_pid)


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        pr = probe(a.gravacao)
    except (OSError, ValueError) as exc:
        print("Erro: %s" % exc, file=sys.stderr)
        return 1
    if not a.quieto or a.analisar:
        print_probe(pr)
    if a.analisar:
        return 0
    if not a.saida and not a.destino:
        print("Erro: informe -o/--saida e/ou -d/--destino", file=sys.stderr)
        return 2
    try:
        cfg = config_from_args(a, pr)
        gen = Generator(cfg, pr, log=(lambda m: None) if a.quieto else (lambda m: print(m, file=sys.stderr)))
    except ValueError as exc:
        print("Erro: %s" % exc, file=sys.stderr)
        return 2
    th = threading.Thread(target=gen.run, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(1.0)
            if not a.quieto and th.is_alive():
                st = gen.stats
                n = st.net
                extra = ""
                if n:
                    extra = " | %d datagramas" % n.get("datagrams", 0)
                    if n.get("fec_col") or n.get("fec_row"):
                        extra += ", FEC %d col + %d lin" % (n.get("fec_col", 0), n.get("fec_row", 0))
                sys.stderr.write("\r  %7.1f s de mídia | %.3f Mbps | %d pacotes%s%s   " % (
                    st.media_t, st.rate_bps() / 1e6, st.packets_out, extra,
                    (" | loop %d" % st.loops) if st.loops else " | %3.0f%%" % (100 * st.progress)))
                sys.stderr.flush()
    except KeyboardInterrupt:
        gen.stop()
        th.join(3)
    if not a.quieto:
        sys.stderr.write("\n")
    return 1 if gen.stats.error else 0


if __name__ == "__main__":
    sys.exit(main())

