# 阶段二：cost-report/alipay-report 脚本迁进来跑，装 LibreOffice（soffice）+ CJK 字体
# （渲染中文单元格用）+ 跟宿主机版本对齐的 Python 渲染依赖。业务代码本身不打进镜像，靠
# skills:ro bind mount 挂进来。
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libreoffice-calc \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        httpx==0.28.1 \
        openpyxl==3.1.5 \
        pymupdf==1.28.2 \
        pillow==12.3.0

WORKDIR /workspace

CMD ["sleep", "infinity"]
