@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem Ensure cargo is on PATH (fallback to default rustup location)
where cargo >nul 2>&1
if errorlevel 1 (
    if exist "%USERPROFILE%\.cargo\bin\cargo.exe" (
        set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    )
)

where cargo >nul 2>&1
if errorlevel 1 (
    echo [ERROR] cargo ^(Rust toolchain^) not found.
    echo Please install Rustup ^(https://rustup.rs^) and Visual Studio / Build Tools ^(Desktop development with C++^).
    echo On first run, rustup will install the version pinned in rust-toolchain.toml.
    echo.
    pause
    exit /b 1
)

rem Debug-friendly defaults (can be overridden by external env vars)
if not defined RUST_BACKTRACE set "RUST_BACKTRACE=1"
if not defined RUST_LOG set "RUST_LOG=photocraft=info,photocraft_ui_egui=debug"

rem Support optional --release flag as the first argument; remaining args pass to photocraft
set "PROFILE_ARGS="
set "MODE_LABEL=dev"
set "APP_ARGS="
if /i "%~1"=="--release" (
    set "PROFILE_ARGS=--release"
    set "MODE_LABEL=release"
    shift
)

:collect_args
if "%~1"=="" goto run_app
set "APP_ARGS=%APP_ARGS% %1"
shift
goto collect_args

:run_app
echo [PhotoCraft] Building and launching from source (%MODE_LABEL% mode)...
echo [PhotoCraft] UI source: crates\ui-egui\src\
echo.

cargo run %PROFILE_ARGS% -p photocraft --%APP_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Build or launch failed with exit code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
