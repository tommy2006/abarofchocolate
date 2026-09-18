"""Per-group windowed features computed on chunks.

Blocks (each of width p = number of signals, in signal order):
    value  robust z of the raw value (median / MAD from the baseline)
    rmean  rolling mean of z over `window` rows within the group
    rstd   rolling std of z over `window` rows within the group
    d1     first difference of z (rate of change)
    d2     second difference of z (acceleration)
Each block is standardized with a robust center/scale estimated on baseline rows and clipped, so a
detector sees comparable magnitudes. `feature_signal[i]` maps feature i to its signal index, which is how
per-feature contributions are attributed back to signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ._common import robust_scale, segments

BLOCKS = ("value", "rmean", "rstd", "d1", "d2")
RSTD_EPS = 0.02  # in robust-sigma units; log(rstd + eps) makes variance changes symmetric and a frozen signal extreme


def block_transform(name: str, b: np.ndarray) -> np.ndarray:
    """Variance-stabilizing transform applied before standardization."""
    if name == "rstd":
        with np.errstate(invalid="ignore"):
            return np.log(np.maximum(b, 0.0) + RSTD_EPS)  # NaN (warm-up rows) stays NaN
    return b


def _rolling_mean_std(z: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling mean/std with min_periods=1 over axis 0 of one contiguous segment (float64 accumulation)."""
    n, p = z.shape
    z64 = z.astype(np.float64)
    c1 = np.vstack([np.zeros((1, p)), np.cumsum(z64, axis=0)])
    c2 = np.vstack([np.zeros((1, p)), np.cumsum(z64 * z64, axis=0)])
    idx = np.arange(1, n + 1)
    lo = np.maximum(0, idx - w)
    cnt = (idx - lo)[:, None].astype(np.float64)
    m = (c1[idx] - c1[lo]) / cnt
    v = (c2[idx] - c2[lo]) / cnt - m * m
    sd = np.sqrt(np.maximum(v, 0.0))
    return m.astype(np.float32), sd.astype(np.float32)


def raw_blocks(z: np.ndarray, groups: np.ndarray, window: int) -> dict[str, np.ndarray]:
    """Compute the unstandardized blocks from z (n, p) within contiguous group segments. The rolling std of
    the first rows of a segment (fewer than max(3, window // 4) samples) is undefined (NaN -> neutral after
    standardization) so a group start never looks like a frozen sensor."""
    n, p = z.shape
    rmean = np.empty_like(z)
    rstd = np.empty_like(z)
    d1 = np.zeros_like(z)
    d2 = np.zeros_like(z)
    warm = max(3, window // 4)
    for s, e in segments(groups):
        seg = z[s:e]
        m, sd = _rolling_mean_std(seg, window)
        sd[: min(warm, e - s)] = np.nan
        rmean[s:e] = m
        rstd[s:e] = sd
        if e - s > 1:
            dd = np.diff(seg, axis=0)
            d1[s + 1 : e] = dd
            if e - s > 2:
                d2[s + 2 : e] = np.diff(dd, axis=0)
    return {"value": z, "rmean": rmean, "rstd": rstd, "d1": d1, "d2": d2}


@dataclass
class FeatureSpec:
    window: int
    aliases: list[str]
    center: np.ndarray  # (p,) raw median
    scale: np.ndarray  # (p,) raw robust scale
    block_center: dict[str, np.ndarray] = field(default_factory=dict)
    block_scale: dict[str, np.ndarray] = field(default_factory=dict)
    clip: float = 10.0
    blocks: tuple[str, ...] = BLOCKS

    @property
    def p(self) -> int:
        return len(self.aliases)

    @property
    def n_features(self) -> int:
        return self.p * len(self.blocks)

    @property
    def feature_signal(self) -> np.ndarray:
        return np.tile(np.arange(self.p), len(self.blocks))

    def block_slice(self, name: str) -> slice:
        i = self.blocks.index(name)
        return slice(i * self.p, (i + 1) * self.p)

    def zscore(self, X: np.ndarray) -> np.ndarray:
        z = (X.astype(np.float32) - self.center.astype(np.float32)) / self.scale.astype(np.float32)
        z = np.nan_to_num(z, nan=0.0, posinf=self.clip, neginf=-self.clip)
        return np.clip(z, -self.clip, self.clip)

    def transform(self, X: np.ndarray, groups: np.ndarray) -> np.ndarray:
        """(n, p) raw values -> (n, n_features) standardized features (float32)."""
        z = self.zscore(X)
        blocks = raw_blocks(z, groups, self.window)
        out = np.empty((X.shape[0], self.n_features), dtype=np.float32)
        for name in self.blocks:
            b = blocks[name]
            if name != "value":
                b = (block_transform(name, b) - self.block_center[name]) / self.block_scale[name]
            out[:, self.block_slice(name)] = np.clip(np.nan_to_num(b, nan=0.0), -self.clip, self.clip)
        return out

    def raw_rstd(self, X: np.ndarray, groups: np.ndarray) -> np.ndarray:
        """Unstandardized rolling std of z (used to detect stuck signals: exactly 0)."""
        z = self.zscore(X)
        return raw_blocks(z, groups, self.window)["rstd"]

    def to_dict(self) -> dict[str, Any]:
        return {"window": self.window, "aliases": self.aliases, "center": self.center.tolist(), "scale": self.scale.tolist(), "block_center": {k: v.tolist() for k, v in self.block_center.items()}, "block_scale": {k: v.tolist() for k, v in self.block_scale.items()}, "clip": self.clip, "blocks": list(self.blocks)}


def feature_scale(X: np.ndarray, floor: float = 1e-6, spike_z: float = 5.0) -> tuple[np.ndarray, np.ndarray]:
    """Per-column (median, scale) for standardization. The scale is the MAD, but never smaller than half
    the std nor than q99(|x - median|)/spike_z: piecewise-constant or sample-and-hold signals have a MAD of
    zero on their difference/spread features while their perfectly normal steps would otherwise become
    10-sigma spikes that dominate every max-type statistic. With this rule the normal extreme of every
    feature lands near `spike_z`, while Gaussian-like features keep their MAD scale (q99/5 ~ 0.5 sigma)."""
    Xf = np.where(np.isfinite(X), X, np.nan).astype(np.float64)
    med = np.nan_to_num(np.nanmedian(Xf, axis=0), nan=0.0)
    dev = np.abs(Xf - med)
    mad = np.nanmedian(dev, axis=0) * 1.4826
    std = np.nanstd(Xf, axis=0)
    q99 = np.nanquantile(dev, 0.99, axis=0)
    scale = np.maximum.reduce([np.nan_to_num(mad), 0.5 * np.nan_to_num(std), np.nan_to_num(q99) / spike_z, np.full(X.shape[1], floor)])
    return med, scale


def fit_feature_spec(X: np.ndarray, groups: np.ndarray, aliases: list[str], window: int, baseline_mask: Optional[np.ndarray] = None, clip: float = 10.0) -> FeatureSpec:
    """Estimate robust centers/scales on baseline rows (mask) of an ordered sample (X, groups)."""
    mask = np.ones(len(X), dtype=bool) if baseline_mask is None else baseline_mask.astype(bool)
    if mask.sum() < 10:
        mask = np.ones(len(X), dtype=bool)
    center, scale = feature_scale(X[mask])
    spec = FeatureSpec(window=window, aliases=list(aliases), center=center.astype(np.float32), scale=scale.astype(np.float32), clip=clip)
    z = spec.zscore(X)
    blocks = raw_blocks(z, groups, window)
    for name in BLOCKS:
        if name == "value":
            continue
        c, s = feature_scale(block_transform(name, blocks[name][mask]), floor=1e-3)
        if name in ("d1", "d2", "rstd"):
            s = np.maximum(s, 0.05)
        spec.block_center[name] = c.astype(np.float32)
        spec.block_scale[name] = s.astype(np.float32)
    return spec


def per_signal(contrib_feat: np.ndarray, spec: FeatureSpec) -> np.ndarray:
    """(n, n_features) feature contributions -> (n, p) signal contributions (sum over blocks)."""
    n = contrib_feat.shape[0]
    return contrib_feat.reshape(n, len(spec.blocks), spec.p).sum(axis=1)
