"""Qlib integration boundary."""


class QlibAlpha158Adapter:
    def __init__(self, qlib_module, alpha158_factory):
        self.qlib_module = qlib_module
        self.alpha158_factory = alpha158_factory

    def compute(self, provider_uri: str, instruments: str, start_time: str, end_time: str):
        self.qlib_module.init(provider_uri=provider_uri, region="us")
        handler = self.alpha158_factory(
            instruments=instruments,
            start_time=start_time,
            end_time=end_time,
        )
        if hasattr(handler, "fetch"):
            return handler.fetch()
        return handler
