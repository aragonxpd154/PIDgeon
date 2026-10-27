"""Decodificação de strings de SI (DVB / ABNT NBR 15603-2) e datas MJD/BCD."""

import datetime

_TABLE_SELECT = {
    0x01: "iso8859_5", 0x02: "iso8859_6", 0x03: "iso8859_7", 0x04: "iso8859_8",
    0x05: "iso8859_9", 0x06: "iso8859_10", 0x07: "iso8859_11", 0x09: "iso8859_13",
    0x0A: "iso8859_14", 0x0B: "iso8859_15",
}
_8859_N = {1: "iso8859_1", 2: "iso8859_2", 3: "iso8859_3", 4: "iso8859_4", 5: "iso8859_5",
           6: "iso8859_6", 7: "iso8859_7", 8: "iso8859_8", 9: "iso8859_9", 10: "iso8859_10",
           11: "iso8859_11", 13: "iso8859_13", 14: "iso8859_14", 15: "iso8859_15",
           16: "iso8859_16"}

# Codificação padrão quando não há byte de seleção. No ISDB-Tb (Brasil) as
# emissoras usam Latin (ISO 8859-15 / 8859-1).
DEFAULT_ENCODING = "iso8859_15"


def decode_text(data):
    """Decodifica uma string de SI com seleção de tabela de caracteres estilo DVB/ABNT."""
    if not data:
        return ""
    data = bytes(data)
    enc = DEFAULT_ENCODING
    first = data[0]
    if first < 0x20:
        if first in _TABLE_SELECT:
            enc = _TABLE_SELECT[first]
            data = data[1:]
        elif first == 0x10 and len(data) >= 3:
            enc = _8859_N.get((data[1] << 8) | data[2], DEFAULT_ENCODING)
            data = data[3:]
        elif first == 0x11:
            enc = "utf_16_be"
            data = data[1:]
        elif first == 0x13:
            enc = "gb2312"
            data = data[1:]
        elif first == 0x14:
            enc = "big5"
            data = data[1:]
        elif first == 0x15:
            enc = "utf_8"
            data = data[1:]
        elif first == 0x1F and len(data) >= 2:
            data = data[2:]
        else:
            data = data[1:]
    if enc in ("utf_16_be", "utf_8", "gb2312", "big5"):
        try:
            return data.decode(enc, errors="replace").replace("\x00", "")
        except LookupError:
            return data.decode("latin-1", errors="replace")
    out = []
    for b in data:
        if b == 0x8A:
            out.append("\n")
        elif 0x80 <= b <= 0x9F or b < 0x20 and b not in (0x0A, 0x0D):
            continue
        else:
            out.append(bytes((b,)).decode(enc, errors="replace"))
    return "".join(out).strip()


def lang_code(b):
    try:
        return bytes(b[:3]).decode("latin-1")
    except Exception:
        return "???"


def bcd(v):
    return ((v >> 4) & 0xF) * 10 + (v & 0xF)


def mjd_to_date(mjd):
    """Converte Modified Julian Date (ETSI EN 300 468 anexo C)."""
    if mjd in (0, 0xFFFF):
        return None
    yp = int((mjd - 15078.2) / 365.25)
    mp = int((mjd - 14956.1 - int(yp * 365.25)) / 30.6001)
    day = mjd - 14956 - int(yp * 365.25) - int(mp * 30.6001)
    k = 1 if mp in (14, 15) else 0
    year = yp + k + 1900
    month = mp - 1 - k * 12
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def mjd_utc(b):
    """5 bytes: MJD(16) + hh mm ss em BCD. Retorna datetime (sem fuso) ou None."""
    if len(b) < 5 or bytes(b[:5]) == b"\xff\xff\xff\xff\xff":
        return None
    d = mjd_to_date((b[0] << 8) | b[1])
    if d is None:
        return None
    try:
        return datetime.datetime(d.year, d.month, d.day, bcd(b[2]), bcd(b[3]), bcd(b[4]))
    except ValueError:
        return None


def bcd_duration(b):
    """3 bytes hh mm ss em BCD -> timedelta (ou None se indefinido)."""
    if bytes(b[:3]) == b"\xff\xff\xff":
        return None
    return datetime.timedelta(hours=bcd(b[0]), minutes=bcd(b[1]), seconds=bcd(b[2]))


def hexdump(data, width=16, max_bytes=None):
    data = bytes(data)
    if max_bytes is not None:
        data = data[:max_bytes]
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join("%02X" % c for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        lines.append("%04X  %-*s  %s" % (off, width * 3 - 1, hx, asc))
    return "\n".join(lines)


def short_hex(data, limit=32):
    data = bytes(data)
    s = " ".join("%02X" % c for c in data[:limit])
    if len(data) > limit:
        s += " …"
    return s
