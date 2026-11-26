#!/usr/bin/env python3
"""PhotoCraft CLI wrapper for creating, inspecting, rendering, and editing layered PSD files.

Avoids PowerShell JSON-quoting pitfalls by invoking photocraft-cli.exe via subprocess argv arrays.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_PHOTOCRAFT_CLI = r"C:\pandy\github\tools\photocraft\photocraft-0.8.2-windows-x64-portable\photocraft-cli.exe"


def find_cli(cli_arg: str | None = None) -> str:
    cli = cli_arg or os.environ.get("PHOTOCRAFT_CLI") or DEFAULT_PHOTOCRAFT_CLI
    if not Path(cli).is_file():
        raise FileNotFoundError(
            f"未找到 photocraft-cli.exe：{cli}（可通过 --cli 或环境变量 PHOTOCRAFT_CLI 指定）"
        )
    return cli


def run_cli(cli: str, args: list[str]) -> str:
    proc = subprocess.run(
        [cli, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"photocraft-cli 失败 (rc={proc.returncode}): {detail}")
    return proc.stdout


def get_psd_info(cli: str, psd_path: str) -> dict[str, Any]:
    out = run_cli(cli, ["info", str(Path(psd_path).resolve()), "--compact"])
    return json.loads(out)


def print_layer_summary(info: dict[str, Any]) -> None:
    print(f"PSD: {info.get('path')}  ({info.get('width')}x{info.get('height')}, {info.get('mode')} {info.get('depth')}bit)")
    layers = info.get("layers") or []
    print(f"Layers ({len(layers)}, top -> bottom):")
    def show(items: list[dict[str, Any]], depth: int = 0) -> None:
        for idx, layer in enumerate(items, 1):
            vis = "ON " if layer.get("visible", True) else "OFF"
            op = int(round(float(layer.get("opacity", 1.0)) * 100))
            print(f"  {'  ' * depth}{idx}. [id={layer.get('id')}] {layer.get('name')} | {vis} | "
                  f"opacity={op}% | blend={layer.get('blend')} | bounds={layer.get('bounds')}")
            show(layer.get('children') or [], depth + 1)
    show(layers)


def resolve_layer_id(info: dict[str, Any], target: int | str) -> int:
    if isinstance(target, int):
        return target
    if isinstance(target, str) and target.isdigit():
        return int(target)
    def flatten(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for layer in layers for item in [layer, *flatten(layer.get("children") or [])]]

    all_layers = flatten(info.get("layers") or [])
    matches = [l["id"] for l in all_layers if l.get("name") == target]
    if not matches:
        names = [l.get("name") for l in all_layers]
        raise ValueError(f"找不到图层 '{target}'，当前图层：{names}")
    if len(matches) > 1:
        raise ValueError(f"存在多个同名图层 '{target}' (ids={matches})，请改用 layer id 指定")
    return int(matches[0])


def cmd_info(args: argparse.Namespace) -> None:
    cli = find_cli(args.cli)
    info = get_psd_info(cli, args.psd)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print_layer_summary(info)


def cmd_render(args: argparse.Namespace) -> None:
    cli = find_cli(args.cli)
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_cli(cli, ["convert", str(Path(args.psd).resolve()), str(out_path)])
    print(f"Rendered: {out_path}")


def parse_layer_spec_str(spec: str) -> dict[str, Any]:
    """Parse 'path|name|scale|cx|cy' where trailing fields are optional."""
    parts = spec.split("|")
    item: dict[str, Any] = {"path": parts[0].strip()}
    if len(parts) > 1 and parts[1].strip():
        item["name"] = parts[1].strip()
    if len(parts) > 2 and parts[2].strip():
        item["scale"] = float(parts[2].strip())
    if len(parts) > 4 and parts[3].strip() and parts[4].strip():
        item["center"] = [float(parts[3].strip()), float(parts[4].strip())]
    return item


def cmd_compose(args: argparse.Namespace) -> None:
    cli = find_cli(args.cli)
    layers: list[dict[str, Any]] = []
    bg = args.bg
    new_doc = args.new

    if args.spec:
        spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        bg = spec_data.get("bg", bg)
        new_doc = spec_data.get("new", new_doc)
        layers.extend(spec_data.get("layers") or [])

    for raw in args.layer or []:
        layers.append(parse_layer_spec_str(raw))

    if not bg and not new_doc:
        raise ValueError("必须提供 --bg <底图路径> 或 --new <宽x高>（或在 --spec JSON 中指定）")

    cli_args: list[str] = ["run"]
    if bg:
        cli_args.append(str(Path(bg).resolve()))
    else:
        w_str, h_str = str(new_doc).lower().split("x", 1)
        cli_args.extend(["--new", json.dumps({"width": int(w_str), "height": int(h_str)})])

    for layer in layers:
        lpath = str(Path(layer["path"]).resolve())
        place_params: dict[str, Any] = {
            "path": lpath,
            "scale": float(layer.get("scale", 100)),
            "fit": bool(layer.get("fit", False)),
            "smartObject": bool(layer.get("smartObject", False)),
        }
        if "center" in layer and layer["center"] is not None:
            place_params["center"] = layer["center"]
        cli_args.extend(["--cmd", "file.placeEmbedded", "--params", json.dumps(place_params, ensure_ascii=False)])

        props: dict[str, Any] = {}
        for k in ("name", "visible", "opacity", "blend", "clipped"):
            if k in layer and layer[k] is not None:
                props[k] = layer[k]
        if props:
            cli_args.extend(["--cmd", "layer.setProps", "--params", json.dumps(props, ensure_ascii=False)])

    out_psd = Path(args.out).resolve()
    out_psd.parent.mkdir(parents=True, exist_ok=True)
    cli_args.extend(["--out", str(out_psd)])
    run_cli(cli, cli_args)

    info = get_psd_info(cli, str(out_psd))
    print_layer_summary(info)

    if args.preview:
        preview_path = Path(args.preview).resolve()
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        run_cli(cli, ["convert", str(out_psd), str(preview_path)])
        print(f"Preview: {preview_path}")


def cmd_edit(args: argparse.Namespace) -> None:
    cli = find_cli(args.cli)
    in_psd = Path(args.input).resolve()
    out_psd = Path(args.out).resolve() if args.out else in_psd
    if out_psd == in_psd and not args.in_place:
        raise ValueError("禁止原地覆盖原 PSD；请指定新的 --out 路径，或显式传 --in-place")
    if out_psd != in_psd and out_psd.exists():
        raise FileExistsError(f"输出文件已存在，不覆盖：{out_psd}")

    info = get_psd_info(cli, str(in_psd))
    ops_raw = json.loads(Path(args.ops).read_text(encoding="utf-8"))
    ops = ops_raw.get("ops", ops_raw) if isinstance(ops_raw, dict) else ops_raw
    if not isinstance(ops, list) or not ops:
        raise ValueError("ops JSON 必须是非空操作数组")

    cli_args: list[str] = ["run", str(in_psd)]
    for op in ops:
        action = str(op.get("op") or "").strip().lower()
        if action == "props":
            lid = resolve_layer_id(info, op["layer"])
            params: dict[str, Any] = {"layer": lid}
            for k in ("name", "visible", "opacity", "fill", "blend", "clipped", "locked"):
                if k in op:
                    params[k] = op[k]
            cli_args.extend(["--cmd", "layer.setProps", "--params", json.dumps(params, ensure_ascii=False)])
        elif action == "move":
            lid = resolve_layer_id(info, op["layer"])
            params = {"layer": lid, "dx": int(op.get("dx", 0)), "dy": int(op.get("dy", 0))}
            cli_args.extend(["--cmd", "layer.translate", "--params", json.dumps(params, ensure_ascii=False)])
        elif action == "reorder":
            lid = resolve_layer_id(info, op["layer"])
            tid = resolve_layer_id(info, op["target"])
            pos = str(op.get("position", "above"))
            cli_args.extend(
                ["--cmd", "layer.moveTo", "--params", json.dumps({"layer": lid, "target": tid, "position": pos})]
            )
        elif action == "delete":
            lid = resolve_layer_id(info, op["layer"])
            cli_args.extend(["--cmd", "layer.select", "--params", json.dumps({"layer": lid})])
            cli_args.extend(["--cmd", "layer.delete"])
        elif action == "add":
            place_params = {
                "path": str(Path(op["path"]).resolve()),
                "scale": float(op.get("scale", 100)),
                "fit": bool(op.get("fit", False)),
                "smartObject": bool(op.get("smartObject", False)),
            }
            if "center" in op and op["center"] is not None:
                place_params["center"] = op["center"]
            cli_args.extend(["--cmd", "file.placeEmbedded", "--params", json.dumps(place_params, ensure_ascii=False)])
            props = {k: op[k] for k in ("name", "visible", "opacity", "blend", "clipped") if k in op}
            if props:
                cli_args.extend(["--cmd", "layer.setProps", "--params", json.dumps(props, ensure_ascii=False)])
        elif action == "replace":
            lid = resolve_layer_id(info, op["layer"])
            old_layer = next(l for l in info["layers"] if l["id"] == lid)
            bx, by, bw, bh = old_layer["bounds"]
            default_center = [bx + bw / 2.0, by + bh / 2.0]
            cli_args.extend(["--cmd", "layer.select", "--params", json.dumps({"layer": lid})])
            place_params = {
                "path": str(Path(op["path"]).resolve()),
                "scale": float(op.get("scale", 100)),
                "fit": bool(op.get("fit", False)),
                "smartObject": bool(op.get("smartObject", False)),
                "center": op.get("center", default_center),
            }
            cli_args.extend(["--cmd", "file.placeEmbedded", "--params", json.dumps(place_params, ensure_ascii=False)])
            new_name = op.get("name", old_layer.get("name"))
            props = {"name": new_name}
            for k in ("visible", "opacity", "blend", "clipped"):
                if k in op:
                    props[k] = op[k]
            cli_args.extend(["--cmd", "layer.setProps", "--params", json.dumps(props, ensure_ascii=False)])
            cli_args.extend(["--cmd", "layer.select", "--params", json.dumps({"layer": lid})])
            cli_args.extend(["--cmd", "layer.delete"])
        elif action == "raw":
            cli_args.extend(["--cmd", str(op["cmd"])])
            if "params" in op and op["params"] is not None:
                cli_args.extend(["--params", json.dumps(op["params"], ensure_ascii=False)])
        else:
            raise ValueError(f"不支持的 op 类型：{action}")

    out_psd.parent.mkdir(parents=True, exist_ok=True)
    cli_args.extend(["--out", str(out_psd)])
    run_cli(cli, cli_args)

    new_info = get_psd_info(cli, str(out_psd))
    print_layer_summary(new_info)

    if args.preview:
        preview_path = Path(args.preview).resolve()
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        run_cli(cli, ["convert", str(out_psd), str(preview_path)])
        print(f"Preview: {preview_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PhotoCraft CLI 分层 PSD 生成与修改工具")
    parser.add_argument("--cli", help="photocraft-cli.exe 路径（默认读取 PHOTOCRAFT_CLI 或本机便携版路径）")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_info = sub.add_parser("info", help="查看 PSD 画布与图层树")
    p_info.add_argument("psd", help="PSD 文件路径")
    p_info.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_info.set_defaults(func=cmd_info)

    p_render = sub.add_parser("render", help="将 PSD 渲染导出为 PNG/JPG 预览图")
    p_render.add_argument("psd", help="PSD 文件路径")
    p_render.add_argument("out", help="输出图片路径（.png / .jpg）")
    p_render.set_defaults(func=cmd_render)

    p_comp = sub.add_parser("compose", help="从底图和多层素材新建分层 PSD")
    p_comp.add_argument("--bg", help="底图路径（如背景 JPG/PNG）")
    p_comp.add_argument("--new", help="空白画布尺寸，如 768x1024")
    p_comp.add_argument("--spec", help="JSON 配置文件（含 bg/new 与 layers 数组）")
    p_comp.add_argument(
        "--layer",
        action="append",
        help="叠加图层（自下而上），格式：path|name|scale|cx|cy（可重复多次）",
    )
    p_comp.add_argument("--out", required=True, help="输出 PSD 路径")
    p_comp.add_argument("--preview", help="同步导出合成预览图路径（可选）")
    p_comp.set_defaults(func=cmd_compose)

    p_edit = sub.add_parser("edit", help="修改已有 PSD（移动/改名/显隐/替换/新增/删层/调层序）")
    p_edit.add_argument("--in", dest="input", required=True, help="源 PSD 路径")
    p_edit.add_argument("--ops", required=True, help="修改操作 JSON 文件路径")
    p_edit.add_argument("--out", help="输出新 PSD 路径（默认不覆盖源 PSD）")
    p_edit.add_argument("--in-place", action="store_true", help="显式允许原地覆盖源 PSD")
    p_edit.add_argument("--preview", help="同步导出合成预览图路径（可选）")
    p_edit.set_defaults(func=cmd_edit)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
