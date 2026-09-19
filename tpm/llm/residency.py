"""Where the external model really is: measurements a judge can repeat, not claims.

Saying "we use Mistral on Verda in Finland" proves nothing - any app can print that sentence. This module gathers
evidence that does not depend on us being honest:

* the run's own egress ledger: which hosts the calls actually went to (and that none went anywhere else),
* the TLS certificate the endpoint presents: which service answered,
* the round-trip time of a TCP handshake, measured against public endpoints whose location is documented
  (AWS regional S3), at the same moment from the same machine: a signal in fibre travels ~200 km per millisecond
  there and back, so the round trip puts a hard ceiling on how far away the machine that answered can be,
* what the regional internet registry says about the address,
* what the app refuses: a US endpoint or a worldwide inference profile is rejected by configuration, not by promise.

Nothing here sends any data: DNS, a TLS handshake, TCP connects and public registry lookups only. Every step may fail
(offline, firewall, registry down) and then says so; a missing measurement is never reported as a pass.
"""
from __future__ import annotations

import socket
import ssl
import statistics
import time
from typing import Any, Optional

from ..config import NON_EU_MODEL_PREFIXES, Settings
from ..contracts import now_iso

# A signal in fibre travels at about two thirds of the speed of light: ~200 km/ms, so a round trip of 1 ms means the
# other machine is at most ~100 km away. Switching and queueing only add time, so the bound is generous.
KM_PER_MS_ONE_WAY = 100.0

# Public endpoints whose region AWS documents, used as a ruler for the round-trip times of this machine, right now.
REFERENCE_ENDPOINTS = [
    ("s3.eu-north-1.amazonaws.com", "Stockholm, Sweden", "EU"),
    ("s3.eu-central-1.amazonaws.com", "Frankfurt, Germany", "EU"),
    ("s3.us-east-1.amazonaws.com", "N. Virginia, USA", "US"),
    ("s3.us-west-2.amazonaws.com", "Oregon, USA", "US"),
]

# Negative controls: endpoints and model ids the eu-hosted profile must refuse, checked live against the config.
REFUSAL_CASES = [
    ("a US region of the same provider", {"base_url": "https://bedrock-mantle.us-east-1.api.aws/anthropic"}),
    ("a look-alike host", {"base_url": "https://containers.datacrunch.io.example.net/v1"}),
    ("Anthropic's own API (no EU processing)", {"base_url": None}),
    ("a worldwide inference profile", {"model": "global.anthropic.claude-sonnet-5"}),
]


def _rtt_ms(host: str, port: int = 443, attempts: int = 5, timeout: float = 4.0) -> dict[str, Any]:
    """Round-trip time of a TCP handshake, several times; the minimum is the cleanest measurement (no queueing)."""
    times: list[float] = []
    error = ""
    for _ in range(max(1, attempts)):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.perf_counter() - t0) * 1000.0)
        except OSError as e:
            error = f"{type(e).__name__}: {e}"
    if not times:
        return {"host": host, "n": 0, "error": error or "no answer"}
    return {"host": host, "n": len(times), "min_ms": round(min(times), 1), "median_ms": round(statistics.median(times), 1)}


def _resolve(host: str) -> dict[str, Any]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return {"host": host, "addresses": ips}
    except OSError as e:
        return {"host": host, "addresses": [], "error": f"{type(e).__name__}: {e}"}


def _tls(host: str, port: int = 443, timeout: float = 6.0) -> dict[str, Any]:
    """The certificate the endpoint presents, verified against the system trust store: which service answered."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                peer_ip = tls.getpeername()[0]
    except Exception as e:
        return {"verified": False, "error": f"{type(e).__name__}: {e}"}

    def field(part: str, key: str) -> str:
        for rdn in cert.get(part, ()):
            for k, v in rdn:
                if k == key:
                    return str(v)
        return ""

    return {
        "verified": True,
        "peer_ip": peer_ip,
        "subject": field("subject", "commonName"),
        "issuer": field("issuer", "organizationName") or field("issuer", "commonName"),
        "names": [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"][:8],
        "valid_until": cert.get("notAfter", ""),
    }


def _registry(ip: str, timeout: float = 8.0) -> dict[str, Any]:
    """What the regional internet registry says about the address (holder, country). Administrative data: address
    blocks are leased and the country field can be stale, so this is context, never the proof on its own."""
    if not ip:
        return {"error": "no address"}
    try:
        import httpx

        r = httpx.get(f"https://rdap.org/ip/{ip}", timeout=timeout, follow_redirects=True)
        if r.status_code >= 400:
            return {"ip": ip, "error": f"HTTP {r.status_code}"}
        d = r.json()
        holders = []
        for e in d.get("entities", []) or []:
            vcard = (e.get("vcardArray") or [None, []])[1]
            name = next((x[3] for x in vcard if isinstance(x, list) and x[0] == "fn"), "")
            kind = next((x[3] for x in vcard if isinstance(x, list) and x[0] == "kind"), "")
            if name and kind == "org":
                holders.append(str(name))
        return {"ip": ip, "range": f"{d.get('startAddress', '')} - {d.get('endAddress', '')}", "name": d.get("name", ""),
                "country": d.get("country", ""), "holders": holders[:3], "source": "RDAP (rdap.org)"}
    except Exception as e:
        return {"ip": ip, "error": f"{type(e).__name__}: {e}"}


def refusals(settings: Settings) -> list[dict[str, Any]]:
    """What the app refuses, checked against the live configuration: each case must make the external route
    unavailable. No network call is made - the refusal happens before any call could."""
    out: list[dict[str, Any]] = []
    for label, override in REFUSAL_CASES:
        probe = settings.model_copy(deep=True)
        for key, value in override.items():
            setattr(probe.external_llm, key, value)
        reason = probe.external_block_reason()
        out.append({"case": label, "setting": {k: v for k, v in override.items()}, "refused": bool(reason), "reason": reason or ""})
    return out


def ledger_hosts(ws: Any, settings: Settings) -> dict[str, Any]:
    """Which endpoints this run's external calls actually went to, from the run's own egress ledger."""
    from . import ledger as ledger_mod

    try:
        recs = ledger_mod.read(ws)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    ext = [r for r in recs if r.route == "external"]
    sent = [r for r in ext if r.guard_result == "allowed"]
    hosts: dict[str, int] = {}
    for r in sent:
        host = r.provider.split("@")[-1].strip() if "@" in r.provider else "(provider default endpoint)"
        hosts[host] = hosts.get(host, 0) + 1
    allow = [str(p).strip().lower() for p in settings.active_profile.eu_hosts if p]
    from fnmatch import fnmatch

    off = sorted(h for h in hosts if allow and not any(fnmatch(h, p) for p in allow))
    return {"run_id": getattr(ws, "run_id", ""), "external_records": len(ext), "payloads_sent": len(sent),
            "hosts": hosts, "off_allowlist": off, "blocked_by_guard": sum(1 for r in ext if r.guard_result == "blocked"),
            "models": sorted({r.model for r in sent})}


def check(settings: Settings, ws: Any = None, calibrate: bool = True, attempts: int = 5) -> dict[str, Any]:
    """Gather the evidence. `ws`: a finished run whose ledger is cross-checked. `calibrate`: also measure the
    reference endpoints (TCP connects to AWS S3; no data, no account)."""
    cfg = settings.external_llm
    prof = settings.active_profile
    result: dict[str, Any] = {
        "checked_at": now_iso(),
        "profile": settings.profile,
        "endpoint": {"url": cfg.base_url or "", "host": cfg.host, "model": cfg.model, "provider": cfg.provider,
                     "operator_says": cfg.operator, "location_says": cfg.location},
        "allowlist": {"hosts": list(prof.eu_hosts), "on_list": settings.external_block_reason() is None,
                      "block_reason": settings.external_block_reason() or ""},
        "refusals": refusals(settings),
    }
    if not cfg.host:
        result["verdict"] = {"ok": None, "lines": [f"Profile '{settings.profile}' has no external endpoint: nothing leaves this machine, so there is nothing to locate."]}
        return result

    result["dns"] = _resolve(cfg.host)
    result["tls"] = _tls(cfg.host)
    rtt = _rtt_ms(cfg.host, attempts=attempts)
    result["rtt"] = rtt
    if rtt.get("n"):
        result["distance"] = {"max_km": int(round(rtt["min_ms"] * KM_PER_MS_ONE_WAY)),
                              "basis": f"{rtt['min_ms']} ms round trip at ~200 km/ms in fibre (switching only adds time)"}
    if calibrate:
        result["references"] = [{"host": h, "where": where, "region": region, **_rtt_ms(h, attempts=max(2, attempts // 2))}
                                for h, where, region in REFERENCE_ENDPOINTS]
    ip = (result.get("tls") or {}).get("peer_ip") or next(iter(result["dns"].get("addresses") or []), "")
    result["registry"] = _registry(ip)
    if ws is not None:
        result["run"] = ledger_hosts(ws, settings)
    result["verdict"] = _verdict(result)
    return result


def _verdict(r: dict[str, Any]) -> dict[str, Any]:
    """The conclusions the measurements support, and the ones they do not."""
    lines: list[str] = []
    ok = True
    host = r["endpoint"]["host"]
    run = r.get("run") or {}
    if run and not run.get("error"):
        hosts = run.get("hosts") or {}
        where = ", ".join(f"{h} ({n})" for h, n in sorted(hosts.items()))
        n_blocked = run.get("blocked_by_guard") or 0
        lines.append(f"Run {run.get('run_id')}: {run.get('payloads_sent', 0)} payload(s) left this machine, all to {where or 'nowhere'}."
                     + (f" {n_blocked} more {'was' if n_blocked == 1 else 'were'} blocked by the guard and never sent." if n_blocked else ""))
        if run.get("off_allowlist"):
            ok = False
            lines.append(f"WARNING: calls went to {', '.join(run['off_allowlist'])}, which is not on the EU list.")
    if r["allowlist"]["on_list"]:
        lines.append(f"{host} is on this profile's list of EU-hosted services, and the model id carries no cross-region routing prefix "
                     f"({', '.join(NON_EU_MODEL_PREFIXES[:3])}...), so nothing in the request asks for processing outside the region.")
    else:
        ok = False
        lines.append(f"The external route is not usable: {r['allowlist']['block_reason']}")
    tls = r.get("tls") or {}
    if tls.get("verified"):
        lines.append(f"The endpoint's certificate is valid and issued to {tls.get('subject') or '(no common name)'} by {tls.get('issuer')}, "
                     f"valid until {tls.get('valid_until')}: the answer came from that service, not from something in between.")
    else:
        ok = False
        lines.append(f"The TLS certificate could not be checked: {tls.get('error')}")
    dist = r.get("distance")
    rtt = r.get("rtt") or {}
    if dist:
        lines.append(f"A TCP handshake with {host} takes {rtt.get('min_ms')} ms ({rtt.get('n')} attempts). In fibre that is at most "
                     f"~{dist['max_km']:,} km from this machine.".replace(",", " "))
        refs = [x for x in (r.get("references") or []) if x.get("n")]
        us = [x for x in refs if x["region"] == "US"]
        eu = [x for x in refs if x["region"] == "EU"]
        if us:
            slowest_eu = max((x["min_ms"] for x in eu), default=None)
            fastest_us = min(x["min_ms"] for x in us)
            lines.append("From this machine at the same moment: "
                         + "; ".join(f"{x['where']} {x['min_ms']} ms" for x in refs) + ".")
            if rtt.get("min_ms", 0) < fastest_us / 2:
                lines.append(f"The endpoint answers {round(fastest_us / max(rtt['min_ms'], 0.1))}x faster than the nearest US reference point, "
                             f"so the machine that answered cannot be in North America" + (f" (the European reference points answer within {slowest_eu} ms)." if slowest_eu else "."))
            else:
                ok = False
                lines.append("The endpoint is NOT clearly closer than the US reference points: do not claim EU processing from this measurement.")
    else:
        ok = False
        lines.append(f"The round trip could not be measured: {rtt.get('error', 'unknown')}")
    reg = r.get("registry") or {}
    if reg.get("holders") or reg.get("country"):
        lines.append(f"The address {reg.get('ip')} belongs to {', '.join(reg.get('holders') or []) or reg.get('name') or 'an unnamed holder'}"
                     + (f", registry country field '{reg['country']}'" if reg.get("country") else "")
                     + f" ({reg.get('source')}). Address blocks are leased and this field can be stale, so it is context, not proof.")
    refused = [x for x in r.get("refusals", []) if not x["refused"]]
    if refused:
        ok = False
        lines.append("WARNING: these should have been refused but were not: " + "; ".join(x["case"] for x in refused))
    else:
        lines.append("Checked against the live configuration: " + "; ".join(x["case"] for x in r.get("refusals", [])) + " - each one makes the external route unavailable before any call.")
    return {"ok": ok, "lines": lines, "limits": [
        "This shows where the machine that answered is, not where it might forward the request afterwards.",
        "A certificate proves which service answered, not in which country it stands.",
        "Registry data is administrative: leased address blocks can carry a stale country.",
        "For the rest, ask the operator for a written statement of the data centre and its logging.",
    ]}


def format_check(result: dict[str, Any]) -> list[str]:
    """The evidence as lines for the console."""
    out = [f"Where does the external model run?  (profile {result['profile']}, checked {result['checked_at']})", ""]
    ep = result["endpoint"]
    out.append(f"  endpoint   {ep['url'] or '(none)'}")
    if ep["url"]:
        out.append(f"  model      {ep['model']}")
        out.append(f"  claimed    {ep['operator_says'] or '(no operator in the settings)'}; {ep['location_says'] or '(no location in the settings)'}  <- a claim, the lines below are measurements")
        dns = result.get("dns") or {}
        out.append(f"  addresses  {', '.join(dns.get('addresses') or []) or dns.get('error', '')}")
    out.append("")
    verdict = result.get("verdict") or {}
    for line in verdict.get("lines", []):
        out.append("  " + line)
    if verdict.get("limits"):
        out.append("")
        out.append("  What this does not prove:")
        out += [f"   - {x}" for x in verdict["limits"]]
    out.append("")
    out.append("  Verdict: " + {True: "the measurements support EU processing", False: "NOT proven - see the warnings above", None: "no external model is configured"}[verdict.get("ok")])
    return out
