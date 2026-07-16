# 文档索引

本目录由原开发期 `.trae/documents` 文档整理而来，已移除真实 IP、SSH 私钥路径、个人本地路径等信息。文档中的主机名、内网地址、证书和密钥路径均为占位符，请替换为自己的部署环境。

## 入门与配置

- [本地真实模型测试指南](getting-started/local-models.md)
- [配置说明](configuration.md)
- [总体技术方案与架构](architecture/overview.md)

## ASR

- [语音识别 ASR / FunASR 方案](asr/funasr.md)
- [实时录音转写：真流式 2-pass 方案](asr/streaming-2pass.md)

## TTS

- [语音合成 TTS / CosyVoice2 方案](tts/cosyvoice2.md)
- [TTS 文本处理链路与领域 TN 排查手册](tts/text-processing.md)
- [分句流式 TTS / TTFB 优化方案](tts/sentence-streaming.md)
- [分句流式压测与文档补全](tts/sentence-streaming-loadtest.md)

## 语音聊天

- [语音到语音对话方案](chat/speech-to-speech.md)
- [原生端到端语音对话升级与 A/B 验收](chat/native-speech-upgrade.md)

## 部署与运维

- [公网部署 Runbook](deployment/public-deploy.md)
- [HTTPS 自签证书 Runbook](deployment/https-self-signed.md)
- [水平扩容 Runbook](deployment/scaling.md)
- [生产部署复盘模板（已脱敏）](deployment/production-summary-template.md)
- [QoS 监控栈 Runbook](observability/qos-monitoring.md)

## 评估报告

- [T4 显存限制下 vLLM 可行性评估](evaluations/t4-vllm.md)
