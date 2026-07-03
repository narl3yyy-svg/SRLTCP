[app]
title = SRLTCP
package.name = srltcp
package.domain = org.srltcp
source.dir = ..
source.include_exts = py,png,jpg,jpeg,kv,atlas,json,css,js,html,md,txt
source.include_patterns = srltcp/*,android/service/*
source.exclude_dirs = tests,.venv,.git,.github,.mypy_cache,.pytest_cache,.ruff_cache,android/bin,android/.buildozer
version = 0.1.20
requirements = python3,aiohttp,aiofiles,cryptography,pyopenssl,zstandard,android
orientation = portrait
fullscreen = 0

[buildozer]
log_level = 2
warn_on_root = 1

[android]
android.api = 34
android.minapi = 24
android.ndk = 26b
android.archs = arm64-v8a,x86_64
android.permissions = INTERNET,ACCESS_NETWORK_STATE,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,POST_NOTIFICATIONS,WAKE_LOCK
android.services = SRLTCP:service/srltcp_service.py:foreground:Secure peer node
android.add_src = src/main/java
android.add_resources = src/main/res
android.gradle_dependencies = androidx.webkit:webkit:1.11.0
android.manifest = src/main/AndroidManifest.xml
android.manifest.application_attributes = android:usesCleartextTraffic=false,android:largeHeap=true,android:networkSecurityConfig=@xml/network_security_config
android.bootstrap = sdl2
android.debug_artifact = apk
android.release_artifact = apk