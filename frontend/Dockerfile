# 前端镜像: 纯 nginx 服务预构建静态资源 + 反代后端。
# dist/ 在本地/CI 用 `npm run build` 产出后再构建镜像 (避免在容器内跑 npm, 国内 npm-in-docker 易失败)。
FROM nginx:1.27-alpine
COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
