"""Reed-Solomon sobre GF(2^8) (polinômio x^8+x^4+x^3+x^2+1, alfa = 0x02).

- RS(204,188), t=8: código externo do DVB/ISDB-T (encurtado do RS(255,239)) — usado no "TS 204".
- RS(204,196), t=4: paridade opcional dos 8 últimos bytes do BTS ISDB-T (encurtado do RS(255,247)),
  calculada sobre os 188 bytes do TSP + 8 bytes de informação ISDB-T.

A paridade é linear nos dados, então cada posição do pacote tem uma tabela de 256 entradas com a
contribuição daquele byte (inteiro de 64/128 bits). Codificar = XOR de 188 (ou 196) consultas —
rápido o bastante em Python puro para transmitir em tempo real, sem numpy.
"""

import functools
import operator

PRIM = 0x11D

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= PRIM
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def generator_poly(nparity):
    """g(x) = (x - a^0)(x - a^1)…(x - a^(n-1)), coeficientes do grau maior para o menor."""
    g = [1]
    for i in range(nparity):
        r = EXP[i]
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= gmul(c, r)
        g = ng
    return g


class RSEncoder:
    """Codificador sistemático RS(k+n, k) encurtado. encode(dados) -> n bytes de paridade."""

    def __init__(self, k, nparity):
        self.k = k
        self.n = nparity
        g = generator_poly(nparity)
        self.g = g
        bits = nparity * 8
        # contribuição de um byte 0x01 na posição i (0 = primeiro byte transmitido):
        # d_i·x^(k-1-i)·x^n mod g(x)
        tables = []
        for i in range(k):
            deg = k - 1 - i
            base = self._mod_xpow(deg + nparity)
            # base[v] para v = 2^b, depois combina por linearidade
            col = [0] * 256
            powv = []
            for b in range(8):
                v = 1 << b
                coeffs = [gmul(v, c) for c in base]
                powv.append(int.from_bytes(bytes(coeffs), "big"))
            for v in range(1, 256):
                low = v & -v
                col[v] = col[v ^ low] ^ powv[low.bit_length() - 1]
            tables.append(col)
        self.tables = tables
        self.bits = bits

    def _mod_xpow(self, e):
        """x^e mod g(x), coeficientes (grau n-1 … 0)."""
        g = self.g
        n = self.n
        # representação: lista de n coeficientes; começa em x^0 = [0,…,0,1]
        r = [0] * n
        r[-1] = 1
        for _ in range(e):
            # multiplica por x: desloca à esquerda; o coeficiente que sai (grau n) é reduzido por g
            top = r[0]
            r = r[1:] + [0]
            if top:
                for j in range(n):
                    r[j] ^= gmul(top, g[j + 1])
        return r

    def parity_int(self, data):
        return functools.reduce(operator.xor, map(list.__getitem__, self.tables, data), 0)

    def encode(self, data):
        """Paridade (bytes) para os k primeiros bytes de data."""
        return self.parity_int(data[:self.k]).to_bytes(self.n, "big")

    def encode_lfsr(self, data):
        """Implementação de referência (LFSR byte a byte) — usada nos testes."""
        g = self.g
        n = self.n
        reg = [0] * n
        for b in data[:self.k]:
            fb = b ^ reg[0]
            reg = reg[1:] + [0]
            if fb:
                for j in range(n):
                    reg[j] ^= gmul(fb, g[j + 1])
        return bytes(reg)

    def syndromes_ok(self, codeword):
        """True se as n síndromes da palavra (dados + paridade) forem nulas."""
        for j in range(self.n):
            aj = EXP[j]
            s = 0
            for c in codeword:
                s = gmul(s, aj) ^ c
            if s:
                return False
        return True


@functools.lru_cache(maxsize=None)
def rs_204_188():
    return RSEncoder(188, 16)


@functools.lru_cache(maxsize=None)
def rs_204_196():
    return RSEncoder(196, 8)


def add_rs204(pkt188):
    """TS 188 -> TS 204 com paridade RS(204,188)."""
    return pkt188 + rs_204_188().encode(pkt188)


def looks_like_rs204(pkts, need=4):
    """Verdadeiro se os pacotes de 204 bytes carregam paridade RS(204,188) válida."""
    enc = rs_204_188()
    ok = 0
    for p in pkts[:need]:
        if len(p) != 204 or enc.encode(p[:188]) != bytes(p[188:204]):
            return False
        ok += 1
    return ok > 0

