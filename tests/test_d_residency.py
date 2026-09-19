"""The residency evidence (tpm.llm.residency): what the app can actually show a judge about where the external model
runs. Every network step is monkeypatched: the tests never touch the network."""
from __future__ import annotations

import pytest

from tpm.config import load_settings
from tpm.contracts import EgressRecord
from tpm.llm import residency

VERDA = "containers.datacrunch.io"


def _measurements(monkeypatch, endpoint_ms: float = 9.0, us_ms: float = 130.0, eu_ms: float = 24.0) -> dict:
    """Stub DNS, TLS, round trips and the registry; returns the call log so a test can prove nothing was measured."""
    calls: dict[str, list] = {"rtt": [], "tls": [], "dns": [], "registry": []}

    def rtt(host, port=443, attempts=5, timeout=4.0):
        calls["rtt"].append(host)
        ms = endpoint_ms if host == VERDA else (us_ms if ".us-" in host else eu_ms)
        return {"host": host, "n": attempts, "min_ms": ms, "median_ms": ms + 1.0}

    monkeypatch.setattr(residency, "_rtt_ms", rtt)
    monkeypatch.setattr(residency, "_resolve", lambda host: (calls["dns"].append(host), {"host": host, "addresses": ["86.38.238.249"]})[1])
    monkeypatch.setattr(residency, "_tls", lambda host, port=443, timeout=6.0: (calls["tls"].append(host), {
        "verified": True, "peer_ip": "86.38.238.249", "subject": host, "issuer": "Let's Encrypt", "names": [host], "valid_until": "Oct 20 01:30:04 2026 GMT"})[1])
    monkeypatch.setattr(residency, "_registry", lambda ip, timeout=8.0: (calls["registry"].append(ip), {
        "ip": ip, "range": "86.38.238.0 - 86.38.238.255", "name": "LT-LRTC", "country": "US", "holders": ['SC "Lithuanian Radio and TV Center"'], "source": "RDAP (rdap.org)"})[1])
    return calls


class _Ws:
    """A workspace whose ledger holds the given external records."""

    def __init__(self, records, run_id="eu_run"):
        self.run_id = run_id
        self._records = records

    def read(self):
        return self._records


def _ledger(monkeypatch, records):
    from tpm.llm import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "read", lambda ws: records)


def _rec(provider: str, guard_result: str = "allowed", task: str = "critique") -> EgressRecord:
    return EgressRecord(id="EGR-000001", task=task, purpose="test", route="external", provider=provider,
                        model="mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4", guard_result=guard_result, guard_reason="ok")


def test_the_profile_refuses_every_non_eu_case_before_any_call():
    s = load_settings(profile="eu-hosted")
    cases = residency.refusals(s)
    assert len(cases) == len(residency.REFUSAL_CASES)
    assert all(c["refused"] and c["reason"] for c in cases), [c for c in cases if not c["refused"]]
    assert s.external_llm.base_url and "datacrunch" in s.external_llm.host, "the probe must not change the real settings"


def test_evidence_says_what_was_measured_and_what_is_only_claimed(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    calls = _measurements(monkeypatch)
    _ledger(monkeypatch, [_rec(f"openai-compatible @ {VERDA}") for _ in range(12)] + [_rec("openai-compatible @ " + VERDA, "blocked")])
    s = load_settings(profile="eu-hosted")
    r = residency.check(s, ws=_Ws([]), attempts=5)

    assert r["verdict"]["ok"] is True
    text = "\n".join(r["verdict"]["lines"])
    assert "12 payload(s) left this machine, all to containers.datacrunch.io (12)" in text
    assert "1 more was blocked by the guard" in text
    assert "at most ~900 km" in text, text                      # 9.0 ms round trip
    assert "cannot be in North America" in text
    assert "Lithuanian Radio and TV Center" in text and "not proof" in text, "registry data must be shown with its caveat"
    assert r["distance"]["max_km"] == 900 and r["rtt"]["min_ms"] == 9.0
    assert VERDA in calls["rtt"] and calls["tls"] == [VERDA] and calls["registry"] == ["86.38.238.249"]
    assert len(r["verdict"]["limits"]) >= 3, "the limits of the evidence must be stated"

    lines = "\n".join(residency.format_check(r))
    assert "a claim, the lines below are measurements" in lines and "Finland (EU)" in lines
    assert "Verdict: the measurements support EU processing" in lines


def test_a_call_to_a_host_outside_the_list_fails_the_check(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    _measurements(monkeypatch)
    _ledger(monkeypatch, [_rec(f"openai-compatible @ {VERDA}"), _rec("anthropic @ api.anthropic.com")])
    r = residency.check(load_settings(profile="eu-hosted"), ws=_Ws([]))
    assert r["verdict"]["ok"] is False
    assert "api.anthropic.com" in "\n".join(r["verdict"]["lines"]) and "not on the EU list" in "\n".join(r["verdict"]["lines"])


def test_a_round_trip_that_does_not_beat_the_us_references_is_not_proof(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    _measurements(monkeypatch, endpoint_ms=95.0, us_ms=130.0)
    r = residency.check(load_settings(profile="eu-hosted"))
    assert r["verdict"]["ok"] is False
    assert "do not claim EU processing from this measurement" in "\n".join(r["verdict"]["lines"])


def test_an_unreachable_endpoint_is_never_reported_as_proven(monkeypatch):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    _measurements(monkeypatch)
    monkeypatch.setattr(residency, "_rtt_ms", lambda host, port=443, attempts=5, timeout=4.0: {"host": host, "n": 0, "error": "timed out"})
    monkeypatch.setattr(residency, "_tls", lambda host, port=443, timeout=6.0: {"verified": False, "error": "timed out"})
    r = residency.check(load_settings(profile="eu-hosted"))
    assert r["verdict"]["ok"] is False
    assert "could not be measured" in "\n".join(r["verdict"]["lines"])


def test_without_an_external_endpoint_nothing_is_measured(monkeypatch):
    calls = _measurements(monkeypatch)
    r = residency.check(load_settings(profile="no-egress"))
    assert r["verdict"]["ok"] is None and "nothing to locate" in r["verdict"]["lines"][0]
    assert not calls["rtt"] and not calls["tls"] and not calls["registry"], "no-egress must not touch the network"


def test_the_report_carries_the_measurement_not_only_the_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("TPM_EU_API_KEY", "k")
    _measurements(monkeypatch)
    _ledger(monkeypatch, [_rec(f"openai-compatible @ {VERDA}")])
    result = residency.check(load_settings(profile="eu-hosted"), ws=_Ws([]))

    from tpm.llm import ledger as ledger_mod

    class Ws:
        def exists(self, name):
            return name == "eu_residency"

        def read_json(self, name):
            return result

    text = ledger_mod.residency_statement(Ws())
    assert "9.0 ms" in text and "at most ~900 km" in text and "N. Virginia, USA 130.0 ms" in text
    assert "not where it might forward the request afterwards" in text, "the report must carry the limits too"
    assert "The settings claim:" in text and "Finland (EU)" in text

    class NoCheck:
        def exists(self, name):
            return False

    assert ledger_mod.residency_statement(NoCheck()) is None


@pytest.mark.parametrize("path", ["/api/eu-check", "/api/runs/{run_id}/eu-check"])
def test_the_ui_can_ask_for_the_check(path):
    from tpm.api.server import create_app

    assert path in {getattr(r, "path", "") for r in create_app().routes}
