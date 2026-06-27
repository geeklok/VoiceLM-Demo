# 后端生产镜像: FunASR + CosyVoice2 (GPU / CUDA)
# 模型权重不打进镜像, 通过挂载 /models 卷提供 (见 deploy/docker-compose.yml)。
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

# 不设 PIP_NO_CACHE_DIR: 配合下方 BuildKit cache mount 缓存已下载的 wheel
# (torch + CUDA 系列约 2.5GB), 失败重试时无需重新下载; 缓存挂载不进镜像层, 不撑大体积。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTORCH_ENABLE_MPS_FALLBACK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

# 系统依赖: python, ffmpeg(音频解码), git(clone CosyVoice), sox/libsndfile(soundfile)
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-venv python3.10-dev python3-pip \
        ffmpeg git build-essential \
        libsndfile1 sox \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# CosyVoice 必须从源码仓库运行 (需要 third_party/Matcha-TTS 子模块)
ARG COSYVOICE_REF=main
RUN git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git /opt/CosyVoice \
    && cd /opt/CosyVoice && git checkout ${COSYVOICE_REF} \
    && git submodule update --init --recursive

# 限制 setuptools<81: openai-whisper 旧 setup.py 在 setuptools>=81 的隔离构建环境里
# 找不到 pkg_resources 会失败; 该约束对主环境和 PEP517 隔离构建均生效。
RUN echo "setuptools<81" > /etc/pip-constraints.txt
ENV PIP_CONSTRAINT=/etc/pip-constraints.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip wheel "setuptools<81"

# CosyVoice 依赖: 过滤掉 T4 推理用不到且拖慢/易失败的训练/部署专用包
#   - tensorrt-cu12*: 仅 LOAD_TRT 用, 体积巨大 (本服务 COSYVOICE_LOAD_TRT=false)
#   - deepspeed: 训练加速, runtime 镜像无 nvcc 难编译
#   - gradio/fastapi-cli/grpcio*/tensorboard: 官方 demo/训练用
#   - --extra-index-url(pytorch.org / azure): 国内不稳; torch 走 Aliyun 镜像(自带 CUDA)
RUN --mount=type=cache,target=/root/.cache/pip \
    cd /opt/CosyVoice \
    && grep -vE '^(--extra-index-url|deepspeed|tensorrt-cu12|gradio|fastapi-cli|grpcio|tensorboard)' requirements.txt > /tmp/req.cosy.txt \
    && pip install -r /tmp/req.cosy.txt

# 后端应用
WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY app /app/app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e ".[engines]"

# 运行期配置经环境变量注入 (compose env_file)。仓库路径固定到镜像内 clone 位置。
ENV COSYVOICE_REPO_DIR=/opt/CosyVoice

EXPOSE 8000
# 单进程多 worker 在 GPU 场景按显存配置; MVP 先单 worker。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
