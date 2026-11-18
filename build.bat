@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/3] 检查 Python...
where python >nul 2>nul
if errorlevel 1 (
    echo   没找到 python，请先安装 Python 3.9 及以上，并勾选 "Add Python to PATH"。
    exit /b 1
)
python -c "import sys; print('   Python', sys.version.split()[0])"

echo [2/3] 安装依赖...
python -m pip install --upgrade -r requirements.txt -r requirements-dev.txt
if errorlevel 1 (
    echo   依赖安装失败，检查网络或换国内镜像：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...
    exit /b 1
)

echo [3/3] 打包单文件 exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name SmoothScroll ^
  --icon "assets\SmoothScroll.ico" ^
  --distpath "dist" --workpath "build" --specpath "build" ^
  SmoothScroll.py
if errorlevel 1 (
    echo   打包失败，往上翻看 PyInstaller 的报错。
    exit /b 1
)

echo.
echo 完成：dist\SmoothScroll.exe
echo 直接双击即可运行，无需 Python 环境。
endlocal
