"""Streamlit dashboard rendering adapter."""


class StreamlitDashboardRenderer:
    def __init__(self, streamlit_module):
        self.st = streamlit_module

    def render(self, snapshot) -> None:
        self.st.title("StockBot Dashboard")
        self.st.metric("Regime", snapshot["regime"])
        self.st.metric("Gross Exposure", "%.2f%%" % (snapshot["gross_exposure"] * 100))
        self.st.write({"performance": snapshot.get("performance", {})})
        self.st.write({"factor_exposure": snapshot.get("factor_exposure", {})})
        self.st.write({"top_positions": snapshot.get("top_positions", [])})
