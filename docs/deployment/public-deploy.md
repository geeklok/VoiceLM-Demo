> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 公网部署 Runbook（MVP，单 GPU 实例，IP 直连）

本文档把 Phase 1 MVP 部署到**阿里云单台 GPU ECS 实例**，通过 `http://<公网IP>` 访问（标准 80 端口，URL 无需带端口号）。无域名、无 HTTPS。

> 本次范围：仅 MVP（FunASR + CosyVoice2 真实模型 + 前端），对应里程碑 M4。Phase 2 扩缩容/容灾不在本次。

***

## 0. ⚠️ 必读限制

| 限制                       | 影响                       | 说明                                                                    |
| ------------------------ | ------------------------ | --------------------------------------------------------------------- |
| **HTTP（无 TLS）下浏览器禁用麦克风** | 录音 / 流式 ASR 不可用          | `getUserMedia` 仅在 HTTPS 或 localhost 可用。**文件式 ASR 上传**和 **TTS 合成播放**正常 |
| GPU 必需                   | CPU 上 CosyVoice 太慢，不适合公网 | 实例须带 NVIDIA GPU（A10/T4 起步）                                            |
| 无备案                      | 仅 IP 访问，不能绑大陆域名          | 后续要域名再走备案/TLS                                                         |

如果你后面需要麦克风/流式，最小代价是加一层 HTTPS（自签证书或买证书 + 域名）。**已落地：见《HTTPS配置Runbook-自签证书》**，线上现已升级为 `https://your-domain.example.com`。本文余下章节描述的是最初的纯 HTTP 形态，作为基础部署参考保留。

***

## 1. 你需要先开通的资源（清单）

* [ ] **阿里云 GPU ECS 实例**：建议 `ecs.gn7i-c8g1.2xlarge`（A10 24GB）或同级；镜像选 **Ubuntu 22.04**。

* [ ] **系统盘 ≥ 100GB**（模型权重 + 镜像约占数十 GB）。

* [ ] **安全组放行入方向 TCP** **`80`**（来源 `0.0.0.0/0` 或限你的 IP）；保留 `22`（SSH）。

* [ ] 记下**公网 IP**。

> 这些需要你在阿里云控制台操作；开通后把公网 IP 和 SSH 方式给我，或自行按下面步骤执行。

> **端口映射方案（最终）**：安全组只放行了 TCP `80`，因此 compose 把宿主机 `80` 映射到 web 容器的 `80`（`ports: ["80:80"]`）。后端 `backend:8000` 仅在 compose 内部网络可达，由 nginx 反代，不对外暴露、无需放行。访问统一走 `http://<公网IP>`。

***

## 2. 实例初始化（在 ECS 上执行）

```bash
# 2.1 NVIDIA 驱动 (若镜像未预装)
nvidia-smi || sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common && sudo ubuntu-drivers autoinstall && sudo reboot
# reboot 后再次 nvidia-smi 应能看到 GPU

# 2.2 Docker + Compose 插件
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # 重新登录生效

# 2.3 NVIDIA Container Toolkit (让容器用 GPU)
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 验证容器内能看到 GPU
docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi
```

***

## 3. 拉代码 + 准备模型与音色

```bash
git clone <你的仓库地址> asr && cd asr

# 3.1 下载模型权重到 deploy/models (容器只读挂载到 /models)
#     可在实例上用 Python venv 跑下载脚本, 或手动 git clone modelscope 仓库
python3 -m venv /tmp/dl && source /tmp/dl/bin/activate
pip install "modelscope>=1.13"
python backend/scripts/download_models.py --all --dest deploy/models
deactivate
# 完成后 deploy/models/ 下应有:
#   iic__SenseVoiceSmall/  iic__speech_fsmn_vad_zh-cn-16k-common-pytorch/  iic__CosyVoice2-0.5B/

# 3.2 准备 CosyVoice 参考音色 (3-10s, 清晰人声)
mkdir -p deploy/voices
# 把女声 / 男声 16k 单声道 wav 分别放到 deploy/voices/default.wav、deploy/voices/male.wav
# (本地 ffmpeg 转: ffmpeg -i raw.m4a -ar 16000 -ac 1 default.wav)
```

***

## 4. 配置 + 启动

```bash
cp deploy/.env.deploy.example deploy/.env.deploy
# 编辑 deploy/.env.deploy:
#   - COSYVOICE_VOICES 里 prompt_text 改成 default.wav / male.wav 的真实文字内容
#   - 其余路径默认即可 (都是容器内 /models /voices /opt/CosyVoice)

# 构建并启动 (首次构建较久: 装 torch/cosyvoice 依赖)
docker compose -f deploy/docker-compose.yml up -d --build

# 看日志, 等待 "engines ready" (首次预热加载模型需 1-3 分钟)
docker compose -f deploy/docker-compose.yml logs -f backend
```

***

## 5. 验证

```bash
# 实例本机
curl http://127.0.0.1/readyz        # 期望 {"ready": true}
curl http://127.0.0.1/api/v1/models # 看到 funasr / cosyvoice
```

浏览器打开 `http://<公网IP>`：

* [ ] 顶部显示「服务就绪」

* [ ] **文件式 ASR**：上传一段 wav/mp3，得到真实转写

* [ ] **TTS**：输入文本，合成并播放（音色为你注册的「中文女」）

* [ ] 录音/流式：HTTP 下会被浏览器拦截，属预期（见第 0 节）

***

## 5.5 增量更新（改完代码重新部署）

远端 `/opt/asr` 是 rsync 管理的仓库副本（非 git）。代码改完后用一键脚本部署，**不要手动 rsync**——脚本已固化「构建前端 + 单独同步最新 `dist/` + 重建重启」，避免前端改动不生效的坑（前端镜像 `COPY dist`，而 `dist/` 被 `.gitignore` 忽略、会被普通 rsync 排除）。

```bash
# 首次: 复制示例并填入线上信息 (sync.env 已 gitignore, 不入库, 不泄露)
cp deploy/sync.env.example deploy/sync.env
# 编辑 deploy/sync.env: 至少填 DEPLOY_HOST 和 DEPLOY_KEY

# 之后直接运行 (脚本自动读取 deploy/sync.env):
deploy/sync.sh                # 全流程: build dist + 同步代码 + 同步 dist + 重建重启全部容器
deploy/sync.sh --web-only     # 只改了前端: 仅重建/重启 web, 不动 backend
deploy/sync.sh --no-build     # 用已有 dist (上一步已构建过)
deploy/sync.sh --no-restart   # 只同步不重启
```

脚本内**不写死任何线上信息**（IP / 私钥等），全部经环境变量传入：
- `DEPLOY_HOST`（**必填**，远端 IP 或域名）、`DEPLOY_KEY`（**必填**，SSH 私钥路径，支持 `~`）
- `DEPLOY_USER`（可选，默认 `root`）、`DEPLOY_PATH`（可选，默认 `/opt/asr`）

优先级：命令行显式传入的环境变量 > `deploy/sync.env` 文件。也可用 `DEPLOY_ENV_FILE` 指定其他配置文件路径。一次性传入示例：

```bash
DEPLOY_HOST=your-server.example.com DEPLOY_KEY=~/.ssh/your_key.pem deploy/sync.sh
```

***

## 6. 运维常用命令

```bash
docker compose -f deploy/docker-compose.yml ps          # 状态
docker compose -f deploy/docker-compose.yml logs -f web  # nginx 日志
docker compose -f deploy/docker-compose.yml restart backend
docker compose -f deploy/docker-compose.yml down         # 停止
docker stats                                              # 资源占用
nvidia-smi                                                # GPU 占用
```

***

## 7. 常见问题

**Q1.** **`docker run --gpus all`** **报错 / 容器内 nvidia-smi 不可用**

* NVIDIA Container Toolkit 未配置好，重做 2.3，并 `sudo systemctl restart docker`。

**Q2. backend 起来但 /readyz 一直 false**

* 看 `logs -f backend`：多为模型路径不对（`deploy/models` 下目录名与 `.env.deploy` 不一致）或权重没下全。

**Q3. CosyVoice 镜像构建时 pynini/WeTextProcessing 失败**

* Dockerfile 已对该步做容错（打 WARN 继续）。若运行时报缺包，进容器 `pip install` 对应包，或改用 conda-forge 的 pynini。需要我给一份 conda 版 Dockerfile 可告诉我。

**Q4. 上传大音频 413**

* nginx `client_max_body_size`（已设 64m）与后端 `max_upload_bytes` 对齐调大。

**Q5. 想用麦克风/流式**

* 需要 HTTPS。最快：买/申请证书 + 域名，给 nginx 加 443 + TLS；我可补配置。

**Q6. 显存不够（OOM）**

* `.env.deploy` 设 `COSYVOICE_FP16=true`（已默认）；必要时拆分 ASR/TTS 到不同卡或降并发。

***

## 8. 部署后续（非本次范围，确认后再做）

* **M4+**：加 HTTPS。**自签证书 + IP 方案已落地（见《HTTPS配置Runbook-自签证书》），麦克风/流式已开放。** 后续若要消除浏览器警告，再走域名 + ICP 备案 + 受信任证书。

* **M5 / Phase 2**：推理层拆独立服务、实例池、预热、负载扩缩容、容灾降级。

* **Phase 3**：vLLM PagedAttention / KV Cache / 连续批处理 + QoS 监控看板。
