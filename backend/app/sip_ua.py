"""Minimal asyncio SIP UA: REGISTER / INVITE / BYE + RTP PCMU for Telphin-style trunks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import re
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .sip_audio import (
    OPENAI_FRAME_SAMPLES,
    SIP_FRAME_SAMPLES,
    Codec,
    g711_encode,
    openai_to_sip,
    sip_to_openai,
)

logger = logging.getLogger(__name__)

OnRtpPcm24 = Callable[[bytes], Awaitable[None]]
OnCallState = Callable[[str, dict[str, Any]], Awaitable[None]]


def _md5(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode("utf-8")).hexdigest()


def _quote(value: str) -> str:
    return f'"{value}"'


_SIP_COMPACT = {
    "i": "Call-ID",
    "m": "Contact",
    "e": "Content-Encoding",
    "l": "Content-Length",
    "c": "Content-Type",
    "f": "From",
    "s": "Subject",
    "k": "Supported",
    "t": "To",
    "v": "Via",
}
_SIP_CANON = {
    "via": "Via",
    "from": "From",
    "to": "To",
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "contact": "Contact",
    "www-authenticate": "WWW-Authenticate",
    "authorization": "Authorization",
    "proxy-authenticate": "Proxy-Authenticate",
    "proxy-authorization": "Proxy-Authorization",
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "record-route": "Record-Route",
    "route": "Route",
    "allow": "Allow",
    "expires": "Expires",
    "user-agent": "User-Agent",
    "max-forwards": "Max-Forwards",
    "warning": "Warning",
}


def _sip_header_name(raw: str) -> str:
    key = raw.strip()
    compact = _SIP_COMPACT.get(key.lower())
    if compact:
        return compact
    return _SIP_CANON.get(key.lower(), key)


def _via_branch(via: str) -> str:
    match = re.search(r'branch\s*=\s*"?([^;\s"]+)"?', via, re.I)
    return match.group(1) if match else ""


def _parse_www_authenticate(header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header):
        result[match.group(1).lower()] = match.group(2) if match.group(2) is not None else match.group(3)
    return result


def _exc_text(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _sip_uri(user: str, host: str, port: int | None = None) -> str:
    if port and port not in (5060, 5061):
        return f"sip:{user}@{host}:{port}"
    return f"sip:{user}@{host}"


def _host_port(value: str, default_port: int = 5060) -> tuple[str, int]:
    if ":" in value and not value.startswith("["):
        host, port_s = value.rsplit(":", 1)
        try:
            return host, int(port_s)
        except ValueError:
            return value, default_port
    return value, default_port


def _is_ipv4(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        return False


def _is_private_ipv4(host: str) -> bool:
    if not _is_ipv4(host):
        return False
    parts = [int(p) for p in host.split(".")]
    if parts[0] == 10 or parts[0] == 127:
        return True
    if parts[0] == 192 and parts[1] == 168:
        return True
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return True
    return False


def _local_ipv4(toward: str | None = None) -> str:
    """Outbound interface IP (same idea as softphone / MtzVersion)."""
    targets: list[tuple[str, int]] = []
    if toward and _is_ipv4(toward):
        targets.append((toward, 80))
    targets.append(("8.8.8.8", 80))
    for host, port in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            ip = sock.getsockname()[0]
            if ip and ip != "0.0.0.0":
                return str(ip)
        except OSError:
            continue
        finally:
            sock.close()
    return "127.0.0.1"


@dataclass
class SipEndpointConfig:
    account_id: int
    login: str
    password: str
    domain: str
    sip_server: str
    auth_username: str | None = None
    transport: str = "udp"
    sip_proxy: str | None = None
    display_name: str = ""
    caller_id: str | None = None
    stun_server: str | None = None
    public_ip: str | None = None
    max_concurrent_calls: int = 1
    ring_delay_seconds: float = 4.0
    wait_first_rtp_seconds: float = 5.0
    local_sip_port: int = 5060
    rtp_port_min: int = 10000
    rtp_port_max: int = 10199


@dataclass
class ActiveCall:
    call_id: str
    direction: str
    remote_number: str
    local_tag: str
    remote_tag: str = ""
    remote_host: str = ""
    remote_port: int = 5060
    remote_rtp_host: str = ""
    remote_rtp_port: int = 0
    local_rtp_port: int = 0
    codec: Codec = "pcmu"
    state: str = "initiated"
    cseq: int = 1
    invite_branch: str = ""
    from_header: str = ""
    to_header: str = ""
    contact: str = ""
    rtp_transport: asyncio.DatagramTransport | None = None
    rtp_protocol: RtpProtocol | None = None
    media_task: asyncio.Task[None] | None = None
    on_rtp: OnRtpPcm24 | None = None
    playback_provider: Callable[[], bytes] | None = None
    invite_headers: dict[str, str] = field(default_factory=dict)
    invite_addr: tuple[str, int] | None = None
    cancelled: asyncio.Event | None = None
    answered_at: float | None = None
    last_rtp_at: float | None = None
    rtp_packets_rx: int = 0
    rtp_packets_tx: int = 0
    rtp_learned: asyncio.Event = field(default_factory=asyncio.Event)
    media_tx_enabled: bool = False
    rtp_marker_next: bool = True
    seq: int = field(default_factory=lambda: random.randint(1, 0xFFFF))
    timestamp: int = field(default_factory=lambda: random.randint(1, 0xFFFFFFFF))
    ssrc: int = field(default_factory=lambda: random.randint(1, 0x7FFFFFFF))


def _parse_rtp_packet(data: bytes) -> tuple[int, bytes] | None:
    """Return (payload_type, payload) — same parsing as MtzVersion."""
    if len(data) < 12:
        return None
    cc = data[0] & 0x0F
    hlen = 12 + cc * 4
    if len(data) < hlen:
        return None
    # Skip header extension if present (X bit)
    if data[0] & 0x10:
        if len(data) < hlen + 4:
            return None
        ext_words = int.from_bytes(data[hlen + 2 : hlen + 4], "big")
        hlen += 4 + ext_words * 4
        if len(data) < hlen:
            return None
    pt = data[1] & 0x7F
    return pt, data[hlen:]


def _rtp_header(pt: int, seq: int, ts: int, ssrc: int, marker: int = 0) -> bytes:
    b0 = 0x80
    b1 = ((1 if marker else 0) << 7) | (pt & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)


def _ringback_pcm8k_frame(sample_index: int) -> bytes:
    """Russian-style ringback ~425 Hz, 1s tone / 2.5s silence — same idea as MtzVersion."""
    n = SIP_FRAME_SAMPLES
    tone_hz = 425.0
    tone_ms = 1000.0
    cycle_ms = 3500.0
    out = bytearray(n * 2)
    view = memoryview(out).cast("h")
    for i in range(n):
        idx = sample_index + i
        t_ms = (idx / 8000.0) * 1000.0
        in_tone = (t_ms % cycle_ms) < tone_ms
        if in_tone:
            view[i] = int(0.22 * 32767.0 * math.sin(2.0 * math.pi * tone_hz * idx / 8000.0))
        else:
            view[i] = 0
    return bytes(out)


class RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, call: ActiveCall) -> None:
        self.call = call
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = _parse_rtp_packet(data)
        if parsed is None:
            return
        # Symmetric RTP: Telphin/NAT often sends media from a different IP:port than SDP.
        host, port = addr[0], int(addr[1])
        if _is_ipv4(host) and (
            self.call.remote_rtp_host != host or self.call.remote_rtp_port != port
        ):
            logger.info(
                "RTP peer learned %s:%s -> %s:%s (call %s)",
                self.call.remote_rtp_host,
                self.call.remote_rtp_port,
                host,
                port,
                self.call.call_id[:24],
            )
            self.call.remote_rtp_host = host
            self.call.remote_rtp_port = port
        self.call.last_rtp_at = time.time()
        self.call.rtp_packets_rx += 1
        if not self.call.rtp_learned.is_set():
            self.call.rtp_learned.set()
            logger.info(
                "SIP first RTP from %s:%s (call %s)",
                host,
                port,
                self.call.call_id[:24],
            )
        pt, payload = parsed
        if pt == 8:
            self.call.codec = "pcma"
        elif pt == 0:
            self.call.codec = "pcmu"
        if not payload or self.call.on_rtp is None:
            return
        try:
            pcm24 = sip_to_openai(payload, self.call.codec)
        except Exception:
            return
        asyncio.create_task(self._safe_on_rtp(pcm24))

    async def _safe_on_rtp(self, pcm24: bytes) -> None:
        try:
            if self.call.on_rtp:
                await self.call.on_rtp(pcm24)
        except Exception:
            logger.exception("RTP on_rtp failed")

    def send_payload(self, payload: bytes, *, marker: bool | None = None) -> None:
        if not self.transport or not self.call.remote_rtp_host or not self.call.remote_rtp_port:
            return
        pt = 0 if self.call.codec == "pcmu" else 8
        use_marker = self.call.rtp_marker_next if marker is None else marker
        pkt = _rtp_header(pt, self.call.seq, self.call.timestamp, self.call.ssrc, 1 if use_marker else 0)
        self.call.rtp_marker_next = False
        self.call.seq = (self.call.seq + 1) & 0xFFFF
        self.call.timestamp = (self.call.timestamp + SIP_FRAME_SAMPLES) & 0xFFFFFFFF
        dest = self.call.remote_rtp_host
        if not _is_ipv4(dest):
            return
        try:
            self.transport.sendto(pkt + payload, (dest, self.call.remote_rtp_port))
            self.call.rtp_packets_tx += 1
        except OSError as exc:
            logger.warning("RTP sendto failed: %s", exc)


class SipProtocol(asyncio.DatagramProtocol):
    def __init__(self, ua: "SipUserAgent") -> None:
        self.ua = ua
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text = data.decode("utf-8", errors="ignore")
        if not text.strip():
            return
        asyncio.create_task(self.ua.handle_message(text, addr))


class SipUserAgent:
    def __init__(
        self,
        config: SipEndpointConfig,
        *,
        on_incoming: Callable[[ActiveCall], Awaitable[None]] | None = None,
        on_reg_state: Callable[[bool, str], Awaitable[None]] | None = None,
        on_call_state: OnCallState | None = None,
    ) -> None:
        self.config = config
        self.on_incoming = on_incoming
        self.on_reg_state = on_reg_state
        self.on_call_state = on_call_state
        self.registered = False
        self.registration_status = "idle"
        self.local_ip = config.public_ip or "127.0.0.1"
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SipProtocol | None = None
        self._server_host, self._server_port = _host_port(config.sip_server, 5068)
        self._proxy_host, self._proxy_port = (
            _host_port(config.sip_proxy, 5060) if config.sip_proxy else (self._server_host, self._server_port)
        )
        # asyncio UDP sendto() needs a resolved IPv4 address, not a hostname
        self._proxy_ip = self._proxy_host
        self._resolved: dict[str, str] = {}
        self._cseq = 1
        self._call_id_reg = f"{random.randint(10**10, 10**12)}@{self.local_ip}"
        self._from_tag = f"{random.randint(10**6, 10**9)}"
        self._reg_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[tuple[int, dict[str, str], str]]] = {}
        self.calls: dict[str, ActiveCall] = {}
        self._rtp_ports = set(range(config.rtp_port_min, config.rtp_port_max + 1, 2))
        self._closed = False

    @property
    def auth_user(self) -> str:
        return self.config.auth_username or self.config.login

    async def _resolve_ipv4(self, host: str) -> str:
        if _is_ipv4(host):
            return host
        cached = self._resolved.get(host)
        if cached:
            return cached
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host,
                None,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
        except OSError as exc:
            raise RuntimeError(f"DNS lookup failed for {host}: {exc}") from exc
        if not infos:
            raise RuntimeError(f"DNS lookup failed for {host}: no IPv4 address")
        ip = str(infos[0][4][0])
        self._resolved[host] = ip
        logger.info("SIP DNS %s -> %s", host, ip)
        return ip

    def _udp_addr(self, addr: tuple[str, int] | None = None) -> tuple[str, int]:
        host, port = addr if addr is not None else (self._proxy_ip, self._proxy_port)
        if _is_ipv4(host):
            return host, port
        resolved = self._resolved.get(host)
        if resolved:
            return resolved, port
        if host in {self._proxy_host, self._server_host}:
            return self._proxy_ip, port
        raise RuntimeError(
            f"SIP UDP target {host!r} is not a resolved IPv4 address"
        )

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._proxy_ip = await self._resolve_ipv4(self._proxy_host)
        # Public IP only if explicitly set; otherwise discover like a softphone / MtzVersion.
        if self.config.public_ip:
            self.local_ip = self.config.public_ip.strip()
        else:
            self.local_ip = await loop.run_in_executor(None, _local_ipv4, self._proxy_ip)
        try:
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: SipProtocol(self),
                local_addr=("0.0.0.0", self.config.local_sip_port),
            )
        except OSError:
            # port busy — ephemeral
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: SipProtocol(self),
                local_addr=("0.0.0.0", 0),
            )
            sockname = self._transport.get_extra_info("sockname")
            self.config.local_sip_port = int(sockname[1])
        logger.info(
            "SIP UA started login=%s contact=%s:%s -> %s:%s (public_ip_override=%s)",
            self.config.login,
            self.local_ip,
            self.config.local_sip_port,
            self._proxy_ip,
            self._proxy_port,
            bool(self.config.public_ip),
        )
        if _is_private_ipv4(self.local_ip) and not self.config.public_ip:
            logger.warning(
                "SIP advertises private IP %s (typical in Docker). "
                "If remote hears silence, set account Public IP / ICE_SIP_PUBLIC_IP "
                "to the host LAN or public address, and publish UDP RTP ports.",
                self.local_ip,
            )

    async def close(self) -> None:
        self._closed = True
        if self._reg_task:
            self._reg_task.cancel()
            try:
                await self._reg_task
            except asyncio.CancelledError:
                pass
        for call in list(self.calls.values()):
            await self.hangup(call.call_id, cause="shutdown")
        if self.registered:
            try:
                await self.unregister()
            except Exception:
                pass
        if self._transport:
            self._transport.close()
            self._transport = None

    def _send(self, message: str, addr: tuple[str, int] | None = None) -> None:
        if not self._transport:
            raise RuntimeError("SIP transport not started")
        dest = self._udp_addr(addr)
        try:
            self._transport.sendto(message.encode("utf-8"), dest)
        except OSError as exc:
            raise RuntimeError(f"SIP UDP send to {dest[0]}:{dest[1]} failed: {_exc_text(exc)}") from exc

    def _allocate_rtp_port(self) -> int:
        if not self._rtp_ports:
            raise RuntimeError("RTP port pool exhausted")
        port = min(self._rtp_ports)
        self._rtp_ports.discard(port)
        self._rtp_ports.discard(port + 1)
        return port

    def _release_rtp_port(self, port: int) -> None:
        if self.config.rtp_port_min <= port <= self.config.rtp_port_max:
            self._rtp_ports.add(port)
            if port + 1 <= self.config.rtp_port_max:
                self._rtp_ports.add(port + 1)

    def _contact(self) -> str:
        transport = (self.config.transport or "udp").lower()
        return f"<sip:{self.config.login}@{self.local_ip}:{self.config.local_sip_port};transport={transport}>"

    def _from(self) -> str:
        display = self.config.display_name or self.config.login
        uri = _sip_uri(self.config.caller_id or self.config.login, self.config.domain)
        return f"{_quote(display)} <{uri}>;tag={self._from_tag}"

    def _auth_header(self, method: str, uri: str, challenge: dict[str, str]) -> str:
        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce", "")
        qop = challenge.get("qop", "")
        opaque = challenge.get("opaque")
        algorithm = challenge.get("algorithm") or "MD5"
        username = self.auth_user
        ha1 = _md5(username, realm, self.config.password)
        ha2 = _md5(method, uri)
        if qop:
            nc = "00000001"
            cnonce = f"{random.randint(10**8, 10**10)}"
            response = _md5(ha1, nonce, nc, cnonce, qop.split(",")[0].strip(), ha2)
            parts = [
                f'Digest username={_quote(username)}',
                f'realm={_quote(realm)}',
                f'nonce={_quote(nonce)}',
                f'uri={_quote(uri)}',
                f'response={_quote(response)}',
                f'algorithm={algorithm}',
                f'qop={qop.split(",")[0].strip()}',
                f'nc={nc}',
                f'cnonce={_quote(cnonce)}',
            ]
        else:
            response = _md5(ha1, nonce, ha2)
            parts = [
                f'Digest username={_quote(username)}',
                f'realm={_quote(realm)}',
                f'nonce={_quote(nonce)}',
                f'uri={_quote(uri)}',
                f'response={_quote(response)}',
                f'algorithm={algorithm}',
            ]
        if opaque:
            parts.append(f'opaque={_quote(opaque)}')
        return ", ".join(parts)

    async def _request(
        self,
        method: str,
        request_uri: str,
        headers: dict[str, str],
        body: str = "",
        addr: tuple[str, int] | None = None,
        auth_retry: bool = True,
    ) -> tuple[int, dict[str, str], str]:
        branch = f"z9hG4bK{random.randint(10**8, 10**12)}"
        headers = dict(headers)
        headers.setdefault("Via", f"SIP/2.0/UDP {self.local_ip}:{self.config.local_sip_port};rport;branch={branch}")
        headers.setdefault("Max-Forwards", "70")
        headers.setdefault("User-Agent", "Ice.agent-SIP/1.0")
        headers.setdefault("Content-Length", str(len(body.encode("utf-8"))))
        lines = [f"{method} {request_uri} SIP/2.0"]
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        message = "\r\n".join(lines) + "\r\n\r\n" + body
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[int, dict[str, str], str]] = loop.create_future()
        self._pending[branch] = fut
        dest = self._udp_addr(addr)
        logger.info("SIP TX %s %s -> %s:%s branch=%s", method, request_uri, dest[0], dest[1], branch)
        self._send(message, addr)
        interval = 0.5
        t2 = 4.0
        started = loop.time()
        timeout_total = 32.0
        try:
            while True:
                remaining = timeout_total - (loop.time() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"SIP {method} timed out waiting for {dest[0]}:{dest[1]} "
                        f"(no matching response in {int(timeout_total)}s)"
                    )
                try:
                    status, resp_headers, resp_body = await asyncio.wait_for(
                        asyncio.shield(fut),
                        timeout=min(interval, remaining),
                    )
                except TimeoutError:
                    logger.info("SIP %s retransmit -> %s:%s", method, dest[0], dest[1])
                    self._send(message, addr)
                    interval = min(interval * 2, t2)
                    continue
                if status < 200:
                    fut = loop.create_future()
                    self._pending[branch] = fut
                    continue
                break
        except TimeoutError:
            raise
        except Exception as exc:
            raise RuntimeError(f"SIP {method} to {dest[0]}:{dest[1]} failed: {_exc_text(exc)}") from exc
        finally:
            self._pending.pop(branch, None)
        if auth_retry and status in {401, 407}:
            auth_key = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
            auth_hdr = "Authorization" if status == 401 else "Proxy-Authorization"
            challenge_raw = resp_headers.get(auth_key, "")
            challenge = _parse_www_authenticate(challenge_raw)
            if not challenge.get("nonce"):
                raise RuntimeError(
                    f"SIP {method} got {status} without digest nonce from {dest[0]}:{dest[1]} "
                    f"(header={challenge_raw[:180]!r})"
                )
            headers[auth_hdr] = self._auth_header(method, request_uri, challenge)
            self._cseq += 1
            headers["CSeq"] = f"{self._cseq} {method}"
            headers.pop("Via", None)
            return await self._request(method, request_uri, headers, body, addr, auth_retry=False)
        return status, resp_headers, resp_body

    async def register(self, expires: int = 600) -> None:
        self.registration_status = "registering"
        request_uri = f"sip:{self.config.domain}"
        self._cseq += 1
        headers = {
            "From": self._from(),
            "To": f"<{_sip_uri(self.config.login, self.config.domain)}>",
            "Call-ID": self._call_id_reg,
            "CSeq": f"{self._cseq} REGISTER",
            "Contact": self._contact(),
            "Expires": str(expires),
            "Allow": "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO",
        }
        try:
            status, resp_headers, resp_body = await self._request("REGISTER", request_uri, headers)
        except Exception as exc:
            self.registered = False
            detail = str(exc).strip() or _exc_text(exc)
            self.registration_status = f"error:{detail}"
            if self.on_reg_state:
                await self.on_reg_state(False, self.registration_status)
            raise RuntimeError(detail) from exc
        if status in {200, 202}:
            self.registered = True
            self.registration_status = "registered"
            if self.on_reg_state:
                await self.on_reg_state(True, "registered")
            if self._reg_task is None or self._reg_task.done():
                self._reg_task = asyncio.create_task(self._reregister_loop(expires), name=f"sip-reg-{self.config.account_id}")
            return
        self.registered = False
        reason = (resp_headers.get("Reason-Phrase") or "").strip()
        warning = (resp_headers.get("Warning") or "").strip()
        extra = warning or (resp_body.strip().replace("\n", " ")[:180] if resp_body.strip() else "")
        detail = f"SIP REGISTER rejected ({status}{(' ' + reason) if reason else ''}) by {self._proxy_ip}:{self._proxy_port}"
        if extra:
            detail = f"{detail}: {extra}"
        self.registration_status = f"failed:{status}"
        if self.on_reg_state:
            await self.on_reg_state(False, detail)
        raise RuntimeError(detail)

    async def unregister(self) -> None:
        request_uri = f"sip:{self.config.domain}"
        self._cseq += 1
        headers = {
            "From": self._from(),
            "To": f"<{_sip_uri(self.config.login, self.config.domain)}>",
            "Call-ID": self._call_id_reg,
            "CSeq": f"{self._cseq} REGISTER",
            "Contact": self._contact(),
            "Expires": "0",
        }
        await self._request("REGISTER", request_uri, headers)
        self.registered = False
        self.registration_status = "unregistered"

    async def _reregister_loop(self, expires: int) -> None:
        while not self._closed:
            await asyncio.sleep(max(30, expires - 30))
            try:
                await self.register(expires)
            except Exception as exc:
                logger.warning("SIP re-REGISTER failed for %s: %s", self.config.login, exc)
                self.registered = False
                self.registration_status = f"error:{exc}"

    def _parse_message(self, text: str) -> tuple[str, dict[str, str], str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        head, _, body = text.partition("\n\n")
        raw_lines = head.split("\n")
        lines: list[str] = []
        for line in raw_lines:
            if lines and (line.startswith(" ") or line.startswith("\t")):
                lines[-1] += " " + line.strip()
            elif line:
                lines.append(line)
        start = lines[0] if lines else ""
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name = _sip_header_name(key)
            if name not in headers:
                headers[name] = value.strip()
        return start, headers, body

    def _complete_pending(self, branch: str, result: tuple[int, dict[str, str], str]) -> None:
        fut = self._pending.get(branch)
        if fut is None or fut.done():
            pending = [item for item in self._pending.values() if not item.done()]
            if len(pending) == 1:
                fut = pending[0]
            else:
                if branch:
                    logger.debug("SIP unmatched response branch=%s pending=%s", branch, list(self._pending))
                return
        if not fut.done():
            fut.set_result(result)

    async def handle_message(self, text: str, addr: tuple[str, int]) -> None:
        start, headers, body = self._parse_message(text)
        via = headers.get("Via", "")
        branch = _via_branch(via)
        logger.info("SIP RX %s from %s:%s branch=%s", start[:80], addr[0], addr[1], branch or "-")

        if start.upper().startswith("SIP/2.0"):
            parts = start.split(None, 2)
            try:
                status = int(parts[1])
            except (IndexError, ValueError):
                logger.warning("SIP bad status line from %s: %r", addr, start)
                return
            if len(parts) > 2:
                headers["Reason-Phrase"] = parts[2]
            self._complete_pending(branch, (status, headers, body))
            call_id = headers.get("Call-ID", "")
            call = self.calls.get(call_id)
            if call and call.direction == "outbound":
                await self._handle_outbound_response(call, status, headers, body, addr)
            return

        method = start.split()[0].upper() if start else ""
        if method == "INVITE":
            await self._handle_invite(start, headers, body, addr)
        elif method == "ACK":
            call_id = headers.get("Call-ID", "")
            call = self.calls.get(call_id)
            if call is not None and call.state == "ringing":
                return
            if call:
                call.state = "answered"
                if self.on_call_state:
                    await self.on_call_state(call.call_id, {"status": "answered"})
        elif method == "BYE":
            await self._handle_bye(headers, addr)
        elif method == "CANCEL":
            await self._handle_cancel(headers, addr)
        elif method == "OPTIONS":
            self._reply(200, "OK", headers, addr)
        else:
            self._reply(405, "Method Not Allowed", headers, addr)

    def _reply(
        self,
        code: int,
        reason: str,
        req_headers: dict[str, str],
        addr: tuple[str, int],
        extra: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        headers = {
            "Via": req_headers.get("Via", ""),
            "From": req_headers.get("From", ""),
            "To": req_headers.get("To", ""),
            "Call-ID": req_headers.get("Call-ID", ""),
            "CSeq": req_headers.get("CSeq", ""),
            "Contact": self._contact(),
            "Content-Length": str(len(body.encode("utf-8"))),
            "User-Agent": "Ice.agent-SIP/1.0",
        }
        if extra:
            headers.update(extra)
        if ";tag=" not in headers["To"]:
            headers["To"] = f"{headers['To']};tag={random.randint(10**6, 10**9)}"
        lines = [f"SIP/2.0 {code} {reason}"] + [f"{k}: {v}" for k, v in headers.items()]
        self._send("\r\n".join(lines) + "\r\n\r\n" + body, addr)

    def _parse_sdp_media(self, body: str) -> tuple[str, int, Codec]:
        host = ""
        port = 0
        codec: Codec = "pcmu"
        for line in body.splitlines():
            if line.startswith("c=IN IP4 "):
                host = line.split()[-1].strip()
            elif line.startswith("m=audio "):
                parts = line.split()
                port = int(parts[1])
            elif line.startswith("a=rtpmap:"):
                if "PCMA" in line.upper() or "G711A" in line.upper():
                    codec = "pcma"
                elif "PCMU" in line.upper() or "G711U" in line.upper():
                    codec = "pcmu"
        return host, port, codec

    def _local_sdp(self, rtp_port: int, codec: Codec = "pcmu") -> str:
        pt = 0 if codec == "pcmu" else 8
        name = "PCMU" if codec == "pcmu" else "PCMA"
        return (
            "v=0\r\n"
            f"o=iceagent {int(time.time())} {int(time.time())} IN IP4 {self.local_ip}\r\n"
            "s=Ice.agent\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP {pt}\r\n"
            f"a=rtpmap:{pt} {name}/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )

    async def _setup_rtp(self, call: ActiveCall, *, start_loop: bool = True) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: RtpProtocol(call),
            local_addr=("0.0.0.0", call.local_rtp_port),
        )
        call.rtp_transport = transport
        call.rtp_protocol = protocol
        call.rtp_marker_next = True
        if start_loop:
            # Send RTP immediately (silence) so NAT hole opens.
            call.media_tx_enabled = True
            call.media_task = asyncio.create_task(self._media_loop(call), name=f"rtp-tx-{call.call_id}")

    async def _media_loop(self, call: ActiveCall) -> None:
        try:
            while call.state in {"ringing", "answered", "early"} and call.rtp_protocol:
                now = time.time()
                if call.state == "answered" and call.answered_at is not None:
                    if call.last_rtp_at is None and (now - call.answered_at) > 25:
                        logger.warning(
                            "SIP call %s: no RTP for 25s after answer — hanging up",
                            call.call_id[:24],
                        )
                        asyncio.create_task(self.hangup(call.call_id, cause="rtp_timeout"))
                        return
                    if call.last_rtp_at is not None and (now - call.last_rtp_at) > 45:
                        logger.warning(
                            "SIP call %s: RTP stalled for 45s — hanging up",
                            call.call_id[:24],
                        )
                        asyncio.create_task(self.hangup(call.call_id, cause="rtp_timeout"))
                        return
                if not call.media_tx_enabled:
                    await asyncio.sleep(0.02)
                    continue
                pcm24 = b""
                if call.playback_provider:
                    try:
                        pcm24 = call.playback_provider() or b""
                    except Exception:
                        pcm24 = b""
                had_audio = len(pcm24) > 0
                if len(pcm24) < OPENAI_FRAME_SAMPLES * 2:
                    if not had_audio:
                        call.rtp_marker_next = True
                    pcm24 = pcm24 + b"\x00" * (OPENAI_FRAME_SAMPLES * 2 - len(pcm24))
                payload = openai_to_sip(pcm24[: OPENAI_FRAME_SAMPLES * 2], call.codec)
                call.rtp_protocol.send_payload(payload)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media loop failed for %s", call.call_id)

    async def wait_first_rtp(self, call: ActiveCall, timeout: float | None = None) -> bool:
        """Wait until remote RTP arrives (symmetric NAT) or timeout.

        TX is already enabled (silence) for NAT hole-punching; this only
        synchronizes the greeting like MtzVersion's wait_first_rtp_seconds.
        """
        wait_s = self.config.wait_first_rtp_seconds if timeout is None else timeout
        wait_s = max(0.0, float(wait_s))
        call.media_tx_enabled = True
        learned = call.rtp_learned.is_set()
        if not learned and wait_s > 0:
            try:
                await asyncio.wait_for(call.rtp_learned.wait(), timeout=wait_s)
                learned = True
            except TimeoutError:
                learned = call.rtp_learned.is_set()
                logger.warning(
                    "SIP wait_first_rtp timed out (%.1fs) — greeting uses SDP dest %s:%s call=%s",
                    wait_s,
                    call.remote_rtp_host,
                    call.remote_rtp_port,
                    call.call_id[:24],
                )
        logger.info(
            "SIP ready for speech learned=%s peer=%s:%s tx=%s rx=%s call=%s",
            learned,
            call.remote_rtp_host,
            call.remote_rtp_port,
            call.rtp_packets_tx,
            call.rtp_packets_rx,
            call.call_id[:24],
        )
        return learned

    def _active_calls(self) -> list[ActiveCall]:
        return [c for c in self.calls.values() if c.state in {"ringing", "answered", "early", "dialing"}]

    async def _reclaim_stale_calls(self) -> None:
        now = time.time()
        for call in list(self.calls.values()):
            stale = False
            if call.state == "answered":
                if call.answered_at and call.last_rtp_at is None and (now - call.answered_at) > 20:
                    stale = True
                elif call.last_rtp_at and (now - call.last_rtp_at) > 30:
                    stale = True
                elif call.answered_at and (now - call.answered_at) > 3600:
                    stale = True
            elif call.state == "ringing" and call.answered_at is None:
                # answered_at unused while ringing — use invite time via cancelled wait; track via invite
                pass
            if stale:
                logger.warning("Reclaiming stale SIP call %s state=%s", call.call_id[:24], call.state)
                await self.hangup(call.call_id, cause="stale_reclaim", send_bye=True)

    async def _handle_invite(
        self,
        start: str,
        headers: dict[str, str],
        body: str,
        addr: tuple[str, int],
    ) -> None:
        call_id = headers.get("Call-ID", f"in-{random.randint(10**8, 10**12)}")
        existing = self.calls.get(call_id)
        if existing is not None:
            self._retransmit_invite_response(existing, headers, addr)
            return
        await self._reclaim_stale_calls()
        if len(self._active_calls()) >= self.config.max_concurrent_calls:
            # Last resort: drop oldest answered call so inbound is not permanently stuck.
            answered = [c for c in self._active_calls() if c.state == "answered"]
            if answered:
                oldest = min(answered, key=lambda c: c.answered_at or 0)
                logger.warning(
                    "Max calls reached — dropping %s to accept inbound",
                    oldest.call_id[:24],
                )
                await self.hangup(oldest.call_id, cause="replaced_by_inbound")
            if len(self._active_calls()) >= self.config.max_concurrent_calls:
                self._reply(486, "Busy Here", headers, addr)
                return
        remote = headers.get("From", "")
        number_match = re.search(r"sip:([^@>;]+)", remote)
        remote_number = number_match.group(1) if number_match else "unknown"
        rtp_port = self._allocate_rtp_port()
        remote_rtp_host, remote_rtp_port, codec = self._parse_sdp_media(body)
        if remote_rtp_host and not _is_ipv4(remote_rtp_host):
            remote_rtp_host = await self._resolve_ipv4(remote_rtp_host)
        # If SDP has no usable media IP, fall back to the SIP source (common behind PBX).
        if not remote_rtp_host or remote_rtp_host in {"0.0.0.0", "127.0.0.1"}:
            remote_rtp_host = addr[0]
            logger.info("SDP media IP missing — using SIP source %s for RTP", remote_rtp_host)
        if not remote_rtp_port:
            logger.warning("SDP has no audio port for inbound call")
        local_tag = f"{random.randint(10**6, 10**9)}"
        to_header = headers.get("To", "")
        if ";tag=" not in to_header:
            to_header = f"{to_header};tag={local_tag}"
        call = ActiveCall(
            call_id=call_id,
            direction="inbound",
            remote_number=remote_number,
            local_tag=local_tag,
            remote_tag=re.search(r"tag=([^;]+)", headers.get("From", "")).group(1)
            if re.search(r"tag=([^;]+)", headers.get("From", ""))
            else "",
            remote_host=addr[0],
            remote_port=addr[1],
            remote_rtp_host=remote_rtp_host,
            remote_rtp_port=remote_rtp_port,
            local_rtp_port=rtp_port,
            codec=codec,
            state="ringing",
            from_header=headers.get("From", ""),
            to_header=to_header,
            contact=headers.get("Contact", ""),
            invite_headers=dict(headers),
            invite_addr=addr,
            cancelled=asyncio.Event(),
        )
        self.calls[call_id] = call
        self._reply(100, "Trying", headers, addr, extra={"To": to_header})
        self._reply(180, "Ringing", headers, addr, extra={"To": to_header})
        if self.on_call_state:
            await self.on_call_state(
                call.call_id,
                {"status": "ringing", "direction": "inbound", "remote_number": remote_number},
            )
        asyncio.create_task(self._answer_after_ring(call), name=f"sip-ring-{call_id[:16]}")

    def _retransmit_invite_response(
        self,
        call: ActiveCall,
        headers: dict[str, str],
        addr: tuple[str, int],
    ) -> None:
        if call.state == "ringing":
            self._reply(100, "Trying", headers, addr, extra={"To": call.to_header})
            # Prefer 183+SDP retransmit once early media is up (Mtz-style).
            if call.rtp_protocol is not None:
                sdp = self._local_sdp(call.local_rtp_port, call.codec)
                self._reply(
                    183,
                    "Session Progress",
                    headers,
                    addr,
                    extra={
                        "To": call.to_header,
                        "Content-Type": "application/sdp",
                        "Content-Length": str(len(sdp.encode("utf-8"))),
                    },
                    body=sdp,
                )
            else:
                self._reply(180, "Ringing", headers, addr, extra={"To": call.to_header})
            return
        if call.state == "answered":
            sdp = self._local_sdp(call.local_rtp_port, call.codec)
            self._reply(
                200,
                "OK",
                headers,
                addr,
                extra={
                    "To": call.to_header,
                    "Content-Type": "application/sdp",
                    "Content-Length": str(len(sdp.encode("utf-8"))),
                },
                body=sdp,
            )

    async def _stream_early_ringback(self, call: ActiveCall, duration: float) -> None:
        """183 early-media ringback — punches Docker/NAT UDP mapping like MtzVersion."""
        if duration <= 0 or call.rtp_protocol is None:
            return
        pt = 0 if call.codec == "pcmu" else 8
        sample_idx = 0
        deadline = time.time() + duration
        logger.info(
            "SIP early media ringback %.1fs -> %s:%s call=%s",
            duration,
            call.remote_rtp_host,
            call.remote_rtp_port,
            call.call_id[:24],
        )
        while time.time() < deadline:
            if call.cancelled is not None and call.cancelled.is_set():
                return
            if call.call_id not in self.calls or call.state not in {"ringing", "early"}:
                return
            pcm8 = _ringback_pcm8k_frame(sample_idx)
            sample_idx += SIP_FRAME_SAMPLES
            payload = g711_encode(pcm8, call.codec)
            # Bypass media_tx gate — early media must always TX.
            if call.rtp_protocol and call.remote_rtp_host and call.remote_rtp_port:
                marker = 1 if sample_idx == SIP_FRAME_SAMPLES else 0
                pkt = _rtp_header(pt, call.seq, call.timestamp, call.ssrc, marker)
                call.seq = (call.seq + 1) & 0xFFFF
                call.timestamp = (call.timestamp + SIP_FRAME_SAMPLES) & 0xFFFFFFFF
                try:
                    assert call.rtp_transport is not None
                    call.rtp_transport.sendto(pkt + payload, (call.remote_rtp_host, call.remote_rtp_port))
                    call.rtp_packets_tx += 1
                except OSError as exc:
                    logger.warning("early media RTP send failed: %s", exc)
                    return
            await asyncio.sleep(0.02)

    async def _answer_after_ring(self, call: ActiveCall) -> None:
        try:
            await self._answer_after_ring_inner(call)
        except Exception:
            logger.exception("inbound answer failed for %s", call.call_id)

    async def _answer_after_ring_inner(self, call: ActiveCall) -> None:
        delay = max(0.0, float(self.config.ring_delay_seconds if self.config.ring_delay_seconds is not None else 0))
        headers = call.invite_headers
        addr = call.invite_addr
        if not headers or addr is None:
            return

        # MtzVersion Docker trick: 183 + SDP + RTP ringback BEFORE 200 OK.
        # Outbound RTP creates NAT mapping so media works without publishing every port
        # and without a perfect Public IP in SDP.
        if delay > 0:
            sdp = self._local_sdp(call.local_rtp_port, call.codec)
            self._reply(
                183,
                "Session Progress",
                headers,
                addr,
                extra={
                    "To": call.to_header,
                    "Content-Type": "application/sdp",
                    "Content-Length": str(len(sdp.encode("utf-8"))),
                },
                body=sdp,
            )
            call.state = "early"
            await self._setup_rtp(call, start_loop=False)
            call.media_tx_enabled = False
            ring_task = asyncio.create_task(
                self._stream_early_ringback(call, delay),
                name=f"sip-early-{call.call_id[:16]}",
            )
            if call.cancelled is not None:
                try:
                    await asyncio.wait_for(call.cancelled.wait(), timeout=delay)
                    ring_task.cancel()
                    try:
                        await ring_task
                    except asyncio.CancelledError:
                        pass
                    return
                except TimeoutError:
                    pass
            else:
                await ring_task
            if call.call_id not in self.calls:
                return
            if call.cancelled is not None and call.cancelled.is_set():
                return

        if call.call_id not in self.calls or call.state not in {"ringing", "early"}:
            return
        if call.cancelled is not None and call.cancelled.is_set():
            return

        sdp = self._local_sdp(call.local_rtp_port, call.codec)
        self._reply(
            200,
            "OK",
            headers,
            addr,
            extra={
                "To": call.to_header,
                "Content-Type": "application/sdp",
                "Content-Length": str(len(sdp.encode("utf-8"))),
            },
            body=sdp,
        )
        if call.rtp_protocol is None:
            await self._setup_rtp(call)
        else:
            # Resume normal media loop after early media.
            call.media_tx_enabled = True
            call.rtp_marker_next = True
            if call.media_task is None or call.media_task.done():
                call.media_task = asyncio.create_task(
                    self._media_loop(call),
                    name=f"rtp-tx-{call.call_id}",
                )
        call.state = "answered"
        call.answered_at = time.time()
        if self.on_incoming:
            asyncio.create_task(self._safe_on_incoming(call), name=f"sip-in-{call.call_id[:16]}")
        if self.on_call_state:
            await self.on_call_state(call.call_id, {"status": "answered", "direction": "inbound"})

    async def _safe_on_incoming(self, call: ActiveCall) -> None:
        if self.on_incoming is None:
            return
        try:
            await self.on_incoming(call)
        except Exception:
            logger.exception("on_incoming failed for %s", call.call_id)
            await self.hangup(call.call_id, cause="on_incoming_failed")

    async def _handle_bye(self, headers: dict[str, str], addr: tuple[str, int]) -> None:
        call_id = headers.get("Call-ID", "")
        self._reply(200, "OK", headers, addr)
        await self.hangup(call_id, cause="remote_bye", send_bye=False)

    async def _handle_cancel(self, headers: dict[str, str], addr: tuple[str, int]) -> None:
        call_id = headers.get("Call-ID", "")
        self._reply(200, "OK", headers, addr)
        call = self.calls.get(call_id)
        if call is not None:
            if call.cancelled is not None and not call.cancelled.is_set():
                call.cancelled.set()
            if call.invite_headers and call.invite_addr and call.state == "ringing":
                self._reply(487, "Request Terminated", call.invite_headers, call.invite_addr, extra={"To": call.to_header})
        await self.hangup(call_id, cause="cancelled", send_bye=False)

    async def _handle_outbound_response(
        self,
        call: ActiveCall,
        status: int,
        headers: dict[str, str],
        body: str,
        addr: tuple[str, int],
    ) -> None:
        if status in {180, 183}:
            call.state = "ringing"
            if self.on_call_state:
                await self.on_call_state(call.call_id, {"status": "ringing"})
            return
        if status == 200:
            to_tag = re.search(r"tag=([^;]+)", headers.get("To", ""))
            call.remote_tag = to_tag.group(1) if to_tag else call.remote_tag
            call.to_header = headers.get("To", call.to_header)
            remote_rtp_host, remote_rtp_port, codec = self._parse_sdp_media(body)
            if remote_rtp_host:
                if not _is_ipv4(remote_rtp_host):
                    remote_rtp_host = await self._resolve_ipv4(remote_rtp_host)
                call.remote_rtp_host = remote_rtp_host
                call.remote_rtp_port = remote_rtp_port
                call.codec = codec
            # ACK
            request_uri = f"sip:{call.remote_number}@{self.config.domain}"
            ack = (
                f"ACK {request_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.config.local_sip_port};rport;branch=z9hG4bK{random.randint(10**8, 10**12)}\r\n"
                f"From: {call.from_header}\r\n"
                f"To: {call.to_header}\r\n"
                f"Call-ID: {call.call_id}\r\n"
                f"CSeq: {call.cseq} ACK\r\n"
                f"Contact: {self._contact()}\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            self._send(ack, addr)
            if call.rtp_protocol is None:
                await self._setup_rtp(call)
            call.state = "answered"
            call.answered_at = time.time()
            if self.on_call_state:
                await self.on_call_state(call.call_id, {"status": "answered"})
            return
        if status >= 400:
            call.state = "failed"
            if self.on_call_state:
                await self.on_call_state(call.call_id, {"status": "failed", "code": status})
            await self.hangup(call.call_id, cause=f"sip_{status}", send_bye=False)

    async def dial(
        self,
        number: str,
        *,
        on_rtp: OnRtpPcm24 | None = None,
        playback_provider: Callable[[], bytes] | None = None,
    ) -> ActiveCall:
        await self._reclaim_stale_calls()
        active = self._active_calls()
        if len(active) >= self.config.max_concurrent_calls:
            raise RuntimeError("Max concurrent calls reached for this SIP account")
        number = re.sub(r"[^\d+*#]", "", number)
        if not number:
            raise ValueError("Empty number")
        rtp_port = self._allocate_rtp_port()
        call_id = f"{random.randint(10**10, 10**13)}@{self.local_ip}"
        local_tag = f"{random.randint(10**6, 10**9)}"
        self._cseq += 1
        request_uri = f"sip:{number}@{self.config.domain}"
        from_header = f"{_quote(self.config.display_name or self.config.login)} <{_sip_uri(self.config.caller_id or self.config.login, self.config.domain)}>;tag={local_tag}"
        to_header = f"<{request_uri}>"
        sdp = self._local_sdp(rtp_port)
        call = ActiveCall(
            call_id=call_id,
            direction="outbound",
            remote_number=number,
            local_tag=local_tag,
            remote_host=self._proxy_ip,
            remote_port=self._proxy_port,
            local_rtp_port=rtp_port,
            state="dialing",
            cseq=self._cseq,
            from_header=from_header,
            to_header=to_header,
            on_rtp=on_rtp,
            playback_provider=playback_provider,
        )
        self.calls[call_id] = call
        headers = {
            "From": from_header,
            "To": to_header,
            "Call-ID": call_id,
            "CSeq": f"{self._cseq} INVITE",
            "Contact": self._contact(),
            "Content-Type": "application/sdp",
            "Allow": "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO",
        }
        # fire INVITE with auth retry; responses also drive call state via handle_message
        status, resp_headers, resp_body = await self._request(
            "INVITE",
            request_uri,
            headers,
            body=sdp,
        )
        await self._handle_outbound_response(call, status, resp_headers, resp_body, (self._proxy_ip, self._proxy_port))
        if call.state == "failed":
            raise RuntimeError(f"Outbound call failed ({call.state})")
        return call

    async def hangup(self, call_id: str, cause: str = "local_hangup", send_bye: bool = True) -> None:
        call = self.calls.pop(call_id, None)
        if call is None:
            return
        if call.cancelled is not None and not call.cancelled.is_set():
            call.cancelled.set()
        call.state = "ended"
        logger.info(
            "SIP hangup %s cause=%s rtp_rx=%s rtp_tx=%s peer=%s:%s",
            call_id[:24],
            cause,
            call.rtp_packets_rx,
            call.rtp_packets_tx,
            call.remote_rtp_host,
            call.remote_rtp_port,
        )
        if call.media_task:
            call.media_task.cancel()
            try:
                await call.media_task
            except asyncio.CancelledError:
                pass
        if call.rtp_transport:
            call.rtp_transport.close()
        self._release_rtp_port(call.local_rtp_port)
        if send_bye and call.direction and call.remote_host:
            self._cseq += 1
            request_uri = f"sip:{call.remote_number}@{self.config.domain}"
            bye = (
                f"BYE {request_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.config.local_sip_port};rport;branch=z9hG4bK{random.randint(10**8, 10**12)}\r\n"
                f"From: {call.from_header}\r\n"
                f"To: {call.to_header}\r\n"
                f"Call-ID: {call.call_id}\r\n"
                f"CSeq: {self._cseq} BYE\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            try:
                self._send(bye, (call.remote_host, call.remote_port))
            except Exception:
                pass
        if self.on_call_state:
            await self.on_call_state(call_id, {"status": "ended", "cause": cause})
