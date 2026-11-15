-- Studio 启动器：双击 → 起本地服务 → 打开网页。已在跑就直接开网页。
set base to "/Users/zehanruan/Documents/deepseek-harness/default-workspace/studio"
set webAddress to "http://127.0.0.1:8770"

on portOpen()
	try
		return (do shell script "/usr/sbin/lsof -nP -iTCP:8770 -sTCP:LISTEN | wc -l") as integer > 0
	on error
		return false
	end try
end portOpen

if not portOpen() then
	do shell script "cd " & quoted form of base & " && nohup /usr/bin/python3 server.py >> /tmp/studio.log 2>&1 &"
	repeat 24 times
		delay 0.5
		if portOpen() then exit repeat
	end repeat
end if

open location webAddress
