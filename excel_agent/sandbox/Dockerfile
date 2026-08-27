# 阶段一：最小化镜像，只用来验证 DockerSandbox 机制本身（execute/upload/download/网络连通）。
# 阶段二迁移 cost-report/alipay-report 脚本时，这里需要加 LibreOffice（soffice）+ 项目 Python 依赖。
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["sleep", "infinity"]
