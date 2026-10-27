"""Sincronismo de pacotes: detecta TS 188, BTS/RS 204 e M2TS 192 automaticamente."""

SYNC = 0x47
# (tamanho do pacote, posição do byte 0x47 dentro do pacote)
CANDIDATES = ((188, 0), (204, 0), (192, 4))
LOCK_PACKETS = 5


class Framer:
    """Recebe bytes em blocos arbitrários e devolve pacotes completos.

    push() retorna uma lista de pacotes `bytes` com o tamanho bruto (188/192/204).
    Para 192 o pacote começa no cabeçalho de 4 bytes (o 0x47 fica na posição 4).
    """

    def __init__(self, on_event=None, forced_size=None):
        self.buf = bytearray()
        self.size = None
        self.sync_off = 0
        self.locked = False
        self.on_event = on_event
        self.forced = forced_size
        self.sync_losses = 0
        self.sync_byte_errors = 0
        self.bytes_skipped = 0
        self._bad_run = 0

    def _event(self, kind, text):
        if self.on_event:
            self.on_event(kind, text)

    def _find_lock(self):
        buf = self.buf
        n = len(buf)
        cands = CANDIDATES
        if self.forced:
            cands = tuple(c for c in CANDIDATES if c[0] == self.forced) or CANDIDATES
        best_need = max(sz for sz, _ in cands) * LOCK_PACKETS + 4
        start = 0
        while True:
            i = buf.find(b"\x47", start)
            if i < 0:
                # descarta tudo, guardando uma cauda curta
                drop = max(0, n - 4)
                self.bytes_skipped += drop
                del buf[:drop]
                return False
            for size, off in cands:
                base = i - off
                if base < 0:
                    continue
                if base + size * LOCK_PACKETS > n:
                    continue
                ok = True
                for k in range(1, LOCK_PACKETS):
                    if buf[base + k * size + off] != SYNC:
                        ok = False
                        break
                if ok:
                    if base:
                        self.bytes_skipped += base
                        del buf[:base]
                    self.size = size
                    self.sync_off = off
                    self.locked = True
                    self._bad_run = 0
                    self._event("sync", size)
                    return True
            if n - i < best_need:
                # aguarda mais dados antes de decidir (mantém 4 bytes antes do 0x47 p/ o M2TS)
                cut = max(0, i - 4)
                if cut:
                    self.bytes_skipped += cut
                    del buf[:cut]
                return False
            start = i + 1

    def push(self, data):
        self.buf += data
        out = []
        while True:
            if not self.locked:
                if not self._find_lock():
                    return out
            size = self.size
            off = self.sync_off
            buf = self.buf
            n = len(buf)
            pos = 0
            append = out.append
            while pos + size <= n:
                if buf[pos + off] != SYNC:
                    self.sync_byte_errors += 1
                    self._bad_run += 1
                    if self._bad_run >= 3:
                        break
                    # tolera um byte de sync corrompido isolado (pacote descartado)
                    self._event("sync_byte", size)
                    pos += size
                    continue
                self._bad_run = 0
                append(bytes(buf[pos:pos + size]))
                pos += size
            if pos:
                del buf[:pos]
            if self._bad_run >= 3:
                self.locked = False
                self.sync_losses += 1
                self._bad_run = 0
                self._event("loss", size)
                # remove 1 byte para forçar nova busca
                del self.buf[:1]
                continue
            return out

    def reset(self):
        self.buf = bytearray()
        self.locked = False
        self.size = None
