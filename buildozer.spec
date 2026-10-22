[app]
title = DoraGram
package.name = doragram
package.domain = org.dora
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = server,.github,bin,.buildozer
version = 0.1
requirements = python3,kivy==2.3.0,requests,urllib3,certifi,idna,charset-normalizer,websocket-client
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
p4a.branch = v2024.01.21
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
# сервер на http:// — Android 9+ иначе блокирует трафик
android.extra_manifest_application_arguments = ./cleartext.xml

[buildozer]
log_level = 2
warn_on_root = 1
