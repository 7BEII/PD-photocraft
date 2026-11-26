---
name: pd-photocraft
description: Assemble generated PNGs into structured editable layered PSDs, iteratively modify positions/foreground groups/assets in-place on the SAME PSD, drive the live PhotoCraft GUI visually, and persist every edit as a replayable Recipe script.
---

# PD-photocraft Agent Workflow

Use `pd-automation/pd_photocraft.py` whenever the user asks for:
- **"给我可编辑 PSD" / assembling generated PNGs into a clean layered PSD**
- **Iterative edits on the SAME PSD** (repositioning layers or groups, modifying the `前景` group, swapping PNG assets without rebuilding, tweaking opacity/blend/adjustments)
- **Live visual editing** in the PhotoCraft desktop GUI (`--live`)
- **Persisting / replaying adjustments** as `.recipe.json` scripts or native `actions.json`

## Standard Workflow

### 1. Build a structured editable PSD from PNGs
Create a declarative `scene.json` with clear groups (`背景`, `前景`, `文字`, `调色`), placing PNGs as embedded Smart Objects (so they can be swapped later without losing position/scale):

```sh
python pd-automation/pd_photocraft.py build-psd \
  --spec project.scene.json \
  --out project.psd \
  --preview project-preview.png
```
This outputs:
- `project.psd` (editable layered PSD)
- `project.recipe.json` (complete replayable script)
- `project-preview.png` (visual check)

### 2. Edit the SAME PSD in-place (and auto-record to Recipe)
Every command below modifies `project.psd` in-place AND appends the step to `project.recipe.json`. Add `--live` to apply directly to the open desktop GUI canvas!

- **Inspect layer tree first**:
  ```sh
  python pd-automation/pd_photocraft.py inspect --psd project.psd
  ```
- **Move / scale / rotate a layer or entire group** (e.g., move `前景` or `前景/主角`):
  ```sh
  python pd-automation/pd_photocraft.py move --psd project.psd --layer "前景" --dx 80 --dy -30 --preview project-preview.png
  ```
- **Swap a PNG asset in-place (换素材)** without losing position, transform, or group membership:
  ```sh
  python pd-automation/pd_photocraft.py swap --psd project.psd --layer "前景/主角" --asset hero_v2.png --preview project-preview.png
  ```
- **Modify layer/group properties** (visibility, opacity, blend mode, rename):
  ```sh
  python pd-automation/pd_photocraft.py set-layer --psd project.psd --layer "前景" --opacity 85 --blend Normal
  ```
- **Add a new PNG into an existing group**:
  ```sh
  python pd-automation/pd_photocraft.py add-layer --psd project.psd --group "前景" --name "光效" --asset glow.png --blend Screen
  ```
- **Add a non-destructive adjustment layer**:
  ```sh
  python pd-automation/pd_photocraft.py adjust --psd project.psd --group "调色" --kind curves --name "暖调曲线" --params '{"points":[[0,0],[128,142],[255,255]]}'
  ```

### 3. Live GUI Visual Mode + MCP Bridge
1. Launch desktop GUI with control channel:
   ```powershell
   .\pd-automation\launch-gui.ps1 -Open .\project.psd
   ```
2. Either pass `--live` to any `pd_photocraft.py` command, OR attach an MCP client via:
   ```powershell
   .\pd-automation\start-mcp-bridge.ps1
   ```

### 4. Convert Manual GUI Actions ↔ Recipe Scripts
- Manual recording from PhotoCraft Actions panel → `.recipe.json`:
  ```sh
  python pd-automation/pd_photocraft.py import-actions --actions actions.json --out my.recipe.json
  ```
- Replay a `.recipe.json` onto a folder of images:
  ```sh
  python pd-automation/pd_photocraft.py replay --recipe my.recipe.json --batch-in ./in --batch-out ./out --format png
  ```
