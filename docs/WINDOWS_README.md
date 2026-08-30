# Local Video Transcriber for Windows

Windows 10 22H2 / Windows 11 x64 本地视频转写工具。

请先阅读发行包根目录的 `新手使用说明.txt`，然后双击：

`启动 Local Video Transcriber.cmd`

应用以普通用户权限安装到
`%LOCALAPPDATA%\LocalVideoTranscriber`，不会修改系统 PATH。Chrome 扩展需要
在 `chrome://extensions` 中通过“加载已解压的扩展程序”加载。

当前发行基线使用 CPU/int8 faster-whisper。运行依赖和模型会从固定 HTTPS
地址下载，并在使用前核对固定大小和 SHA-256。
