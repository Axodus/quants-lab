"""Statistical diagnostics, moments, confidence intervals, and degradation measures."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence

from core.quant_simulation.models import text
from .models import StatisticalDiagnostics


class StatisticalAnalyzer:
    """Non-parametric and sample moment diagnostics across validation folds."""

    @staticmethod
    def analyze_series(
        metric_name: str,
        values: Sequence[Decimal],
        *,
        annualization_factor: int = 252,
    ) -> StatisticalDiagnostics:
        if not values:
            return StatisticalDiagnostics(
                metric_name=metric_name,
                mean="0",
                std_dev="0",
                median="0",
                positive_fold_ratio="0",
                annualized_sharpe_estimate=None,
                confidence_interval_95=None,
                sample_count=0,
            )

        sorted_vals = sorted(values)
        n = len(sorted_vals)
        mean_val = sum(sorted_vals) / Decimal(n)

        # Sample variance / std dev
        if n > 1:
            variance = sum((x - mean_val) ** 2 for x in sorted_vals) / Decimal(n - 1)
            std_val = Decimal(str(math.sqrt(float(variance))))
        else:
            std_val = Decimal("0")

        median_val = sorted_vals[n // 2]
        pos_count = sum(1 for x in sorted_vals if x > 0)
        pos_ratio = Decimal(pos_count) / Decimal(n)

        # Sharpe estimate: (mean / std) * sqrt(annualization_factor)
        sharpe: str | None = None
        if std_val > 0 and n >= 3:
            raw_sharpe = (mean_val / std_val) * Decimal(str(math.sqrt(annualization_factor)))
            sharpe = text(raw_sharpe)

        # 95% Confidence Interval using standard error
        ci_95: tuple[str, str] | None = None
        if n >= 3 and std_val > 0:
            stderr = std_val / Decimal(str(math.sqrt(n)))
            z_95 = Decimal("1.96")
            ci_lower = mean_val - (z_95 * stderr)
            ci_upper = mean_val + (z_95 * stderr)
            ci_95 = (text(ci_lower), text(ci_upper))

        return StatisticalDiagnostics(
            metric_name=metric_name,
            mean=text(mean_val),
            std_dev=text(std_val),
            median=text(median_val),
            positive_fold_ratio=text(pos_ratio),
            annualized_sharpe_estimate=sharpe,
            confidence_interval_95=ci_95,
            sample_count=n,
        )

    @staticmethod
    def compute_degradation(is_val: Decimal, oos_val: Decimal) -> str | None:
        """Compute percentage degradation from In-Sample to Out-of-Sample."""
        if is_val == 0:
            return None
        degradation = ((is_val - oos_val) / abs(is_val)) * Decimal("100")
        return text(degradation)
