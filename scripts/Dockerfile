FROM alpine:3.19.1

# 指定默认时区
RUN apk add --no-cache tzdata
ENV TZ Asia/Shanghai


# 固定安装目录，为避免破坏兼容性，继承旧版使用 /
ENV SEAL_HOME=

# 移入对应文件
VOLUME ["/backups", "/data"]
ARG TARGETARCH
COPY ./${TARGETARCH}/data ${SEAL_HOME}/buildin-data
COPY --chmod=0755 ./${TARGETARCH}/sealdice-core  ${SEAL_HOME}/sealdice-core

RUN echo "#!/bin/sh" > ${SEAL_HOME}/entrypoint.sh
RUN echo "cp -r /buildin-data/* /data/" >> ${SEAL_HOME}/entrypoint.sh
RUN echo "/sealdice-core --container-mode" >> ${SEAL_HOME}/entrypoint.sh
RUN chmod +x ${SEAL_HOME}/entrypoint.sh

# 启动
EXPOSE 3211
ENTRYPOINT [ "/entrypoint.sh" ]
