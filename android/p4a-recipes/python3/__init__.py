"""Disable stdlib grp when cross-compiling for Android (NDK lacks setgrent)."""

from os.path import exists, join

from pythonforandroid.logger import info
from pythonforandroid.recipes.python3 import Python3Recipe as _BasePython3Recipe
from pythonforandroid.util import ensure_dir


class Python3Recipe(_BasePython3Recipe):
    _GRP_SETUP_LINE = "*grp*\n"

    def _ensure_grp_disabled(self, arch):
        build_dir = self.get_build_dir(arch.arch)
        modules_dir = join(build_dir, "Modules")
        setup_local = join(modules_dir, "Setup.local")
        marker = join(modules_dir, ".p4a_grp_disabled")
        if exists(marker):
            return
        ensure_dir(modules_dir)
        with open(setup_local, "a", encoding="utf-8") as fh:
            fh.write(self._GRP_SETUP_LINE)
        open(marker, "w", encoding="utf-8").close()
        info("Disabled grp module via Modules/Setup.local")

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        self._ensure_grp_disabled(arch)

    def build_arch(self, arch):
        if "ac_cv_func_getgrent=no" not in self.configure_args:
            self.configure_args.append("ac_cv_func_getgrent=no")
        self._ensure_grp_disabled(arch)
        super().build_arch(arch)


recipe = Python3Recipe()