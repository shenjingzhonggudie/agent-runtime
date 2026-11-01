# 运行镜像：只装运行依赖，不含 pytest / ruff / mypy。
#
# 数据默认落在 /data——事件日志、账本目录、以及 SQLite 的 journal 文件都在那里。
# 用命名卷而不是 bind mount：命名卷由 Docker 管属主，bind mount 在 Linux 上会因
# 容器内 uid 与宿主机文件属主不一致而写不进去（Windows 的 Docker Desktop 宽松，
# 但镜像不该依赖宿主机行为）。

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 构建期可换源：中国大陆网络下官方源经常超时。
#   docker build --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple .
#
# 用环境变量而不是 --index-url：pip 在构建隔离环境时会再起一个子进程装 setuptools，
# 那个子进程只认 PIP_INDEX_URL。写在 RUN 前面，不给最终镜像留运行期变量。
ARG PIP_INDEX=https://pypi.org/simple

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN PIP_INDEX_URL="${PIP_INDEX}" pip install .

# 非 root 运行。uid 固定成 10001，需要把宿主机目录挂进来时 chown 成这个数字即可。
#
# /data 必须在这里建好并改属主：Docker 创建 WORKDIR 时用的是 root，先 USER 再
# WORKDIR 会留下一个 app 写不进去的目录——而它是唯一的数据落点。
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data
USER app

# 默认参数是相对路径（agentrun.db / ledger），所以工作目录决定了数据落在哪。
WORKDIR /data
VOLUME ["/data"]

ENTRYPOINT ["agentrun"]
CMD ["--help"]
