# Changelog

## 1.2.0 - 2026-11-10

Adaptador de rede que acompanha a troca de placa, diagnóstico de rede e pacotes autocontidos.

- **Correção: fluxo de rede não abria em outra máquina.** A janela de rede gravava o IP do adaptador
  no endereço (`iface=192.168.x.x`) e a lista de interfaces era lida uma única vez; em outra
  máquina, ou depois de trocar de placa, o IP não existia e o IGMP join falhava. Sem rota padrão
  (rede isolada), o modo "padrão do sistema" também não conseguia entrar no grupo. Não era falta
  de privilégio: receber multicast não precisa de root.
- **Adaptador escolhido pelo nome**, com join pelo índice da interface (`ip_mreqn`): continua valendo
  se o IP mudar. Endereços antigos com IP continuam funcionando (viram automático).
- **Automático que acompanha a placa:** join em todos os adaptadores conectados (um por sub-rede,
  cabo antes de Wi-Fi), trava no adaptador por onde o fluxo chega (`IP_PKTINFO`) e, a cada 2 s,
  refaz o join se a placa for trocada, removida, perder o cabo ou mudar de IP - sem reabrir o fluxo.
- **Adaptador fixo** espera a placa voltar (ex.: USB desplugada) e refaz o join quando o cabo volta,
  para o switch com IGMP snooping reaprender a porta.
- **Lista de adaptadores ao vivo** nas janelas de rede e de geração (atualiza a cada 2 s), com IP,
  tipo (cabo, USB, Wi-Fi, VLAN, bond, bridge), velocidade e estado; VLANs, bonds e bridges contam
  como rede real.
- **Erros na hora e explicados:** o socket e o join são feitos ao clicar em Abrir; adaptador
  inexistente, porta abaixo de 1024 ou nenhum adaptador conectado aparecem numa mensagem com
  botão para o diagnóstico.
- **Novo: Diagnóstico de rede** (Início → Diagnóstico, `pidgeon-cli --diagnostico`): adaptadores,
  tráfego chegando em cada placa, grupos IGMP inscritos (`/proc/net/igmp`), rota padrão,
  `rp_filter` (com botão para corrigir via `pkexec`, opcionalmente permanente) e firewall
  ufw/firewalld com o comando para liberar a porta. `pidgeon-cli --interfaces` lista os adaptadores.
- Barra de status mostra o adaptador em uso; a aba TS mostra o adaptador e os joins refeitos.
- Transmissão (`pidgeon-gerar` / Gerar / Tx) também escolhe o adaptador de saída pelo nome, usa o
  adaptador conectado quando não há rota para o grupo e reaplica a interface se a placa mudar.
- **Correção no remux TS → BTS:** em picos de taxa do vídeo (quadros I) o intervalo de PCR podia
  passar de 40 ms (TR 101 290 2.3a). O remux agora insere pacotes só com PCR (adaptation field)
  quando um PID ficaria mais de 30 ms sem PCR.
- **Pacote .deb autocontido:** as bibliotecas xcb-util do Qt vão dentro do pacote (conferidas por
  SHA-256 no build) e só são usadas se faltarem no sistema; o .deb depende apenas do que já vem em
  qualquer desktop e instala sem internet (`sudo dpkg -i`). O .zip continua offline e sem sudo.
- 9 testes novos (troca de placa, cabo, adaptador inexistente, IP antigo, diagnóstico) - 45 no total.

## 1.1.0 - 2026-11-09

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

## 1.0.0 - 2026-10-27

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
