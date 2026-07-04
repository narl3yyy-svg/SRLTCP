[app]

title = SRLTCP
package.name = srltcp
package.domain = org.srltcp

source.dir = ..
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = tests, .git, .github, __pycache__, .pytest_cache, scripts, bin, .buildozer

version = 1.0.0

requirements = python3,kivy==2.2.1

orientation = portrait
fullscreen = 0

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

p4a.branch = develop
