#!/usr/bin/env python3
"""Export top-level PSD groups without modifying the original PSD."""
import argparse
import hashlib
import re
import sys
from pathlib import Path

from PIL import Image
from psd_tools import PSDImage


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def filename(name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    if not safe or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', safe):
        raise ValueError(f'编组名不能用作文件名: {name!r}')
    return safe


def main():
    ap = argparse.ArgumentParser(description='按顶层编组分别导出 JPG/PNG，保留原 PSD')
    ap.add_argument('psd', type=Path)
    ap.add_argument('--out-dir', type=Path, required=True)
    ap.add_argument('--group', action='append', help='指定顶层编组名，可重复；默认全部顶层编组')
    ap.add_argument('--format', choices=['jpg', 'png'], default='jpg')
    ap.add_argument('--quality', type=int, default=95)
    ap.add_argument('--list', action='store_true', help='只列编组，不输出文件')
    args = ap.parse_args()
    if not 1 <= args.quality <= 100: ap.error('--quality 须为 1–100')
    source = args.psd.resolve(strict=True)
    before = file_hash(source)
    psd = PSDImage.open(source)
    groups = [layer for layer in psd if layer.is_group()]
    if not groups: raise ValueError('没有顶层编组')
    names = [g.name for g in groups]
    if len(names) != len(set(names)): raise ValueError('存在同名顶层编组，请先在 PSD 中改成不同名称')
    if args.list:
        for g in groups: print(f'{g.name} | 当前显示={g.visible} | 子层数={len(g)}')
        return
    selected = args.group or names
    if len(selected) != len(set(selected)): raise ValueError('编组名称重复')
    if any(n not in names for n in selected): raise ValueError(f'找不到指定编组，现有: {names}')
    targets = [args.out_dir.resolve() / (filename(n) + '.' + args.format) for n in selected]
    if len({str(p).casefold() for p in targets}) != len(targets): raise ValueError('编组名称转换后文件名冲突')
    if any(p.exists() for p in targets): raise FileExistsError('输出已存在，未导出：' + ', '.join(str(p) for p in targets if p.exists()))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, target in zip(selected, targets):
        for group in groups: group.visible = group.name == name
        canvas = psd.composite(force=True)
        if args.format == 'jpg':
            if canvas.mode == 'RGBA':
                white = Image.new('RGB', canvas.size, 'white')
                white.paste(canvas, mask=canvas.getchannel('A'))
                canvas = white
            else: canvas = canvas.convert('RGB')
            canvas.save(target, 'JPEG', quality=args.quality, subsampling=0)
        else: canvas.save(target, 'PNG')
        print(f'{name} -> {target} ({canvas.width}x{canvas.height})')
    if file_hash(source) != before: raise RuntimeError('源 PSD 在导出期间发生变化，请检查输出是否对应同一版本')
    print('已保留源 PSD；使用 psd-tools 合成，请对照 Photoshop 检查复杂效果。')


if __name__ == '__main__':
    try: main()
    except Exception as e:
        print(f'错误: {e}', file=sys.stderr)
        sys.exit(1)
