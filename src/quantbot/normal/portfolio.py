"""Constrained normal-trading portfolio construction."""

from __future__ import annotations

from math import fsum, sqrt

from ..regime import Regime
from ..trading import TargetPortfolio
from .domain import (
    NormalAllocationReport,
    NormalAllocationRequest,
    NormalPortfolioConfig,
)
from .risk_model import correlation_matrix, covariance_matrix, portfolio_volatility, solve


REGIME_EXPOSURE = {
    Regime.R1_BULL_LOW_VOL: 1.0,
    Regime.R2_BULL_HIGH_VOL: 0.80,
    Regime.R3_SIDEWAYS: 0.65,
    Regime.R4_BEAR: 0.30,
    Regime.R5_CRISIS: 0.05,
}


class NormalPortfolioAllocator:
    def __init__(self, config: NormalPortfolioConfig | None = None):
        self.config = config or NormalPortfolioConfig()

    def allocate(self, request: NormalAllocationRequest) -> NormalAllocationReport:
        if not isinstance(request, NormalAllocationRequest):
            raise TypeError("request must be a NormalAllocationRequest")
        sleeve_weights = self._sleeve_weights(request)
        combined_scores = self._combined_scores(request, sleeve_weights)
        exclusions = self._exclusions(request)
        eligible = tuple(
            security_id
            for security_id in request.security_master.security_ids
            if not any(item.startswith(security_id + ":") for item in exclusions)
            and security_id in combined_scores
        )
        ranked = tuple(sorted(eligible, key=lambda key: (-combined_scores[key], key)))
        if not ranked or self._has_no_signal(combined_scores, ranked):
            return self._empty_report(request, sleeve_weights, combined_scores, exclusions)
        selected = self._select_with_hysteresis(ranked, request.current_weights)
        desired = self._asset_risk_weights(request, selected, combined_scores)
        exposure = self._target_exposure(request)
        desired = {key: value * exposure for key, value in desired.items()}
        # The construction pass may expand back to the permitted exposure; the
        # enforcement pass after smoothing may only shrink, otherwise it would
        # undo the turnover control and re-trade the whole book every session.
        constrained = self._apply_constraints(desired, request, exposure, expand=True)
        smoothed = self._smooth(constrained, request.current_weights)
        smoothed = {key: value for key, value in smoothed.items() if key in selected}
        final = self._apply_constraints(smoothed, request, exposure)
        final = {key: value for key, value in sorted(final.items()) if value > 1e-15}
        annual_volatility = self._annual_volatility(final, request)
        turnover = 0.5 * fsum(
            abs(final.get(key, 0.0) - request.current_weights.get(key, 0.0))
            for key in set(final).union(request.current_weights)
        )
        return NormalAllocationReport(
            targets=TargetPortfolio(request.security_master.as_of, final),
            sleeve_weights=sleeve_weights,
            combined_scores=combined_scores,
            selected_security_ids=selected,
            expected_turnover=turnover,
            annualized_volatility=annual_volatility,
            exclusions=exclusions,
            target_exposure=exposure,
            capital_utilization=(fsum(final.values()) / exposure) if exposure > 0 else 0.0,
            risk_utilization=annual_volatility / self.config.target_volatility_annual,
        )

    def _sleeve_weights(self, request):
        names = tuple(sorted(request.sleeve_confidences))
        confidence = []
        for name in names:
            value = request.sleeve_confidences[name]
            if request.regime.high_volatility_rebound and name.startswith("residual_momentum"):
                value *= self.config.momentum_rebound_multiplier
            confidence.append(value)
        correlation, volatilities = correlation_matrix(
            request.sleeve_return_history,
            names,
            self.config.covariance_shrinkage,
        )
        decorrelated = solve(correlation, confidence)
        raw = {}
        if decorrelated is not None:
            raw = {
                name: max(decorrelated[index], 0.0) / volatilities[index]
                for index, name in enumerate(names)
                if volatilities[index] > 0
            }
        if not raw or fsum(raw.values()) <= 0:
            raw = {
                name: confidence[index] / volatilities[index]
                for index, name in enumerate(names)
                if volatilities[index] > 0
            }
        total = fsum(raw.values())
        if total <= 0:
            raise ValueError("sleeve histories contain no estimable risk")
        return {name: raw.get(name, 0.0) / total for name in names}

    @staticmethod
    def _combined_scores(request, sleeve_weights):
        standardized = {
            sleeve.name: _zscore(sleeve.scores)
            for sleeve in request.sleeves
        }
        result = {}
        for security_id in request.security_master.security_ids:
            available = [
                (sleeve_weights[sleeve.name], standardized[sleeve.name][security_id])
                for sleeve in request.sleeves
                if security_id in standardized[sleeve.name] and sleeve_weights[sleeve.name] > 0
            ]
            total_weight = fsum(weight for weight, _score in available)
            if total_weight > 0:
                result[security_id] = fsum(weight * score for weight, score in available) / total_weight
        return result

    @staticmethod
    def _exclusions(request):
        reasons = []
        for security_id in request.security_master.security_ids:
            if security_id not in request.asset_return_history:
                reasons.append(f"{security_id}:missing_asset_return_history")
            if security_id not in request.market_betas:
                reasons.append(f"{security_id}:missing_market_beta")
            if security_id not in request.adv_dollars or request.adv_dollars.get(security_id, 0.0) <= 0:
                reasons.append(f"{security_id}:missing_adv")
        return tuple(sorted(reasons))

    @staticmethod
    def _has_no_signal(scores, ranked):
        values = tuple(scores[key] for key in ranked)
        return max(values) - min(values) <= max(max(abs(value) for value in values), 1.0) * 1e-12

    def _select_with_hysteresis(self, ranked, current_weights):
        rank = {security_id: index + 1 for index, security_id in enumerate(ranked)}
        held = sorted(
            (
                security_id
                for security_id, weight in current_weights.items()
                if weight > 0
                and security_id in rank
                and rank[security_id] <= self.config.top_n + self.config.exit_rank_buffer
            ),
            key=lambda key: rank[key],
        )
        selected = held[: self.config.top_n]
        for security_id in ranked:
            if len(selected) >= self.config.top_n:
                break
            if security_id not in selected:
                selected.append(security_id)
        return tuple(selected)

    def _asset_risk_weights(self, request, selected, scores):
        covariance = covariance_matrix(
            request.asset_return_history,
            selected,
            self.config.covariance_shrinkage,
        )
        minimum = min(scores[key] for key in selected)
        alpha = tuple(scores[key] - minimum + 1.0 / len(selected) for key in selected)
        solved = solve(covariance, alpha)
        raw = {}
        if solved is not None:
            raw = {key: max(solved[index], 0.0) for index, key in enumerate(selected)}
        risk_scaled = {
            key: alpha[index] / sqrt(max(covariance[index][index], 1e-18))
            for index, key in enumerate(selected)
        }
        if not raw or fsum(raw.values()) <= 0:
            raw = risk_scaled
        elif self._underfunded(raw, selected):
            # The long-only clip on an unconstrained mean-variance solve can
            # zero out most of the selection, which silently shrinks the book
            # to a handful of names.  Fall back to the risk-scaled alpha, which
            # funds every selected name, instead of dropping the rest.
            raw = risk_scaled
        total = fsum(raw.values())
        return {key: value / total for key, value in raw.items() if value > 0}

    def _underfunded(self, raw, selected):
        required = min(self.config.min_funded_names, len(selected))
        if required <= 0:
            return False
        return sum(1 for value in raw.values() if value > 0) < required

    def _target_exposure(self, request):
        confidence_scale = 0.5 + 0.5 * request.regime.confidence
        return (
            self.config.target_gross_exposure
            * REGIME_EXPOSURE[request.regime.regime]
            * confidence_scale
        )

    def _apply_constraints(self, weights, request, exposure, *, expand=False):
        caps = self._name_caps(weights, request)
        result = {
            security_id: min(max(float(weight), 0.0), caps[security_id])
            for security_id, weight in weights.items()
        }
        result = self._apply_sector_caps(result, request)
        if expand and self.config.redistribute_constrained_capital:
            result = self._redistribute_to_exposure(result, request, exposure, caps)
        beta = fsum(result[key] * request.market_betas[key] for key in result)
        if abs(beta) > self.config.max_market_beta_exposure:
            scale = self.config.max_market_beta_exposure / abs(beta)
            result = {key: value * scale for key, value in result.items()}
        gross = fsum(result.values())
        if gross > exposure and gross > 0:
            result = {key: value * exposure / gross for key, value in result.items()}
        return self._apply_volatility_target(
            result, request, exposure, caps, allow_scale_up=expand
        )

    def _name_caps(self, security_ids, request):
        """Per-name upper bound from the single-stock and liquidity limits."""

        return {
            security_id: min(
                self.config.max_single_stock_weight,
                request.adv_dollars[security_id]
                * self.config.max_adv_participation
                / request.capital,
            )
            for security_id in security_ids
        }

    def _apply_sector_caps(self, weights, request):
        result = dict(weights)
        for security_ids in self._sector_groups(result, request).values():
            total = fsum(result[key] for key in security_ids)
            if total > self.config.max_sector_weight:
                scale = self.config.max_sector_weight / total
                for key in security_ids:
                    result[key] *= scale
        return result

    @staticmethod
    def _sector_groups(weights, request):
        groups = {}
        for security_id in weights:
            sector = request.security_master.classification_by_security_id[security_id].sector
            groups.setdefault(sector, []).append(security_id)
        return groups

    def _redistribute_to_exposure(self, weights, request, exposure, caps):
        """Re-deploy capital freed by the single-stock and sector caps.

        Truncating a weight at its cap does not reduce the intended risk
        budget, it just moves that budget to cash.  Without redistribution a
        20-name book with a 5% single-name cap and a sparse mean-variance
        solution funds roughly half the selection and idles the remainder, so
        the strategy earns a fraction of whatever its signal is worth.  Each
        pass hands the shortfall to the names that still have headroom, then
        re-applies the sector caps, so no limit is ever exceeded.
        """

        if not weights or exposure <= 0:
            return dict(weights)
        tolerance = max(exposure, 1.0) * 1e-12
        result = dict(weights)
        for _ in range(64):
            gross = fsum(result.values())
            shortfall = exposure - gross
            if shortfall <= tolerance:
                break
            sector_totals = {
                sector: fsum(result[key] for key in security_ids)
                for sector, security_ids in self._sector_groups(result, request).items()
            }
            sector_by_id = {
                security_id: request.security_master.classification_by_security_id[
                    security_id
                ].sector
                for security_id in result
            }
            headroom = {}
            for security_id, weight in result.items():
                sector_room = (
                    self.config.max_sector_weight - sector_totals[sector_by_id[security_id]]
                )
                headroom[security_id] = max(
                    min(caps[security_id] - weight, sector_room), 0.0
                )
            total_headroom = fsum(headroom.values())
            if total_headroom <= tolerance:
                break
            added = min(shortfall, total_headroom)
            candidate = {
                security_id: weight + added * headroom[security_id] / total_headroom
                for security_id, weight in result.items()
            }
            candidate = self._apply_sector_caps(candidate, request)
            if fsum(candidate.values()) <= gross + tolerance:
                return candidate
            result = candidate
        return result

    def _apply_volatility_target(self, weights, request, exposure, caps, *, allow_scale_up):
        target = self.config.target_volatility_annual
        volatility = self._annual_volatility(weights, request)
        if volatility > target:
            scale = target / volatility
            return {key: value * scale for key, value in weights.items()}
        if (
            not allow_scale_up
            or self.config.volatility_targeting != "two_sided"
            or volatility <= 0
            or not weights
        ):
            return dict(weights)
        # Below-target risk is unused risk budget.  Scale up, but never past
        # the long-only exposure ceiling or any per-name or sector cap, so
        # this can only convert idle cash into the intended exposure.
        scale = target / volatility
        scaled = {
            key: min(value * scale, caps[key]) for key, value in weights.items()
        }
        scaled = self._apply_sector_caps(scaled, request)
        gross = fsum(scaled.values())
        if gross > exposure and gross > 0:
            scaled = {key: value * exposure / gross for key, value in scaled.items()}
        beta = fsum(scaled[key] * request.market_betas[key] for key in scaled)
        if abs(beta) > self.config.max_market_beta_exposure:
            beta_scale = self.config.max_market_beta_exposure / abs(beta)
            scaled = {key: value * beta_scale for key, value in scaled.items()}
        if self._annual_volatility(scaled, request) > target:
            return dict(weights)
        return scaled

    def _smooth(self, desired, current):
        result = {}
        divisor = 1.0 + self.config.turnover_penalty
        for security_id in set(desired).union(current):
            previous = current.get(security_id, 0.0)
            target = desired.get(security_id, 0.0)
            blended = previous + (target - previous) / divisor
            result[security_id] = (
                previous
                if abs(blended - previous) < self.config.no_trade_band
                else blended
            )
        return result

    def _annual_volatility(self, weights, request):
        names = tuple(sorted(weights))
        if not names:
            return 0.0
        covariance = covariance_matrix(
            request.asset_return_history,
            names,
            self.config.covariance_shrinkage,
        )
        return portfolio_volatility(
            weights,
            names,
            covariance,
            self.config.periods_per_year,
        )

    @staticmethod
    def _empty_report(request, sleeve_weights, combined_scores, exclusions):
        return NormalAllocationReport(
            targets=TargetPortfolio(request.security_master.as_of, {}),
            sleeve_weights=sleeve_weights,
            combined_scores=combined_scores,
            selected_security_ids=(),
            expected_turnover=0.5 * fsum(request.current_weights.values()),
            annualized_volatility=0.0,
            exclusions=tuple(exclusions) + ("portfolio:no_cross_sectional_signal",),
            target_exposure=0.0,
            capital_utilization=0.0,
            risk_utilization=0.0,
        )


def _zscore(scores):
    if not scores:
        return {}
    mean = fsum(scores.values()) / len(scores)
    centered = {key: value - mean for key, value in scores.items()}
    variance = fsum(value * value for value in centered.values()) / len(centered)
    scale = sqrt(max(variance, 0.0))
    if scale <= max(max((abs(value) for value in scores.values()), default=0.0), 1.0) * 1e-12:
        return {key: 0.0 for key in scores}
    return {key: value / scale for key, value in centered.items()}
