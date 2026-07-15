# 贡献指南

感谢你愿意贡献本项目！请在提交 Issue 或 Pull Request 前阅读以下约定。

## 开发环境

```bash
# 后端
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ASR_ENGINE=stub TTS_ENGINE=stub CHAT_ENABLED=false pytest

# 前端
cd ../frontend
npm install
npm run build
```

## 提交 PR 前检查

- 后端测试通过：`cd backend && ASR_ENGINE=stub TTS_ENGINE=stub CHAT_ENABLED=false pytest`
- 前端构建通过：`cd frontend && npm run build`
- 没有提交 `.env`、API Key、SSH 私钥、证书私钥、模型权重、参考音频、真实 IP 或个人本地路径。
- 新功能请补充必要测试、文档或手工验证步骤。
- 涉及 API/协议变更时，请同步更新 `README.md` 和 `docs/`。

## Issue 建议

提交 Bug 时请尽量提供：

- 复现步骤、期望行为、实际行为；
- 后端/前端日志；
- 模型配置（请脱敏）；
- 浏览器、Python、Node.js、CUDA/驱动版本等环境信息。

## 代码风格

- Python 代码优先保持类型标注清晰、错误信息可诊断。
- 前端组件保持状态命名明确，不在 UI 中硬编码后端模型能力；优先依赖 `/api/v1/models` 和 `/api/v1/tn-categories`。
- 部署脚本不得写死个人主机、IP、密钥路径或云厂商账号信息。
