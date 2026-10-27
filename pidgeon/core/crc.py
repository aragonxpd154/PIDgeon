"""CRC-32/MPEG-2 usado nas seções PSI/SI e no IIP do ISDB-T."""

_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if (_c & 0x80000000) else (_c << 1)
    _TABLE.append(_c & 0xFFFFFFFF)
_TABLE = tuple(_TABLE)


def crc32_mpeg(data, crc=0xFFFFFFFF):
    """Calcula o CRC-32/MPEG-2. Sobre uma seção inteira (com o CRC) o resultado é 0."""
    t = _TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ t[((crc >> 24) ^ b) & 0xFF]
    return crc
