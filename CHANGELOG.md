# Changelog

## 1.0.0 — 2026-10-27

Primeira versão.

- Entradas: arquivo (TS 188, BTS/RS 204, M2TS 192 com detecção automática), UDP e RTP
  multicast/unicast (IGMP join, SSM, escolha de interface), HTTP e stdin.
- Análise: PIDs com bitrate/%, CC, TEI, embaralhamento, PCR (intervalo, precisão, jitter),
  PSI/SI completas (PAT, CAT, PMT, NIT, SDT, EIT H/M/L, TDT/TOT, BIT, SDTT, CDT, AIT, DSM-CC).
- ISDB-Tb: camada de cada pacote do BTS (A/B/C/nulo/IIP), contador TSP, quadros,
  flag EWBS, IIP com parâmetros TMCC atuais/próximos, capacidade por camada e
  informação de sincronismo SFN; descritores ABNT NBR 15603.
- TR 101 290 prioridades 1, 2 e 3 + verificações ISDB-T (contador TSP, IIP).
- Player embutido (PyAV/FFmpeg): H.264, HEVC, MPEG-2, AAC/HE-AAC LATM, 1-seg, desentrelaçamento.
- Mosaico de serviços, EPG, visão hexadecimal, gráficos de bitrate/PCR/camadas.
- Gravação: mux completo (formato original ou TS 188), serviço com tabelas, PID; divisão por tempo/tamanho.
- CLI com relatório JSON, gerador de BTS ISDB-Tb de teste e transmissor UDP/RTP.
- Instalador offline sem sudo para Zorin OS / Ubuntu / Debian.
