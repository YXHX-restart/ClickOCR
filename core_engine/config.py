"""core_engine.config — 路径 / INI 配置管理（无 Qt 依赖）。

设计：
- `ConfigManager(app_root)`：app_root 为软件根目录（截图模式 = ClickOCR/，看图模式 = AI-Gallery/）。
  所有资源路径（config.ini / clickocr.db / screenshots/ / logs/）都相对 app_root 解析，
  便携版拔走 U 盘不留痕迹。
- 兼容旧用法：`ConfigManager(path=...)` 显式指定 ini 文件（测试用）；不传 app_root 时
  默认取当前工作目录。

段结构（2026-08-26 起与软件 UI 分类对齐）：
- [software]        设置窗口「软件设置」页（历史/保存/OCR/GPU/二维码等）
- [ui]              设置窗口「UI设置」页（主题/面板/边框/取色器/开关）+ 外观交互
- [hotkey_global]   快捷键设置「全局热键」组
- [hotkey_select]   快捷键设置「截图选择」组
- [hotkey_overlay]  快捷键设置「贴图窗口」组
- [hotkey_anno]     快捷键设置「标注」组
- [engine]          引擎/内部参数（设置窗口未展示）
- [annotation]      标注工具运行期记忆参数（标注工具栏调参后写入）

旧结构（general/ocr/hotkey/hotkey_full/selection/result/preprocess/screenshot/
db/hotkey_ext/ui/paths/annotation）由 _LEGACY_MAP 自动迁移到新结构。
"""
from __future__ import annotations

import configparser
import sys
from pathlib import Path

# 出厂默认值（新结构；旧结构由 _LEGACY_MAP 迁移后按此补默认）
_DEFAULTS = {
    # ---- 设置窗口「软件设置」页 ----
    "software": {
        "history_max_count": "50",        # 历史保留条数（默认 50，不设上限）
        "screenshots_dir": "",            # 历史图片目录（空 = 默认 screenshots/）
        # 两类贴图窗口配额，**互不相干**，勿混：
        #   max_shot_windows    = 「截图贴图」：截图完成后自动贴出的窗口（默认 1）
        #   max_restore_windows = 「历史贴图」：从历史记录恢复打开的窗口（默认 3）
        # 旧名 max_history_windows / max_hist_windows 仅差 3 个字符、语义又都带
        # "history"，历次审查都把它们误判成「改名遗留的重复键」——故显式改成
        # 现在这两名字（shot / restore），旧名由 _LEGACY_MAP 自动迁移。
        "max_shot_windows": "1",          # 截图贴图最大数量
        "max_restore_windows": "3",       # 历史贴图最大数量
        "max_pin_windows": "3",           # 钉图最大数量（上限 20）
        "enable_qr": "false",             # 二维码识别默认开关
        "selection_mode": "auto_commit",  # 复制模式：auto_commit | manual_copy
        "save_format": "png",             # 保存格式：png | jpg | bmp | webp
        "save_quality": "90",             # 有损质量 1-100
        "name_pattern": "{datetime}_{ms}",
        "save_no_op_close": "false",      # 截图无操作也保存
        "ocr_device": "cpu",              # cpu | cuda | dml | migraphx | openvino
        "ocr_model": "v6-tiny",           # v6-tiny | v6-small | v6-medium
        "ocr_keep_alive": "true",         # OCR 引擎常驻
        "ocr_auto": "true",               # 截图后自动 OCR
        "drop_ocr_auto": "true",          # 拖入图片进历史面板后自动 OCR
    },
    # ---- 设置窗口「UI设置」页 + 外观/交互 ----
    "ui": {
        "theme": "system",                # dark | light | system（跟随系统）
        "history_panel_width": "620",     # 历史面板宽度（400~1600）
        "history_panel_height": "0",      # 历史面板高度（运行期记忆；0=默认）
        "history_grid_cols": "2",         # 历史面板列数
        "history_visible_cards": "5",     # 历史面板显示卡片数（决定高度）
        "start_minimized": "true",        # 启动最小化（托盘）
        "border_color": "100,190,255",    # 截图框选边框色（R,G,B）
        "border_width": "2",              # 截图框选边框宽
        "fill_alpha": "50",               # 框内填充透明度
        "mask_alpha": "120",              # 遮罩压暗透明度
        "picker_size": "7",               # 取色器采样边长（3~15，奇数）
        "picker_zoom": "9",               # 取色器每像素放大倍数（2~20）
        "overlay_border_color": "100,190,255",  # 贴图窗口边框色
        "overlay_border_width": "1",            # 贴图窗口边框宽（0=不显示）
        "image_border_color": "100,190,255",    # 结果图边框色
        "image_border_width": "1",              # 结果图边框线宽（0=不显示）
        "qr_border_color": "156,39,176",        # 二维码框色（紫）
        "qr_border_width": "1",                 # 二维码框线宽（1~10，同截图边框）
        "table_border_color": "90,220,120",     # 表格网格框色（绿）
        "table_border_width": "1",              # 表格网格线宽（1~10，同截图边框）
        "show_text_boxes": "true",              # 识别文字框默认显示
        "anno_toolbar_default": "false",        # 截图后标注栏默认开启
        "toolbar_enabled": "true",              # 统一工具栏全局开关
        "strip_enabled": "true",                # 纯信息状态栏全局开关
        "persist_params": "true",               # 永久化记录标注工具参数
    },
    # ---- 快捷键设置：全局热键 ----
    "hotkey_global": {
        "screenshot_combo": "ctrl+alt+r",  # 触发截图
        "fullscreen_combo": "off",         # 全屏截图（off = 未启用）
        "history": "ctrl+`",               # 打开历史记录
        "through": "alt+f",                # 切换贴图/钉图点击穿透
    },
    # ---- 快捷键设置：截图选择（截图遮罩中）----
    "hotkey_select": {
        "full_sel": "a",                   # 全屏框选
        "scroll_sel": "r",                 # 滚动截图模式开关
        "color_pick": "c",                 # 复制光标处色值
        "color_fmt": "h",                  # 切换色值格式
    },
    # ---- 快捷键设置：贴图窗口 ----
    "hotkey_overlay": {
        "reocr": "ctrl+r",                 # 重新 OCR
        "table_ocr": "ctrl+shift+t",       # 表格识别
        "toggle_anno": "ctrl+shift+d",     # 进入/退出标注
        "nav": "m",                        # 导航图开关
        "ratio": "b",                      # 等比例调节窗口
        "toggle_strip": "s",               # 状态栏开关
        "toggle_qr": "q",                  # 二维码开关
        "toggle_boxes": "w",               # 文字框开关
        "toggle_table": "e",               # 表格开关
        "fav": "f",                        # 收藏截图
        "copy_shortcut": "shift+c",        # 全复制到剪贴板
        "select_all": "ctrl+a",            # 全选文字（仅选中）
        "copy_selected": "ctrl+c",         # 复制选中文字
        "copy_image": "ctrl+shift+c",      # 复制整张图片（含标注）
        "copy_raw_image": "ctrl+alt+c",    # 复制原图（不含标注）
        "zoom_modifier": "ctrl",           # 图片缩放修饰键：ctrl/alt/shift/off
        "geom_zoom_modifier": "shift",     # 窗口几何缩放修饰键
    },
    # ---- 快捷键设置：标注 ----
    "hotkey_anno": {
        "undo": "ctrl+z",                  # 撤销
        "redo": "ctrl+shift+z",            # 重做
    },
    # ---- 引擎/内部参数（设置窗口未展示）----
    "engine": {
        "ocr_engine": "rapidocr",          # OCR 引擎（固定 rapidocr）
        "use_cls": "false",                # 方向分类（v6 无独立 cls，恒 false）
        "table_model": "slanet_plus",      # 表格模型
        "preprocess_preset": "off",        # 预处理预设：off | light | standard | strong
        "scroll_speed": "2",               # 滚动截图自动模式每秒帧数
        "scroll_max_height": "12000",      # 滚动截图最大拼接高度 px
        "image_max_disk_mb": "50",         # 图片磁盘上限 MB（预留）
    },
    # ---- 标注工具运行期记忆参数（标注工具栏调参后写入）----
    "annotation": {
        "mosaic_mode": "rect",             # 马赛克模式：pen | rect | textbox
        "tool_color": "255,95,95",         # 标注工具颜色
        "tool_width": "2",                 # 线宽
        "text_size": "18",                 # 文字字号
        "text_bold": "false",
        "text_italic": "false",
        "num_size": "18",                  # 序号字号
        "num_start": "1",                  # 序号起始
        "num_style": "circle",             # 序号样式
        "arrow_style": "single",           # 箭头样式
        "pen_style": "solid",              # 画笔样式
        "rect_style": "rect",              # 矩形样式
        "line_style": "solid",             # 直线样式
    },
}

# 旧结构 → 新结构迁移映射（旧 sec,key → 新 sec,key）
_LEGACY_MAP = {
    # 快捷键：散落的 3 个 section → 按速查面板状态分组
    ("hotkey", "combo"): ("hotkey_global", "screenshot_combo"),
    ("hotkey_full", "combo"): ("hotkey_global", "fullscreen_combo"),
    ("hotkey_ext", "history_hotkey"): ("hotkey_global", "history"),
    ("hotkey_ext", "through_hotkey"): ("hotkey_global", "through"),
    ("hotkey_ext", "full_sel"): ("hotkey_select", "full_sel"),
    ("hotkey_ext", "scroll_sel"): ("hotkey_select", "scroll_sel"),
    ("hotkey_ext", "color_pick"): ("hotkey_select", "color_pick"),
    ("hotkey_ext", "color_fmt"): ("hotkey_select", "color_fmt"),
    ("hotkey_ext", "reocr"): ("hotkey_overlay", "reocr"),
    ("hotkey_ext", "table_ocr"): ("hotkey_overlay", "table_ocr"),
    ("hotkey_ext", "toggle_anno"): ("hotkey_overlay", "toggle_anno"),
    ("hotkey_ext", "nav_hotkey"): ("hotkey_overlay", "nav"),
    ("hotkey_ext", "ratio_hotkey"): ("hotkey_overlay", "ratio"),
    ("hotkey_ext", "toggle_strip"): ("hotkey_overlay", "toggle_strip"),
    ("hotkey_ext", "toggle_qr"): ("hotkey_overlay", "toggle_qr"),
    ("hotkey_ext", "toggle_boxes"): ("hotkey_overlay", "toggle_boxes"),
    ("hotkey_ext", "toggle_table"): ("hotkey_overlay", "toggle_table"),
    ("hotkey_ext", "fav"): ("hotkey_overlay", "fav"),
    ("hotkey_ext", "copy_shortcut"): ("hotkey_overlay", "copy_shortcut"),
    ("hotkey_ext", "select_all"): ("hotkey_overlay", "select_all"),
    ("hotkey_ext", "copy_selected"): ("hotkey_overlay", "copy_selected"),
    ("hotkey_ext", "copy_image"): ("hotkey_overlay", "copy_image"),
    ("hotkey_ext", "copy_raw_image"): ("hotkey_overlay", "copy_raw_image"),
    ("hotkey_ext", "zoom_modifier"): ("hotkey_overlay", "zoom_modifier"),
    ("hotkey_ext", "geom_zoom_modifier"): ("hotkey_overlay", "geom_zoom_modifier"),
    ("hotkey_ext", "undo_anno"): ("hotkey_anno", "undo"),
    ("hotkey_ext", "redo_anno"): ("hotkey_anno", "redo"),
    # 设置窗口「软件设置」页
    ("db", "screenshot_max_count"): ("software", "history_max_count"),
    ("paths", "screenshots_dir"): ("software", "screenshots_dir"),
    ("ui", "max_history_windows"): ("software", "max_shot_windows"),
    ("ui", "max_hist_windows"): ("software", "max_restore_windows"),
    # 2026-09-18 键名消歧：旧名只差 3 个字符且都含 "history"，被历次审查误判为
    # 「重复键」。同 section 改名，靠 _LEGACY_MAP 把用户现有取值搬过去（不重置）。
    ("software", "max_history_windows"): ("software", "max_shot_windows"),
    ("software", "max_hist_windows"): ("software", "max_restore_windows"),
    ("ui", "max_pin_windows"): ("software", "max_pin_windows"),
    ("ui", "enable_qr"): ("software", "enable_qr"),
    ("screenshot", "selection_mode"): ("software", "selection_mode"),
    ("screenshot", "save_format"): ("software", "save_format"),
    ("screenshot", "save_quality"): ("software", "save_quality"),
    ("screenshot", "name_pattern"): ("software", "name_pattern"),
    ("screenshot", "save_no_op_close"): ("software", "save_no_op_close"),
    ("ocr", "device"): ("software", "ocr_device"),
    ("ocr", "model_set"): ("software", "ocr_model"),
    ("ocr", "ocr_keep_alive"): ("software", "ocr_keep_alive"),
    ("ocr", "ocr_auto"): ("software", "ocr_auto"),
    # 设置窗口「UI设置」页 + 外观/交互
    ("general", "start_minimized"): ("ui", "start_minimized"),
    ("selection", "border_color"): ("ui", "border_color"),
    ("selection", "border_width"): ("ui", "border_width"),
    ("selection", "fill_alpha"): ("ui", "fill_alpha"),
    ("selection", "mask_alpha"): ("ui", "mask_alpha"),
    ("selection", "picker_size"): ("ui", "picker_size"),
    ("selection", "picker_zoom"): ("ui", "picker_zoom"),
    ("result", "overlay_border_color"): ("ui", "overlay_border_color"),
    ("result", "overlay_border_width"): ("ui", "overlay_border_width"),
    ("result", "image_border_color"): ("ui", "image_border_color"),
    ("result", "image_border_width"): ("ui", "image_border_width"),
    ("result", "qr_border_color"): ("ui", "qr_border_color"),
    ("result", "table_border_color"): ("ui", "table_border_color"),
    ("result", "show_text_boxes"): ("ui", "show_text_boxes"),
    ("result", "anno_toolbar_default"): ("ui", "anno_toolbar_default"),
    ("result", "toolbar_enabled"): ("ui", "toolbar_enabled"),
    ("result", "strip_enabled"): ("ui", "strip_enabled"),
    ("annotation", "persist_params"): ("ui", "persist_params"),
    # 引擎/内部参数
    ("ocr", "engine"): ("engine", "ocr_engine"),
    ("ocr", "use_cls"): ("engine", "use_cls"),
    ("ocr", "table_model"): ("engine", "table_model"),
    ("preprocess", "preset"): ("engine", "preprocess_preset"),
    ("screenshot", "scroll_speed"): ("engine", "scroll_speed"),
    ("screenshot", "scroll_max_height"): ("engine", "scroll_max_height"),
    ("db", "image_max_disk_mb"): ("engine", "image_max_disk_mb"),
}


class ConfigManager:
    def __init__(self, app_root: str | Path | None = None,
                 path: str | Path | None = None):
        self.app_root = Path(app_root) if app_root else Path.cwd()
        # 打包（PyInstaller onedir）后资源/数据分离：
        # - res_root  = _MEIPASS（_internal）：只读资源（models/config 默认值）
        # - data_root = exe 同目录：可写数据（db/screenshots/logs/用户配置）
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            self._res_root = Path(sys._MEIPASS)
            self._data_root = Path(sys.executable).resolve().parent
        else:
            self._res_root = self._data_root = self.app_root
        # 用户配置优先读 data_root/config.ini；不存在则读打包默认
        if path is not None:
            self.path = Path(path)
            self._path_explicit = True
        else:
            self.path = self._data_root / "config.ini"
            self._path_explicit = False
        self._default_cfg = self._res_root / "resources" / "config.ini"
        # 支持行内注释（# / ;），如 "border_color = 100,190,255   # 浅蓝"。
        # interpolation=None：本项目所有值都是原样字符串，默认的
        # BasicInterpolation 下含单个 % 的值（如手改的 name_pattern = shot_%d）
        # 会让 cp.read() 抛 InterpolationSyntaxError → 启动即崩溃
        self.cp = configparser.ConfigParser(
            inline_comment_prefixes=("#", ";"), interpolation=None)
        self._load_source = None   # 本次实际加载的文件（save 写回同一来源）
        self._load()

    def _seed_from_template(self) -> None:
        """首次运行：把出厂模板复制到可写位置，作为本次及以后的配置来源。

        「资源只读、数据分离」：`resources/config.ini` 是**出厂模板**（只读资源），
        用户配置放可写的数据目录。复制而不是直接读模板，是为了让 save() 写回
        数据目录 —— 否则 dev 模式下用户设置会被写进仓库、打包时又被带进发布包。

        复制失败（只读介质 / 无权限）时退回**只读**加载模板，保证仍能启动。
        """
        import shutil

        from core_engine.logging_setup import get_logger

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._default_cfg, self.path)
            self.cp.read(self.path, encoding="utf-8")
            self._load_source = self.path
            get_logger("config").info(
                "首次运行：已由出厂模板生成用户配置 %s（此后改动只写这里）",
                self.path)
        except OSError as e:                # noqa: BLE001  只读介质等
            get_logger("config").warning(
                "无法生成用户配置（%s），本次以只读方式加载出厂模板", e)
            self.cp.read(self._default_cfg, encoding="utf-8")
            self._load_source = self._default_cfg

    def _load(self):
        # 记住实际加载来源：save() 必须写回同一文件，否则「读 A 写 B」会让
        # 用户的所有修改重启后全部回退（dev 模式下 app_root/config.ini 存在
        # 时曾踩此洞：load 读它、save 却固定写 resources/config.ini）
        if self.path.exists():
            self.cp.read(self.path, encoding="utf-8")
            self._load_source = self.path
        elif self._default_cfg.exists() and self._default_cfg != self.path:
            # 首次运行：把出厂模板**复制**到可写位置再读写。
            # 绝不能把 resources/config.ini 本身当配置读写 —— 那是只读资源目录：
            #   · dev 模式下会把用户的实时配置写进仓库（含个人偏好、机器探测结果）
            #   · 打包时又把这些个人配置打进发布包（典型后果：发布包带着
            #     ocr_device=cuda，纯 CPU 机器每次启动都白走一轮 CUDA 探测）
            self._seed_from_template()
        elif self._default_cfg.exists():
            self.cp.read(self._default_cfg, encoding="utf-8")
            self._load_source = self._default_cfg
        # 兼容旧版转义：interpolation 开启时 configparser 落盘会把值中的 %
        # 转义成 %%；关闭插值后原样读出 %%，这里还原成 %（与旧读取语义一致）
        for _sec in self.cp.sections():
            for _k, _v in self.cp.items(_sec):
                if "%%" in _v:
                    self.cp.set(_sec, _k, _v.replace("%%", "%"))
        # 旧结构（v0.13 及以前）→ 新结构迁移，或同 section 改名：搬值后删旧键
        removed = set()
        for (old_sec, old_key), (new_sec, new_key) in _LEGACY_MAP.items():
            if self.cp.has_option(old_sec, old_key):
                # 目标键已存在 = 已迁移过（或被新版/用户显式写过）：**以目标键为准**，
                # 只把旧键清掉。否则「旧值覆盖新值」会让改过的设置被历史值回退
                # （改名类迁移尤其明显：旧键若被手工补回，新值就被顶掉）。
                if not self.cp.has_option(new_sec, new_key):
                    if not self.cp.has_section(new_sec):
                        self.cp.add_section(new_sec)
                    self.cp.set(new_sec, new_key, self.cp.get(old_sec, old_key))
                self.cp.remove_option(old_sec, old_key)
                removed.add(old_sec)
        for sec in removed:
            # 仅删除已搬空的旧 section；仍有未知键的保留（不丢用户手填项）
            if sec not in _DEFAULTS and not self.cp.options(sec):
                self.cp.remove_section(sec)
        for sec, opts in _DEFAULTS.items():
            if not self.cp.has_section(sec):
                self.cp.add_section(sec)
            for k, v in opts.items():
                if not self.cp.has_option(sec, k):
                    self.cp.set(sec, k, v)

    # ---- 读取 ----
    def get(self, section: str, key: str, default=None):
        return self.cp.get(section, key, fallback=default)

    def getbool(self, section: str, key: str, default=False) -> bool:
        # fallback 只在「节/键不存在」时生效；键存在但值不是布尔词法
        # （用户手改 config.ini 写成 yes-no 之外的东西，如 "1 "、"true."）
        # 时 configparser 仍抛 ValueError —— 与 getint 对齐，回退默认值，
        # 不让一处手改把整个调用点崩掉。
        try:
            return self.cp.getboolean(section, key, fallback=default)
        except ValueError:
            return default

    def getint(self, section: str, key: str, default: int = 0) -> int:
        try:
            return self.cp.getint(section, key, fallback=default)
        except ValueError:
            return default

    def getcolor(self, section: str, key: str,
                 default: tuple = (0, 0, 0)) -> tuple:
        """读取 'R,G,B' 形式的颜色配置，返回 (r, g, b)。"""
        raw = self.get(section, key, None)
        if raw is None:
            return tuple(default)
        try:
            parts = [int(x.strip()) for x in str(raw).split(",")]
            if len(parts) == 3 and all(0 <= v <= 255 for v in parts):
                return tuple(parts)
        except ValueError:
            pass
        return tuple(default)

    def save(self):
        """写回配置文件，位置跟随运行方式：
        - 显式指定 path（测试/自定义）：写回该 path；
        - 打包（frozen）且内容来自只读的 _internal 默认配置：写 exe 旁
          config.ini（data_root）；
        - 其余：写回本次实际加载的来源文件（读哪写哪，不再漂移）。"""
        if self._path_explicit:
            target = self.path
        elif getattr(sys, "frozen", False) \
                and self._load_source == self._default_cfg:
            target = self.path
        else:
            target = self._load_source or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            self.cp.write(f)

    # ---- 路径 ----
    @property
    def db_path(self) -> Path:
        return self._data_root / "clickocr.db"

    def get_screenshots_dir(self) -> Path:
        """历史图片目录：[software] screenshots_dir 优先（绝对路径原样，
        相对路径相对软件根）；为空时默认 data_root/screenshots。"""
        raw = str(self.get("software", "screenshots_dir", "") or "").strip()
        if raw:
            p = Path(raw)
            return p if p.is_absolute() else (self._data_root / p)
        return self._data_root / "screenshots"

    @property
    def screenshots_dir(self) -> Path:
        return self.get_screenshots_dir()

    @property
    def thumbnails_dir(self) -> Path:
        return self.get_screenshots_dir() / "thumb"

    @property
    def logs_dir(self) -> Path:
        return self._data_root / "logs"

    @property
    def models_dir(self) -> Path:
        return self._res_root / "resources" / "models_v6"
