@echo off
title Turret Control Brain
echo =======================================================
echo 	        _.,-----.,_
echo              ,-'           `-.
echo           ,-'   ,---------.   `-.
echo         ,'    ,'           `.    `.
echo       ,'    ,'    ,-----.    `.    `.
echo      /     /    ,'       `.    \     \
echo     ^|     ^|    /    ___    \    ^|     ^|
echo     ^|     ^|   ^|    ( X )    ^|   ^|     ^|
echo     ^|     ^|    \    ---    /    ^|     ^|
echo      \     \    `.       ,'    /     /
echo       `.    `.    `-----'    ,'    ,'
echo         `.    `.           ,'    ,'
echo           `-.   `---------'   ,-'
echo              `-.           ,-'
echo                 `---------'
echo  Starting Water Turret Tracking Brain...
echo =======================================================

:: Resolve local network IP address
for /f "usebackq tokens=*" %%i in (`powershell -Command "(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex (Get-NetRoute -DestinationPrefix 0.0.0.0/0).InterfaceIndex).IPAddress"`) do set LOCAL_IP=%%i

echo Network access address: http://%LOCAL_IP%:5001
echo =======================================================

:: Launch default browser to the Flask target feed after a 7-second delay (handled asynchronously in the background)
start /b cmd /c "timeout /t 7 /nobreak >nul && start http://localhost:5001"

:: Launch main.py inside WaTurrent subfolder in the foreground
cd WaTurret
python main.py %*

pause
