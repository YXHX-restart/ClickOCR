"""SoftwareTabMixin —— 「软件设置」页（历史保留/路径/模型档位/GPU 探测）。

从 settings_panel.py 拆出（2026-09-11 重构 S2），纯搬移不改行为。
方法体里用到的 self.*（_cfg/_hotkey/_scroll_page/_form/_notify 等）都
由混入后的 SettingsPanel 提供，本 mixin 不单独实例化。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core_engine.logging_setup import get_logger

log = get_logger("settings")

from PySide6.QtCore import Qt, Signal, QSize, QUrl
from PySide6.QtGui import QKeyEvent, QDesktopServices
from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QVBoxLayout,
                               QTabWidget, QWidget, QCheckBox, QComboBox,
                               QLineEdit, QSpinBox, QLabel, QPushButton,
                               QApplication, QFileDialog, QScrollArea, QFrame,
                               QSlider)

from config import ConfigManager
from ui.base.card_window import CardWindow
from ui.base.texts import T
from ui.base.theme import apply_theme, resolve_theme
from ui.base.theme_dialog import ThemeDialog
from ui.base.widgets import (ThemeSpinBox, ColorButton as _ColorButton,
                             FitComboBox)
from ui.windows.settings.widgets import _ModBox, _ShortcutRowEdit, KeyCaptureEdit


class SoftwareTabMixin:
    # ---- Tab 1：软件设置 ----
    def _build_software_tab(self) -> QWidget:
        page, scroll, inner = self._scroll_page()
        w = inner
        form = self._form(w)

        # 历史保留条数（默认 50，不设上限）
        self.spin_capacity = ThemeSpinBox(1, 3000,   # 上限 3000 张
                                          self._cfg.getint(
                                              "software", "history_max_count", 50),
                                          width=56)
        form.addRow(T("history.count", "历史数量"), self.spin_capacity)
        self._attach_hint(
            form, self.spin_capacity,
            T("ui.hist_count_hint", "默认 50 条；条数越多占用磁盘越大（超出时自动清理最旧记录）。"))

        # 历史图片位置
        self.edit_shot_dir = QLineEdit(
            self._cfg.get("software", "screenshots_dir", ""))
        self.edit_shot_dir.setPlaceholderText(T("ui.shot_dir_ph", "留空 = 软件目录下 screenshots/"))
        b_browse = QPushButton(T("btn.browse", "浏览…"))
        b_browse.clicked.connect(self._pick_shot_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.edit_shot_dir, 1)
        dir_row.addWidget(b_browse)
        form.addRow(T("history.file_loc", "文件位置"), dir_row)
        self._attach_hint(
            form, dir_row,
            T("ui.shot_dir_hint", "截图与缩略图保存目录；可填绝对路径或相对软件根的路径。"))

        # 截图贴图最大数量（默认 1，打开下一个销毁最旧）
        self.spin_max_shot = ThemeSpinBox(
            1, 10, self._cfg.getint("software", "max_history_windows", 1), width=56)
        form.addRow(T("history.shot_count", "截图贴图"), self.spin_max_shot)

        # 历史贴图最大数量（从历史记录恢复的图片窗口，默认 3）
        self.spin_max_hist = ThemeSpinBox(
            1, 10, self._cfg.getint("software", "max_hist_windows", 3), width=56)
        form.addRow(T("history.hist_count", "历史贴图"), self.spin_max_hist)
        self._attach_hint(
            form, self.spin_max_hist,
            T("ui.hist_restore_hint",
              "历史贴图 = 从历史记录恢复打开的图片窗口；超出上限时自动关闭最旧的历史贴图。"))

        # 钉图数量上限（默认 3，上限 20；钉图不占贴图数量）
        self.spin_max_pin = ThemeSpinBox(
            1, 20, self._cfg.getint("software", "max_pin_windows", 3), width=56)
        form.addRow(T("history.pin_count", "钉图数量"), self.spin_max_pin)
        self._attach_hint(
            form, self.spin_max_pin,
            T("ui.pin_hint", "钉图把截图/历史图片置顶显示，不占用贴图数量；超出上限时自动移除最旧的钉图。"))

        # 复制模式（本期仅存配置，行为不变）
        self.combo_selection = FitComboBox()
        self.combo_selection.addItem(T("ui.copy_mode_auto", "auto_commit（选中即复制，现行为）"), "auto_commit")
        self.combo_selection.addItem(T("ui.copy_mode_manual", "manual_copy（手动 Ctrl+C，本期仅预留）"), "manual_copy")
        cur2 = self._cfg.get("software", "selection_mode", "auto_commit")
        i2 = self.combo_selection.findData(cur2)
        self.combo_selection.setCurrentIndex(i2 if i2 >= 0 else 0)
        form.addRow(T("history.copy_mode", "复制模式"), self.combo_selection)
        self._attach_hint(
            form, self.combo_selection,
            T("ui.copy_mode_hint", "提示：复制模式本期仅保存配置，行为暂不变（M2 计划 §5.2）。"))

        # ---- 图片保存格式（PNG/JPG/BMP/WebP）+ 质量（有损格式 1-100）----
        self.combo_save_fmt = FitComboBox()
        for label, data in ((T("ui.save_fmt_png", "PNG（无损）"), "png"),
                            (T("ui.save_fmt_jpg", "JPG（有损·小体积）"), "jpg"),
                            (T("ui.save_fmt_bmp", "BMP（无损）"), "bmp"),
                            (T("ui.save_fmt_webp", "WebP（有损·小体积）"), "webp")):
            self.combo_save_fmt.addItem(label, data)
        cur_fmt = self._cfg.get("software", "save_format", "png").lower()
        if cur_fmt not in ("png", "jpg", "bmp", "webp"):
            cur_fmt = "png"
        self.combo_save_fmt.setCurrentIndex(
            self.combo_save_fmt.findData(cur_fmt))
        form.addRow(T("ui.save_fmt", "保存格式"), self.combo_save_fmt)
        self._attach_hint(
            form, self.combo_save_fmt,
            T("ui.save_fmt_hint",
              "历史记录图片的保存格式；JPG/WebP 体积更小，PNG/BMP 无损。"))

        self.slider_quality = QSlider(Qt.Horizontal)
        self.slider_quality.setRange(1, 100)
        self.slider_quality.setFixedWidth(140)
        self.slider_quality.setValue(max(1, min(100, self._cfg.getint(
            "software", "save_quality", 90))))
        self.lbl_quality = QLabel(str(self.slider_quality.value()))
        self.slider_quality.valueChanged.connect(
            lambda v: self.lbl_quality.setText(str(v)))
        quality_row = QHBoxLayout()
        quality_row.addWidget(self.slider_quality)
        quality_row.addWidget(self.lbl_quality)
        quality_row.addStretch(1)
        form.addRow(T("ui.save_quality", "图片质量"), quality_row)
        self._attach_hint(
            form, quality_row,
            T("ui.save_quality_hint",
              "JPG/WebP 有损压缩质量（1-100，越高越清晰体积越大）；"
              "PNG/BMP 无损，质量不生效。"))
        # 质量仅对 JPG/WebP 生效
        self.combo_save_fmt.currentIndexChanged.connect(
            self._on_save_fmt_changed)
        self._on_save_fmt_changed()

        # ---- 文件名规则（占位符模板，默认时间排序）----
        self.edit_name_pattern = QLineEdit(
            self._cfg.get("software", "name_pattern", "{datetime}_{ms}"))
        self.edit_name_pattern.setPlaceholderText(
            T("ui.name_pattern_ph", "{datetime}_{ms}"))
        form.addRow(T("ui.name_pattern", "文件名规则"),
                    self.edit_name_pattern)
        self._attach_hint(
            form, self.edit_name_pattern,
            T("ui.name_pattern_hint",
              "文件名占位符模板，默认 {datetime}_{ms}（时间排序）。\n"
              "{datetime}=日期时间 {date}=日期 {time}=时间 {ms}=毫秒 "
              "{ts}=时间戳 {seq}=序号 {ext}=扩展名；"
              "留空 = 默认模板，重名自动加 _2 后缀。"))

        # GPU 加速（[software] ocr_device，按平台动态：Win=cpu|cuda|dml；Linux=cpu|cuda|migraphx|openvino）
        from core_engine.ort_runtime import available_devices
        # 各加速方式的内存开销说明（悬停 tooltip）
        self._MEM_HINT = {
            "cpu": T("ui.mem_cpu", "无额外显存占用。"),
            "cuda": T("ui.mem_cuda", "额外占用显存约 0.5~1.5GB（模型权重 + CUDA 上下文）；需 NVIDIA 显卡 + 驱动。"),
            "dml": T("ui.mem_dml", "额外占用显存/共享内存约 0.3~1GB（核显用系统内存）；任意 DX12 显卡（AMD/Intel/NVIDIA）。"),
            "migraphx": T("ui.mem_migraphx", "额外占用显存约 0.5~1.5GB；需 AMD Radeon 7000/9000 + ROCm。"),
            "openvino": T("ui.mem_openvino", "额外占用显存约 0.3~1GB；需 Intel 核显 / Arc / NPU。"),
        }
        self.combo_accel = FitComboBox()
        _dev_labels = {
            "cpu": ("ui.accel_auto", "自动（CPU）"),
            "cuda": ("ui.accel_cuda", "CUDA（NVIDIA 显卡）"),
            "dml": ("ui.accel_dml", "DirectML（AMD / Intel / 通用）"),
            "migraphx": ("ui.accel_migraphx", "MIGraphX（AMD 显卡，Linux）"),
            "openvino": ("ui.accel_openvino", "OpenVINO（Intel 核显 / Arc / NPU，Linux）"),
        }
        for _d in available_devices():
            _k, _df = _dev_labels[_d]
            _text = T(_k, _df)
            # DML / CUDA 选项：把最近一次检测的倍率拼进选项文字，
            # 这样「检测后」与「下次重开」显示一致；同时附在 tooltip（含显存说明）。
            # 选项文字较长由 FitComboBox 宽度上限兜底，不会把整行撑宽触发横滚动条。
            _tip = self._MEM_HINT.get(_d, "")
            if _d in ("dml", "cuda"):
                _ok = [x for x in self._cfg_get_probe(_d).get("devices", [])
                       if x.get("ok")]
                if _ok:
                    _best = max(_ok, key=lambda x: x.get("speedup", 0))
                    _text += f"（≈{_best['speedup']:.1f}×）"
                    _tip += f"\n（实测推理 ≈{_best['speedup']:.1f}×）"
            self.combo_accel.addItem(_text, _d)
            _idx = self.combo_accel.count() - 1
            self.combo_accel.setItemData(_idx, _tip, Qt.ToolTipRole)
        dev = self._cfg.get("software", "ocr_device", "cpu").lower()
        i = self.combo_accel.findData(dev if dev in available_devices() else "cpu")
        self.combo_accel.setCurrentIndex(i if i >= 0 else 0)
        self.lbl_accel_status = QLabel("")
        self.lbl_accel_status.setProperty("kind", "info")
        self.lbl_accel_status.setWordWrap(True)
        accel_row = QHBoxLayout()
        accel_row.addWidget(self.combo_accel, 1)
        self.btn_accel_install = QPushButton(T("btn.detect", "检测"))
        self.btn_accel_install.setToolTip(T(
            "ui.accel_install_tip",
            "检测 DirectML 各设备的实际推理倍率（记录到配置，下拉框显示）；"
            "CUDA 显示就绪状态。检测约 1~4 秒。"))
        self.btn_accel_install.clicked.connect(self._run_gpu_probe)
        accel_row.addWidget(self.btn_accel_install)
        form.addRow(T("gpu.accel", "GPU 加速"), accel_row)
        self._attach_hint(
            form, accel_row,
            T("ui.accel_hint",
              "CUDA 需 onnxruntime-gpu；DirectML 需 onnxruntime-directml"
              "（AMD/Intel 核显/NVIDIA 通用）。无对应环境自动回退 CPU。重启程序生效。"))
        self._refresh_accel_status()

        # ---- OCR 模型档位（[software] ocr_model = v6-tiny | v6-small | v6-medium）----
        self.combo_model = FitComboBox()
        self.combo_model.addItem(T("ui.model_tiny", "轻量（v6-tiny）"), "v6-tiny")
        self.combo_model.addItem(T("ui.model_small", "标准（v6-small）"), "v6-small")
        self.combo_model.addItem(T("ui.model_medium", "高精度（v6-medium）"), "v6-medium")
        cur_model = self._cfg.get("software", "ocr_model", "v6-tiny")
        i_m = self.combo_model.findData(cur_model)
        self.combo_model.setCurrentIndex(i_m if i_m >= 0 else 0)
        form.addRow(T("ui.model_set", "OCR 模型"), self.combo_model)
        self._attach_hint(
            form, self.combo_model,
            T("ui.model_set_hint",
              "轻量=识别最快；标准=速度与精度均衡；高精度=识别更准但更慢。\n"
              "切到高精度模型首次加载约 1–2 秒（保存后立即生效）。"))

        # ---- OCR 引擎常驻（[software] ocr_keep_alive，默认勾选）----
        self.chk_ocr_keep_alive = QCheckBox(T("ui.ocr_keep_alive", "OCR 引擎常驻"))
        self.chk_ocr_keep_alive.setChecked(
            self._cfg.getbool("software", "ocr_keep_alive", True))
        self.chk_ocr_keep_alive.setToolTip(T(
            "ui.ocr_keep_alive_hint",
            "勾选：启动时加载 OCR 模型（识别更快，占用内存）；\n"
            "不勾选：启动不加载模型，首次识别时才加载（省内存、首次稍慢）。"))
        form.addRow("", self.chk_ocr_keep_alive)

        # ---- 截图后自动 OCR（[software] ocr_auto，默认勾选）----
        self.chk_ocr_auto = QCheckBox(T("ocr.auto", "截图后自动 OCR"))
        self.chk_ocr_auto.setChecked(
            self._cfg.getbool("software", "ocr_auto", True))
        self.chk_ocr_auto.setToolTip(
            T("ui.ocr_auto_hint",
              "勾选：截图后自动识别文字并显示文字框；\n"
              "不勾选：截图仅贴图，点「文字框」或「表格」按钮时才识别。"))
        form.addRow("", self.chk_ocr_auto)

        # ---- 拖入图片后自动 OCR（[software] drop_ocr_auto，默认勾选）----
        # 独立于「截图后自动 OCR」：拖进历史面板的图片是否自动识别
        self.chk_drop_ocr = QCheckBox(
            T("ocr.drop_auto", "拖入图片后自动 OCR"))
        self.chk_drop_ocr.setChecked(
            self._cfg.getbool("software", "drop_ocr_auto", True))
        self.chk_drop_ocr.setToolTip(
            T("ocr.drop_auto_hint",
              "勾选：把图片拖进历史面板后自动识别文字（原行为）；\n"
              "不勾选：仅入库为「待识别」，需要时在历史面板点开该图，"
              "再点「文字框」识别。"))
        form.addRow("", self.chk_drop_ocr)

        # ---- 二维码识别（[ui] enable_qr，默认关）----
        self.chk_qr = QCheckBox(T("ui.enable_qr", "二维码识别"))
        self.chk_qr.setChecked(
            self._cfg.getbool("software", "enable_qr", False))
        self.chk_qr.setToolTip(T(
            "ui.enable_qr_hint",
            "勾选：截图 OCR 时顺带识别二维码/条码（需 zxing-cpp）。\n"
            "当前贴图窗口用工具栏 QR 按钮 / Q 键即时切换；此处为默认值，保存后对后续截图生效。"))
        form.addRow("", self.chk_qr)

        # ---- 截图无操作也保存（[software] save_no_op_close，默认关）----
        self.chk_save_no_op = QCheckBox(T("ui.save_no_op", "截图无操作也保存"))
        self.chk_save_no_op.setChecked(
            self._cfg.getbool("software", "save_no_op_close", False))
        self.chk_save_no_op.setToolTip(T("ui.save_no_op_hint",
            "勾选：截图窗口未做任何操作（不复制 / 不标注 / 不识别）就关闭时，仍保存截图到历史记录；\n"
            "不勾选：未做任何操作就关闭的截图不保存。"))
        form.addRow("", self.chk_save_no_op)

        # 开机自启动（M2-8：注册表 HKCU Run）
        from core_engine.autostart import is_enabled
        self.chk_autostart = QCheckBox(T("autostart", "开机自动启动 ClickOCR"))
        self.chk_autostart.setChecked(is_enabled())
        form.addRow("", self.chk_autostart)

        # ---- 历史管理：打开历史文件夹（左） + 清空全部历史（右，危险操作）----
        self.btn_open_history = QPushButton(
            T("settings.open_history", "打开历史文件夹"))
        self.btn_open_history.clicked.connect(self._open_history_folder)
        self._attach_hint(
            form, self.btn_open_history,
            T("settings.open_history_hint",
              "在系统文件管理器中打开历史记录图片的保存目录。"))
        self.btn_clear_history = QPushButton(
            T("settings.clear_all_hist", "清空全部历史"))
        self.btn_clear_history.clicked.connect(self._clear_all_history)
        self._attach_hint(
            form, self.btn_clear_history,
            T("settings.clear_all_hist_hint",
              "清空全部历史记录（收藏的图片会保留）；不可恢复，请谨慎操作。"))
        _hist_row = QHBoxLayout()
        _hist_row.setSpacing(8)
        _hist_row.addStretch(1)                 # 按钮组靠右，与底部保存/关闭区呼应
        _hist_row.addWidget(self.btn_open_history)
        _hist_row.addWidget(self.btn_clear_history)
        form.addRow("", _hist_row)
        # 滚动区接管内容：setWidget 会强制开启 autoFill，需在其后关闭
        scroll.setWidget(inner)
        inner.setAutoFillBackground(False)
        return page   # 必须返回页面 widget：返回 None 会导致 tab 页丢失、
                      # 页内控件（spin/combo）失去 Qt 持有后被 PySide6 GC 销毁

    def _refresh_accel_status(self):
        """显示当前所选档位的运行时状态。"""
        from core_engine.ort_runtime import runtime_status
        dev = self.combo_accel.currentData()
        self.lbl_accel_status.setText(runtime_status(dev))

    def _cfg_get_probe(self, ep: str = "dml") -> dict:
        """读配置里最近一次 GPU 检测结果（JSON）；缺失/损坏返回空 dict。"""
        key = "cuda_probe" if ep == "cuda" else "dml_probe"
        try:
            raw = self._cfg.get("software", key, "")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _run_gpu_probe(self):
        """检测按钮：探测所选 EP（DirectML / CUDA）各设备实际推理倍率，
        记录到配置（[software] dml_probe / cuda_probe）并刷新显示。

        用当前模型档位的 det 模型计时（640x640）；探测约 1~4 秒同步执行。
        """
        dev = self.combo_accel.currentData()
        if dev == "cpu":
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"),
                             T("ui.probe_cpu", "CPU 模式无需 GPU 检测。"))
            self._refresh_accel_status()
            return
        if dev == "dml":
            from ocr.rapid_engine import probe_dml_devices, _DIR_V6
            probe_fn = probe_dml_devices
        elif dev == "cuda":
            from ocr.rapid_engine import probe_cuda_devices, _DIR_V6
            probe_fn = probe_cuda_devices
        else:
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"),
                             T("ui.probe_unsupported",
                               "{} 暂不支持性能检测。").format(dev))
            return
        det_path = None
        try:
            _names = {"v6-tiny": "PP-OCRv6_det_tiny.onnx",
                      "v6-small": "PP-OCRv6_det_small.onnx",
                      "v6-medium": "PP-OCRv6_det_medium.onnx"}
            _n = _names.get(self.combo_model.currentData())
            if _n:
                det_path = _DIR_V6 / _n
        except Exception:  # noqa: BLE001
            det_path = None
        self.btn_accel_install.setEnabled(False)
        self.btn_accel_install.setText(T("btn.detect_running", "检测中…"))
        self.lbl_accel_status.setText(T("ui.probe_running", "正在探测设备…"))
        QApplication.processEvents()
        try:
            devices = probe_fn(det_path)
        except Exception as e:  # noqa: BLE001
            log.exception("GPU 检测失败")
            devices = []
            self.lbl_accel_status.setText(
                T("ui.probe_fail", "检测失败：{}").format(e))
        finally:
            self.btn_accel_install.setEnabled(True)
            self.btn_accel_install.setText(T("btn.detect", "检测"))
        if not devices:
            self.lbl_accel_status.setText(T(
                "ui.probe_no_dev", "未检测到可用的 {} 设备（当前环境无支持）。"
                ).format(dev.upper()))
            return
        # 记录到配置（下次启动直接显示倍率）
        _key = "cuda_probe" if dev == "cuda" else "dml_probe"
        try:
            self._cfg.cp.set("software", _key, json.dumps(
                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "det": self.combo_model.currentData(),
                 "devices": devices}, ensure_ascii=False))
            self._cfg.save()
        except Exception:  # noqa: BLE001
            pass
        # 刷新下拉框：当前 EP 项附加最快倍率
        _ok = [d for d in devices if d.get("ok")]
        for _i in range(self.combo_accel.count()):
            if self.combo_accel.itemData(_i) == dev:
                _base_text = {
                    "dml": "DirectML（AMD / Intel / 通用）",
                    "cuda": "CUDA（NVIDIA 显卡）",
                }.get(dev, dev)
                _text = _base_text
                if _ok:
                    _best = max(_ok, key=lambda d: d.get("speedup", 0))
                    _text += f"（≈{_best['speedup']:.1f}×）"
                self.combo_accel.setItemText(_i, _text)
                break
        # 状态显示各设备倍率
        lines = [f"[{time.strftime('%H:%M:%S')}] CPU 基准 "
                 f"{_ok[0]['cpu_ms']:.0f}ms/张" if _ok else "无可用设备"]
        for d in devices:
            if d.get("ok"):
                lines.append(f"设备{d['id']}：≈{d['speedup']:.2f}× "
                             f"({d['dml_ms']:.0f}ms vs CPU {d['cpu_ms']:.0f}ms)")
            else:
                lines.append(f"设备{d['id']}：不可用"
                             + (f"（{d['err']}）" if d.get("err") else ""))
        self.lbl_accel_status.setText("\n".join(lines))

    def _open_history_folder(self):
        """打开历史记录图片的保存目录（系统文件管理器，跨平台走 QDesktopServices）。"""
        d = None
        if self._history is not None:
            d = getattr(self._history, "screenshots_dir", None)
        if not d:
            self._notify(T("ui.msg_history_none", "历史记录不可用"))
            return
        try:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
                self._notify(T("settings.open_history_fail",
                               "打开历史文件夹失败：{}").format(p))
        except Exception as e:      # noqa: BLE001
            log.exception("打开历史文件夹失败: %s", e)
            self._notify(T("settings.open_history_fail",
                           "打开历史文件夹失败：{}").format(e))

    def _clear_all_history(self):
        """清空全部历史（原历史面板按钮移入）：确认后只清非收藏，收藏保留；
        完成后经 App 回调刷新已打开的历史面板。"""
        if self._history is None:
            self._notify(T("ui.msg_history_none", "历史记录不可用"))
            return
        if not ThemeDialog.question(
                None,
                T("settings.title", "ClickOCR 设置"),
                T("settings.clear_all_hist_confirm",
                  "确定清空全部历史记录？\n（收藏的图片会保留，此操作不可恢复）")):
            return
        try:
            self._history.clear(keep_favorites=True)
        except Exception as e:      # noqa: BLE001
            log.exception("清空历史失败: %s", e)
            self._notify(T("ui.msg_save_fail", "清空失败：{}").format(e))
            return
        if self._on_clear_history is not None:
            try:
                self._on_clear_history()
            except Exception:       # noqa: BLE001
                log.exception("清空后刷新历史面板失败")
        self._notify(T("ui.msg_cleared", "已清空全部历史（收藏保留）"))

    def _on_save_fmt_changed(self):
        """质量滑块仅对有损格式（JPG/WebP）生效。"""
        fmt = self.combo_save_fmt.currentData()
        lossy = fmt in ("jpg", "webp")
        self.slider_quality.setEnabled(lossy)
        self.lbl_quality.setEnabled(lossy)

    def _check_install_accel(self):
        """检测并（开发模式）自动安装所选 GPU 运行时；下载前弹确认。"""
        from core_engine.ort_runtime import (device_ready, install_runtime,
                                             install_command, runtime_status,
                                             DEVICE_PKG, PKG_SIZE)
        dev = self.combo_accel.currentData()
        if dev not in DEVICE_PKG:
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_cpu_ok", "CPU 模式无需安装 GPU 运行时。"))
            return
        pkg, ep = DEVICE_PKG[dev]
        if device_ready(dev):
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_ready", "{} 已就绪，无需安装。").format(ep))
            self._refresh_accel_status()
            return
        import sys as _sys
        if getattr(_sys, "frozen", False):
            # 打包版已捆绑运行时：直接显示实际状态（已就绪 / 设备不支持自动回退 CPU）
            ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), runtime_status(dev))
            self._refresh_accel_status()
            return
        if not ThemeDialog.question(None, T("gpu.accel", "GPU 加速"),
                                     T("ui.gpu_need_install",
                                       "需要下载安装 {}（{}，需联网）\n"
                                       "才能使用 {}。安装后请重启程序生效。\n\n是否继续安装？")
                                     .format(pkg, PKG_SIZE[pkg], ep)):
            return
        self.btn_accel_install.setEnabled(False)
        self.btn_accel_install.setText(T("ui.gpu_installing", "安装中…"))
        try:
            ok, msg = install_runtime(dev)
            if ok:
                ThemeDialog.info(None, T("gpu.accel", "GPU 加速"), msg)
            else:
                ThemeDialog.warning(None, T("gpu.accel", "GPU 加速"), T("ui.gpu_install_fail", "安装失败：\n{}").format(msg))
        finally:
            self.btn_accel_install.setEnabled(True)
            self.btn_accel_install.setText(T("btn.detect", "检测"))
            self._refresh_accel_status()

    def _pick_shot_dir(self):
        cur = self.edit_shot_dir.text().strip()
        d = QFileDialog.getExistingDirectory(
            self, T("ui.pick_shot_dir_title", "选择历史图片保存目录"), cur or str(self._cfg.app_root))
        if d:
            self.edit_shot_dir.setText(d)

    # ---- Tab 2：UI 设置 ----
