# Local Video Transcriber

面向 Apple Silicon Mac 的本地视频转写 Chrome 扩展。任务、模型和转写结果保存在本机。

第一次使用请先打开发行包根目录的 `新手使用说明.txt`。

## 最短使用流程

1. 解压 ZIP。
2. 双击 `启动 Local Video Transcriber.command`。首次运行会联网安装固定版本依赖并启动服务。
3. 打开 `chrome://extensions`，启用“开发者模式”，点击“加载已解压的扩展程序”。
4. 选择安装日志给出的稳定 `extension` 目录。
5. 打开插件。插件会自动配对，不需要输入 Token。

详细说明见 `docs/INSTALLATION.md` 和 `docs/USER_GUIDE.md`。

已经安装过旧版时，也从新版解压目录双击启动文件；它会先安全升级，再启动服务。
