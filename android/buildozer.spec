[app]

# (str) Title of your application
title = SRLTCP

# (str) Package name
package.name = srltcp

# (str) Package domain (needed for android/ios packaging)
package.domain = org.srltcp

# (str) Source code where the main.py live
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas

# (list) List of inclusions using pattern matching
# source.include_patterns = assets/*, images/*.png

# (list) Source files to exclude (let empty to not exclude anything)
source.exclude_exts = spec

# (list) List of directory to exclude (let empty to not exclude anything)
source.exclude_dirs = tests, bin

# (str) Application versioning (method 1)
version = 1.0.0

# (list) Application requirements
requirements = python3,kivy==2.2.1,plyer,pyjnius

# (str) Presplash of the application
# presplash.filename = %(source.dir)s/data/presplash.png

# (str) Icon of the application
# icon.filename = %(source.dir)s/data/icon.png

# (str) Supported orientation (one of landscape, sensorLandscape, portrait or all)
orientation = portrait

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

# (list) Permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE

# (int) Android API to use
android.api = 34

# (int) Minimum API required (21 = Android 5.0)
android.minapi = 21

# (str) Android NDK version to use
android.ndk = 23b

# (int) Android NDK API to use
android.ndk_api = 21

# (bool) If True, then automatically accept SDK license
android.accept_sdk_license = True

# (list) Android architectures to build for
android.archs = arm64-v8a, armeabi-v7a

# (str) Android entry point
# android.entrypoint = org.kivy.android.PythonActivity

# (str) Android logcat filters to use
android.logcat_filters = *:S python:D

# (bool) Copy library instead of making a libpy.so
android.copy_libs = 1

# (bool) Use the android default activity (default False)
# android.default_activity = False

# (bool) Enable Java/JNI hot swapping (default False)
# android.hot_swapping = True

# (str) Gradle build system (default 'gradle')
android.gradle_dependencies =

# (str) Android Gradle plugin version
android.gradle_plugin_version = 7.4.2

# (list) Additional Java source files to include
# android.add_src =

# (list) Additional Java jar files to include
# android.add_jar =

# (list) Extra Java dependencies to add to the build.gradle
# android.gradle_dependencies =

# (list) Android Gradle repositories
# android.gradle_repositories =

# (list) Android NDK libraries to include
# android.ndk_libs =

# (bool) Enable or disable the packaging of the Python library
# android.package_py_library = True

# (str) Python for Android branch to use
p4a.branch = develop

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug)
log_level = 2

# (bool) Display warning if buildozer is run as root
warn_on_root = 1

# (str) Path to build artifact storage
build_dir = ./.buildozer

# (str) Path to build output (i.e. .apk, .ipa) storage
bin_dir = ./bin

# (bool) Automatically use the --ignore-setup-py flag
# ignore_setup_py = False
