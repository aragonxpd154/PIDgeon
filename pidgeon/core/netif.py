"""Adaptadores de rede: listagem ao vivo, escolha automática e diagnóstico (sem precisar de root).

Tudo vem do próprio kernel: /sys/class/net (estado, cabo, Wi-Fi, velocidade, contadores),
ioctl SIOCGIFADDR/SIOCGIFNETMASK (IPv4), /proc/net/route, /proc/net/igmp e
/proc/sys/net/ipv4/conf/*/rp_filter. O join multicast é feito pelo ÍNDICE da interface
(struct ip_mreqn), então continua válido mesmo se o IP do adaptador mudar.
"""

import fcntl
import os
import socket
import struct
import subprocess
import time
from dataclasses import dataclass

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
SYS = "/sys/class/net"
VIRTUAL_PREFIXES = ("docker", "veth", "virbr", "br-", "vmnet", "vboxnet", "tailscale", "zt", "wg", "tun",
                    "tap", "lxc", "lxd", "cni", "flannel", "kube", "ifb", "dummy", "sit", "ip6tnl", "gre")


@dataclass
class NetIf:
    name: str
    index: int
    ipv4: str = None
    prefix: int = None
    mac: str = ""
    up: bool = False
    carrier: bool = False
    wireless: bool = False
    loopback: bool = False
    virtual: bool = False
    speed: int = None          # Mb/s
    vlan: bool = False
    bond: bool = False
    bridge: bool = False

    @property
    def kind(self):
        if self.loopback:
            return "loopback"
        if self.wireless:
            return "Wi-Fi"
        if self.virtual:
            return "virtual"
        if self.vlan:
            return "VLAN"
        if self.bond:
            return "bond"
        if self.bridge:
            return "bridge"
        if self.name.startswith(("enx", "usb")):
            return "cabo (USB)"
        return "cabo"

    @property
    def usable(self):
        """Adaptador real, ligado, com cabo/conectado e com IPv4."""
        return self.up and self.carrier and self.ipv4 is not None and not self.loopback and not self.virtual

    def network(self):
        if self.ipv4 is None or self.prefix is None:
            return None
        ip = struct.unpack("!I", socket.inet_aton(self.ipv4))[0]
        mask = (0xFFFFFFFF << (32 - self.prefix)) & 0xFFFFFFFF if self.prefix else 0
        return ip & mask, self.prefix

    def label(self):
        parts = [self.ipv4 or "sem IPv4", self.kind]
        if self.speed and self.speed > 0:
            parts.append("%s" % ("%d Gb/s" % (self.speed // 1000) if self.speed >= 1000 else "%d Mb/s" % self.speed))
        if not self.up:
            parts.append("desligado")
        elif not self.carrier:
            parts.append("sem cabo / desconectado")
        return "%s — %s" % (self.name, " · ".join(parts))

    def key(self):
        return (self.name, self.index, self.ipv4, self.prefix, self.up, self.carrier)


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _ioctl_ip(sock, name, req):
    try:
        r = fcntl.ioctl(sock.fileno(), req, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(r[20:24])
    except OSError:
        return None


def interfaces(include_loopback=False):
    """Lista atual dos adaptadores (relê o kernel a cada chamada)."""
    out = []
    try:
        pairs = socket.if_nameindex()
    except OSError:
        pairs = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for idx, name in pairs:
            base = os.path.join(SYS, name)
            loop = _read(os.path.join(base, "type")) == "772" or name == "lo"
            if loop and not include_loopback:
                continue
            ip = _ioctl_ip(s, name, SIOCGIFADDR)
            prefix = None
            if ip:
                mask = _ioctl_ip(s, name, SIOCGIFNETMASK)
                if mask:
                    prefix = bin(struct.unpack("!I", socket.inet_aton(mask))[0]).count("1")
            oper = _read(os.path.join(base, "operstate"), "unknown")
            flags = int(_read(os.path.join(base, "flags"), "0x0"), 16)
            up = bool(flags & 0x1) and oper != "down"
            carrier = _read(os.path.join(base, "carrier"), "0") == "1" if up else False
            if oper == "unknown" and up and ip:      # alguns drivers/túneis não informam o enlace
                carrier = True
            speed = _read(os.path.join(base, "speed"))
            try:
                speed = int(speed) if speed is not None else None
            except ValueError:
                speed = None
            # VLAN (enp3s0.100), bond e bridge contam como rede real; só contêineres/VPN/túneis são virtuais
            virtual = name.startswith(VIRTUAL_PREFIXES)
            vlan = os.path.exists("/proc/net/vlan/%s" % name) or ("." in name and not loop)
            out.append(NetIf(name=name, index=idx, ipv4=ip, prefix=prefix,
                             mac=_read(os.path.join(base, "address"), ""), up=up, carrier=carrier,
                             wireless=os.path.isdir(os.path.join(base, "wireless")) or
                             os.path.isdir(os.path.join(base, "phy80211")),
                             loopback=loop, virtual=virtual, speed=speed, vlan=vlan,
                             bond=os.path.isdir(os.path.join(base, "bonding")),
                             bridge=os.path.isdir(os.path.join(base, "bridge"))))
    finally:
        s.close()
    out.sort(key=lambda i: (i.loopback, i.virtual, not i.usable, i.wireless, i.name))
    return out


def snapshot():
    """Chave que muda quando algum adaptador aparece, some, troca de IP ou de estado."""
    return tuple(i.key() for i in interfaces(include_loopback=True))


def find(name_or_ip, ifs=None):
    """Procura por nome (enp3s0) ou por IPv4 (compatibilidade com endereços antigos)."""
    if not name_or_ip:
        return None
    ifs = ifs if ifs is not None else interfaces(include_loopback=True)
    for i in ifs:
        if i.name == name_or_ip:
            return i
    for i in ifs:
        if i.ipv4 == name_or_ip:
            return i
    return None


def is_ipv4(text):
    try:
        socket.inet_aton(text)
        return text.count(".") == 3
    except (OSError, TypeError):
        return False


def route_source_ip(dest):
    """IP local que o kernel usaria para chegar em dest (None se não houver rota)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def route_iface(dest, ifs=None):
    ip = route_source_ip(dest)
    if not ip or ip == "0.0.0.0":
        return None
    ifs = ifs if ifs is not None else interfaces(include_loopback=True)
    for i in ifs:
        if i.ipv4 == ip:
            return i
    return None


def default_route_iface():
    txt = _read("/proc/net/route", "")
    for line in txt.splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000":
            return f[0]
    return None


def auto_join_set(group=None, ifs=None):
    """Interfaces para o join automático.

    Todos os adaptadores reais conectados com IPv4, um por sub-rede (cabo antes de Wi-Fi), para não
    receber o mesmo fluxo duas vezes. Depois que o tráfego chega, o receptor trava na interface por
    onde ele veio (IP_PKTINFO). Sem nenhum adaptador conectado, cai na rota do kernel.
    """
    ifs = ifs if ifs is not None else interfaces()
    cand = [i for i in ifs if i.usable]
    nets = {}
    for i in sorted(cand, key=lambda i: (i.wireless, i.name.startswith(("enx", "usb")), i.name)):
        nets.setdefault(i.network(), i)
    chosen = list(nets.values())
    if not chosen and group:
        r = route_iface(group, ifs)
        if r is not None and not r.loopback:
            chosen = [r]
    return chosen


@dataclass
class Resolution:
    mode: str                 # auto | fixed
    ifaces: list              # NetIf para o join
    note: str = ""
    missing: bool = False     # interface escolhida não existe/está desligada


def resolve(spec, group=None):
    """spec: '' / None / 'auto' = automático; nome do adaptador; ou IP (endereços antigos)."""
    ifs = interfaces(include_loopback=True)
    if not spec or spec in ("auto", "automatico", "automático", "0.0.0.0"):
        chosen = auto_join_set(group, [i for i in ifs if not i.loopback])
        if not chosen:
            return Resolution("auto", [], "nenhum adaptador conectado com IPv4", missing=True)
        return Resolution("auto", chosen, "automático: " + ", ".join(i.name for i in chosen))
    if spec in ("all", "todas"):
        chosen = [i for i in ifs if i.usable]
        return Resolution("fixed", chosen, "todas: " + ", ".join(i.name for i in chosen), missing=not chosen)
    i = find(spec, ifs)
    if i is None:
        if is_ipv4(spec):
            # endereço antigo salvo com o IP do adaptador: segue no automático
            chosen = auto_join_set(group, [x for x in ifs if not x.loopback])
            return Resolution("auto", chosen, "o IP %s não existe mais nesta máquina — usando o automático (%s)"
                              % (spec, ", ".join(x.name for x in chosen) or "nenhum adaptador"),
                              missing=not chosen)
        return Resolution("fixed", [], "o adaptador '%s' não existe nesta máquina (disponíveis: %s)"
                          % (spec, ", ".join(x.name for x in ifs if not x.loopback and not x.virtual) or "nenhum"), missing=True)
    if not i.up:
        return Resolution("fixed", [i], "o adaptador %s está desligado" % i.name, missing=True)
    note = "%s (%s)" % (i.name, i.ipv4 or "sem IPv4")
    if not i.carrier and not i.loopback:
        note += " — sem cabo / desconectado"
    return Resolution("fixed", [i], note)


def mreqn(group, ifindex):
    """struct ip_mreqn: grupo, endereço local (0.0.0.0), índice da interface."""
    return socket.inet_aton(group) + socket.inet_aton("0.0.0.0") + struct.pack("@i", ifindex)


def list_interfaces():
    """Compatibilidade: [(nome, ipv4)] das interfaces com IPv4."""
    return [(i.name, i.ipv4) for i in interfaces(include_loopback=True) if i.ipv4]


# ------------------------------------------------------------------ diagnóstico
def rp_filter(name):
    v = [_read("/proc/sys/net/ipv4/conf/%s/rp_filter" % n) for n in ("all", name)]
    try:
        return max(int(x) for x in v if x is not None)
    except ValueError:
        return None


def rx_bytes():
    out = {}
    for n in os.listdir(SYS) if os.path.isdir(SYS) else []:
        v = _read(os.path.join(SYS, n, "statistics", "rx_bytes"))
        if v is not None:
            out[n] = int(v)
    return out


def igmp_memberships():
    """{interface: [grupos]} a partir de /proc/net/igmp."""
    out = {}
    cur = None
    for line in (_read("/proc/net/igmp", "") or "").splitlines()[1:]:
        if line and not line[0].isspace():
            f = line.split()
            cur = f[1].rstrip(":") if len(f) > 1 else None
            out.setdefault(cur, [])
        elif cur and line.strip():
            g = line.split()[0]
            try:
                out[cur].append(socket.inet_ntoa(struct.pack("<I", int(g, 16))))
            except (ValueError, struct.error):
                pass
    return out


def _service_active(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def diagnose(group=None, port=None, iface=None, sample=1.0):
    """Lista de (nível, texto) — nível: ok | info | warn | erro. Não precisa de root."""
    res = []
    ifs = interfaces()
    real = [i for i in ifs if not i.virtual]
    if not real:
        res.append(("erro", "Nenhum adaptador de rede físico encontrado."))
    for i in real:
        lvl = "ok" if i.usable else ("warn" if i.up else "info")
        res.append((lvl, "Adaptador " + i.label()))
    if not [i for i in ifs if i.usable]:
        res.append(("erro", "Nenhum adaptador conectado com IPv4: confira o cabo e o endereço (DHCP ou fixo)."))
    dflt = default_route_iface()
    res.append(("info", "Rota padrão: %s" % (dflt or "nenhuma (rede isolada) — o PIDgeon faz o join direto "
                                                     "no adaptador, sem depender de rota")))
    mc = False
    if group:
        try:
            mc = 224 <= int(group.split(".")[0]) <= 239
        except ValueError:
            mc = False
    if group and mc:
        r = resolve(iface, group)
        if r.missing:
            res.append(("erro", "Interface para o join: %s" % r.note))
        else:
            res.append(("ok", "Join IGMP de %s será feito em: %s" % (group, ", ".join(i.name for i in r.ifaces))))
        rt = route_iface(group)
        if rt is not None and r.ifaces and rt.name not in [i.name for i in r.ifaces]:
            res.append(("info", "A rota do sistema para %s sai por %s, mas o PIDgeon usa a interface escolhida "
                                "(o join é feito pelo índice do adaptador)." % (group, rt.name)))
        mem = igmp_memberships()
        joined = [n for n, gs in mem.items() if group in gs]
        if joined:
            res.append(("ok", "O kernel está inscrito em %s em: %s" % (group, ", ".join(joined))))
        for i in (r.ifaces or []):
            rp = rp_filter(i.name)
            if rp == 1:
                res.append(("warn", "rp_filter estrito em %s: multicast de fonte em outra sub-rede (comum em "
                                    "encoders/IRDs) é descartado pelo kernel. Corrigir: sudo sysctl -w "
                                    "net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.%s.rp_filter=0" % (i.name, i.name)))
            elif rp == 2 and dflt is None:
                res.append(("warn", "rp_filter 'loose' sem rota padrão em %s: multicast de fonte em outra sub-rede "
                                    "é descartado. Corrigir: sudo sysctl -w net.ipv4.conf.all.rp_filter=0 "
                                    "net.ipv4.conf.%s.rp_filter=0" % (i.name, i.name)))
    if port:
        if port < 1024:
            res.append(("warn", "Porta %d < 1024: no Linux só o root pode abrir. Use uma porta ≥ 1024 "
                                "(ex.: 1234, 5000)." % port))
        for fw, cmd in (("ufw", "sudo ufw allow %d/udp" % port), ("firewalld", "sudo firewall-cmd --add-port=%d/udp"
                                                                          % port)):
            if _service_active(fw):
                res.append(("warn", "Firewall %s ativo: libere a porta com  %s%s" % (
                    fw, cmd, ("  (com FEC também %d e %d)" % (port + 2, port + 4)))))
    if sample:
        a = rx_bytes()
        time.sleep(sample)
        b = rx_bytes()
        for i in real:
            if i.name in a and i.name in b:
                mbps = (b[i.name] - a[i.name]) * 8 / sample / 1e6
                if mbps >= 0.05:
                    res.append(("info", "Tráfego chegando em %s: %.2f Mbps" % (i.name, mbps)))
    return res
