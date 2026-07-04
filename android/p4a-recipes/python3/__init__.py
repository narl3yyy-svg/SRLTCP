"""Disable stdlib grp when cross-compiling for Android (NDK lacks setgrent)."""

import re

import pythonforandroid
from os.path import dirname, exists, join

from pythonforandroid.logger import info
from pythonforandroid.recipes.python3 import Python3Recipe as _BasePython3Recipe
from pythonforandroid.util import ensure_dir

_UPSTREAM_RECIPE_DIR = join(dirname(pythonforandroid.__file__), "recipes", "python3")
_LOCAL_RECIPE_DIR = dirname(__file__)
_GRP_SETUP_LINE = "*grp*\n"


class Python3Recipe(_BasePython3Recipe):
    def get_recipe_dir(self):
        # Local override has no patches/ tree — use upstream p4a recipe files.
        return _UPSTREAM_RECIPE_DIR

    def apply_patches(self, arch, build_dir):
        super().apply_patches(arch, build_dir)
        local_patch = join(_LOCAL_RECIPE_DIR, "patches", "disable-grp.patch")
        if exists(local_patch):
            from pythonforandroid.logger import shprint
            import sh

            shprint(sh.patch, "-d", build_dir, "-p1", "-i", local_patch)
        self._ensure_grp_disabled(arch)

    def _ensure_grp_disabled(self, arch):
        build_dir = self.get_build_dir(arch.arch)
        modules_dir = join(build_dir, "Modules")
        setup_local = join(modules_dir, "Setup.local")
        setup = join(modules_dir, "Setup")
        marker = join(modules_dir, ".p4a_grp_disabled")
        if exists(marker):
            return
        ensure_dir(modules_dir)
        existing = ""
        if exists(setup_local):
            existing = open(setup_local, encoding="utf-8").read()
        if _GRP_SETUP_LINE.strip() not in existing:
            with open(setup_local, "a", encoding="utf-8") as fh:
                fh.write(_GRP_SETUP_LINE)
        if exists(setup):
            text = open(setup, encoding="utf-8").read()
            updated = re.sub(r"^grp\s", "#grp ", text, flags=re.MULTILINE)
            if updated != text:
                with open(setup, "w", encoding="utf-8") as fh:
                    fh.write(updated)
        open(marker, "w", encoding="utf-8").close()
        info("Disabled grp module via Modules/Setup.local")

    def prebuild_arch(self, arch):
        if "ac_cv_func_getgrent=no" not in self.configure_args:
            self.configure_args.append("ac_cv_func_getgrent=no")
        super().prebuild_arch(arch)
        self._ensure_grp_disabled(arch)

    def build_arch(self, arch):
        if "ac_cv_func_getgrent=no" not in self.configure_args:
            self.configure_args.append("ac_cv_func_getgrent=no")
        self._ensure_grp_disabled(arch)
        super().build_arch(arch)
        self._ensure_grp_disabled(arch)


recipe = Python3Recipe()