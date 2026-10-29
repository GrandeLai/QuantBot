"""Chronos/TimesFM-style time-series model adapter."""

from ..factors.kronos import KronosFeatureExtractor


class ChronosAdapter:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def predict_features(self, returns_context, prediction_length: int = 5):
        forecast = self.pipeline.predict(_to_chronos_context(returns_context), prediction_length=prediction_length)
        return KronosFeatureExtractor(context_length=prediction_length).extract(_flatten_forecast(forecast))


class SelfTrainedTimeSeriesAdapter:
    """Adapter boundary for PatchTST/Transformer/LSTM-style forecasters."""

    def __init__(self, model):
        self.model = model

    def fit(self, feature_rows, targets):
        self.model.fit([_flatten_features(row["alpha158"], row["ohlcv"]) for row in feature_rows], list(targets))

    def predict_excess_return(self, alpha158_features, ohlcv) -> float:
        predictions = self.model.predict([_flatten_features(alpha158_features, ohlcv)])
        return float(predictions[0])


def _flatten_features(alpha158_features, ohlcv):
    output = {}
    for key, value in sorted(alpha158_features.items()):
        output["alpha158.%s" % key] = float(value)
    for key, value in sorted(ohlcv.items()):
        output["ohlcv.%s" % key] = float(value)
    return output


def _to_chronos_context(returns_context):
    try:
        import torch
    except ModuleNotFoundError:
        return returns_context
    if isinstance(returns_context, torch.Tensor):
        return returns_context
    return torch.tensor(list(returns_context), dtype=torch.float32)


def _flatten_forecast(forecast):
    if hasattr(forecast, "detach"):
        forecast = forecast.detach().cpu().reshape(-1).tolist()
    return list(_iter_scalars(forecast))


def _iter_scalars(value):
    if isinstance(value, (str, bytes)):
        yield float(value)
        return
    try:
        iterator = iter(value)
    except TypeError:
        yield float(value)
        return
    for item in iterator:
        yield from _iter_scalars(item)
