# 安装

支持 macOS 13 或更高版本的 Apple Silicon Mac。首次安装需要网络，并会下载 Python、FFmpeg、Ollama 和本地模型，建议预留至少 12 GB 空间。

1. 解压发行 ZIP，不要直接在 ZIP 预览中运行文件。
2. 右键点击 `启动 Local Video Transcriber.command`，选择“打开”。以后可直接双击。
3. 等待终端显示 `FIRST_INSTALL_PUBLISHED` 或 `START_READY`。
4. 打开 `chrome://extensions` 并启用开发者模式。
5. 点击“加载已解压的扩展程序”，选择终端输出的稳定 extension 目录。
6. 打开插件；首次连接会自动配对，不需要复制 Token。

Chrome 不允许普通 ZIP 静默安装未上架扩展，因此第 4–5 步首次必须由用户完成。

升级时解压新版 ZIP 并双击其中的同名启动文件；启动器会比较版本并走安全发布事务。相同版本只启动服务，旧发行包不会覆盖已安装的新版本。
