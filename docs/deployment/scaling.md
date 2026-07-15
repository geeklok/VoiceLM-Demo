> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# 水平扩容 Runbook（Phase 2 §6.3/6.4 — 零成本 LB 骨架）

本文档落地 Phase 2 的**水平扩容/容灾骨架**。当前线上是**单台 Tesla T4（单 GPU）**，所以本阶段做的是「不加机器、不花钱、可逆」的负载均衡 + 故障摘除骨架：

- 在现有单机上用 `docker compose --scale backend=N` 起多副本，演示「杀掉一个副本请求不中断」（验收 §6.6 第 4 条的机制）。
- nginx upstream 已配 `least_conn` + 被动健康摘除 + 故障转移重试。
- 为将来加 GPU 机器预留了跨实例占位，到时只填 IP 即可平滑切到真扩容。

> ⚠️ **重要边界**：单 GPU 上多副本**共享同一块算力**，QPS **不会线性提升**（验收 §6.6 第 3 条「QPS 线性提升」**必须加 GPU 机器**才能达成）。真正的整机级高可用也需要**至少 2 台机器**——同机多副本扛不住整机/GPU 故障。这两项涉及购买云资源（花钱），需用户决策，AI 不代为购买。

---

## 1. 已落地的骨架（代码层）

### 1.1 nginx upstream（`frontend/nginx.conf`）

```nginx
upstream backend {
    least_conn;
    server backend:8000 max_fails=3 fail_timeout=15s;
    # ---- 跨实例扩容 (加 GPU 机器后启用) ----
    # server 10.0.0.11:8000 max_fails=3 fail_timeout=15s;
    # server 10.0.0.12:8000 max_fails=3 fail_timeout=15s;
}
```

- **least_conn**：按最少连接分发，适合推理这种耗时不均的长请求。
- **max_fails=3 / fail_timeout=15s**：被动健康摘除——某后端 15s 窗口内连续失败 3 次即被短路跳过，冷却后自动恢复探测。无需主动探针。
- **proxy_next_upstream**（在 `location /api/`）：后端连接失败/5xx 时自动切到池中下一个实例，故障对用户不可见。非幂等 POST 仅在「连接尚未建立」阶段重试，避免推理被重复执行。

### 1.2 compose 多副本（`deploy/docker-compose.yml`）

backend 服务**无固定 `container_name`**，因此支持同机多副本：

```bash
docker compose -f deploy/docker-compose.yml up -d --scale backend=2
```

服务名 `backend` 被 docker 内置 DNS 解析为多条 A 记录；web 容器经 `depends_on` 在 backend 之后启动，nginx 启动时即把全部副本 IP 加入 upstream 池。

---

## 2. 同机多副本 + 故障摘除演示（零成本，可在线上单机执行）

> 前提：显存够。单 T4 16GB，加载全部模型约 4.9GB/副本，2 副本约 ~10GB，尚有余量；**不要盲目调大 N**，超显存会 OOM。建议 N=2 演示即可。

```bash
cd /opt/asr/deploy

# 2.1 起 2 个 backend 副本 (web 不变)
docker compose up -d --scale backend=2
docker compose ps                      # 应看到 deploy-backend-1 / deploy-backend-2

# 2.2 等两个副本都 healthy (各自预热加载模型, 约 1-3 分钟)
docker compose ps | grep backend

# 2.3 持续打流 (另开一个终端), 观察请求不中断
while true; do curl -sk -o /dev/null -w "%{http_code} " https://127.0.0.1/api/v1/models; sleep 0.5; done

# 2.4 杀掉一个副本, 模拟实例故障
docker kill deploy-backend-2

# 期望: 2.3 的输出仍持续 200 (nginx 被动摘除故障副本 + 故障转移到副本1),
#       不出现长时间 5xx。fail_timeout 窗口后 nginx 不再向已挂副本转发。

# 2.5 恢复
docker compose up -d --scale backend=2  # 重新拉起被杀副本
```

**验收对照**：本演示满足 §6.6 第 4 条「杀掉一个推理实例，请求不中断」的**机制验证**（单机层面）。整机级高可用仍需多机。

### 2.6 实测记录（2026-06-27，线上 <PUBLIC_HOST>）

在线上单机实跑了两轮，均**零中断**：

| 场景 | 操作 | 打流总数 | 200 | 非 200 | 说明 |
|------|------|---------|-----|--------|------|
| 优雅停（best case） | `docker stop deploy-backend-2` | 200 | 200 | 0 | SIGTERM → 后端 drain，存量处理完再退 |
| 硬杀（sudden crash） | `docker kill --signal=SIGKILL deploy-backend-2` | 300 | 300 | 0 | 无 drain，靠 nginx `proxy_next_upstream` 重试到存活副本 |

- 显存：2 副本满载约 **11GB / 15GB**（单副本约 4.9GB），无 OOM。
- 硬杀场景下，**击杀瞬间正在发起的那一次请求**（req#15，时间戳与 kill 同一刻）仍返回 200——证明 `proxy_next_upstream` 在连接失败时切到了另一副本。
- 演示后已 `docker compose up -d --scale backend=1` 缩回单副本，GPU 回落 4932MiB，公网 `/readyz` 正常，线上恢复常态。

> 结论：单机层面的「故障摘除 + 故障转移」机制已验证可用。但这仍是**同机多副本共享一块 GPU**，QPS 不会线性提升，且整机/GPU 整体故障无法靠它兜底——这两项需加机器（见第 3 节）。

---

## 3. 将来加 GPU 机器 → 切到真扩容（B 方案，花钱）

> ⚠️ **本节涉及购买云资源（花钱），由用户自行在阿里云下单，AI 不代为购买。** 价格为 2026 年国内公开参考价，以实际下单为准。

### 3.0 现状基线（规划依据）

通过实例 metadata 查得现有机：

| 项 | 值 |
|---|---|
| 实例规格 | `ecs.gn6i-c8g1.2xlarge`（T4 16G，8核 32G）|
| 地域/可用区 | **cn-hangzhou / cn-hangzhou-h** |
| VPC 内网 IP | **<NODE1_PRIVATE_IP>** |
| 系统盘 | 100G ESSD（已用 43G）|
| 实际内存占用 | 仅 ~6.3G（单 backend 远未吃满 32G）|

### 3.1 架构决策：新机只跑 backend，不跑 web

现有机继续当**唯一入口**（web/nginx = LB + 静态资源 + 公网 443）。新机只跑 `backend`，在 **VPC 内网**暴露 8000，由现有机 nginx upstream 分发：

```
                    公网 443
                       │
        ┌──────────────▼──────────────┐
        │  现有机 <NODE1_PRIVATE_IP>        │
        │  web(nginx LB) + backend     │  T4
        └──────┬───────────────┬───────┘
               │ 内网 8000      │ 内网 8000
               ▼                ▼
          backend(本机)   ┌─────────────────┐
                          │ 新机 172.17.x.x │  T4 (新购)
                          │   backend only  │
                          └─────────────────┘
```

这样做的好处：

- 新机**不需要公网 IP / 公网带宽** → 省钱（VPC 内网流量免费）。
- 入口、证书、域名都不变，前端无感知。
- 与 `frontend/nginx.conf` 里预留的跨实例占位完全对齐，只填 IP 即可。

### 3.2 操作步骤

**Step 1 — 购买新 GPU ECS（用户下单，花钱）**
- 规格：**同 `ecs.gn6i-c8g1.2xlarge`**（T4 16G，8核32G）；或省钱选 `ecs.gn6i-c4g1.xlarge`（4核15G，单 backend 够用——本机实测才占 6.3G 内存）。
- **地域/可用区必须同为 cn-hangzhou-h，且加入同一 VPC / 同一交换机**，否则内网不通或跨区延迟高。
- 镜像：选**预装 GPU 驱动的 Ubuntu 镜像**，省掉装驱动步骤。
- 公网：**不分配 EIP**（backend-only）。仅初始化阶段需联网装包，见 Step 3。
- 系统盘：100G ESSD 起步（放模型权重，约几 GB～十几 GB）。

**Step 2 — 安全组（关键，别开公网）**
- 新机安全组：放行**来源 = 现有机所在安全组（或 `<NODE1_PRIVATE_CIDR>`）**的 TCP **8000**。
- **绝不**对 `0.0.0.0/0` 放行 8000——backend 无鉴权，公网暴露有风险。

**Step 3 — 初始化新机**
- 用预装驱动镜像时，只需装 docker + NVIDIA Container Toolkit（参考《公网部署Runbook-MVP》第 2 节）。
- 联网装包二选一：① 临时挂**按流量计费**公网带宽（¥0.8/GB，装完释放）；② 用现有机做出口代理。

**Step 4 — 部署 backend 到新机（走内网，省流量）**
- **不重新下模型**：从现有机内网 rsync（免费、快），把新机内网 IP 记为 `NEW_IP`：

  ```bash
  # 在现有机上执行
  rsync -az /opt/asr/deploy/models/ root@${NEW_IP}:/opt/asr/deploy/models/
  rsync -az /opt/asr/deploy/voices/ root@${NEW_IP}:/opt/asr/deploy/voices/
  rsync -az /opt/asr/deploy/certs/  root@${NEW_IP}:/opt/asr/deploy/certs/
  ```
- **不重新 build 镜像**：直接传镜像，避免再拉基础镜像 / pip 安装：

  ```bash
  # 在现有机上执行
  docker save asr-tts-backend:latest | ssh root@${NEW_IP} 'docker load'
  ```
- 同步代码（用 `deploy/sync.sh` 指向新机，或手动 rsync 仓库），新机**只起 backend**：

  ```bash
  # 在新机上执行
  cd /opt/asr/deploy && docker compose up -d backend
  ```
  > `docker compose up -d backend` 只拉起 backend 服务，新机不跑 web。
- 等新机就绪：`docker exec <backend容器> python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read())"` → `{"ready":true}`。

**Step 5 — 接入 LB（改 upstream + 重启 web）**
- 在 `frontend/nginx.conf` 的 upstream 取消占位注释，填新机内网 IP：

  ```nginx
  upstream backend {
      least_conn;
      server backend:8000     max_fails=3 fail_timeout=15s;   # 本机
      server 172.17.x.x:8000  max_fails=3 fail_timeout=15s;   # 新机 (填实际 NEW_IP)
  }
  ```
- 只重启 web（backend 不动）：

  ```bash
  DEPLOY_KEY=~/.ssh/your_key.pem bash deploy/sync.sh --no-build --web-only
  ```

**Step 6 — 验证**
- 打流看分发到两机（两机 `docker logs` 都有请求记录）。
- **停掉整个新机**（或 `docker stop` 新机 backend），按 §2.6 同样的 while 打流，确认请求不中断——这次验证的是**整机级高可用**（比同机多副本更强）。
- 此时 QPS 才随 GPU 数近似**线性提升**（满足验收 §6.6 第 3 条）。

### 3.3 费用预估（2026 国内参考价，cn-hangzhou）

| 项目 | 规格 | 计费方式 | 估算 |
|---|---|---|---|
| **GPU 实例（主成本）** | gn6i 8核32G T4 | 包月 | **¥2890/月**；年付 ¥26780（6.5折，约 ¥2232/月）|
| 同上（省钱档） | gn6i **4核15G** T4 | 包月 | **¥1681/月**；年付约 ¥15937 |
| 同上（弹性/测试） | gn6i 8核32G T4 | 按量 | **¥8–10/小时** → 全天候约 ¥5760–7200/月（持续开机比包月贵很多）|
| 同上（可中断任务） | gn6i 抢占式 | 抢占式 | 约按量 **1/5**（¥1.6–2/h），**可能被回收**，不适合常驻服务 |
| 系统盘 | 100G ESSD | 包月 | 约 **¥50–100/月** |
| 公网带宽 | 无（backend-only 走内网）| — | **¥0**（仅初始化临时按流量 ¥0.8/GB，可忽略）|
| 内网流量 | VPC 内 | — | **免费** |

**典型方案总价（新增第二台）：**
- **常驻 + 省钱**：4核15G 包年 ≈ **¥1681/月**（含盘约 ¥1730–1780/月）。
- **常驻 + 同规格**：8核32G 包月 **¥2890/月**（含盘约 ¥2940–2990/月）；包年摊薄约 ¥2280/月。
- **只在高峰/演示用**：按量 ¥8–10/h，用完即停最划算。

> 现有机同规格（gn6i-c8g1.2xlarge）月成本同量级，**加第二台 ≈ 月度 GPU 开销翻倍**。是否值得取决于诉求是「整机高可用」还是「更高 QPS」——这两点单机多副本都给不了，必须加机器。

### 3.4 风险 / 注意事项

- **可用区一致**：必须 cn-hangzhou-h 同 VPC，否则内网不通或延迟高、LB 分发失衡。（实测同 VPC 默认安全组即放行内网互访，无需额外加 8000 规则。）
- **backend 无鉴权**：8000 只能内网放行，严禁公网暴露。新机虽有公网 IP，但 `override` 的 `ports: 8000:8000` 绑的是宿主机所有网卡——**务必靠安全组**限制 8000 仅内网/来源=入口机可达，不要在公网安全组放行 8000。
- **模型/镜像走内网传输**：用上面的 rsync + `docker save | docker load`，省下重新下载的时间与流量。
- **nginx 解析时机**：upstream 填的是 IP（非 DNS 名），nginx 启动即生效，无 DNS 缓存问题。
- **优雅退出**：新机下线时用 `docker stop`（SIGTERM → drain），配合 nginx 摘除，存量请求不丢。
- **临时互信密钥**：两机间传输用的临时密钥用完即删，不长期保留。

> 后续若实例数量频繁变动，再考虑 K8s + HPA（Phase 2 §6.3 的演进路径）或 nginx Plus 动态 upstream / consul-template 自动改写。当前规模手工维护 upstream 列表即可。

### 3.5 实测记录（2026-06-27，已上线双机）

第二台 GPU 机器已购买并接入，**真扩容 + 整机高可用已落地上线**。

**机器清单：**

| 角色 | 公网 IP | 内网 IP | 规格 | 跑什么 |
|------|---------|---------|------|--------|
| 入口 + 节点1 | <PUBLIC_HOST> | <NODE1_PRIVATE_IP> | gn6i-c8g1.2xlarge (T4) | web(nginx LB) + backend |
| 节点2 | <NODE2_PUBLIC_HOST> | <NODE2_PRIVATE_IP> | gn6i-c8g1.2xlarge (T4) | backend only |

**接入过程关键点（含踩坑）：**
- 新机是**裸镜像**，无驱动/docker/toolkit，全部走阿里云镜像源现装：`nvidia-driver-535-server`（535.309.01，与现有机一致，DKMS 编译后 `modprobe` 即用，无需重启）+ `docker-ce 29.6.1` + `nvidia-container-toolkit 1.19.1`。
- **dockerhub 国内拉取超时**：`docker run nvidia/cuda` 验证 GPU 直接 i/o timeout。故 backend 镜像不走 dockerhub，改从现有机内网 `docker save | ssh | docker load` 直传——这也正是省钱省时的正解。
- 模型 7.3G 内网 rsync **仅 24 秒**（VPC 内网 0.2ms 延迟）；镜像两机 image ID 一致（`3178560316bf`）。
- 两机互信用**现有机临时生成的 ed25519 密钥**（公钥加到新机 authorized_keys），传完即从两机清除，私钥不落地长存。
- 新机 backend-only：用 `deploy/docker-compose.override.yml` 把容器 8000 **发布到宿主机** `<NODE2_PRIVATE_HOST>:8000`（现有机靠同机 docker DNS 无需发布，跨机必须发布），供现有机 nginx 跨内网反代。

**LB 分发实测**：经公网入口打 20 请求，分发到 现有机 ~11 / 新机 ~9（`least_conn` 跨机均衡）。

**整机高可用实测（两轮均零中断）：**

| 场景 | 操作 | 打流 | 200 | 非 200 | 说明 |
|------|------|------|-----|--------|------|
| 优雅停整机 backend | `docker stop`（SIGTERM→drain） | 250 | 250 | 0 | 存量处理完再退，nginx 摘除 |
| 硬杀整机 backend | `docker kill -SIGKILL` | 200 | 200 | 0 | 击杀瞬间的 req#88 仍 200，`proxy_next_upstream` 转到存活机 |

> 与 §2.6 同机多副本相比，这次杀的是**整台机器的 backend**，验证的是**真正的整机级高可用**（§6.6 第 4 条的完整达成）；且两机各一块 T4 独立算力，**QPS 可随 GPU 数近似线性提升**（§6.6 第 3 条达成）。演示后已恢复双机，两机 GPU 各 ~4933MiB，集群健康。

**新机下线 / 缩容**（将来不想花这份钱时）：
1. 在 `frontend/nginx.conf` upstream 删掉 `server <NODE2_PRIVATE_HOST>:8000` 那行；`deploy/sync.sh --no-build --web-only` 重启 web（先摘流量）。
2. 新机 `docker compose down`；在阿里云控制台**释放实例**（停止计费）。
3. 集群回到单机骨架（§2），随时可再加回来。

---

## 3.6 backend-only 节点的增量代码更新（node2 无 Docker Hub 出网 → overlay 构建）

> 场景：只改了 **Python 业务代码、无新依赖**，要把改动同步到一台 **Docker Hub 不可达**的 backend-only 节点（本项目 node2 `<NODE_PUBLIC_HOST>` / 内网 `<NODE2_PRIVATE_IP>`）。2026-07-01 热词特性同步即用此法。

### 为什么不能直接 `docker compose build`
backend 业务代码是 **build 时 `COPY app` 烤进镜像**（非 bind-mount），改代码必须重建镜像。但 [backend/Dockerfile](../../backend/Dockerfile) 第一行 `FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04` + 构建中还要 `git clone` CosyVoice——**这两步都要连公网/Docker Hub**。node2 实测 `docker run nvidia/cuda` 直接 i/o timeout（与初次接入时同样的 HUB_FAIL），所以在 node2 上跑完整 Dockerfile 必失败。

历史上（§3.5 首次接入、以及真流式全量铺开）的解法是 **node1 `docker save | ssh node2 docker load` 直传 17.3GB 整镜像**——可靠但慢、且要走内网互信。当改动只是几个 `.py` 文件时，传 17GB 属杀鸡用牛刀。

### overlay 增量构建（零联网、不传镜像、不动依赖）
node2 本地已有上一版完整镜像 `asr-tts-backend:latest`（含全套 CUDA + torch + CosyVoice 依赖）。用一个**只两行的 overlay Dockerfile**，以它为 base 层，只把新的 `app/` 源码盖上去：

```dockerfile
# /opt/asr/backend/Dockerfile.node2overlay
FROM asr-tts-backend:latest
COPY app /app/app
```

构建时 base 层命中本地缓存、`COPY app` 只拷几十 KB Python 源码，**全程不联网、不重装任何依赖**：

```bash
# 在 node2 /opt/asr/backend 上执行 (源码已 rsync 到位)
docker build -f Dockerfile.node2overlay -t asr-tts-backend:latest .   # BUILD_EXIT=0, 秒级
cd /opt/asr/deploy && docker compose up -d backend                     # recreate 用新层
```

### ⚠️ 适用边界（关键）
- **仅限纯 Python 改动**。一旦动了 `pyproject.toml` / 加了新 pip 依赖 / 改了系统库，overlay 就不够（`COPY app` 不会触发 `pip install`），必须回到 §3.5 的 `docker save | docker load` 整镜像直传，或想办法给 node2 临时开出网。
- overlay 直接把 tag `asr-tts-backend:latest` 覆盖成新层，**上一版镜像 id 会失去 latest 指向**（仍在本地悬挂，可 `docker images` 找回）。回退就是再 build 一次旧源码，或 `docker load` 旧镜像。

### 配套：模型与 env（node2 无 Docker Hub，但 ModelScope / Aliyun PyPI 可达）
- **权重现下**：热词特性要 seaco 权重，node2 直接 ModelScope 拉（`modelscope download ...` 到 `deploy/models/`），无需从 node1 传。
- **env 对齐**：`.env.deploy` 先 `cp` 备份，再注释 `FUNASR_PARAFORMER_MODEL`（下线）、追加 `FUNASR_SEACO_MODEL=/models/iic__speech_seaco_...`，与 node1 保持一致。
- recreate 后 5s healthy，`/api/v1/models` 三引擎标志与 node1 完全一致（`sensevoice=False / seaco=True / streaming=True`），GPU 3201/15360 MiB（比 node1 低，因去掉了独立 paraformer-zh）。

### SSH 纪律（沿用安全约定）
- **全程串行单条 SSH**，不并发建连——node2 云端对短时间多条 SSH 会限流封 IP（前序会话踩过）。
- 新 host 加 `-o UserKnownHostsFile=/dev/null`（沙箱禁写 `~/.ssh/known_hosts`）。
- 容器以非 root 跑，`docker exec ... cat > /tmp/x` 覆盖 root 文件会被拒；传脚本进容器改用 host 侧 `rsync` + `docker cp`（cp 在 host 以 root 执行）。

---

## 4. 常见问题

**Q1. `--scale backend=2` 后只有一个副本起来 / 另一个一直 starting？**
- 多为显存不足（两副本各自加载全套模型）。`docker logs deploy-backend-2` 看是否 CUDA OOM；可减少加载的模型或降到 N=1。

**Q2. nginx 没在副本间分发，全打到一个？**
- 开源 nginx 在启动时解析一次 upstream DNS。若先起 web 后 scale，nginx 可能只解析到旧副本。顺序应为先 `--scale backend=2` 再确保 web 已启动；必要时 `docker compose restart web` 让 nginx 重新解析。

**Q3. 杀副本后短暂出现 502/504？**
- 正在该副本上处理的**进行中请求**会失败一次，之后被 `max_fails` 摘除。配合后端 §6.5 优雅退出（SIGTERM drain），用 `docker stop`（发 SIGTERM）而非 `docker kill`（SIGKILL）可让存量请求先处理完，更平滑。

**Q4. 想验证优雅退出（drain）效果？**
- 用 `docker stop deploy-backend-2`：后端收到 SIGTERM 进入 draining（新请求 503、健康检查放行、存量处理完才退），nginx 据此摘除，体验比 `docker kill` 更平滑。
