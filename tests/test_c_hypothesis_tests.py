"""Round 6: actuator hypotheses are tested against lead / lag (every branch writes a valid inference)."""
from tpm.config import load_settings
from tpm.contracts import SignalDescriptor
from tpm.profile.roles import check_hypotheses
from tpm.workspace import Workspace


def _d(sid, role):
    return SignalDescriptor(id=sid, column_index=int(sid[1:]), dtype="float64", structural_role=role, evidence_ids=["EV-000001"])


def test_every_branch_writes_a_valid_inference(tmp_path):
    ws = Workspace(run_id="hyp", settings=load_settings(), root=tmp_path)
    ds = [_d("S01", "actuator_like"), _d("S02", "continuous_measured"), _d("S03", "actuator_like"), _d("S04", "actuator_like"), _d("S05", "continuous_measured")]
    rel = {"pairs": [
        {"a": "S01", "b": "S02", "r": 0.8, "lag": 3},   # S02 follows the valve S01: confirmed
        {"a": "S05", "b": "S03", "r": -0.7, "lag": 2},  # the valve S03 reacts after S05: consistent
    ]}                                                  # S04: nothing -> not confirmed
    assert check_hypotheses(ws, ds, rel) == 3
    infs = {i.subject: i for i in ws.inferences.all()}
    assert "confirmed" in infs["S01"].claim and infs["S01"].status == "inferred"
    assert "consistent" in infs["S03"].claim and infs["S03"].status == "inferred"
    assert "not confirmed" in infs["S04"].claim and infs["S04"].status == "uncertain"
    assert all(infs[s].id in next(d for d in ds if d.id == s).inference_ids for s in ("S01", "S03", "S04"))
    ws.close()
