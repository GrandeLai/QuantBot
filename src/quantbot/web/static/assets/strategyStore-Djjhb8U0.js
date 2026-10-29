import{H as e}from"./vendor-misc-D4ZmGtM_.js";var t=`"""
在此编写您的策略代码.

可继承 BaseStrategy 并实现 on_bar 方法：
"""
from quantpilot.strategy.base import BaseStrategy, StrategyContext
from quantpilot.data.models import OHLCVBar


class MyStrategy(BaseStrategy):
    name = "我的策略"
    description = "策略描述"
    version = "1.0.0"
    default_params = {"period": 20}

    def on_bar(self, bar: OHLCVBar, context: StrategyContext) -> None:
        # 在此处理每根 K 线
        pass
`,n=e(e=>({strategies:[],templates:[],selectedId:null,editorCode:t,editorName:`新策略`,editorDescription:``,isDirty:!1,setStrategies:t=>e({strategies:t}),setTemplates:t=>e({templates:t}),selectStrategy:t=>e({selectedId:t}),setEditorCode:t=>e({editorCode:t,isDirty:!0}),setEditorName:t=>e({editorName:t,isDirty:!0}),setEditorDescription:t=>e({editorDescription:t,isDirty:!0}),markClean:()=>e({isDirty:!1})}));export{n as t};
//# sourceMappingURL=strategyStore-Djjhb8U0.js.map