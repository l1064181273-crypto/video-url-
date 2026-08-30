# 排错

- 显示“本地服务未启动”：再次双击启动文件，再点击插件中的“重新连接”。
- 显示 `unsafe_or_corrupt`：不要删除 PID 文件或使用 `kill -9`，运行 `scripts/doctor.command --json` 收集去敏报告。
- Chrome 找不到插件：确认加载的是终端输出的稳定 extension 目录，而不是 ZIP 文件本身。
- 自动配对失败：重新加载扩展并重启后端；手动 Token 输入框仅作为兼容和排错备用。
