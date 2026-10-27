"""Montagem de seções PSI/SI a partir de pacotes TS, e empacotamento de seções."""


class SectionAssembler:
    """Reconstrói seções de um PID. feed() devolve a lista de seções completas (bytes)."""
    __slots__ = ("buf", "active")

    def __init__(self):
        self.buf = bytearray()
        self.active = False

    def reset(self):
        self.buf = bytearray()
        self.active = False

    def feed(self, payload, pusi, discontinuity=False):
        out = []
        if discontinuity:
            self.reset()
        if not payload:
            return out
        if pusi:
            ptr = payload[0]
            if self.active and ptr:
                self.buf += payload[1:1 + ptr]
                self._extract(out, final=True)
            self.buf = bytearray(payload[1 + ptr:])
            self.active = True
            self._extract(out)
        elif self.active:
            self.buf += payload
            self._extract(out)
        return out

    def _extract(self, out, final=False):
        buf = self.buf
        while True:
            if len(buf) < 3:
                break
            if buf[0] == 0xFF:  # enchimento até o fim do pacote
                self.buf = bytearray()
                self.active = False
                return
            slen = ((buf[1] & 0x0F) << 8) | buf[2]
            total = 3 + slen
            if len(buf) < total:
                break
            out.append(bytes(buf[:total]))
            del buf[:total]
        if final:
            self.buf = bytearray()


def section_to_packets(pid, section, cc):
    """Empacota uma seção em pacotes TS de 188 bytes. Retorna (lista, próximo cc)."""
    pkts = []
    data = b"\x00" + bytes(section)  # pointer_field = 0
    first = True
    while data:
        chunk = data[:184]
        data = data[184:]
        hdr = bytes((0x47, (0x40 if first else 0x00) | (pid >> 8), pid & 0xFF, 0x10 | (cc & 0x0F)))
        pkts.append(hdr + chunk + b"\xFF" * (184 - len(chunk)))
        cc = (cc + 1) & 0x0F
        first = False
    return pkts, cc
