# Changelog

## 1.1.0 — 2026-11-09

Gerador e transmissor de BTS / TS a partir de gravações.

- **Novo: Gerar / Tx** (interface) e **`pidgeon-gerar`** (linha de comando): a partir de uma
  gravação gera **BTS 204**, **BTS 188**, **TS 188**, **TS 204** (Reed-Solomon RS(204,188)) ou repassa
  o original, gravando em arquivo e/ou transmitindo na rede.
- **Remultiplexação TS → BTS ISDB-Tb**: quadro de multiplexação pelo modo e intervalo de guarda,
  TSPs por camada a partir de modulação/CR/segmentos, IIP (da gravação ou gerada), nulos, trailer
  ISDB-T de 8 bytes (camada, contador TSP, cabeçalho de quadro) e PCR regenerado pelo relógio do BTS.
  Camada por serviço configurável, 1-seg automático na camada A e aviso de capacidade por camada.
- **BTS → TS**: IIP substituída por nulo (taxa constante e PCR exato), remoção de nulos opcional e
  extração de camadas (ex.: só o 1-seg). Paridade RS(204,196) opcional nos 8 bytes finais do BTS.
- **Saída de rede**: UDP puro, RTP (RFC 2250) e **RTP + FEC SMPTE 2022-1** (1D/2D, L×D), para
  **IP unicast ou grupo multicast**, com interface de saída, TTL, DSCP, pacotes por datagrama,
  velocidade, loop e envio no ritmo do PCR.
- **Recepção com FEC**: entradas `rtp://…?fec=on` (ou a opção na janela de rede) recuperam
  datagramas perdidos com o FEC de coluna e de linha (2D iterativo); estatísticas na aba TS.
- Analisador distingue **TS 204 com Reed-Solomon** de BTS 204 e verifica a paridade RS; a barra de
  status mostra o formato (TS 188, BTS 188, BTS 204, TS 204 (RS), M2TS 192).
- `pidgeon-enviar` passa a usar o novo motor (aceita `--fec`).
- **Pacote .deb** (`sudo apt install ./pidgeon_1.1.0_amd64.deb`) para Zorin OS 17/18, Ubuntu
  22.04/24.04, Debian 12/13 e Mint; o CI monta, instala, testa e publica `.deb` + `.zip` na release.
- 20 testes novos: Reed-Solomon, quadro ISDB-T, IIP, todas as conversões com TR 101 290 zerada,
  FEC com perdas simuladas, saída UDP/RTP/FEC e CLI.

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
