> **开源说明**：本文为开发期方案或 Runbook 脱敏整理，示例主机名、IP、密钥路径均为占位符；当前开源版的安装与配置请优先参考仓库根目录 `README.md` 与 `docs/configuration.md`。

# HTTPS 配置 Runbook（自签证书 + IP，无域名）

本文档记录把已上线的 MVP（见《公网部署Runbook-MVP》）从纯 HTTP 升级到 **HTTPS** 的全部步骤，对应里程碑 **M4**。

采用**自签证书 + 公网 IP** 方案：当天即可上线、0 成本、无需域名和 ICP 备案。代价是浏览器首次访问会有一次证书警告（手动放行即可，不影响功能）。

> 为什么要 HTTPS：浏览器的 `getUserMedia`（麦克风）只在**安全上下文**（HTTPS 或 localhost）可用。升级 HTTPS 后，页面的「实时录音转写」、流式 ASR、流式 TTS 才能解锁。

> 当前线上：**https://your-domain.example.com**（实例 <PUBLIC_HOST>，Tesla T4）。

***

## 0. 方案对比（为什么选自签）

| 方案 | 上线周期 | 成本 | 浏览器警告 | 备注 |
| --- | --- | --- | --- | --- |
| **自签证书 + IP（本文）** | 当天 | 0 | 有（手动放行一次） | 大陆 IP 直连，无需备案 |
| 域名 + ICP 备案 + 免费证书 | 1–3 周 | 域名年费 | 无 | 备案期间 80/443 被拦；需本人实名 |

如果后续要彻底消除浏览器警告，再走「域名 + 备案 + 免费证书（如 Let's Encrypt / 阿里云免费证书）」那条路，届时只需替换 `deploy/certs/` 下的证书并把 `server_name` 改为域名。

***

## 1. 前置：放行安全组 443

⚠️ 这一步在**阿里云控制台**操作，须你本人完成：

1. ECS 控制台 → 目标实例 → **安全组** → 入方向规则
2. 手动添加一条：协议 **TCP**、端口 **443/443**、授权对象 **`0.0.0.0/0`**（与现有 80 一致）
3. 保存即时生效

> 80 端口保留放行（用于 301 跳转到 443）；22（SSH）保留。

***

## 2. 生成自签证书（在实例上执行）

关键点：现代浏览器**忽略 CN，只认 SAN**，因此 `subjectAltName` 必须包含实例公网 IP，否则即使放行也无法建立信任。

```bash
mkdir -p /opt/asr/deploy/certs && cd /opt/asr/deploy/certs

# RSA 2048，有效期 10 年，SAN 写实例公网 IP
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout server.key -out server.crt -days 3650 \
  -subj "/CN=<PUBLIC_HOST>" \
  -addext "subjectAltName=IP:<PUBLIC_HOST>"

# 校验 SAN 与有效期
openssl x509 -in server.crt -noout -ext subjectAltName
openssl x509 -in server.crt -noout -dates
```

产物：`deploy/certs/server.crt`（证书）、`deploy/certs/server.key`（私钥）。
> `certs/` 含私钥，**不要提交到 git**（确认 `.gitignore` 已忽略，或证书目录仅存在于实例）。

***

## 3. nginx 配置（`frontend/nginx.conf`）

拆成两个 server 块：80 整体 301 跳转到 443，443 提供 HTTPS + 反代。

```nginx
upstream backend {
    server backend:8000;
}

# HTTP: 整体 301 跳转到 HTTPS (浏览器麦克风/getUserMedia 需要安全上下文)。
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

# HTTPS: 前端 + 反代主入口。
server {
    listen 443 ssl;
    http2 on;
    server_name _;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    root /usr/share/nginx/html;
    index index.html;
    client_max_body_size 64m;

    location = /healthz { proxy_pass http://backend; }
    location = /readyz  { proxy_pass http://backend; }

    location /api/ {
        proxy_pass http://backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    location /ws/ {
        proxy_pass http://backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

> **前端无需改动**：`frontend/src/api/client.ts` 的 `wsBase()` 已按 `location.protocol` 自动切换 `ws://` / `wss://`，所有请求走同源相对路径。HTTPS 下 WebSocket 自动变 `wss://`，不会触发混合内容拦截。

***

## 4. compose 配置（`deploy/docker-compose.yml`）

web 服务增加 443 端口映射与证书卷挂载（其余不变）：

```yaml
  web:
    image: asr-tts-web:latest
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./certs:/etc/nginx/certs:ro   # 自签证书只读挂载
    depends_on:
      - backend
    restart: unless-stopped
```

`frontend/Dockerfile` 同步 `EXPOSE 80 443`（仅文档性，实际端口由 compose `ports` 决定）。

***

## 5. 构建与上线

`nginx.conf` 是 `COPY` 进 web 镜像的，改完须**重建 web 镜像**再起：

```bash
cd /opt/asr
docker compose -f deploy/docker-compose.yml build web
docker compose -f deploy/docker-compose.yml up -d

# nginx 配置自检
docker compose -f deploy/docker-compose.yml exec web nginx -t
```

> backend 未改动，不必重建；`up -d` 只会重建 web 容器。

***

## 6. 验证

```bash
# 实例本机（自签证书用 -k 跳过校验）
curl -sk https://127.0.0.1/readyz                 # {"status":"ok","ready":true}
curl -s  -o /dev/null -w "%{http_code} %{redirect_url}\n" http://127.0.0.1/   # 301 -> https://...
echo | openssl s_client -connect 127.0.0.1:443 2>/dev/null | openssl x509 -noout -ext subjectAltName

# 外网（放行 443 后，从任意外部机器）
curl -sk -o /dev/null -w "%{http_code}\n" https://your-domain.example.com/   # 200
```

浏览器打开 `https://your-domain.example.com`：

* [ ] 首次出现「您的连接不是私密连接」→ 点 **高级 → 继续前往**（自签证书正常现象）
* [ ] 顶部显示「服务就绪」
* [ ] **麦克风 / 实时录音转写**：浏览器弹出麦克风授权，流式 ASR 可用
* [ ] **流式 TTS**：合成边出边播
* [ ] 文件式 ASR + TTS 合成：仍正常
* [ ] `http://your-domain.example.com` 自动 301 跳到 https

***

## 7. 常见问题

**Q1. 外网 443 超时（`http=000`），但 80 正常**
安全组没放行 TCP 443，回到第 1 节添加规则。

**Q2. 浏览器 `NET::ERR_CERT_AUTHORITY_INVALID` / `ERR_CERT_COMMON_NAME_INVALID`**
自签证书的预期警告，手动「高级 → 继续前往」即可。若是 `COMMON_NAME_INVALID`，检查证书 SAN 是否含访问用的 IP（第 2 节 `-addext`）。

**Q3. 麦克风仍不可用**
确认地址栏是 `https://` 而非 `http://`（被缓存的 http 页面不会自动升级，强刷一次）。

**Q4. WebSocket 连接失败（混合内容）**
前端已自动用 `wss://`，一般不会出现；若自定义改过前端，确认未硬编码 `ws://`。

**Q5. 证书过期**
本证书有效期 10 年（到 2036）。到期或更换 IP 时，重跑第 2 节生成新证书，再 `docker compose ... restart web` 即可（证书是挂载卷，无需重建镜像）。

***

## 8. 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `frontend/nginx.conf` | 拆 80（301 跳转）+ 443（SSL）两个 server 块；加 `X-Forwarded-Proto` |
| `deploy/docker-compose.yml` | web 加 `443:443` 映射 + `./certs:/etc/nginx/certs:ro` 卷 |
| `frontend/Dockerfile` | `EXPOSE 80 443` |
| `deploy/certs/server.crt` `server.key` | 实例上生成的自签证书（不入 git） |

> 升级到域名 + 备案 + 受信任证书时：替换 `deploy/certs/` 下证书、把两个 server 块的 `server_name _` 改为域名、`restart web` 即可。
