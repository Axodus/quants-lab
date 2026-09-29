"""Canonical research optimization. Never grants deployment authority."""
from .models import OptimizationSpec, OptimizationTrial, OptimizationStudy, ParameterCandidate, Objective, Constraint, Window, DatasetRef
from .search_space import SearchSpace, IntegerParameter, DecimalParameter, CategoricalParameter, BooleanParameter
