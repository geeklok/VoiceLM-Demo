# 安全策略

## 报告安全问题

如果你发现漏洞或敏感信息泄漏风险，请不要直接公开利用细节。请通过 GitHub Security Advisory（如仓库开启）或私下联系维护者报告，并附上影响范围与复现方式。

## 已知安全边界

- backend 默认无用户鉴权，生产环境不要将 `8000` 端口直接暴露到公网。
- 浏览器麦克风能力通常需要 HTTPS 或 localhost；公网部署请配置可信证书。
- `.env`、`deploy/.env.deploy`、`deploy/sync.env`、证书私钥、SSH 私钥、模型权重、参考音频和用户语音数据均不应提交到仓库。
- 如需公网服务，请在入口层增加鉴权、限流、审计日志、TLS 和必要的访问控制。

## Secret 处理建议

- 本地使用 `.env`，生产使用云厂商 Secret Manager、Kubernetes Secret 或受控环境变量。
- API Key 定期轮换；一旦误提交，请立即吊销并重发。
- 提交前可运行：

```bash
rg -n --hidden --glob '!.git/**' --glob '!frontend/node_modules/**' \
  '(sk-|api[_-]?key|secret|token|password|BEGIN .*PRIVATE|\.pem|/Users/)'
```
