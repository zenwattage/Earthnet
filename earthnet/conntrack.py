"""Connection capture for earthnet.

Three backends, tried in order:

1. ``conntrack -L``       -- the kernel's live flow table (needs root or
   CAP_NET_ADMIN). Sees all tracked TCP/UDP/ICMP flows on this host/router.
2. ``/proc/net/nf_conntrack`` -- same data, read directly (also root-only).
3. ``ss -tunH``           -- active sockets on this host only, no root. Used as
   a graceful fallback so the app is demoable unprivileged.

Each ``poll()`` returns a list of ``Flow`` records describing the two endpoints.
The geo layer decides which side is the "remote" endpoint to trace.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Flow:
    proto: str
    src: str
    sport: int
    dst: str
    dport: int
    state: str = ""

    def key(self) -> tuple:
        a = (self.src, self.sport)
        b = (self.dst, self.dport)
        if a > b:
            a, b = b, a
        return (self.proto, a, b)


def _strip_zone(ip: str) -> str:
    return ip.split("%", 1)[0]


def _parse_addr_port(tok: str) -> tuple[str, int]:
    """Parse 'host:port' or '[host]:port'."""
    if tok.startswith("["):
        host, _, rest = tok[1:].partition("]")
        port = int(rest.lstrip(":"))
        return host, port
    # rsplit for IPv6 without brackets (rare in ss output) -- but ss brackets v6
    host, _, port = tok.rpartition(":")
    return host, int(port)


def _from_conntrack_line(line: str) -> Flow | None:
    parts = line.split()
    if not parts:
        return None
    proto = parts[0]
    if proto.startswith("ipv"):
        proto = parts[2] if len(parts) > 2 else parts[0]
    # find first src=/dst=/sport=/dport=
    src = dst = sport = dport = None
    state = ""
    for p in parts:
        if p.startswith("src=") and src is None:
            src = p[4:]
        elif p.startswith("dst=") and dst is None:
            dst = p[4:]
        elif p.startswith("sport=") and sport is None:
            try:
                sport = int(p[6:])
            except ValueError:
                pass
        elif p.startswith("dport=") and dport is None:
            try:
                dport = int(p[6:])
            except ValueError:
                pass
        elif p in ("ESTABLISHED", "TIME_WAIT", "CLOSE", "SYN_SENT",
                   "SYN_RECV", "FIN_WAIT", "LAST_ACK", "LISTEN", "UNREPLIED",
                   "ASSURED"):
            state = p
    if not src or not dst:
        return None
    return Flow(proto, _strip_zone(src), sport or 0, _strip_zone(dst), dport or 0, state)


def _conntrack_cmd() -> list[Flow]:
    if not shutil.which("conntrack"):
        return []
    try:
        out = subprocess.run(
            ["conntrack", "-L"], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    flows = []
    for line in out.stdout.splitlines():
        if line.startswith("con"):
            continue  # header "conntrack v..."
        f = _from_conntrack_line(line)
        if f:
            flows.append(f)
    return flows


def _conntrack_proc() -> list[Flow]:
    try:
        with open("/proc/net/nf_conntrack") as fh:
            data = fh.read()
    except PermissionError:
        return []
    except Exception:
        return []
    flows = []
    for line in data.splitlines():
        f = _from_conntrack_line(line)
        if f:
            flows.append(f)
    return flows


def _ss() -> list[Flow]:
    if not shutil.which("ss"):
        return []
    try:
        out = subprocess.run(
            ["ss", "-tunH"], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    flows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0]
        state = parts[1]
        # local addr is parts[-2], peer is parts[-1] in typical ss -tunH output
        local_tok = None
        peer_tok = None
        # find the two address tokens (contain ':' and a digit port)
        addr_toks = [p for p in parts if ":" in p and p[-1].isdigit()]
        if len(addr_toks) >= 2:
            local_tok = addr_toks[0]
            peer_tok = addr_toks[1]
        elif len(addr_toks) == 1:
            local_tok = addr_toks[0]
        if not local_tok:
            continue
        try:
            src, sport = _parse_addr_port(local_tok)
        except Exception:
            continue
        dst, dport = "", 0
        if peer_tok:
            try:
                dst, dport = _parse_addr_port(peer_tok)
            except Exception:
                pass
        if not dst:
            continue
        flows.append(Flow(proto, _strip_zone(src), sport, _strip_zone(dst), dport, state))
    return flows


_BACKENDS = [
    ("conntrack -L", _conntrack_cmd),
    ("/proc/net/nf_conntrack", _conntrack_proc),
    ("ss -tunH", _ss),
]


def available_backend() -> str | None:
    """Return the name of the first backend that yields data, or None."""
    for name, fn in _BACKENDS:
        if fn():
            return name
    return None


def poll() -> tuple[list[Flow], str]:
    """Return (deduped flows, backend_name_used)."""
    for name, fn in _BACKENDS:
        flows = fn()
        if flows:
            seen = {}
            for f in flows:
                seen[f.key()] = f
            return list(seen.values()), name
    return [], "none"


if __name__ == "__main__":
    flows, backend = poll()
    print(f"backend: {backend}  flows: {len(flows)}")
    for f in flows[:20]:
        print(f"  {f.proto:4} {f.src}:{f.sport} -> {f.dst}:{f.dport}  [{f.state}]")