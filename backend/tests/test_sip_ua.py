import pytest

from app.sip_ua import (
    ActiveCall,
    SipEndpointConfig,
    SipUserAgent,
    outbound_fail_message,
)


def _ua() -> SipUserAgent:
    return SipUserAgent(
        SipEndpointConfig(
            account_id=1,
            login="062xxx",
            password="secret",
            domain="sip.telphin.com",
            sip_server="voice.telphin.com:5068",
        )
    )


def _call(ua: SipUserAgent) -> ActiveCall:
    call = ActiveCall(
        call_id="c1",
        direction="outbound",
        remote_number="79001234567",
        local_tag="tag1",
        state="dialing",
        local_rtp_port=10000,
    )
    ua.calls[call.call_id] = call
    return call


def test_outbound_fail_message_includes_sip_code() -> None:
    text = outbound_fail_message(403, {"Reason-Phrase": "Forbidden"}, "")
    assert "Outbound call failed (SIP 403 Forbidden)" in text
    assert "Telphin" in text


def test_invite_from_uses_login_not_caller_id() -> None:
    ua = _ua()
    ua.config.caller_id = "74951234567"
    ua.config.display_name = "Sales"
    header = ua._from(tag="abc")
    assert "sip:062xxx@sip.telphin.com" in header
    assert "74951234567" not in header
    identity = ua._identity_headers()
    assert "74951234567" in identity["P-Asserted-Identity"]


def test_digest_nc_increments_for_same_nonce() -> None:
    ua = _ua()
    challenge = {"realm": "sip.telphin.com", "nonce": "abc", "qop": "auth"}
    first = ua._auth_header("INVITE", "sip:7900@sip.telphin.com", challenge)
    second = ua._auth_header("INVITE", "sip:7900@sip.telphin.com", challenge)
    assert "nc=00000001" in first
    assert "nc=00000002" in second


def test_digest_nc_resets_on_new_nonce() -> None:
    ua = _ua()
    first = ua._auth_header("REGISTER", "sip:sip.telphin.com", {"realm": "r", "nonce": "one", "qop": "auth"})
    second = ua._auth_header("REGISTER", "sip:sip.telphin.com", {"realm": "r", "nonce": "two", "qop": "auth"})
    assert "nc=00000001" in first
    assert "nc=00000001" in second


@pytest.mark.asyncio
async def test_invite_401_does_not_fail_call() -> None:
    ua = _ua()
    call = _call(ua)
    await ua._handle_outbound_response(
        call,
        401,
        {"WWW-Authenticate": 'Digest realm="r", nonce="n"', "Reason-Phrase": "Unauthorized"},
        "",
        ("1.1.1.1", 5060),
    )
    assert call.state == "dialing"
    assert call.call_id in ua.calls


@pytest.mark.asyncio
async def test_setup_rtp_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    ua = _ua()
    call = _call(ua)
    binds: list[int] = []

    async def fake_bind(target: ActiveCall) -> None:
        binds.append(target.local_rtp_port)
        target.rtp_protocol = object()  # type: ignore[assignment]

    monkeypatch.setattr(ua, "_bind_rtp_socket", fake_bind)
    await ua._setup_rtp(call, start_loop=False)
    await ua._setup_rtp(call, start_loop=False)
    assert binds == [10000]


@pytest.mark.asyncio
async def test_invite_403_fails_call() -> None:
    ua = _ua()
    call = _call(ua)
    await ua._handle_outbound_response(
        call,
        403,
        {"Reason-Phrase": "Forbidden"},
        "",
        ("1.1.1.1", 5060),
    )
    assert call.state == "ended"
    assert call.call_id not in ua.calls


def test_non2xx_ack_reuses_invite_via(monkeypatch: pytest.MonkeyPatch) -> None:
    ua = _ua()
    sent: list[str] = []
    monkeypatch.setattr(ua, "_send", lambda message, addr=None: sent.append(message))
    ua._send_non2xx_ack(
        "sip:7900@sip.telphin.com",
        {
            "Via": "SIP/2.0/UDP 10.0.0.1:5060;rport;branch=z9hG4bK111",
            "From": '"ice" <sip:062xxx@sip.telphin.com>;tag=t',
            "To": "<sip:7900@sip.telphin.com>",
            "Call-ID": "c1",
            "CSeq": "8 INVITE",
        },
        {"To": "<sip:7900@sip.telphin.com>;tag=pbx"},
        ("1.1.1.1", 5068),
    )
    assert sent
    ack = sent[0]
    assert ack.startswith("ACK sip:7900@sip.telphin.com SIP/2.0")
    assert "branch=z9hG4bK111" in ack
    assert "CSeq: 8 ACK" in ack
    assert "tag=pbx" in ack
