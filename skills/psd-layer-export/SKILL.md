---
name: psd-layer-export
description: 使用 PhotoCraft 调整已有 PSD 的图层位置，并将保存后的 PSD 顶层编组分别导出 JPG 或 PNG；适用于人物定位、多方案编组和分层设计交付。
---

# PSD 图层调整与编组导出

> **统一设计技能正本指向（2026-11-27）**：本模块脚本（`photocraft_psd.py`、`export_psd_groups.py`）与新增单层提取工具（`extract_psd_layers.py`）已同步收口至统一设计技能库 **`C:\pandy\github\skill\super-designer\skills\photocraft-studio\SKILL.md`**（总入口：`C:\pandy\github\skill\super-designer\AGENTS.md`）。本目录原样保留不删除，后续新特性优先在 `super-designer` 维护。

本 Skill 的脚本正本位于 PhotoCraft 项目 `skills/psd-layer-export/scripts/`，不依赖 MCP。`photocraft_psd.py` 调用本机 PhotoCraft CLI；`export_psd_groups.py` 使用 Python 的 `psd-tools` 和 Pillow 读取 PSD 合成，不修改源文件。

## 调整图层

先 inspect，确认组、ID、显隐及坐标。同名图层必须使用 ID。以本项目为工作目录：

```powershell
python skills/psd-layer-export/scripts/photocraft_psd.py info "D:/项目/方案.psd"
python skills/psd-layer-export/scripts/photocraft_psd.py edit --in "D:/项目/方案.psd" --ops "D:/项目/移动.json" --out "D:/项目/方案-调整.psd" --preview "D:/项目/调整预览.png"
```

`移动.json` 示例：`[{"op":"move","layer":3,"dx":12,"dy":0}]`；数值单位为像素，正数向右或向下。默认输出新文件；输出已经存在时拒绝覆盖，源文件只有用户明确要求覆盖且传 `--in-place` 才可原地写。源组或目标层隐藏时，PhotoCraft 可能拒绝移动，先确认显示状态。执行后回读图层坐标并检查渲染；不要用 psd-tools 直接改超出画布的图层坐标，已实测出现蒙版错位。具体操作参数参考 `python .../photocraft_psd.py edit --help` 和原 `pd-automation/SKILL.md`。

## 按编组导出

```powershell
python skills/psd-layer-export/scripts/export_psd_groups.py "D:/项目/方案.psd" --out-dir "D:/项目/导出" --list
python skills/psd-layer-export/scripts/export_psd_groups.py "D:/项目/方案.psd" --out-dir "D:/项目/导出"
python skills/psd-layer-export/scripts/export_psd_groups.py "D:/项目/方案.psd" --out-dir "D:/项目/导出" --group "方案1-真人" --format png
```

默认分别输出每个顶层组的 JPG（质量95），`--group` 可重复；每次只显示目标顶层组，其他组隐藏，组内和非组顶层图层保留原显隐。PNG 支持透明，JPG 透明区域填白。文件名冲突或输出已存在时预检并停止；输出后校验 PSD 未变化。导出后逐图检查；智能对象、调整层、复杂蒙版、图层效果和混合模式可能与 Photoshop 有差异，不能无条件声称一致。

2026-11-26 验证：广电真人/动漫两组 PSD 可分别导出，动漫人物通过 PhotoCraft 右移12像素后回读坐标正确且预览正常；该结果不代表所有复杂 PSD 均已验证。
