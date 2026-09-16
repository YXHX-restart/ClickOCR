"""贴图窗口（ResultOverlay）的职责拆分：五个 Qt Mixin。

按 docs/design/重构方案-App与UI大文件拆分.md §2 执行——QGraphicsView 的
所有状态共享同一个 scene，组合/委托会引入无谓跳转，故用 mixin（多继承、
零转发成本、行为不变）。`ui/windows/result_overlay.py` 只留事件分发中枢。
"""
