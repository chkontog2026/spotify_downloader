@echo off
setlocal
cd /d "%~dp0"
set "ANDROID_SDK_ROOT=%LOCALAPPDATA%\Android\Sdk"
if not exist "%ANDROID_SDK_ROOT%\platforms\android-36" (
  echo Android SDK 36 was not found at %ANDROID_SDK_ROOT%
  exit /b 1
)
if not exist "android\gradlew.bat" (
  echo Gradle wrapper is missing. Re-run project setup or open the android folder in Android Studio.
  exit /b 1
)
call "android\gradlew.bat" -p android testDebugUnitTest assembleRelease
if errorlevel 1 exit /b %errorlevel%
if not exist "android-releases" mkdir "android-releases"
copy /y "android\app\build\outputs\apk\release\app-universal-release.apk" "android-releases\SpotDL_Android_Universal_v1.3.0.apk" >nul
copy /y "android\app\build\outputs\apk\release\app-arm64-v8a-release.apk" "android-releases\SpotDL_Android_ARM64.apk" >nul
copy /y "android\app\build\outputs\apk\release\app-arm64-v8a-release.apk" "android-releases\SpotDL_Android_ARM64_v1.3.0.apk" >nul
echo.
echo APK ready: android-releases\SpotDL_Android_ARM64_v1.3.0.apk
echo Universal fallback: android-releases\SpotDL_Android_Universal_v1.3.0.apk
endlocal
