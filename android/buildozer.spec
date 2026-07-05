[app]

title = SRLTCP
package.name = srltcp
package.domain = org.srltcp

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = tests, .git, .github, __pycache__, .pytest_cache, scripts, bin, .buildozer, p4a-recipes

version = 1.0.0

# Pin Android + host Python — unpinned python3 lets P4A pick 3.14 which breaks builds.
requirements = python3==3.12.8,hostpython3==3.12.8,kivy==2.2.1

orientation = portrait
fullscreen = 0

p4a.branch = master
p4a.local_recipes = p4a-recipes

android.permissions = INTERNET
android.api = 33
android.minapi = 21
android.ndk = 25c
android.ndk_api = 21
android.accept_sdk_license = True
android.archs = arm64-v8a
android.copy_libs = 1

[buildozer]

log_level = 2
warn_on_root = 1
build_dir = ./.buildozer
bin_dir = ./bin