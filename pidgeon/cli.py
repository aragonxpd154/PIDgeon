"""Linha de comando do PIDgeon: análise sem interface gráfica e relatório JSON."""

import argparse
import json
import sys
import time

from . import __version__
from .core.analyzer import Analyzer
from .core.sources import open_source, SourceSpec
from .core.names import LAYER_NAMES


def fmt_rate(bps):
    if bps >= 1e6:
        return "%.3f Mbps" % (bps / 1e6)
    if bps >= 1e3:
        return "%.1f kbps" % (bps / 1e3)
    return "%.0f bps" % bps


def run_analysis(source, seconds=None, fast=True, verbose=False):
    spec = SourceSpec(source)
    clock = "stream" if (spec.kind == "file" and fast) else "wall"
    an = Analyzer(clock=clock)
    src = open_source(source, an, realtime=not fast)
    src.start()
    t0 = time.monotonic()
    shown = 0
    try:
        while src.is_alive():
            time.sleep(0.2)
            if verbose:
                with an.lock:
                    msgs = list(an.messages)[shown:]
                    shown += len(msgs)
                for ts, lvl, txt in msgs:
                    print("%s %-5s %s" % (ts.strftime("%H:%M:%S"), lvl, txt), file=sys.stderr)
            if seconds and (an.elapsed() if clock == "stream" else time.monotonic() - t0) >= seconds:
                break
    except KeyboardInterrupt:
        pass
    src.stop()
    src.join(2)
    with an.lock:
        if clock == "stream" and an.start_t is not None and an.t > an.win_start:
            an._close_window(an.t)
    return an


def print_report(an):
    r = an.to_report()
    s = r["summary"]
    print("PIDgeon %s — resumo" % __version__)
    print("  Pacotes: %d × %s bytes | Bitrate TS: %s | Útil (sem nulos): %s | PCR: %s" % (
        s["packets"], s["packet_size"], fmt_rate(s["bitrate_bps"]), fmt_rate(s["effective_bitrate_bps"]),
        fmt_rate(s["pcr_mux_rate_bps"] or 0)))
    print("  TSID 0x%04X  ONID %s  Rede: %s  TS: %s  Tecla virtual: %s" % (
        s["tsid"] or 0, ("0x%04X" % s["onid"]) if s["onid"] is not None else "-", s["network_name"],
        s["ts_name"], s["remote_control_key"]))
    print("\nServiços:")
    for sv in r["services"]:
        print("  0x%04X %-24s %-14s PMT %-5s PCR %-5s camada %s%s" % (
            sv["service_id"], sv["name"], sv["type"], sv["pmt_pid"], sv["pcr_pid"], sv["layer"] or "-",
            "  [1-seg]" if sv["one_seg"] else ""))
        if sv["present"]:
            print("         agora: %s | depois: %s" % (sv["present"], sv["following"]))
        for es in sv["streams"]:
            print("         PID %-5d %s %s tag=%s %s" % (es["pid"], es["stream_type"], es["type"],
                  es["component_tag"], es["lang"] or ""))
    print("\nPIDs:")
    for p in r["pids"]:
        print("  (%s) %5d 0x%04X %-28s %12s %6.2f%%  CC err %d  TEI %d  pkts %d" % (
            p["layer"] or "?", p["pid"], p["pid"], p["description"][:28], fmt_rate(p["bitrate_bps"]),
            p["percent"], p["cc_errors"], p["tei"], p["packets"]))
    print("\nTabelas:")
    for t in r["tables"]:
        print("  PID %-5d %-22s ext 0x%04X v%-2s seções %d  CRC err %d" % (
            t["pid"], t["table"], t["ext"], t["version"], t["sections"], t["crc_errors"]))
    print("\nTR 101 290:")
    for e in r["tr101290"]:
        print("  [%s] %-4s %-26s %6d  %s" % ("OK " if e["count"] == 0 else "ERR", e["code"], e["name"],
                                           e["count"], e["last"]))
    if "isdbt" in r:
        i = r["isdbt"]
        print("\nISDB-T (BTS 204):")
        print("  Taxas por camada: " + ", ".join("%s=%s" % (k, fmt_rate(v)) for k, v in i["layers_bps"].items()))
        print("  Quadros: %s  TSP/quadro: %s  erros contador TSP: %d  EWBS: %d" % (
            i["frames"], i["packets_per_frame"], i["tsp_counter_errors"], i["emergency_flag"]))
        iip = i.get("iip")
        if iip:
            print("  IIP: modo %s, GI código %s, recepção parcial %s, CRC %s" % (
                iip.get("mode"), iip.get("gi"), iip.get("partial"), "OK" if iip.get("crc_ok") else "ERRO"))
            for l in iip.get("layers_info") or []:
                if l and l != "None":
                    print("    Camada %s: %s CR %s %s seg I=%s capacidade %s" % (
                        l["name"], l["mod"], l["cr"], l["segs"], l["ti"], fmt_rate(float(l["capacity"]))))
    _ = LAYER_NAMES


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pidgeon-cli", description="PIDgeon — análise de TS/BTS sem interface")
    ap.add_argument("fonte", help="arquivo, udp://grupo:porta, rtp://…, http://… ou - (stdin)")
    ap.add_argument("-s", "--segundos", type=float, help="tempo de análise")
    ap.add_argument("--tempo-real", action="store_true", help="lê arquivos na taxa do mux")
    ap.add_argument("-j", "--json", help="salva relatório JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostra mensagens")
    ap.add_argument("--version", action="version", version="PIDgeon %s" % __version__)
    a = ap.parse_args(argv)
    an = run_analysis(a.fonte, a.segundos, fast=not a.tempo_real, verbose=a.verbose)
    print_report(an)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(an.to_report(), f, ensure_ascii=False, indent=2, default=str)
        print("\nRelatório salvo em %s" % a.json)


if __name__ == "__main__":
    main()
