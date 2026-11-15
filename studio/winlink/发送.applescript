-- 「发到 Windows」投放器：把文件拖到图标上，选任务，写进 OneDrive
property submitPath : "/Users/zehanruan/Documents/deepseek-harness/default-workspace/studio/winlink/submit.py"
property pyPath : "/usr/bin/python3"

on open theFiles
	-- 1. 选任务
	set taskList to {"转文字", "翻译", "总结"}
	set picked to choose from list taskList with title "发到 Windows" with prompt "要做哪些？（按住 ⌘ 可多选）" default items taskList with multiple selections allowed
	if picked is false then return

	set opsList to {}
	repeat with t in picked
		if (t as text) is "转文字" then set end of opsList to "transcribe"
		if (t as text) is "翻译" then set end of opsList to "translate"
		if (t as text) is "总结" then set end of opsList to "summary"
	end repeat
	if (count of opsList) is 0 then return
	set AppleScript's text item delimiters to ","
	set opsArg to opsList as text
	set AppleScript's text item delimiters to ""

	-- 2. 拼文件参数
	set argText to ""
	repeat with f in theFiles
		set argText to argText & " " & quoted form of (POSIX path of f)
	end repeat

	-- 3. 调用投放器
	try
		do shell script quoted form of pyPath & " " & quoted form of submitPath & " --ops " & quoted form of opsArg & argText
		display notification "已放进 OneDrive，等 Windows 处理" with title "发到 Windows"
	on error errMsg number errNum
		if errNum is not -128 then
			display dialog "投放失败：" & errMsg buttons {"好"} default button 1
		end if
	end try
end open

on run
	display dialog "把音频或视频文件拖到这个图标上就行。" buttons {"好"} default button 1
end run
