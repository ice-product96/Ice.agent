"""Minimal asyncio SIP UA: REGISTER / INVITE / BYE + RTP PCMU for Telphin-style trunks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .sip_audio import (
    OPENAI_FRAME_SAMPLES,
    SIP_FRAME_SAMPLES,
    Codec,
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


def _parse_www_authenticate(header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header):
        result[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return result


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
    seq: int = field(default_factory=lambda: random.randint(1, 0xFFFF))
    timestamp: int = field(default_factory=lambda: random.randint(1, 0xFFFFFFFF))
    ssrc: int = field(default_factory=lambda: random.randint(1, 0xFFFFFFFF))


class RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, call: ActiveCall) -> None:
        self.call = call
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # noqa: ARG002
        if len(data) < 12:
            return
        payload = data[12:]
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

    def send_payload(self, payload: bytes) -> None:
        if not self.transport or not self.call.remote_rtp_host or not self.call.remote_rtp_port:
            return
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x00 if self.call.codec == "pcmu" else 0x08  # PT 0 / 8
        header[2] = (self.call.seq >> 8) & 0xFF
        header[3] = self.call.seq & 0xFF
        header[4] = (self.call.timestamp >> 24) & 0xFF
        header[5] = (self.call.timestamp >> 16) & 0xFF
        header[6] = (self.call.timestamp >> 8) & 0xFF
        header[7] = self.call.timestamp & 0xFF
        header[8] = (self.call.ssrc >> 24) & 0xFF
        header[9] = (self.call.ssrc >> 16) & 0xFF
        header[10] = (self.call.ssrc >> 8) & 0xFF
        header[11] = self.call.ssrc & 0xFF
        self.call.seq = (self.call.seq + 1) & 0xFFFF
        self.call.timestamp = (self.call.timestamp + SIP_FRAME_SAMPLES) & 0xFFFFFFFF
        self.transport.sendto(bytes(header) + payload, (self.call.remote_rtp_host, self.call.remote_rtp_port))


class SipProtocol(asyncio.DatagramProtocol):
    def __init__(self, ua: "SipUserAgent") -> None:
        self.ua = ua
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text = data.decode("utf-8", errors="ignore")
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

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # Discover outbound IP toward SIP server
        if not self.config.public_ip:
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.connect((self._proxy_host, self._proxy_port))
                self.local_ip = probe.getsockname()[0]
                probe.close()
            except Exception:
                self.local_ip = "127.0.0.1"
        try:
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: SipProtocol(self),
                local_addr=("0.0.0.0", self.config.local_sip_port),
                reuse_port=False,
            )
        except OSError:
            # port busy — ephemeral
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: SipProtocol(self),
                local_addr=("0.0.0.0", 0),
            )
            sockname = self._transport.get_extra_info("sockname")
            self.config.local_sip_port = int(sockname[1])

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
        target = addr or (self._proxy_host, self._proxy_port)
        self._transport.sendto(message.encode("utf-8"), target)

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
        return f"<sip:{self.config.login}@{self.local_ip}:{self.config.local_sip_port}>"

    def _from(self) -> str:
        display = self.config.display_name or self.config.login
        uri = _sip_uri(self.config.caller_id or self.config.login, self.config.domain)
        return f"{_quote(display)} <{uri}>;tag={self._from_tag}"

    def _auth_header(self, method: str, uri: str, challenge: dict[str, str]) -> str:
        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce", "")
        qop = challenge.get("qop", "")
        opaque = challenge.get("opaque")
        algorithm = challenge.get("algorithm", "MD5")
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
        fut: asyncio.Future[tuple[int, dict[str, str], str]] = asyncio.get_running_loop().create_future()
        self._pending[branch] = fut
        self._send(message, addr)
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"SIP {method} timed out")
                status, resp_headers, resp_body = await asyncio.wait_for(fut, timeout=remaining)
                if status < 200:
                    # provisional — keep waiting for final on same branch
                    fut = asyncio.get_running_loop().create_future()
                    self._pending[branch] = fut
                    continue
                break
        finally:
            self._pending.pop(branch, None)
        if auth_retry and status in {401, 407}:
            auth_key = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
            auth_hdr = "Authorization" if status == 401 else "Proxy-Authorization"
            challenge_raw = resp_headers.get(auth_key, "")
            challenge = _parse_www_authenticate(challenge_raw)
            headers[auth_hdr] = self._auth_header(method, request_uri, challenge)
            self._cseq += 1
            headers["CSeq"] = f"{self._cseq} {method}"
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
        status, _, _ = await self._request("REGISTER", request_uri, headers)
        if status in {200, 202}:
            self.registered = True
            self.registration_status = "registered"
            if self.on_reg_state:
                await self.on_reg_state(True, "registered")
            if self._reg_task is None or self._reg_task.done():
                self._reg_task = asyncio.create_task(self._reregister_loop(expires), name=f"sip-reg-{self.config.account_id}")
            return
        self.registered = False
        self.registration_status = f"failed:{status}"
        if self.on_reg_state:
            await self.on_reg_state(False, self.registration_status)
        raise RuntimeError(f"SIP REGISTER failed with status {status}")

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
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        start = lines[0]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
        return start, headers, body

    async def handle_message(self, text: str, addr: tuple[str, int]) -> None:
        start, headers, body = self._parse_message(text)
        via = headers.get("Via", "")
        branch_match = re.search(r"branch=([^;]+)", via)
        branch = branch_match.group(1) if branch_match else ""

        if start.startswith("SIP/2.0"):
            status = int(start.split()[1])
            fut = self._pending.get(branch)
            if fut and not fut.done():
                fut.set_result((status, headers, body))
            # provisional / final for outbound calls
            call_id = headers.get("Call-ID", "")
            call = self.calls.get(call_id)
            if call and call.direction == "outbound":
                await self._handle_outbound_response(call, status, headers, body, addr)
            return

        method = start.split()[0].upper()
        if method == "INVITE":
            await self._handle_invite(start, headers, body, addr)
        elif method == "ACK":
            call_id = headers.get("Call-ID", "")
            call = self.calls.get(call_id)
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

    async def _setup_rtp(self, call: ActiveCall) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: RtpProtocol(call),
            local_addr=("0.0.0.0", call.local_rtp_port),
        )
        call.rtp_transport = transport
        call.rtp_protocol = protocol
        call.media_task = asyncio.create_task(self._media_loop(call), name=f"rtp-tx-{call.call_id}")

    async def _media_loop(self, call: ActiveCall) -> None:
        try:
            while call.state in {"ringing", "answered", "early"} and call.rtp_protocol:
                pcm24 = b""
                if call.playback_provider:
                    try:
                        pcm24 = call.playback_provider() or b""
                    except Exception:
                        pcm24 = b""
                if len(pcm24) < OPENAI_FRAME_SAMPLES * 2:
                    pcm24 = pcm24 + b"\x00" * (OPENAI_FRAME_SAMPLES * 2 - len(pcm24))
                payload = openai_to_sip(pcm24[: OPENAI_FRAME_SAMPLES * 2], call.codec)
                call.rtp_protocol.send_payload(payload)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media loop failed for %s", call.call_id)

    async def _handle_invite(
        self,
        start: str,
        headers: dict[str, str],
        body: str,
        addr: tuple[str, int],
    ) -> None:
        if len([c for c in self.calls.values() if c.state in {"ringing", "answered", "early", "dialing"}]) >= self.config.max_concurrent_calls:
            self._reply(486, "Busy Here", headers, addr)
            return
        call_id = headers.get("Call-ID", f"in-{random.randint(10**8, 10**12)}")
        remote = headers.get("From", "")
        number_match = re.search(r"sip:([^@>;]+)", remote)
        remote_number = number_match.group(1) if number_match else "unknown"
        rtp_port = self._allocate_rtp_port()
        remote_rtp_host, remote_rtp_port, codec = self._parse_sdp_media(body)
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
        )
        self.calls[call_id] = call
        self._reply(100, "Trying", headers, addr)
        self._reply(180, "Ringing", headers, addr, extra={"To": to_header})
        sdp = self._local_sdp(rtp_port, codec)
        self._reply(
            200,
            "OK",
            headers,
            addr,
            extra={
                "To": to_header,
                "Content-Type": "application/sdp",
                "Content-Length": str(len(sdp.encode("utf-8"))),
            },
            body=sdp,
        )
        await self._setup_rtp(call)
        call.state = "answered"
        if self.on_incoming:
            await self.on_incoming(call)
        if self.on_call_state:
            await self.on_call_state(call.call_id, {"status": "answered", "direction": "inbound"})

    async def _handle_bye(self, headers: dict[str, str], addr: tuple[str, int]) -> None:
        call_id = headers.get("Call-ID", "")
        self._reply(200, "OK", headers, addr)
        await self.hangup(call_id, cause="remote_bye", send_bye=False)

    async def _handle_cancel(self, headers: dict[str, str], addr: tuple[str, int]) -> None:
        call_id = headers.get("Call-ID", "")
        self._reply(200, "OK", headers, addr)
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
        active = [c for c in self.calls.values() if c.state in {"ringing", "answered", "early", "dialing"}]
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
            remote_host=self._proxy_host,
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
        await self._handle_outbound_response(call, status, resp_headers, resp_body, (self._proxy_host, self._proxy_port))
        if call.state == "failed":
            raise RuntimeError(f"Outbound call failed ({call.state})")
        return call

    async def hangup(self, call_id: str, cause: str = "local_hangup", send_bye: bool = True) -> None:
        call = self.calls.pop(call_id, None)
        if call is None:
            return
        call.state = "ended"
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
