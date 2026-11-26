# PD-photocraft

**AI 驱动的可编辑分层 PSD 制作、脚本化调图与可视化画布工具**

> 本项目基于 ArtCraft 团队开源的 [PhotoCraft](https://github.com/storytold/photocraft)（纯 Rust 实现、MIT OR Apache-2.0 双协议）进行独立维护与自动化工作流增强，已按上游商标协议移除 `docs/brand/` 中的 ArtCraft 品牌素材。

---

## 核心解决什么问题？

1. **一键组装可编辑分层 PSD**：将 AI 生成或外部准备的多张 PNG 素材按声明式配置自动组装成结构规整的 `.psd`（清晰分组：`背景` / `前景` / `文字` / `调色`，素材默认以**智能对象**置入）。
2. **同一个 PSD 持续迭代，告别重复排版与换素材**：
   - 调整图层或整个图层组位置/缩放/旋转（如移动整个 `前景` 组）
   - **就地换素材**（`swap`）：替换智能对象 PNG 内容，自动保留原位置、缩放、蒙版与所在分组
   - 修改前景图层组可见性、不透明度、混合模式、追加素材或非破坏性调整图层
   - 所有改动直接写回**同一个 `.psd`**，同时可选输出预览图或在桌面端实时查看
3. **所有调整沉淀为脚本（Recipe）**：
   - 每次 AI 操作自动追加记录到同名 `.recipe.json`
   - 支持单图/整批目录无头回放（`replay`），并可与 PhotoCraft 原生 `actions.json` 双向互转（手动在界面 Actions 面板录制的动作也能沉淀为 `.recipe.json`）
4. **桌面 GUI 作为实时可视化画布**：
   - 通过 `--control` 本机鉴权通道或 `photocraft-cli mcp --bridge`，让 AI 直接操控正在运行的桌面界面，你在窗口里实时看到每一步变化。

---

## 快速开始

### 1. 准备运行程序
任选其一：
- **方式 A（推荐，零编译）**：将官方 Windows 便携版解压到仓库根目录（例如 `photocraft-0.8.2-windows-x64-portable/`，已被 `.gitignore` 忽略），`pd_photocraft.py` 会自动发现 `photocraft.exe` 与 `photocraft-cli.exe`。
- **方式 B（从 Rust 源码编译）**：
  ```sh
  cargo build --release -p photocraft -p photocraft-cli
  ```

### 2. 启动实时可视化 GUI + MCP 桥接
```powershell
# 启动带控制通道的桌面 GUI（默认监听 127.0.0.1:7878，token 存放在 .pd-runtime/control.token）
.\pd-automation\launch-gui.ps1

# 如需接入支持 MCP 的 AI 客户端（Antigravity / Claude / Cursor 等），启动桥接：
.\pd-automation\start-mcp-bridge.ps1
```

### 3. 组装可编辑分层 PSD
参考 [starter-layered-psd.scene.json](pd-automation/recipes/starter-layered-psd.scene.json) 编写场景描述，执行：
```sh
python pd-automation/pd_photocraft.py build-psd \
  --spec project.scene.json \
  --out project.psd \
  --preview project-preview.png
```
会同时产出 `project.psd`、`project.recipe.json` 和 `project-preview.png`。

### 4. 在同一个 PSD 上就地迭代（自动沉淀到 Recipe）
```sh
# 查看图层树结构
python pd-automation/pd_photocraft.py inspect --psd project.psd

# 移动整个「前景」图层组（加 --live 可直接操作打开的 GUI 画布）
python pd-automation/pd_photocraft.py move --psd project.psd --layer "前景" --dx 80 --dy -30 --preview project-preview.png

# 就地替换「前景/主角」的 PNG 素材（保留位置、分组与变换）
python pd-automation/pd_photocraft.py swap --psd project.psd --layer "前景/主角" --asset assets/hero_v2.png --preview project-preview.png

# 调整图层组不透明度与混合模式
python pd-automation/pd_photocraft.py set-layer --psd project.psd --layer "前景" --opacity 90 --blend Normal

# 向「前景」图层组追加新 PNG 素材
python pd-automation/pd_photocraft.py add-layer --psd project.psd --group "前景" --name "光效" --asset assets/glow.png --blend Screen

# 添加非破坏性调整图层（曲线 / 色阶 / 自然饱和度 / 色相饱和度等）
python pd-automation/pd_photocraft.py adjust --psd project.psd --group "调色" --kind curves --name "电影曲线" --params '{"points":[[0,0],[64,50],[192,208],[255,255]]}'
```

### 5. 脚本回放 & 手动动作导入
```sh
# 批量将 Recipe 调色脚本应用到整个目录
python pd-automation/pd_photocraft.py replay \
  --recipe pd-automation/recipes/cinematic-grade.recipe.json \
  --batch-in ./input --batch-out ./output --format png

# 将在 GUI Actions 面板手动录制的 actions.json 转为 .recipe.json
python pd-automation/pd_photocraft.py import-actions --actions actions.json --out my-look.recipe.json

# 将 .recipe.json 导出为原生 actions.json
python pd-automation/pd_photocraft.py export-actions --recipe my-look.recipe.json --out actions.json
```

---

## 目录说明

- `pd-automation/pd_photocraft.py`：核心零依赖 Python 自动化引擎（PSD 组装、就地编辑、换素材、Recipe 沉淀/回放、Live GUI 控制通道客户端）
- `pd-automation/launch-gui.ps1`：一键启动带 `--control` 的桌面端
- `pd-automation/start-mcp-bridge.ps1`：一键启动 MCP Bridge 桥接运行中的桌面端
- `pd-automation/SKILL.md`：给 AI Agent 使用的工作流技能说明
- `pd-automation/recipes/`：场景与调色 Recipe 模板
- `crates/` & `apps/`：PhotoCraft 完整 Rust 引擎与 UI 源码（保留 `upstream` 以便同步上游更新）

---

## 协议与致谢

- 代码遵循原仓库双重开源协议：[MIT](LICENSE-MIT) 或 [Apache-2.0](LICENSE-APACHE)。
- 上游项目：[storytold/photocraft](https://github.com/storytold/photocraft)。
