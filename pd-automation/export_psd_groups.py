#!/usr/bin/env python3
"""Compatibility entrypoint; canonical implementation lives in PhotoCraft project skill."""
import runpy
from pathlib import Path
runpy.run_path(str(Path(r'C:\pandy\github\tools\photocraft\skills\psd-layer-export\scripts\export_psd_groups.py')), run_name='__main__')