@echo off
rem Windows convenience wrapper; development happens here, deployment on RHEL 8.
rem EXSIM_PYTHON overrides the interpreter -- point it at venv36 in a checkout.
setlocal
set "ROOT=%~dp0.."
if "%EXSIM_PYTHON%"=="" (
    if exist "%ROOT%\venv36\Scripts\python.exe" (
        set "EXSIM_PYTHON=%ROOT%\venv36\Scripts\python.exe"
    ) else (
        set "EXSIM_PYTHON=python"
    )
)
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
"%EXSIM_PYTHON%" -m exchangesim.ctl.main %*
