#!/usr/bin/env python3
"""PD-photocraft Automation & Scriptable PSD Engine.

Zero-dependency Python 3.10+ bridge for:
1. Building structured, editable layered PSDs (groups, Smart Object PNGs, text, adjustments)
   from declarative scene specs.
2. Iteratively editing the SAME PSD in-place (move/transform layers or groups, swap PNG assets
   without losing layout, tweak foreground groups, add adjustment layers).
3. Driving the live PhotoCraft desktop GUI as a real-time visual canvas via `--control`
   (HMAC-SHA256 challenge auth) OR running headlessly via `photocraft-cli serve`.
4. Automatically persisting every adjustment/edit into replayable `.recipe.json` scripts
   and converting to/from native PhotoCraft `actions.json`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / ".pd-runtime"
DEFAULT_PORT = 7878
DEFAULT_TOKEN_FILE = RUNTIME_DIR / "control.token"


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

def find_binaries(bin_dir: str | None = None) -> tuple[Path | None, Path]:
    """Locate (photocraft_gui_exe, photocraft_cli_exe)."""
    candidates: list[Path] = []
    if bin_dir:
        candidates.append(Path(bin_dir))
    env_dir = os.environ.get("PHOTOCRAFT_BIN_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend([
        REPO_ROOT / "target" / "release",
        REPO_ROOT / "target" / "debug",
    ])
    for p in sorted(REPO_ROOT.glob("photocraft-*-portable*"), reverse=True):
        if p.is_dir():
            candidates.append(p)
    candidates.append(REPO_ROOT)

    exe_ext = ".exe" if os.name == "nt" else ""
    cli_name = f"photocraft-cli{exe_ext}"
    gui_name = f"photocraft{exe_ext}"

    found_gui: Path | None = None
    found_cli: Path | None = None
    for d in candidates:
        c = d / cli_name
        g = d / gui_name
        if c.is_file() and found_cli is None:
            found_cli = c.resolve()
        if g.is_file() and found_gui is None:
            found_gui = g.resolve()
        if found_cli and found_gui:
            break

    if found_cli is None:
        raise FileNotFoundError(
            f"Could not find `{cli_name}`. Pass --bin-dir, set PHOTOCRAFT_BIN_DIR, "
            f"place a portable release under {REPO_ROOT}, or build with `cargo build --release`."
        )
    return found_gui, found_cli


# ---------------------------------------------------------------------------
# Transport clients: Live GUI TCP (`--control`) & Headless stdio (`serve`)
# ---------------------------------------------------------------------------

class ControlError(RuntimeError):
    pass


class BaseSession:
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    def execute(self, command: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"command": command}
        if params is not None:
            payload["params"] = params
        return self.call("engine.execute", payload)

    def close(self) -> None:
        pass

    def __enter__(self) -> "BaseSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class LiveGuiSession(BaseSession):
    """Connect to a running `photocraft --control <port>` over loopback TCP."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str | None = None, token_file: Path | None = None):
        self.host = host
        self.port = port
        self._id = 0
        tok = token
        if not tok:
            tf = token_file or DEFAULT_TOKEN_FILE
            if not tf.is_file():
                raise ControlError(
                    f"Control token file not found: {tf}. Start GUI first with `pd_photocraft.py launch-gui`."
                )
            tok = tf.read_text(encoding="utf-8").strip()
        self.token = tok.lower()
        self.sock = socket.create_connection((self.host, self.port), timeout=60.0)
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")
        self._authenticate()

    def _raw_call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {}}
        self.wfile.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.wfile.flush()
        line = self.rfile.readline()
        if not line:
            raise ControlError("Connection closed by PhotoCraft GUI.")
        return json.loads(line)

    def _authenticate(self) -> None:
        client_nonce = secrets.token_hex(32)
        resp = self._raw_call("auth.challenge", {"clientNonce": client_nonce})
        if resp.get("ok"):
            res = resp["result"]
            server_nonce = res["serverNonce"].lower()
            server_proof = res["serverProof"].lower()
            key = self.token.encode("ascii")
            expected_server = hmac.new(
                key,
                f"photocraft-control-v1 server\n{client_nonce}\n{server_nonce}".encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(server_proof, expected_server):
                self.close()
                raise ControlError("Server proof mismatch during challenge auth.")
            client_proof = hmac.new(
                key,
                f"photocraft-control-v1 client\n{client_nonce}\n{server_nonce}".encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            auth_resp = self._raw_call("auth", {"proof": client_proof})
            if not auth_resp.get("ok"):
                raise ControlError(f"Challenge auth failed: {auth_resp.get('error')}")
            return

        # Fallback for older builds that only accept direct token auth
        auth_resp = self._raw_call("auth", {"token": self.token})
        if not auth_resp.get("ok"):
            raise ControlError(f"Token auth failed: {auth_resp.get('error')}")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._raw_call(method, params)
        if not resp.get("ok"):
            raise ControlError(f"{method} failed: {resp.get('error')}")
        return resp.get("result")

    def close(self) -> None:
        for obj in (self.rfile, self.wfile, self.sock):
            try:
                obj.close()
            except Exception:
                pass


class HeadlessSession(BaseSession):
    """Spawn a persistent `photocraft-cli serve` process over stdio."""

    def __init__(self, cli_exe: Path, read_root: Path, write_root: Path):
        self._id = 0
        cmd = [
            str(cli_exe),
            "serve",
            "--automation-read-root",
            str(read_root.resolve()),
            "--automation-write-root",
            str(write_root.resolve()),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise ControlError(f"photocraft-cli serve exited unexpectedly.\n{stderr}")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise ControlError(f"{method} failed: {resp.get('error')}")
        return resp.get("result")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


# ---------------------------------------------------------------------------
# Layer tree helpers
# ---------------------------------------------------------------------------

def flatten_layers(layers: list[dict[str, Any]], parent_path: str = "") -> list[dict[str, Any]]:
    """Flatten nested doc.inspect layer tree with full group path metadata."""
    out: list[dict[str, Any]] = []
    for layer in layers:
        name = layer.get("name", "")
        full_path = f"{parent_path}/{name}" if parent_path else name
        item = dict(layer)
        item["_path"] = full_path
        out.append(item)
        children = layer.get("children") or []
        if children:
            out.extend(flatten_layers(children, full_path))
    return out


def find_layer(doc: dict[str, Any], target: str | int) -> dict[str, Any]:
    """Find a layer or group by numeric ID, exact path (`前景/主角`), or exact name."""
    flat = flatten_layers(doc.get("layers") or [])
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        tid = int(target)
        for item in flat:
            if item.get("id") == tid:
                return item
        raise ControlError(f"Layer ID {tid} not found.")

    # Prefer exact full path match, then exact name match
    for item in flat:
        if item["_path"] == target:
            return item
    matches = [item for item in flat if item.get("name") == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = ", ".join(m["_path"] for m in matches)
        raise ControlError(f"Ambiguous layer name `{target}`. Use full path: {paths}")
    available = ", ".join(m["_path"] for m in flat)
    raise ControlError(f"Layer `{target}` not found. Available: {available}")


def print_layer_tree(doc: dict[str, Any]) -> None:
    w, h = doc.get("width", "?"), doc.get("height", "?")
    mode = doc.get("colorMode") or doc.get("mode") or "?"
    depth = doc.get("bitDepth") or doc.get("depth") or "?"
    print(f"Document: {doc.get('name', '(untitled)')} ({w}x{h}, mode={mode}, depth={depth})")

    def _walk(nodes: list[dict[str, Any]], indent: int = 0) -> None:
        for n in nodes:
            vis = "V" if n.get("visible", True) else "H"
            op = round(float(n.get("opacity", 1.0)) * 100)
            blend = n.get("blendMode") or n.get("blend") or "Normal"
            kind = n.get("kind", "?")
            bounds = n.get("bounds")
            bstr = f" bounds={bounds}" if bounds else ""
            print(f"{'  ' * indent}- [{vis}] #{n.get('id')} {n.get('name')} ({kind}, {op}%, {blend}){bstr}")
            if n.get("children"):
                _walk(n["children"], indent + 1)

    _walk(doc.get("layers") or [])


def normalize_opacity(val: float) -> float:
    """Accept either 0..1 or 0..100 opacity and normalize to engine's 0.0..1.0."""
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(1.0, val))


# ---------------------------------------------------------------------------
# Recipe persistence (`.recipe.json` <-> native `actions.json`)
# ---------------------------------------------------------------------------

def default_recipe_path(psd_path: Path) -> Path:
    return psd_path.with_suffix(".recipe.json")


def load_or_init_recipe(recipe_path: Path, psd_path: Path | None = None, name: str | None = None) -> dict[str, Any]:
    if recipe_path.is_file():
        return json.loads(recipe_path.read_text(encoding="utf-8"))
    return {
        "schema": "pd-photocraft/recipe/v1",
        "name": name or (psd_path.stem if psd_path else "untitled"),
        "targetPsd": str(psd_path) if psd_path else None,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "steps": [],
    }


def append_recipe_steps(recipe_path: Path, steps: list[dict[str, Any]], psd_path: Path | None = None) -> None:
    recipe = load_or_init_recipe(recipe_path, psd_path=psd_path)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    for st in steps:
        entry = dict(st)
        entry.setdefault("at", ts)
        recipe["steps"].append(entry)
    recipe["updatedAt"] = ts
    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_recipe_to_actions(recipe: dict[str, Any]) -> dict[str, Any]:
    """Convert PD recipe steps into PhotoCraft's native Actions panel JSON."""
    action_steps: list[dict[str, Any]] = []
    for st in recipe.get("steps", []):
        if st.get("type") == "command" and st.get("command"):
            action_steps.append({
                "command": st["command"],
                "params": st.get("params") or {},
            })
    return {
        "actions": [
            {
                "name": recipe.get("name", "PD Recipe"),
                "steps": action_steps,
            }
        ]
    }


def import_actions_to_recipe(actions_data: Any, action_name: str | None = None) -> dict[str, Any]:
    """Convert PhotoCraft native `actions.json` (manual recordings) to `.recipe.json`."""
    raw_steps: list[Any] = []
    chosen_name = action_name or "Imported Action"
    if isinstance(actions_data, list):
        raw_steps = actions_data
    elif isinstance(actions_data, dict):
        if "steps" in actions_data and isinstance(actions_data["steps"], list):
            raw_steps = actions_data["steps"]
            chosen_name = actions_data.get("name", chosen_name)
        elif "actions" in actions_data and isinstance(actions_data["actions"], list):
            acts = actions_data["actions"]
            if action_name:
                matched = [a for a in acts if isinstance(a, dict) and a.get("name") == action_name]
                if not matched:
                    raise ControlError(f"Action `{action_name}` not found in actions.json.")
                raw_steps = matched[0].get("steps", [])
                chosen_name = matched[0].get("name", chosen_name)
            elif acts and isinstance(acts[0], dict) and "steps" in acts[0]:
                raw_steps = acts[0].get("steps", [])
                chosen_name = acts[0].get("name", chosen_name)
            else:
                raw_steps = acts

    recipe_steps: list[dict[str, Any]] = []
    for item in raw_steps:
        if isinstance(item, str):
            recipe_steps.append({"type": "command", "command": item, "params": {}})
        elif isinstance(item, list) and len(item) >= 1:
            recipe_steps.append({
                "type": "command",
                "command": item[0],
                "params": item[1] if len(item) > 1 and isinstance(item[1], dict) else {},
            })
        elif isinstance(item, dict) and "command" in item:
            recipe_steps.append({
                "type": "command",
                "command": item["command"],
                "params": item.get("params") or {},
            })

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "schema": "pd-photocraft/recipe/v1",
        "name": chosen_name,
        "createdAt": now,
        "updatedAt": now,
        "steps": recipe_steps,
    }


# ---------------------------------------------------------------------------
# High-level PSD operations (shared by CLI, Live GUI, and Recipe Replay)
# ---------------------------------------------------------------------------

def resolve_read_write_roots(paths: list[Path]) -> tuple[Path, Path]:
    """Compute a common ancestor directory covering all referenced paths."""
    existing = [p.resolve().parent if not p.is_dir() else p.resolve() for p in paths]
    if not existing:
        cwd = Path.cwd().resolve()
        return cwd, cwd
    try:
        common = Path(os.path.commonpath([str(p) for p in existing]))
    except ValueError:
        common = existing[0]
    return common, common


def open_session(args: argparse.Namespace, paths: list[Path]) -> BaseSession:
    if getattr(args, "live", False):
        return LiveGuiSession(
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", DEFAULT_PORT),
            token=getattr(args, "token", None),
            token_file=Path(args.token_file) if getattr(args, "token_file", None) else DEFAULT_TOKEN_FILE,
        )
    _, cli_exe = find_binaries(getattr(args, "bin_dir", None))
    read_root, write_root = resolve_read_write_roots(paths)
    if getattr(args, "root", None):
        r = Path(args.root).resolve()
        read_root, write_root = r, r
    return HeadlessSession(cli_exe, read_root, write_root)


def place_asset(
    session: BaseSession,
    asset_path: Path,
    name: str | None = None,
    group_name: str | None = None,
    dx: float = 0.0,
    dy: float = 0.0,
    scale: float | None = None,
    opacity: float | None = None,
    blend: str | None = None,
    visible: bool | None = None,
) -> int:
    """Place a PNG/image as an embedded Smart Object, optionally move into a group & position it."""
    place_params: dict[str, Any] = {"path": str(asset_path.resolve())}
    if scale is not None:
        place_params["scale"] = scale
    res = session.call("doc.place", place_params)
    layer_id = res.get("layer") if isinstance(res, dict) else None
    if layer_id is None:
        doc = session.call("doc.inspect")
        layer_id = (doc.get("layers") or [{}])[0].get("id")

    props: dict[str, Any] = {"layer": layer_id}
    if name:
        props["name"] = name
    if opacity is not None:
        props["opacity"] = normalize_opacity(opacity)
    if blend:
        props["blend"] = blend
    if visible is not None:
        props["visible"] = visible
    if len(props) > 1:
        session.execute("layer.setProps", props)

    if group_name:
        doc = session.call("doc.inspect")
        grp = find_layer(doc, group_name)
        session.execute("layer.moveTo", {"layer": layer_id, "target": grp["id"], "position": "into"})

    if dx != 0 or dy != 0:
        session.execute("layer.translate", {"layer": layer_id, "dx": int(round(dx)), "dy": int(round(dy))})

    return int(layer_id)


def swap_smart_object(session: BaseSession, layer_target: str | int, asset_path: Path) -> dict[str, Any]:
    """Replace contents of a Smart Object layer in-place without losing position/group/transform."""
    doc = session.call("doc.inspect")
    layer = find_layer(doc, layer_target)
    lid = layer["id"]
    resolved = str(asset_path.resolve())
    try:
        return session.call("doc.replaceSmartObject", {"layer": lid, "path": resolved})
    except ControlError:
        # Compatibility fallback for builds (e.g. v0.8.2) without `doc.replaceSmartObject`:
        # Place the new PNG as a Smart Object, move it directly above the old layer (same group & z-order),
        # match its exact (x, y) bounds & layer properties, then remove the old layer.
        old_bounds = layer.get("bounds") or [0, 0, 0, 0]
        old_name = layer.get("name", asset_path.stem)
        old_opacity = float(layer.get("opacity", 1.0))
        old_blend = layer.get("blendMode") or layer.get("blend") or "Normal"
        old_visible = bool(layer.get("visible", True))

        placed = session.call("doc.place", {"path": resolved})
        new_id = placed.get("layer") if isinstance(placed, dict) else None
        if new_id is None:
            new_doc = session.call("doc.inspect")
            new_id = (new_doc.get("layers") or [{}])[0].get("id")

        session.execute("layer.moveTo", {"layer": new_id, "target": lid, "position": "above"})
        new_doc = session.call("doc.inspect")
        new_layer = find_layer(new_doc, new_id)
        new_bounds = new_layer.get("bounds") or [0, 0, 0, 0]
        dx = int(round(old_bounds[0] - new_bounds[0]))
        dy = int(round(old_bounds[1] - new_bounds[1]))
        if dx != 0 or dy != 0:
            session.execute("layer.translate", {"layer": new_id, "dx": dx, "dy": dy})

        session.execute("layer.setProps", {
            "layer": new_id,
            "name": old_name,
            "opacity": old_opacity,
            "blend": old_blend,
            "visible": old_visible,
        })
        session.execute("layer.delete", {"layer": lid})
        return {"replaced": old_name, "oldLayer": lid, "newLayer": new_id, "method": "place-and-match"}


def apply_recipe_step(session: BaseSession, step: dict[str, Any], base_dir: Path) -> Any:
    """Execute one structured PD recipe step against an open session."""
    stype = step.get("type", "command")
    if stype == "command":
        cmd = step["command"]
        params = dict(step.get("params") or {})
        if "layerName" in step:
            doc = session.call("doc.inspect")
            lid = find_layer(doc, step["layerName"])["id"]
            params.setdefault("layer", lid)
            session.execute("layer.select", {"layer": lid, "mode": "replace"})
        return session.execute(cmd, params)

    if stype == "move":
        doc = session.call("doc.inspect")
        lid = find_layer(doc, step["layer"])["id"]
        dx = float(step.get("dx", 0))
        dy = float(step.get("dy", 0))
        if dx != 0 or dy != 0:
            session.execute("layer.translate", {"layer": lid, "dx": int(round(dx)), "dy": int(round(dy))})
        sx = step.get("scaleX")
        sy = step.get("scaleY")
        rot = step.get("rotate")
        if sx is not None or sy is not None or rot is not None:
            session.execute("layer.select", {"layer": lid, "mode": "replace"})
            tp: dict[str, Any] = {}
            if sx is not None:
                tp["scaleX"] = float(sx) * 100.0 if float(sx) <= 10.0 else float(sx)
            if sy is not None:
                tp["scaleY"] = float(sy) * 100.0 if float(sy) <= 10.0 else float(sy)
            if rot is not None:
                tp["angle"] = float(rot)
            session.execute("edit.transform", tp)
        return {"moved": step["layer"], "id": lid}

    if stype == "swap":
        asset = Path(step["asset"])
        if not asset.is_absolute():
            asset = (base_dir / asset).resolve()
        return swap_smart_object(session, step["layer"], asset)

    if stype == "set-layer":
        doc = session.call("doc.inspect")
        lid = find_layer(doc, step["layer"])["id"]
        props: dict[str, Any] = {"layer": lid}
        for k in ("name", "visible", "blend"):
            if k in step and step[k] is not None:
                props[k] = step[k]
        if "opacity" in step and step["opacity"] is not None:
            props["opacity"] = normalize_opacity(float(step["opacity"]))
        return session.execute("layer.setProps", props)

    if stype == "add-layer":
        asset = Path(step["asset"])
        if not asset.is_absolute():
            asset = (base_dir / asset).resolve()
        return place_asset(
            session,
            asset_path=asset,
            name=step.get("name"),
            group_name=step.get("group"),
            dx=float(step.get("dx", 0)),
            dy=float(step.get("dy", 0)),
            scale=step.get("scale"),
            opacity=step.get("opacity"),
            blend=step.get("blend"),
            visible=step.get("visible"),
        )

    if stype == "adjust":
        cmd = f"layer.newAdjustmentLayer.{step['kind']}"
        res = session.execute(cmd, step.get("params") or {})
        lid = res.get("layer") if isinstance(res, dict) else None
        if lid is not None:
            if step.get("name"):
                session.execute("layer.setProps", {"layer": lid, "name": step["name"]})
            if step.get("group"):
                doc = session.call("doc.inspect")
                grp = find_layer(doc, step["group"])
                session.execute("layer.moveTo", {"layer": lid, "target": grp["id"], "position": "into"})
        return res

    raise ControlError(f"Unknown recipe step type: {stype}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_launch_gui(args: argparse.Namespace) -> None:
    gui_exe, _ = find_binaries(args.bin_dir)
    if gui_exe is None:
        raise FileNotFoundError("Could not locate desktop `photocraft.exe` binary.")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    token_file = Path(args.token_file) if args.token_file else DEFAULT_TOKEN_FILE
    if not token_file.is_file():
        token_file.write_text(secrets.token_hex(32), encoding="utf-8")
    root = Path(args.root).resolve() if args.root else REPO_ROOT.resolve()
    cmd = [
        str(gui_exe),
        "--control",
        str(args.port),
        "--control-token-file",
        str(token_file.resolve()),
        "--automation-read-root",
        str(root),
        "--automation-write-root",
        str(root),
    ]
    if args.open:
        cmd.append(str(Path(args.open).resolve()))
    proc = subprocess.Popen(cmd)
    print(f"Launched PhotoCraft GUI (pid={proc.pid}) on 127.0.0.1:{args.port}")
    print(f"Token file: {token_file.resolve()}")
    print(f"Authorized root: {root}")
    # Wait briefly to confirm TCP port opens
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", args.port), timeout=0.5):
                print("Control channel is ready for AI commands (`--live`) and MCP bridge.")
                return
        except OSError:
            time.sleep(0.25)
    print("Note: GUI process started; control port may still be initializing.")


def cmd_build_psd(args: argparse.Namespace) -> None:
    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_psd = Path(args.out).resolve()
    out_psd.parent.mkdir(parents=True, exist_ok=True)
    recipe_path = Path(args.recipe).resolve() if args.recipe else default_recipe_path(out_psd)

    asset_paths: list[Path] = [spec_path, out_psd]
    if args.preview:
        asset_paths.append(Path(args.preview).resolve())

    def _collect_assets(nodes: list[dict[str, Any]]) -> None:
        for n in nodes:
            if n.get("asset"):
                p = Path(n["asset"])
                asset_paths.append(p if p.is_absolute() else (spec_path.parent / p).resolve())
            if n.get("layers"):
                _collect_assets(n["layers"])

    _collect_assets(spec.get("groups", []))
    _collect_assets(spec.get("layers", []))

    recorded_steps: list[dict[str, Any]] = []

    with open_session(args, asset_paths) as session:
        canvas = spec.get("canvas") or {}
        new_params = {
            "width": int(canvas.get("width", 1920)),
            "height": int(canvas.get("height", 1080)),
            "background": canvas.get("background", "transparent"),
            "name": spec.get("name", out_psd.stem),
        }
        session.call("doc.new", new_params)
        recorded_steps.append({"type": "command", "command": "file.new", "params": new_params, "note": "Create canvas"})

        def _build_node(node: dict[str, Any], parent_group: str | None = None) -> None:
            ntype = node.get("type", "image")
            if ntype == "group" or "layers" in node:
                gname = node["name"]
                top_root_id = None
                if parent_group is None:
                    doc_before = session.call("doc.inspect")
                    roots = doc_before.get("layers") or []
                    if roots:
                        top_root_id = roots[0]["id"]
                        session.execute("layer.select", {"layer": top_root_id, "mode": "replace"})
                res = session.execute("layer.new.group", {"name": gname})
                gid = res.get("layer") if isinstance(res, dict) else None
                recorded_steps.append({"type": "command", "command": "layer.new.group", "params": {"name": gname}})
                if parent_group and gid is not None:
                    doc = session.call("doc.inspect")
                    pgrp = find_layer(doc, parent_group)
                    session.execute("layer.moveTo", {"layer": gid, "target": pgrp["id"], "position": "into"})
                elif parent_group is None and gid is not None and top_root_id is not None and gid != top_root_id:
                    session.execute("layer.moveTo", {"layer": gid, "target": top_root_id, "position": "above"})
                if gid is not None:
                    gprops: dict[str, Any] = {"layer": gid}
                    if "opacity" in node:
                        gprops["opacity"] = normalize_opacity(float(node["opacity"]))
                    if "visible" in node:
                        gprops["visible"] = bool(node["visible"])
                    if "blend" in node:
                        gprops["blend"] = node["blend"]
                    if len(gprops) > 1:
                        session.execute("layer.setProps", gprops)
                full_group = f"{parent_group}/{gname}" if parent_group else gname
                for child in node.get("layers", []):
                    _build_node(child, parent_group=full_group)
                return

            if ntype == "image":
                asset = Path(node["asset"])
                if not asset.is_absolute():
                    asset = (spec_path.parent / asset).resolve()
                place_asset(
                    session,
                    asset_path=asset,
                    name=node.get("name") or asset.stem,
                    group_name=parent_group,
                    dx=float(node.get("dx", 0)),
                    dy=float(node.get("dy", 0)),
                    scale=node.get("scale"),
                    opacity=node.get("opacity"),
                    blend=node.get("blend"),
                    visible=node.get("visible"),
                )
                recorded_steps.append({
                    "type": "add-layer",
                    "name": node.get("name") or asset.stem,
                    "group": parent_group,
                    "asset": str(asset),
                    "dx": float(node.get("dx", 0)),
                    "dy": float(node.get("dy", 0)),
                    "scale": node.get("scale"),
                    "opacity": node.get("opacity"),
                    "blend": node.get("blend"),
                    "visible": node.get("visible"),
                })
                return

            if ntype == "text":
                tparams = {
                    "text": node.get("text", "Text"),
                    "x": float(node.get("x", 100)),
                    "y": float(node.get("y", 100)),
                    "size": float(node.get("size", 48)),
                }
                if node.get("color"):
                    tparams["color"] = node["color"]
                res = session.execute("type.create", tparams)
                lid = res.get("layer") if isinstance(res, dict) else None
                if lid is not None:
                    if node.get("name"):
                        session.execute("layer.setProps", {"layer": lid, "name": node["name"]})
                    if parent_group:
                        doc = session.call("doc.inspect")
                        pgrp = find_layer(doc, parent_group)
                        session.execute("layer.moveTo", {"layer": lid, "target": pgrp["id"], "position": "into"})
                recorded_steps.append({"type": "command", "command": "type.create", "params": tparams, "group": parent_group})
                return

            if ntype == "adjustment":
                step = {
                    "type": "adjust",
                    "kind": node["kind"],
                    "name": node.get("name"),
                    "group": parent_group,
                    "params": node.get("params") or {},
                }
                apply_recipe_step(session, step, spec_path.parent)
                recorded_steps.append(step)
                return

            raise ControlError(f"Unsupported layer type `{ntype}` in scene spec.")

        # Build bottom-to-top groups and standalone layers
        for grp in spec.get("groups", []):
            _build_node(grp)
        for lyr in spec.get("layers", []):
            _build_node(lyr)

        session.call("doc.save", {"path": str(out_psd), "format": "psd"})
        if args.preview:
            prev = Path(args.preview).resolve()
            prev.parent.mkdir(parents=True, exist_ok=True)
            session.call("doc.render", {"path": str(prev), "maxSide": 1600})
        doc = session.call("doc.inspect")

    # Save fresh recipe for this PSD project
    recipe = {
        "schema": "pd-photocraft/recipe/v1",
        "name": spec.get("name", out_psd.stem),
        "targetPsd": str(out_psd),
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "steps": recorded_steps,
    }
    recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved editable PSD: {out_psd}")
    print(f"Saved recipe script: {recipe_path}")
    if args.preview:
        print(f"Rendered preview: {Path(args.preview).resolve()}")
    print_layer_tree(doc)


def _run_inplace_edit(args: argparse.Namespace, steps: list[dict[str, Any]], extra_paths: list[Path] | None = None) -> None:
    psd_path = Path(args.psd).resolve() if getattr(args, "psd", None) else None
    paths: list[Path] = []
    if psd_path:
        paths.append(psd_path)
    if extra_paths:
        paths.extend(extra_paths)
    if getattr(args, "preview", None):
        paths.append(Path(args.preview).resolve())

    with open_session(args, paths) as session:
        if not getattr(args, "live", False):
            if not psd_path:
                raise ControlError("Either --psd <file.psd> or --live is required.")
            session.call("doc.open", {"path": str(psd_path)})
        elif psd_path and getattr(args, "open_in_live", False):
            session.call("doc.open", {"path": str(psd_path)})

        base_dir = psd_path.parent if psd_path else Path.cwd()
        for st in steps:
            apply_recipe_step(session, st, base_dir)

        if psd_path:
            session.call("doc.save", {"path": str(psd_path), "format": "psd"})
        if getattr(args, "preview", None):
            prev = Path(args.preview).resolve()
            prev.parent.mkdir(parents=True, exist_ok=True)
            session.call("doc.render", {"path": str(prev), "maxSide": 1600})
        doc = session.call("doc.inspect")

    recipe_path = (
        Path(args.recipe).resolve()
        if getattr(args, "recipe", None)
        else (default_recipe_path(psd_path) if psd_path else None)
    )
    if recipe_path:
        append_recipe_steps(recipe_path, steps, psd_path=psd_path)
        print(f"Appended {len(steps)} step(s) to recipe: {recipe_path}")
    if psd_path:
        print(f"Updated PSD in-place: {psd_path}")
    print_layer_tree(doc)


def cmd_inspect(args: argparse.Namespace) -> None:
    psd_path = Path(args.psd).resolve() if args.psd else None
    with open_session(args, [psd_path] if psd_path else []) as session:
        if psd_path and not args.live:
            session.call("doc.open", {"path": str(psd_path)})
        doc = session.call("doc.inspect")
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print_layer_tree(doc)


def cmd_move(args: argparse.Namespace) -> None:
    step: dict[str, Any] = {"type": "move", "layer": args.layer, "dx": args.dx, "dy": args.dy}
    if args.scale_x is not None:
        step["scaleX"] = args.scale_x
    if args.scale_y is not None:
        step["scaleY"] = args.scale_y
    if args.rotate is not None:
        step["rotate"] = args.rotate
    _run_inplace_edit(args, [step])


def cmd_swap(args: argparse.Namespace) -> None:
    asset = Path(args.asset).resolve()
    step = {"type": "swap", "layer": args.layer, "asset": str(asset)}
    _run_inplace_edit(args, [step], extra_paths=[asset])


def cmd_set_layer(args: argparse.Namespace) -> None:
    step: dict[str, Any] = {"type": "set-layer", "layer": args.layer}
    if args.rename is not None:
        step["name"] = args.rename
    if args.visible is not None:
        step["visible"] = args.visible.lower() in ("true", "1", "yes", "on")
    if args.opacity is not None:
        step["opacity"] = args.opacity
    if args.blend is not None:
        step["blend"] = args.blend
    _run_inplace_edit(args, [step])


def cmd_add_layer(args: argparse.Namespace) -> None:
    asset = Path(args.asset).resolve()
    step: dict[str, Any] = {
        "type": "add-layer",
        "asset": str(asset),
        "name": args.name or asset.stem,
        "group": args.group,
        "dx": args.dx,
        "dy": args.dy,
    }
    if args.scale is not None:
        step["scale"] = args.scale
    if args.opacity is not None:
        step["opacity"] = args.opacity
    if args.blend is not None:
        step["blend"] = args.blend
    _run_inplace_edit(args, [step], extra_paths=[asset])


def cmd_adjust(args: argparse.Namespace) -> None:
    params = json.loads(args.params) if args.params else {}
    step: dict[str, Any] = {
        "type": "adjust",
        "kind": args.kind,
        "name": args.name,
        "group": args.group,
        "params": params,
    }
    _run_inplace_edit(args, [step])


def cmd_exec(args: argparse.Namespace) -> None:
    params = json.loads(args.params) if args.params else {}
    step: dict[str, Any] = {"type": "command", "command": args.cmd, "params": params}
    if args.layer:
        step["layerName"] = args.layer
    _run_inplace_edit(args, [step])


def cmd_replay(args: argparse.Namespace) -> None:
    recipe_path = Path(args.recipe).resolve()
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    steps = recipe.get("steps", [])

    if args.batch_in and args.batch_out:
        in_dir = Path(args.batch_in).resolve()
        out_dir = Path(args.batch_out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        files = [p for p in sorted(in_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".psd", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".pcraft"}]
        with open_session(args, [in_dir, out_dir, recipe_path]) as session:
            for f in files:
                session.call("doc.open", {"path": str(f)})
                for st in steps:
                    if st.get("command") == "file.new":
                        continue
                    apply_recipe_step(session, st, recipe_path.parent)
                ext = args.format or f.suffix.lstrip(".")
                out_file = out_dir / f"{f.stem}.{ext}"
                session.call("doc.save", {"path": str(out_file), "format": ext})
                session.call("doc.close", {})
                print(f"Replayed -> {out_file}")
        return

    paths = [recipe_path]
    in_path = Path(args.input).resolve() if args.input else None
    out_path = Path(args.out).resolve() if args.out else None
    if in_path:
        paths.append(in_path)
    if out_path:
        paths.append(out_path)

    with open_session(args, paths) as session:
        if in_path and not args.live:
            session.call("doc.open", {"path": str(in_path)})
        for st in steps:
            if in_path and st.get("command") == "file.new":
                continue
            apply_recipe_step(session, st, recipe_path.parent)
        if out_path:
            fmt = args.format or out_path.suffix.lstrip(".") or "psd"
            session.call("doc.save", {"path": str(out_path), "format": fmt})
        doc = session.call("doc.inspect")
    print(f"Replayed {len(steps)} step(s) from {recipe_path}")
    print_layer_tree(doc)


def cmd_import_actions(args: argparse.Namespace) -> None:
    src = Path(args.actions).resolve()
    dst = Path(args.out).resolve()
    data = json.loads(src.read_text(encoding="utf-8"))
    recipe = import_actions_to_recipe(data, action_name=args.action)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Converted {len(recipe['steps'])} step(s) -> {dst}")


def cmd_export_actions(args: argparse.Namespace) -> None:
    src = Path(args.recipe).resolve()
    dst = Path(args.out).resolve()
    recipe = json.loads(src.read_text(encoding="utf-8"))
    actions = export_recipe_to_actions(recipe)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(actions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(actions['actions'][0]['steps'])} command step(s) -> {dst}")


def cmd_commands(args: argparse.Namespace) -> None:
    _, cli_exe = find_binaries(args.bin_dir)
    cmd = [str(cli_exe), "commands", "--json"]
    if args.filter:
        cmd.extend(["--filter", args.filter])
    out = subprocess.check_output(cmd, text=True, encoding="utf-8")
    cmds = json.loads(out)
    if args.export_index:
        by_ns: dict[str, list[dict[str, Any]]] = {}
        for c in cmds:
            cid = c.get("id", "")
            ns = cid.split(".")[0] if "." in cid else "other"
            by_ns.setdefault(ns, []).append({"id": cid, "label": c.get("label", ""), "menu": c.get("menu", [])})
        dst = Path(args.export_index).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps({"total": len(cmds), "namespaces": by_ns}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Exported {len(cmds)} commands across {len(by_ns)} namespaces -> {dst}")
        return
    for c in cmds[: args.limit]:
        print(f"{c.get('id', ''):44s} | {c.get('label', '')}")
    if len(cmds) > args.limit:
        print(f"... ({len(cmds)} total; pass --limit or --filter)")


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bin-dir", help="Directory containing photocraft / photocraft-cli binaries")
    p.add_argument("--live", action="store_true", help="Send commands directly to running GUI (`photocraft --control`)")
    p.add_argument("--host", default="127.0.0.1", help="Control channel host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Control channel port (default {DEFAULT_PORT})")
    p.add_argument("--token", help="64-hex control token (optional; reads token file by default)")
    p.add_argument("--token-file", help=f"Control token file (default {DEFAULT_TOKEN_FILE})")
    p.add_argument("--root", help="Override authorized read/write root directory")


def add_edit_flags(p: argparse.ArgumentParser) -> None:
    add_common_flags(p)
    p.add_argument("--psd", help="Target PSD file to edit in-place")
    p.add_argument("--recipe", help="Recipe JSON path to append edits (default: <psd>.recipe.json)")
    p.add_argument("--preview", help="Optional PNG path to render updated visual preview")
    p.add_argument("--open-in-live", action="store_true", help="With --live, also open --psd in the GUI first")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pd_photocraft",
        description="PD-photocraft: AI-driven editable PSD workflow, live GUI control, and scriptable Recipes.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_gui = sub.add_parser("launch-gui", help="Start PhotoCraft desktop GUI with --control channel for live AI visualization")
    p_gui.add_argument("--bin-dir")
    p_gui.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_gui.add_argument("--token-file")
    p_gui.add_argument("--root", help="Authorized read/write root directory (default: repo root)")
    p_gui.add_argument("--open", help="Optional PSD/image to open on launch")
    p_gui.set_defaults(func=cmd_launch_gui)

    p_build = sub.add_parser("build-psd", help="Assemble PNGs/text/adjustments into a grouped editable PSD + companion Recipe")
    add_common_flags(p_build)
    p_build.add_argument("--spec", required=True, help="Declarative scene JSON file")
    p_build.add_argument("--out", required=True, help="Output .psd path")
    p_build.add_argument("--recipe", help="Output .recipe.json path (default: <out>.recipe.json)")
    p_build.add_argument("--preview", help="Optional rendered PNG preview path")
    p_build.set_defaults(func=cmd_build_psd)

    p_ins = sub.add_parser("inspect", help="Inspect layer tree (groups, IDs, bounds, opacity, visibility)")
    add_common_flags(p_ins)
    p_ins.add_argument("--psd", help="PSD/image file to inspect (omit with --live to inspect active GUI doc)")
    p_ins.add_argument("--json", action="store_true", help="Print raw JSON")
    p_ins.set_defaults(func=cmd_inspect)

    p_mv = sub.add_parser("move", help="Move/scale/rotate a named layer or layer group in the SAME PSD")
    add_edit_flags(p_mv)
    p_mv.add_argument("--layer", required=True, help="Layer or group name, full path (e.g. 前景/主角), or ID")
    p_mv.add_argument("--dx", type=float, default=0.0, help="Horizontal pixel offset")
    p_mv.add_argument("--dy", type=float, default=0.0, help="Vertical pixel offset")
    p_mv.add_argument("--scale-x", type=float, help="Horizontal scale (e.g. 1.2 or 120)")
    p_mv.add_argument("--scale-y", type=float, help="Vertical scale (e.g. 1.2 or 120)")
    p_mv.add_argument("--rotate", type=float, help="Rotation in degrees")
    p_mv.set_defaults(func=cmd_move)

    p_sw = sub.add_parser("swap", help="Swap a Smart Object PNG asset in-place without losing position/group/transform")
    add_edit_flags(p_sw)
    p_sw.add_argument("--layer", required=True, help="Target Smart Object layer name, path, or ID")
    p_sw.add_argument("--asset", required=True, help="Replacement PNG/image path")
    p_sw.set_defaults(func=cmd_swap)

    p_set = sub.add_parser("set-layer", help="Modify visibility, opacity, blend mode, or name of a layer/group in-place")
    add_edit_flags(p_set)
    p_set.add_argument("--layer", required=True, help="Layer or group name, path, or ID")
    p_set.add_argument("--rename", help="New layer/group name")
    p_set.add_argument("--visible", help="true or false")
    p_set.add_argument("--opacity", type=float, help="0..100 or 0.0..1.0")
    p_set.add_argument("--blend", help="Blend mode (Normal, Multiply, Screen, Overlay, SoftLight, ...)")
    p_set.set_defaults(func=cmd_set_layer)

    p_add = sub.add_parser("add-layer", help="Place a new PNG Smart Object into a specific layer group in the SAME PSD")
    add_edit_flags(p_add)
    p_add.add_argument("--asset", required=True, help="PNG/image file to place")
    p_add.add_argument("--name", help="Layer name (default: filename stem)")
    p_add.add_argument("--group", help="Target layer group name (e.g. 前景)")
    p_add.add_argument("--dx", type=float, default=0.0)
    p_add.add_argument("--dy", type=float, default=0.0)
    p_add.add_argument("--scale", type=float)
    p_add.add_argument("--opacity", type=float)
    p_add.add_argument("--blend")
    p_add.set_defaults(func=cmd_add_layer)

    p_adj = sub.add_parser("adjust", help="Add a non-destructive adjustment layer and record it in Recipe")
    add_edit_flags(p_adj)
    p_adj.add_argument("--kind", required=True, help="curves, levels, vibrance, hueSaturation, brightnessContrast, exposure, colorBalance, blackWhite, photoFilter, ...")
    p_adj.add_argument("--params", default="{}", help="JSON object of adjustment parameters")
    p_adj.add_argument("--name", help="Adjustment layer name")
    p_adj.add_argument("--group", help="Optional target group (e.g. 调色)")
    p_adj.set_defaults(func=cmd_adjust)

    p_ex = sub.add_parser("exec", help="Execute any of the 892 PhotoCraft engine commands and record it in Recipe")
    add_edit_flags(p_ex)
    p_ex.add_argument("--cmd", required=True, help="Engine command ID")
    p_ex.add_argument("--params", default="{}", help="JSON object of command parameters")
    p_ex.add_argument("--layer", help="Optional layer name/path to select before running")
    p_ex.set_defaults(func=cmd_exec)

    p_rep = sub.add_parser("replay", help="Replay a .recipe.json into live GUI, a single file, or a whole folder")
    add_common_flags(p_rep)
    p_rep.add_argument("--recipe", required=True, help="Path to .recipe.json")
    p_rep.add_argument("--input", help="Input PSD/image for single-file replay")
    p_rep.add_argument("--out", help="Output path for single-file replay")
    p_rep.add_argument("--batch-in", help="Input directory for batch replay")
    p_rep.add_argument("--batch-out", help="Output directory for batch replay")
    p_rep.add_argument("--format", help="Output format override (psd, png, jpg, webp)")
    p_rep.set_defaults(func=cmd_replay)

    p_imp = sub.add_parser("import-actions", help="Convert manual PhotoCraft Actions panel JSON into .recipe.json")
    p_imp.add_argument("--actions", required=True)
    p_imp.add_argument("--out", required=True)
    p_imp.add_argument("--action", help="Action name if actions.json has multiple actions")
    p_imp.set_defaults(func=cmd_import_actions)

    p_exp = sub.add_parser("export-actions", help="Export .recipe.json to native PhotoCraft actions.json")
    p_exp.add_argument("--recipe", required=True)
    p_exp.add_argument("--out", required=True)
    p_exp.set_defaults(func=cmd_export_actions)

    p_cmd = sub.add_parser("commands", help="Search or export the PhotoCraft engine command registry")
    p_cmd.add_argument("--bin-dir")
    p_cmd.add_argument("--filter", help="Substring filter")
    p_cmd.add_argument("--limit", type=int, default=40)
    p_cmd.add_argument("--export-index", help="Write categorized JSON command index to path")
    p_cmd.set_defaults(func=cmd_commands)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except (ControlError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
