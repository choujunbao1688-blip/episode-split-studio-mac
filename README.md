# 拆集工作台 · Mac 单体应用

目标：一个 `.app` / `.dmg`，运行时不需要打开终端、输入连接码或单独安装 Python/FFmpeg。视频仅从用户选择的本地/NAS 挂载目录读取，并直接输出到用户选择的目录。

`main.js` 在应用启动时自动运行打包在 `Contents/Resources` 的处理引擎，内部使用本机回环端口；网页界面不再向用户暴露该机制。PyInstaller 将 Python 与 Pillow 一同打包，FFmpeg/ffprobe 二进制文件作为应用资源随包分发。请遵守二进制文件的许可与署名要求。

## 安装包与“已损坏”提示

**0.1.0 的 GitHub Actions 构建日志显示跳过了 macOS 应用签名。** 这份旧 DMG 不能作为经过 Apple 验证的正式安装包；下载后的“已损坏”提示也可能由文件实际损坏或被修改引起，不能仅凭提示断定原因。不要关闭 Gatekeeper 或运行批量清除隔离属性的命令来安装。

0.1.1 起，工作流仅在 Developer ID 签名与 Apple 公证成功、并通过 `codesign`、`spctl`、`stapler` 和 `hdiutil` 校验时上传 DMG。该版本需要仓库维护者自行配置以下 GitHub Actions Secrets，**不要将证书或密码提交进仓库，也不要发送给他人**：

| Secret | 用途 |
| --- | --- |
| `MAC_CSC_LINK` | Developer ID Application 证书的 .p12 文件，base64 编码 |
| `MAC_CSC_KEY_PASSWORD` | .p12 的导出密码 |
| `APPLE_ID` | Apple Developer 账户邮箱 |
| `APPLE_APP_SPECIFIC_PASSWORD` | 专用密码，并非 Apple ID 登录密码 |
| `APPLE_TEAM_ID` | 开发团队 ID |

配置后在 GitHub Actions 手动运行 **Build signed macOS app**。工作流缺少任一凭据就会失败，不会再上传未签名的安装包。首次发布需在真实 Apple Silicon Mac 上验证安装、文件权限、NAS 读写和 30GB 以上项目；CI 验证不等于实际使用验收。

若已安装 0.1.0，先检查提示原文。对于“无法验证开发者”或“Apple 无法检查是否包含恶意软件”的提示，可按 Apple 官方文档在系统设置 → 隐私与安全性中查看“仍要打开”选项。对于明确显示“已损坏”或“将损害你的电脑”的提示，先停止打开并核对下载来源及安装包完整性；不要把解除安全限制当成文件修复。参考 [Apple 的安全说明](https://support.apple.com/en-us/102445)。

## 当前能力

- 本地文件夹 / 已挂载 NAS 目录扫描，按照 EP 编号排序。
- 音量变化、静音及镜头切换切点建议；目标时长、起始 EP 编号、逐集切点手工修正。
- 纯净海报编号样式、样片检验与逐集导出；H.264 MP4 1080×1920、25 FPS、AAC 双声道。
- 片头旧静帧与片尾旧静帧检测；新片尾定帧、炫白光及 rise 音效。
- 本地任务状态保存、已完成文件指纹跳过。

情绪峰值和隐藏信息目前只是音频/镜头候选，仍需人工复核；已烘焙进唯一源片的效果无法无损还原。没有大于 30GB 的真实素材验收前，不宣称生产环境容量已验证。
