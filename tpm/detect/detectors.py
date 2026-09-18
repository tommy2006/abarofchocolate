"""Detectors. Each has fit(F, spec, groups, mask) and score(F, spec, groups) -> (score per row, contribution
per signal per row).

F is the standardized feature matrix from features.FeatureSpec (blocks value | rmean | rstd | d1 | d2). Fit
receives the *contiguous* sample plus a row mask (the baseline rows to learn from) so detectors that need
sequence structure (lagged peers) can build it; the others simply use F[mask]. All detectors are
bounded-memory and the two sequential ones (EWMA, CUSUM) keep a small per-group state so chunked scoring
equals one long pass.

    pca          Hotelling T2 / SPE on value + rolling-mean + rolling-std blocks (the rolling mean averages
                 over more rows than any lead/lag, so the subspace is tight); contributions per feature
    robust_z     per-feature |z| over value + rolling-std, each feature normalized by its own normal extreme
    ewma         EWMA chart per feature (lambda 0.15), mean-centred, per-feature normalized
    cusum        two-sided CUSUM per feature (slack k = 1.0: slow drivers give ~0.7-sigma group offsets),
                 vectorized through the reflection identity, per-feature normalized
    corr_break   each signal regressed (ridge) on its lag-aligned cluster peers fitted on the baseline;
                 statistic = max(|residual|, |EWMA of residual|) per signal, each normalized by its normal
                 extreme. Derived signals (R2 > 0.99 from peers) get no residual: the deviation belongs to
                 their parents. This is the sensor-vs-process evidence.
    resid_spread log rolling std of the same residual (built automatically with corr_break): oscillations
                 and noise increases that keep a zero-mean residual
    iforest      IsolationForest on value + rolling-std; contributions via per-feature z weighting
    autoencoder  sklearn MLPRegressor bottleneck on value + rolling-std; per-signal reconstruction error
"""
from __future__ import annotations

import time
import warnings
from typing import Any, Optional

import numpy as np

from ._common import segments
from .features import FeatureSpec, feature_scale


def _rows(F: np.ndarray, mask: Optional[np.ndarray]) -> np.ndarray:
    return F if mask is None else F[mask]


def per_feature_norm(stat_abs: np.ndarray, floor: float) -> np.ndarray:
    """Each feature's own normal extreme (99.5th percentile of |stat| on baseline rows, floored): dividing by
    it makes features comparable, so a quiet signal's 3-sigma excursion is not hidden behind the routine
    spikes of a step-like actuator. A ratio of 1 means 'at the edge of this feature's normal range'."""
    q25, q75, q90, q995 = np.quantile(stat_abs, [0.25, 0.75, 0.90, 0.995], axis=0)
    fence = q75 + 3.0 * (q75 - q25)  # robust to a contaminated baseline (a few % of abnormal rows)
    q = np.minimum(q995, np.maximum(fence, q90))
    med = np.median(stat_abs, axis=0)
    return np.maximum.reduce([q, 2.0 * med, np.full(stat_abs.shape[1], floor)]).astype(np.float32)


def shift_within(X: np.ndarray, groups: Optional[np.ndarray], lag: int) -> np.ndarray:
    """X[t - lag] inside each contiguous group segment (edge rows repeat the first/last value)."""
    if lag == 0:
        return X
    out = np.empty_like(X)
    segs = segments(groups) if groups is not None else [(0, len(X))]
    for s, e in segs:
        seg = X[s:e]
        n = e - s
        k = min(abs(lag), n)
        if lag > 0:
            out[s:e] = np.vstack([np.repeat(seg[:1], k, axis=0), seg[: n - k]]) if k < n else np.repeat(seg[:1], n, axis=0)
        else:
            out[s:e] = np.vstack([seg[k:], np.repeat(seg[-1:], k, axis=0)]) if k < n else np.repeat(seg[-1:], n, axis=0)
    return out


class Detector:
    name: str = "base"
    uses: tuple[str, ...] = ("value",)
    stateful: bool = False

    def __init__(self, seed: int = 0):
        self.seed = seed
        self.fitted = False
        self.fit_seconds = 0.0
        self.info: dict[str, Any] = {}

    def _cols(self, spec: FeatureSpec) -> np.ndarray:
        return np.concatenate([np.arange(spec.block_slice(b).start, spec.block_slice(b).stop) for b in self.uses])

    def _per_signal(self, contrib_feat: np.ndarray, spec: FeatureSpec) -> np.ndarray:
        n = contrib_feat.shape[0]
        return contrib_feat.reshape(n, len(self.uses), spec.p).sum(axis=1).astype(np.float32)

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:  # pragma: no cover
        raise NotImplementedError

    def reset(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "uses": list(self.uses), "stateful": self.stateful, "fit_seconds": round(self.fit_seconds, 3), **self.info}


# ------------------------------------------------------------------------------------------------


class PCADetector(Detector):
    name = "pca"
    # the rolling mean averages over more rows than any lead/lag between signals, so its correlation
    # subspace is tight and a persistent inconsistency between signals shows up clearly in the SPE
    uses = ("value", "rmean", "rstd")

    def __init__(self, seed: int = 0, var_explained: float = 0.9, max_components: int = 40):
        super().__init__(seed)
        self.var_explained = var_explained
        self.max_components = max_components

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        self.cols = self._cols(spec)
        X = _rows(F, mask)[:, self.cols]
        n, d = X.shape
        mean = np.zeros(d, dtype=np.float64)
        cov = np.zeros((d, d), dtype=np.float64)
        step = 50_000
        for s in range(0, n, step):
            mean += X[s : s + step].astype(np.float64).sum(axis=0)
        mean /= max(1, n)
        for s in range(0, n, step):
            blk = X[s : s + step].astype(np.float64) - mean
            cov += blk.T @ blk
        cov /= max(1, n - 1)
        cov += np.eye(d) * 1e-4
        w, V = np.linalg.eigh(cov)
        order = np.argsort(w)[::-1]
        w, V = np.maximum(w[order], 1e-6), V[:, order]
        cum = np.cumsum(w) / w.sum()
        k = int(np.searchsorted(cum, self.var_explained) + 1)
        k = max(2, min(k, self.max_components, d - 1))
        self.mean = mean.astype(np.float32)
        self.P = V[:, :k].astype(np.float32)
        self.lam = w[:k].astype(np.float32)
        t2, spe, _ = self._raw(X)
        self.t2_ref = float(max(np.percentile(t2, 99), 1e-3))
        self.spe_ref = float(max(np.percentile(spe, 99), 1e-3))
        self.fitted = True
        self.fit_seconds = time.time() - t0
        self.info = {"n_components": k, "var_explained": round(float(cum[k - 1]), 4), "t2_ref": round(self.t2_ref, 3), "spe_ref": round(self.spe_ref, 3)}

    def _raw(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        Xc = X - self.mean
        T = Xc @ self.P
        t2 = ((T * T) / self.lam).sum(axis=1)
        R = Xc - T @ self.P.T
        return t2, (R * R).sum(axis=1), R

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        X = F[:, self.cols]
        Xc = X - self.mean
        T = Xc @ self.P
        t2 = ((T * T) / self.lam).sum(axis=1)
        R = Xc - T @ self.P.T
        spe = (R * R).sum(axis=1)
        score = np.maximum(t2 / self.t2_ref, spe / self.spe_ref)
        contrib_feat = (R * R) / self.spe_ref + np.clip(Xc * ((T / self.lam) @ self.P.T), 0, None) / self.t2_ref
        return score.astype(np.float32), self._per_signal(contrib_feat, spec)


class RobustZDetector(Detector):
    name = "robust_z"
    uses = ("value", "rstd")

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        self.cols = self._cols(spec)
        V = _rows(F, mask)[:, self.cols]
        med, scale = feature_scale(V)
        self.med = med.astype(np.float32)
        self.mad = np.maximum(scale, 0.25).astype(np.float32)
        self.q = per_feature_norm(np.abs((V - self.med) / self.mad), floor=2.0)
        self.fitted = True
        self.fit_seconds = time.time() - t0

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        z = (F[:, self.cols] - self.med) / self.mad / self.q
        return np.abs(z).max(axis=1).astype(np.float32), self._per_signal(z * z, spec)


class EWMADetector(Detector):
    name = "ewma"
    uses = ("value", "rstd")
    stateful = True

    def __init__(self, seed: int = 0, lam: float = 0.15):
        super().__init__(seed)
        self.lam = lam
        self.state: dict[str, np.ndarray] = {}

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        self.cols = self._cols(spec)
        V = _rows(F, mask)[:, self.cols]
        _, scale = feature_scale(V)
        self.med = np.nanmean(V, axis=0).astype(np.float32)  # mean-centred: zero average drift at baseline
        self.mad = np.maximum(scale, 0.25).astype(np.float32)
        self.limit = float(np.sqrt(self.lam / (2 - self.lam)))
        self.state = {}
        self.q = np.ones(len(self.cols), dtype=np.float32)
        e_full = self._stat(F, spec, groups)
        self.q = per_feature_norm(np.abs(_rows(e_full, mask)), floor=1.0)
        self.state = {}
        self.fitted = True
        self.fit_seconds = time.time() - t0

    def reset(self) -> None:
        self.state = {}

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        e_ = self._stat(F, spec, groups) / self.q
        return np.abs(e_).max(axis=1).astype(np.float32), self._per_signal(e_ * e_, spec)

    def _stat(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> np.ndarray:
        from scipy.signal import lfilter

        z = (F[:, self.cols] - self.med) / self.mad
        n, d = z.shape
        out = np.empty_like(z)
        if groups is None:
            groups = np.zeros(n, dtype=object)
        b, a = [self.lam], [1.0, -(1.0 - self.lam)]
        for s, e in segments(groups):
            g = str(groups[s])
            zi = self.state.get(g)
            if zi is None:
                zi = np.zeros((1, d), dtype=np.float64)
            y, zf = lfilter(b, a, z[s:e].astype(np.float64), axis=0, zi=zi)
            out[s:e] = y
            self.state[g] = zf
        if len(self.state) > 50_000:  # bound the state table
            self.state = {}
        return out / self.limit


class CUSUMDetector(Detector):
    name = "cusum"
    uses = ("value", "rstd")
    stateful = True

    def __init__(self, seed: int = 0, k: float = 1.0, cap: float = 30.0):
        super().__init__(seed)
        self.k = k
        self.cap = cap
        self.state: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        self.cols = self._cols(spec)
        V = _rows(F, mask)[:, self.cols]
        _, scale = feature_scale(V)
        self.med = np.nanmean(V, axis=0).astype(np.float32)  # mean-centred: zero average drift at baseline
        self.mad = np.maximum(scale, 0.25).astype(np.float32)
        self.state = {}
        self.q = np.ones(len(self.cols), dtype=np.float32)
        S_full = self._stat(F, spec, groups)
        self.q = per_feature_norm(_rows(S_full, mask), floor=0.1 * self.cap)
        self.state = {}
        self.fitted = True
        self.fit_seconds = time.time() - t0

    def reset(self) -> None:
        self.state = {}

    @staticmethod
    def _reflect(y: np.ndarray, s0: np.ndarray) -> np.ndarray:
        """S_t = max(0, S_{t-1} + y_t), S_0 = s0, vectorized: S = C - min(0, running min of C)."""
        C = np.vstack([s0[None, :], s0[None, :] + np.cumsum(y, axis=0)])
        m = np.minimum(np.minimum.accumulate(C, axis=0), 0.0)
        return (C - m)[1:]

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        S = self._stat(F, spec, groups) / self.q
        return S.max(axis=1).astype(np.float32), self._per_signal(S * S, spec)

    def _stat(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> np.ndarray:
        z = ((F[:, self.cols] - self.med) / self.mad).astype(np.float64)
        n, d = z.shape
        if groups is None:
            groups = np.zeros(n, dtype=object)
        S = np.empty((n, d), dtype=np.float32)
        for s, e in segments(groups):
            g = str(groups[s])
            sp, sm = self.state.get(g, (np.zeros(d), np.zeros(d)))
            Sp = self._reflect(z[s:e] - self.k, sp)
            Sm = self._reflect(-z[s:e] - self.k, sm)
            S[s:e] = np.maximum(Sp, Sm)
            self.state[g] = (np.minimum(Sp[-1], self.cap), np.minimum(Sm[-1], self.cap))
        if len(self.state) > 50_000:
            self.state = {}
        return np.minimum(S, self.cap)


class CorrBreakDetector(Detector):
    name = "corr_break"
    uses = ("value",)
    stateful = True  # EWMA of the peer residual per group: persistent relation breaks accumulate

    def __init__(self, seed: int = 0, clusters: Optional[dict[str, list[str]]] = None, roles: Optional[dict[str, str]] = None, max_peers: int = 8, min_abs_corr: float = 0.3, max_lag: int = 10, lam: float = 0.08):
        super().__init__(seed)
        self.clusters = clusters or {}
        self.roles = roles or {}
        self.max_peers = max_peers
        self.min_abs_corr = min_abs_corr
        self.max_lag = max_lag
        self.lam = lam
        self.cusum_k = 0.5
        self.cusum_cap = 30.0
        self.state: dict[str, np.ndarray] = {}
        self.cstate: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.rstate: dict[str, np.ndarray] = {}

    def reset(self) -> None:
        self.state = {}
        self.cstate = {}
        self.rstate = {}

    def _rstd(self, r: np.ndarray, groups: Optional[np.ndarray], window: int) -> np.ndarray:
        """log rolling std of the residual per group (carry-over of the last `window` rows per group makes
        chunked scoring exact). A residual whose spread grows while its mean stays zero is an oscillation or
        a noise increase that the level-based statistics cannot see."""
        from .features import RSTD_EPS, _rolling_mean_std

        n, p = r.shape
        if groups is None:
            groups = np.zeros(n, dtype=object)
        out = np.empty((n, p), dtype=np.float32)
        warm = max(3, window // 4)
        for s, e in segments(groups):
            g = str(groups[s])
            prev = self.rstate.get(g)
            seg = r[s:e] if prev is None else np.vstack([prev, r[s:e]])
            _, sd = _rolling_mean_std(seg, window)
            sd = sd[len(seg) - (e - s) :]
            if prev is None:
                sd[: min(warm, e - s)] = np.nan
            out[s:e] = sd
            self.rstate[g] = seg[-window:]
        if len(self.rstate) > 20_000:
            self.rstate = {}
        with np.errstate(invalid="ignore"):
            return np.log(np.maximum(out, 0.0) + RSTD_EPS)

    def _cusum(self, rn: np.ndarray, groups: Optional[np.ndarray]) -> np.ndarray:
        """Two-sided CUSUM of the normalized residual per group: a persistent relation break keeps
        accumulating while driver wander (zero-mean residual) does not."""
        n, p = rn.shape
        if groups is None:
            groups = np.zeros(n, dtype=object)
        S = np.empty((n, p), dtype=np.float32)
        for s, e in segments(groups):
            g = str(groups[s])
            sp, sm = self.cstate.get(g, (np.zeros(p), np.zeros(p)))
            x = rn[s:e].astype(np.float64)
            Sp = CUSUMDetector._reflect(x - self.cusum_k, sp)
            Sm = CUSUMDetector._reflect(-x - self.cusum_k, sm)
            S[s:e] = np.maximum(Sp, Sm)
            self.cstate[g] = (np.minimum(Sp[-1], self.cusum_cap), np.minimum(Sm[-1], self.cusum_cap))
        if len(self.cstate) > 50_000:
            self.cstate = {}
        return np.minimum(S, self.cusum_cap)

    def _ewma(self, r: np.ndarray, groups: Optional[np.ndarray]) -> np.ndarray:
        from scipy.signal import lfilter

        n, p = r.shape
        if groups is None:
            groups = np.zeros(n, dtype=object)
        out = np.empty_like(r)
        b, a = [self.lam], [1.0, -(1.0 - self.lam)]
        for s, e in segments(groups):
            g = str(groups[s])
            zi = self.state.get(g)
            if zi is None:
                zi = np.zeros((1, p), dtype=np.float64)
            y, zf = lfilter(b, a, r[s:e].astype(np.float64), axis=0, zi=zi)
            out[s:e] = y
            self.state[g] = zf
        if len(self.state) > 50_000:
            self.state = {}
        return out / float(np.sqrt(self.lam / (2 - self.lam)))

    def _peer_sets(self, C: np.ndarray, aliases: list[str], derived: np.ndarray) -> list[list[int]]:
        p = len(aliases)
        idx = {a: i for i, a in enumerate(aliases)}
        cluster_of: dict[int, list[int]] = {}
        for members in self.clusters.values():
            ids = [idx[m] for m in members if m in idx]
            for i in ids:
                cluster_of[i] = ids
        peers: list[list[int]] = []
        for j in range(p):
            cand = [i for i in cluster_of.get(j, []) if i != j and not derived[i] and abs(C[j, i]) >= 0.15]
            order = np.argsort(-np.abs(C[j]))
            for i in order:
                if i == j or derived[i] or i in cand:
                    continue
                if abs(C[j, i]) < self.min_abs_corr or len(cand) >= self.max_peers:
                    break
                cand.append(int(i))
            peers.append(cand[: self.max_peers])
        return peers

    def _best_lags(self, Vs: np.ndarray, groups_s: Optional[np.ndarray], peers: list[list[int]]) -> dict[tuple[int, int], int]:
        """Lag (in rows) of each peer i for signal j maximizing |corr(x_j[t], x_i[t - lag])|."""
        n, p = Vs.shape
        Vc = Vs - Vs.mean(axis=0)
        sd = Vc.std(axis=0)
        Vc = Vc / np.where(sd > 1e-9, sd, 1.0)
        lags = list(range(-self.max_lag, self.max_lag + 1))
        shifted = {L: shift_within(Vc, groups_s, L) for L in lags}
        out: dict[tuple[int, int], int] = {}
        for j, pj in enumerate(peers):
            for i in pj:
                base = abs(float(np.mean(Vc[:, j] * Vc[:, i])))
                best_L, best_r = 0, base
                for L in lags:
                    if L == 0:
                        continue
                    r = abs(float(np.mean(Vc[:, j] * shifted[L][:, i])))
                    if r > best_r + 0.02:
                        best_L, best_r = L, r
                out[(j, i)] = best_L
        return out

    def _design(self, V: np.ndarray, groups: Optional[np.ndarray]) -> dict[int, np.ndarray]:
        """Lag-shifted copies of V for every lag in use."""
        return {L: shift_within(V, groups, L) for L in self.lags_used}

    def _predict(self, V: np.ndarray, groups: Optional[np.ndarray]) -> np.ndarray:
        Vc = V - self.mean
        design = self._design(Vc, groups)
        pred = np.zeros_like(Vc)
        for L, W in self.W_by_lag.items():
            pred += design[L] @ W
        return pred

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        Vfull = F[:, spec.block_slice("value")].astype(np.float64)
        n_all, p = Vfull.shape
        mask = np.ones(n_all, dtype=bool) if mask is None else mask
        V = Vfull[mask]
        n = len(V)
        sd = V.std(axis=0)
        Vn = (V - V.mean(axis=0)) / np.where(sd > 1e-9, sd, 1.0)
        C = np.nan_to_num((Vn.T @ Vn) / max(1, n - 1))
        derived = np.array([self.roles.get(a, "") == "derived_redundant" for a in spec.aliases])
        peers = self._peer_sets(C, spec.aliases, derived)
        # lags estimated on a bounded contiguous prefix of the sample (masked rows only where possible)
        m_lag = min(n_all, 40_000)
        lag_map = self._best_lags(Vfull[:m_lag], None if groups is None else groups[:m_lag], peers)

        def fit_weights(peers_: list[list[int]]):
            lags_used = sorted({lag_map.get((j, i), 0) for j, pj in enumerate(peers_) for i in pj} | {0})
            mean = V.mean(axis=0)
            Vc_full = Vfull - mean
            design = {L: shift_within(Vc_full, groups, L)[mask] for L in lags_used}
            W_by_lag = {L: np.zeros((p, p), dtype=np.float64) for L in lags_used}
            sigma = np.ones(p)
            r2 = np.zeros(p)
            lam = 0.05 * n
            for j, pj in enumerate(peers_):
                if not pj:
                    sigma[j] = max(design[0][:, j].std(), 0.1)
                    continue
                cols = [design[lag_map.get((j, i), 0)][:, i] for i in pj]
                A = np.stack(cols, axis=1)
                G = A.T @ A + lam * np.eye(len(pj))
                w = np.linalg.solve(G, A.T @ design[0][:, j])
                for i, wi in zip(pj, w):
                    W_by_lag[lag_map.get((j, i), 0)][i, j] = wi
                res = design[0][:, j] - A @ w
                sigma[j] = np.sqrt(res.var())
                r2[j] = 1.0 - res.var() / (design[0][:, j].var() + 1e-12)
            return lags_used, mean, W_by_lag, sigma, r2

        lags_used, mean, W_by_lag, sigma, r2 = fit_weights(peers)
        newly = (r2 > 0.99) & ~derived  # near-exact functions of peers carry no independent information
        if newly.any():
            derived = derived | newly
            peers = self._peer_sets(C, spec.aliases, derived)
            lags_used, mean, W_by_lag, sigma, r2 = fit_weights(peers)
        self.lags_used = lags_used
        self.mean = mean.astype(np.float32)
        self.W_by_lag = {L: W.astype(np.float32) for L, W in W_by_lag.items()}
        self.sigma = np.maximum(sigma, 0.1).astype(np.float32)
        # a derived signal is a function of its parents: it carries no independent information, so it gets
        # no residual of its own (the deviation is attributed to the parents instead)
        self.has_peers = np.array([len(pj) > 0 and not derived[j] for j, pj in enumerate(peers)])
        self.peers = peers
        self.peer_lags = {f"{spec.aliases[j]}<-{spec.aliases[i]}": int(lag_map.get((j, i), 0)) for j, pj in enumerate(peers) for i in pj}
        self.r2 = r2.astype(np.float32)
        self.derived = derived
        self.q = np.ones(p, dtype=np.float32)
        self.q_e = np.ones(p, dtype=np.float32)
        self.window = int(spec.window)
        r_full = self.residuals(F, spec, groups)
        self.reset()
        e_full = self._ewma(r_full, groups)
        v_full = self._rstd(r_full, groups, self.window)
        self.q = per_feature_norm(np.abs(r_full[mask]), floor=2.5)
        self.q_e = per_feature_norm(np.abs(e_full[mask]), floor=2.5)
        # residual spread: centre and scale of log rolling std on baseline rows, then its own normal extreme
        vc, vs = feature_scale(v_full[mask], floor=1e-3)
        self.v_center = vc.astype(np.float32)
        self.v_scale = np.maximum(vs, 0.05).astype(np.float32)
        vz = np.nan_to_num((v_full - self.v_center) / self.v_scale, nan=0.0)
        self.q_v = per_feature_norm(np.abs(vz[mask]), floor=2.5)
        self.reset()
        self.fitted = True
        self.fit_seconds = time.time() - t0
        self.info = {"n_with_peers": int(self.has_peers.sum()), "median_r2": round(float(np.median(r2[self.has_peers])) if self.has_peers.any() else 0.0, 3), "derived": [spec.aliases[i] for i in np.flatnonzero(derived)], "lags_used": [int(L) for L in lags_used]}

    def residuals(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> np.ndarray:
        """Instantaneous standardized residual of every signal against its lag-aligned peers."""
        V = F[:, spec.block_slice("value")]
        Vc = V - self.mean
        r = (Vc - self._predict(V, groups)) / self.sigma
        r[:, ~self.has_peers] = 0.0
        return np.clip(r, -25, 25)

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        r = self.residuals(F, spec, groups)
        e = self._ewma(r, groups)
        # (a CUSUM of the residual was tried and rejected: groups carry persistent residual biases that
        # accumulate on normal rows; the EWMA keeps the persistence gain bounded)
        stat = np.maximum(np.abs(r) / self.q, np.abs(e) / self.q_e)
        return stat.max(axis=1).astype(np.float32), (stat * stat).astype(np.float32)

    def spread_score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        """Residual-spread statistic (used by ResidSpreadDetector): log rolling std of the peer residual,
        standardized per signal and normalized by its own normal extreme."""
        r = self.residuals(F, spec, groups)
        v = self._rstd(r, groups, self.window)
        vz = np.nan_to_num((v - self.v_center) / self.v_scale, nan=0.0)
        vz[:, ~self.has_peers] = 0.0
        stat = np.abs(vz) / self.q_v
        return stat.max(axis=1).astype(np.float32), (stat * stat).astype(np.float32)


class ResidSpreadDetector(Detector):
    """Oscillations and noise increases: the spread of a signal's peer residual grows while its mean stays
    zero, which level statistics cannot see. Shares the fitted regression of a CorrBreakDetector (must be
    fitted first) and is calibrated as a separate specialist."""

    name = "resid_spread"
    uses = ("value",)
    stateful = True

    def __init__(self, parent: "CorrBreakDetector", seed: int = 0):
        super().__init__(seed)
        self.parent = parent

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        t0 = time.time()
        if not self.parent.fitted:
            self.parent.fit(F, spec, groups, mask)
        self.fitted = True
        self.fit_seconds = time.time() - t0
        self.info = {"shares": "corr_break regression"}

    def reset(self) -> None:
        self.parent.rstate = {}

    @property
    def rstate(self) -> dict[str, np.ndarray]:
        return self.parent.rstate

    @rstate.setter
    def rstate(self, value: dict[str, np.ndarray]) -> None:
        self.parent.rstate = value

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        return self.parent.spread_score(F, spec, groups)


class IForestDetector(Detector):
    name = "iforest"
    uses = ("value", "rstd")

    def __init__(self, seed: int = 0, n_estimators: int = 100, max_fit: int = 20_000, n_jobs: int = 2):
        super().__init__(seed)
        self.n_estimators = n_estimators
        self.max_fit = max_fit
        self.n_jobs = n_jobs

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        from sklearn.ensemble import IsolationForest

        t0 = time.time()
        self.cols = self._cols(spec)
        X = _rows(F, mask)[:, self.cols]
        if len(X) > self.max_fit:
            rng = np.random.default_rng(self.seed)
            X = X[np.sort(rng.choice(len(X), self.max_fit, replace=False))]
        self.model = IsolationForest(n_estimators=self.n_estimators, max_samples=min(256, len(X)), random_state=self.seed, n_jobs=self.n_jobs).fit(X)
        s = -self.model.score_samples(X)
        self.offset = float(np.median(s))
        self.fitted = True
        self.fit_seconds = time.time() - t0
        self.info = {"n_fit": int(len(X)), "n_estimators": self.n_estimators}

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        X = F[:, self.cols]
        s = np.clip(-self.model.score_samples(X) - self.offset, 0, None).astype(np.float32)
        w = X * X
        share = w / np.maximum(w.sum(axis=1, keepdims=True), 1e-6)
        return s, self._per_signal(share * s[:, None], spec)


class AutoencoderDetector(Detector):
    name = "autoencoder"
    uses = ("value", "rstd")

    def __init__(self, seed: int = 0, max_rows: int = 60_000, max_iter: int = 80):
        super().__init__(seed)
        self.max_rows = max_rows
        self.max_iter = max_iter

    def fit(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None, mask: Optional[np.ndarray] = None) -> None:
        from sklearn.neural_network import MLPRegressor

        t0 = time.time()
        self.cols = self._cols(spec)
        X = _rows(F, mask)[:, self.cols]
        if len(X) > self.max_rows:
            rng = np.random.default_rng(self.seed)
            X = X[np.sort(rng.choice(len(X), self.max_rows, replace=False))]
        d = X.shape[1]
        h1 = max(8, min(64, d // 2))
        h2 = max(2, min(12, d // 8))
        self.model = MLPRegressor(hidden_layer_sizes=(h1, h2, h1), activation="tanh", solver="adam", max_iter=self.max_iter, early_stopping=len(X) > 200, validation_fraction=0.1, n_iter_no_change=6, random_state=self.seed, batch_size=min(256, len(X)), learning_rate_init=1e-3)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # ConvergenceWarning: capped iterations are intentional (time budget)
            self.model.fit(X, X)
        err = (self.model.predict(X) - X) ** 2
        self.err_scale = np.maximum(np.sqrt(err.mean(axis=0)), 0.05).astype(np.float32)
        self.fitted = True
        self.fit_seconds = time.time() - t0
        self.info = {"n_fit": int(len(X)), "hidden": [h1, h2, h1], "n_iter": int(getattr(self.model, "n_iter_", 0))}

    def score(self, F: np.ndarray, spec: FeatureSpec, groups: Optional[np.ndarray] = None) -> tuple[np.ndarray, np.ndarray]:
        X = F[:, self.cols]
        e = ((self.model.predict(X) - X) / self.err_scale) ** 2
        return e.mean(axis=1).astype(np.float32), self._per_signal(e, spec)


REGISTRY = {"pca": PCADetector, "robust_z": RobustZDetector, "ewma": EWMADetector, "cusum": CUSUMDetector, "corr_break": CorrBreakDetector, "iforest": IForestDetector, "autoencoder": AutoencoderDetector}
DERIVED_DETECTORS = {"resid_spread": "corr_break"}  # built automatically with their parent


def build_detectors(names: list[str], settings, clusters: Optional[dict[str, list[str]]] = None, roles: Optional[dict[str, str]] = None, seed: int = 0, n_jobs: int = 2) -> dict[str, Detector]:
    out: dict[str, Detector] = {}
    window = int(getattr(settings.detect, "window", 20))
    for name in names:
        if name not in REGISTRY:
            continue
        if name == "corr_break":
            out[name] = CorrBreakDetector(seed=seed, clusters=clusters, roles=roles, max_lag=max(2, min(10, window // 2)))
            # the residual-spread specialist rides on the same regression (fitted right after corr_break)
            out["resid_spread"] = ResidSpreadDetector(out[name], seed=seed)
        elif name == "autoencoder":
            out[name] = AutoencoderDetector(seed=seed, max_rows=int(settings.detect.autoencoder_max_rows))
        elif name == "iforest":
            out[name] = IForestDetector(seed=seed, n_jobs=n_jobs)
        else:
            out[name] = REGISTRY[name](seed=seed)
    return out
