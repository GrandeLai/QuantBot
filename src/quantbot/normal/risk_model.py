"""Small deterministic covariance and linear-algebra helpers."""

from __future__ import annotations

from math import fsum, sqrt


def covariance_matrix(histories, names, shrinkage):
    names = tuple(names)
    centered = {}
    for name in names:
        values = tuple(histories[name])
        mean = fsum(values) / len(values)
        centered[name] = tuple(value - mean for value in values)
    denominator = len(next(iter(centered.values()))) - 1
    matrix = []
    for left in names:
        row = []
        for right in names:
            covariance = fsum(
                x * y for x, y in zip(centered[left], centered[right])
            ) / denominator
            if left != right:
                covariance *= 1.0 - shrinkage
            row.append(covariance)
        matrix.append(tuple(row))
    return tuple(matrix)


def correlation_matrix(histories, names, shrinkage):
    covariance = covariance_matrix(histories, names, 0.0)
    volatilities = tuple(sqrt(max(covariance[index][index], 0.0)) for index in range(len(names)))
    result = []
    for row_index in range(len(names)):
        row = []
        for column_index in range(len(names)):
            if row_index == column_index:
                value = 1.0
            elif volatilities[row_index] == 0 or volatilities[column_index] == 0:
                value = 0.0
            else:
                value = (
                    covariance[row_index][column_index]
                    / volatilities[row_index]
                    / volatilities[column_index]
                    * (1.0 - shrinkage)
                )
            row.append(value)
        result.append(tuple(row))
    return tuple(result), volatilities


def solve(matrix, vector):
    size = len(vector)
    augmented = [list(matrix[index]) + [float(vector[index])] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-15:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            multiplier = augmented[row][column]
            augmented[row] = [
                left - multiplier * right
                for left, right in zip(augmented[row], augmented[column])
            ]
    return tuple(augmented[index][-1] for index in range(size))


def portfolio_volatility(weights, names, covariance, periods_per_year):
    variance = fsum(
        weights.get(left, 0.0) * weights.get(right, 0.0) * covariance[i][j]
        for i, left in enumerate(names)
        for j, right in enumerate(names)
    )
    return sqrt(max(variance, 0.0) * periods_per_year)
