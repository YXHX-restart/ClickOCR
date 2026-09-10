"""appmod —— ClickOCR 应用主体 App 的拆分实现（workers + 各职责 Mixin）。

`app.py` 里的 App 多继承这些 Mixin；方法体通过 self 共享状态，
本包内的模块**不得**反向 import app（会成环），依赖一律走 self / 构造注入。
"""
