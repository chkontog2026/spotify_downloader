import asyncio
import os
import subprocess
import sys


if os.name == "nt":
    _original_popen = subprocess.Popen
    _original_create_subprocess_exec = asyncio.create_subprocess_exec
    _original_create_subprocess_shell = asyncio.create_subprocess_shell

    def _hidden_process_options(kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo

    class _HiddenPopen(_original_popen):
        def __init__(self, *args, **kwargs):
            _hidden_process_options(kwargs)
            super().__init__(*args, **kwargs)

    async def _hidden_create_subprocess_exec(program, *args, **kwargs):
        _hidden_process_options(kwargs)
        return await _original_create_subprocess_exec(program, *args, **kwargs)

    async def _hidden_create_subprocess_shell(command, **kwargs):
        _hidden_process_options(kwargs)
        return await _original_create_subprocess_shell(command, **kwargs)

    subprocess.Popen = _HiddenPopen
    asyncio.create_subprocess_exec = _hidden_create_subprocess_exec
    asyncio.create_subprocess_shell = _hidden_create_subprocess_shell


# Import yt-dlp only after subprocess creation has been made windowless.
from yt_dlp import main


if __name__ == "__main__":
    sys.exit(main())
