# Copyright 2025 MOVIN. All Rights Reserved.

"""Shared setup for the checks that need a real Blender.

Two things every one of them has to get right:

  * Load the add-on from this repository, not whatever copy is installed in
    Blender's addons directory. A stale installed copy silently taking priority
    has already cost one debugging session.
  * Fail loudly. Blender in background mode swallows a script's traceback and
    still exits 0, so a failed assertion looks exactly like a passing run. Every
    check runs through run_checks(), which prints the traceback and exits non-zero.
"""

import sys
import traceback
from pathlib import Path

ADDON_PATH = Path(__file__).resolve().parent.parent.parent / "addon" / "movin_blender_plugin.py"

#: Module name used for the copy under test, kept distinct from the installed
#: add-on so the two can never be confused for one another.
MODULE_NAME = "movin_under_test"


def load_addon():
    """Import and register this repository's add-on, returning the module."""
    import bpy
    import importlib.util

    try:
        bpy.ops.preferences.addon_disable(module="movin_blender_plugin")
    except Exception:
        pass  # Not installed in this Blender, which is the tidy case.
    sys.modules.pop("movin_blender_plugin", None)

    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(ADDON_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    module.register()
    return module


def single_armature():
    import bpy

    armatures = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE']
    if len(armatures) != 1:
        raise AssertionError("expected exactly one armature, found %d" % len(armatures))
    return armatures[0]


def run_checks(main):
    """Run main(), then exit 0 or 1 so a caller can actually tell them apart."""
    try:
        main()
    except BaseException:
        traceback.print_exc(file=sys.stdout)
        print("\nCHECK FAILED")
        sys.stdout.flush()
        sys.exit(1)
    print("\nALL CHECKS PASSED")
    sys.stdout.flush()
    sys.exit(0)
