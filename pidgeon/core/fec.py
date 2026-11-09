"""RTP (RFC 3550 / RFC 2250) e FEC SMPTE ST 2022-1 (Pro-MPEG CoP3) — XOR por colunas e linhas.

Matriz L colunas × D linhas de datagramas RTP de mídia (números de sequência consecutivos):

  - FEC de coluna (porta+2): protege seq S+c, S+c+L, …, S+c+(D-1)·L   (offset=L, NA=D, D-bit=0)
  - FEC de linha  (porta+4): protege seq S+r·L … S+r·L+L-1               (offset=1, NA=L, D-bit=1)

Cada pacote FEC = cabeçalho RTP (PT 96) + cabeçalho FEC de 16 bytes + XOR dos payloads.
"""

import struct

RTP_PT_MP2T = 33
RTP_PT_FEC = 96
RTP_CLOCK = 90000


def xor_bytes(a, b):
    """XOR de dois blocos (o menor é completado com zeros)."""
    if len(a) < len(b):
        a, b = b, a
    n = len(b)
    x = (int.from_bytes(a[:n], "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")
    return x + a[n:]


def rtp_header(seq, ts, ssrc, pt=RTP_PT_MP2T, marker=0):
    return struct.pack("!BBHII", 0x80, (marker << 7) | (pt & 0x7F), seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)


def parse_rtp(d):
    """-> (seq, ts, pt, ssrc, payload) ou None."""
    if len(d) < 12 or (d[0] & 0xC0) != 0x80:
        return None
    cc = d[0] & 0x0F
    hdr = 12 + 4 * cc
    if d[0] & 0x10:
        if len(d) < hdr + 4:
            return None
        hdr += 4 + ((d[hdr + 2] << 8) | d[hdr + 3]) * 4
    end = len(d)
    if d[0] & 0x20:
        end -= d[-1]
    if hdr > end:
        return None
    seq, ts, ssrc = struct.unpack("!HII", d[2:12])
    return seq, ts, d[1] & 0x7F, ssrc, d[hdr:end]


class FecPacket:
    __slots__ = ("snbase", "length_rec", "pt_rec", "ts_rec", "is_row", "offset", "na", "payload", "seq")

    @classmethod
    def parse(cls, d):
        r = parse_rtp(d)
        if r is None:
            return None
        seq, _ts, _pt, _ssrc, body = r
        if len(body) < 16:
            return None
        f = cls()
        f.seq = seq
        f.snbase = (body[0] << 8) | body[1]
        f.length_rec = (body[2] << 8) | body[3]
        f.pt_rec = body[4] & 0x7F
        f.ts_rec = struct.unpack("!I", body[8:12])[0]
        f.is_row = bool(body[12] & 0x40)
        f.offset = body[13]
        f.na = body[14]
        f.payload = body[16:]
        return f

    def protected(self):
        return [(self.snbase + k * self.offset) & 0xFFFF for k in range(self.na)]


class FecEncoder:
    """Gera pacotes FEC de coluna (e opcionalmente de linha) a partir dos datagramas de mídia.

    push(seq, ts, payload) -> (lista_coluna, lista_linha) de datagramas prontos para enviar.
    Os pacotes de coluna saem quando a matriz fecha; o transmissor os intercala com a mídia.
    """

    def __init__(self, cols=10, rows=10, row_fec=False):
        if not (1 <= cols <= 20 and 4 <= rows <= 20 and cols * rows <= 100):
            raise ValueError("FEC SMPTE 2022-1: colunas (L) 1–20, linhas (D) 4–20 e L×D ≤ 100")
        if row_fec and cols < 4:
            raise ValueError("FEC 2D exige L ≥ 4")
        self.L = cols
        self.D = rows
        self.row_fec = row_fec
        self.col_seq = 0
        self.row_seq = 0
        self.base = None
        self.col_acc = [None] * cols
        self.row_acc = None
        self.sent_col = 0
        self.sent_row = 0

    def _empty(self):
        return [0, 0, 0, b""]   # length_rec, pt_rec, ts_rec, payload

    @staticmethod
    def _add(acc, pt, ts, pl):
        acc[0] ^= len(pl)
        acc[1] ^= pt
        acc[2] ^= ts
        acc[3] = xor_bytes(acc[3], pl)

    def _emit(self, seq, snbase, offset, na, is_row, acc):
        hdr = struct.pack("!HHB3sIBBBB", snbase & 0xFFFF, acc[0] & 0xFFFF, 0x80 | (acc[1] & 0x7F),
                          b"\x00\x00\x00", acc[2] & 0xFFFFFFFF, (0x40 if is_row else 0x00),
                          offset & 0xFF, na & 0xFF, 0)
        return rtp_header(seq, 0, 0, RTP_PT_FEC) + hdr + acc[3]

    def push(self, seq, ts, payload, pt=RTP_PT_MP2T):
        L, D = self.L, self.D
        if self.base is None:
            self.base = seq
        k = (seq - self.base) & 0xFFFF
        pos = k % (L * D)
        if pos == 0:
            self.col_acc = [self._empty() for _ in range(L)]
        c = pos % L
        r = pos // L
        self._add(self.col_acc[c], pt, ts, payload)
        cols, rows = [], []
        if self.row_fec:
            if c == 0:
                self.row_acc = self._empty()
            self._add(self.row_acc, pt, ts, payload)
            if c == L - 1:
                snb = (seq - (L - 1)) & 0xFFFF
                rows.append(self._emit(self.row_seq, snb, 1, L, True, self.row_acc))
                self.row_seq = (self.row_seq + 1) & 0xFFFF
                self.sent_row += 1
        if r == D - 1:
            snb = (seq - (D - 1) * L) & 0xFFFF
            cols.append(self._emit(self.col_seq, snb, L, D, False, self.col_acc[c]))
            self.col_seq = (self.col_seq + 1) & 0xFFFF
            self.sent_col += 1
        return cols, rows


class FecDecoder:
    """Recupera um datagrama RTP perdido com um pacote FEC (XOR: uma perda por coluna/linha)."""

    def __init__(self):
        self.L = None
        self.D = None

    def note_params(self, f):
        if f.is_row:
            self.L = f.na
        else:
            self.L, self.D = f.offset, f.na

    @staticmethod
    def missing(f, media):
        return [s for s in f.protected() if s not in media]

    @staticmethod
    def recover(f, media, miss):
        """media: dict seq16 -> (pt, ts, payload); miss: o único seq ausente."""
        length = f.length_rec
        pt = f.pt_rec
        ts = f.ts_rec
        acc = f.payload
        for s in f.protected():
            if s == miss:
                continue
            mpt, mts, mpl = media[s]
            length ^= len(mpl)
            pt ^= mpt
            ts ^= mts
            acc = xor_bytes(acc, mpl)
        return miss, pt, ts, acc[:length]


class RtpFecReceiver:
    """Buffer de reordenação RTP com recuperação por FEC SMPTE 2022-1 (1D e 2D iterativo).

    Pacotes em ordem saem imediatamente; numa falha de sequência o buffer espera até a janela
    (≈ L×D + 2L datagramas; 140 até conhecer L e D — o pior caso do 2022-1) pelo FEC de coluna/linha antes de declarar a perda.
    media()/fec() devolvem a lista de payloads liberados, já em ordem.
    """

    def __init__(self, window=140):
        self.min_window = window
        self.window = window
        self.buf = {}          # seq estendido -> (pt, ts, payload) ainda não liberado
        self.hist = {}         # seq 16 bits -> (pt, ts, payload) recentes (para o XOR)
        self.order = []
        self.next = None
        self.last_ext = None
        self.dec = FecDecoder()
        self.pending = []      # pacotes FEC aguardando mídia
        self.lost = 0
        self.received = 0
        self.recovered = 0
        self.unrecoverable = 0
        self.fec_packets = 0
        self.duplicates = 0
        self._tick = 0

    def _ext(self, seq):
        if self.last_ext is None:
            return seq
        base = self.last_ext & ~0xFFFF
        cand = base | seq
        if cand - self.last_ext > 0x8000:
            cand -= 0x10000
        elif self.last_ext - cand > 0x8000:
            cand += 0x10000
        return cand

    def _remember(self, seq16, item):
        if seq16 not in self.hist:
            self.order.append(seq16)
        self.hist[seq16] = item
        lim = 4 * self.window + 64
        if len(self.order) > lim + 256:
            cut = len(self.order) - lim
            for s in self.order[:cut]:
                self.hist.pop(s, None)
            del self.order[:cut]

    def _release(self):
        out = []
        while self.buf:
            if self.next in self.buf:
                out.append(self.buf.pop(self.next)[2])
                self.next += 1
            elif max(self.buf) - self.next >= self.window:
                self.lost += 1
                self.next += 1
            else:
                break
        return out

    def _try_pending(self):
        """Resolve o que der; repete enquanto houver progresso (2D: linha destrava coluna)."""
        progress = True
        while progress and self.pending:
            progress = False
            keep = []
            for f in self.pending:
                prot = f.protected()
                exts = [self._ext(s) for s in prot]
                if max(exts) < self.next - self.window:
                    if self.dec.missing(f, self.hist):
                        self.unrecoverable += 1
                    continue                       # velho demais
                miss = self.dec.missing(f, self.hist)
                if not miss:
                    continue                       # nada a fazer
                if len(miss) > 1:
                    keep.append(f)
                    continue
                m_ext = self._ext(miss[0])
                if m_ext >= self.last_ext:
                    keep.append(f)                 # ainda pode chegar
                    continue
                if m_ext < self.next:
                    continue                       # já desistimos dele
                seq, pt, ts, pl = self.dec.recover(f, self.hist, miss[0])
                item = (pt, ts, pl)
                self.buf[m_ext] = item
                self._remember(seq, item)
                self.recovered += 1
                progress = True
            self.pending = keep

    def media(self, seq, pt, ts, payload):
        ext = self._ext(seq)
        if self.last_ext is None or ext > self.last_ext:
            self.last_ext = ext
        if self.next is None:
            self.next = ext
        if ext < self.next or ext in self.buf:
            self.duplicates += 1
            return []
        self.received += 1
        item = (pt, ts, payload)
        self.buf[ext] = item
        self._remember(seq & 0xFFFF, item)
        if self.pending:
            self._tick += 1
            if len(self.buf) > 1 or self._tick % 8 == 0:
                self._try_pending()
        return self._release()

    def fec(self, f):
        self.fec_packets += 1
        self.dec.note_params(f)
        if self.dec.L and self.dec.D:
            self.window = max(self.min_window, self.dec.L * self.dec.D + 2 * self.dec.L)
        if self.next is None:
            return []
        self.pending.append(f)
        self._try_pending()
        return self._release()

    def flush(self):
        self._try_pending()
        out = [self.buf[k][2] for k in sorted(self.buf)]
        self.buf = {}
        return out
